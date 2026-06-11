# Operations Assistant — Fleet-wide vROps Queries (ChatGPT → VCF)

**Date:** 2026-06-11
**Status:** Design approved, pending spec review
**Branch (suggested):** `feat/ops-assistant-fleet-queries`

## Goal

Let the team ask the bot fleet-wide operational questions about the VCF / vROps
estate in natural language and get a compact, ranked answer. Motivating examples:

- "¿Cuál es el clúster con menos recursos libres en Madrid?" (which cluster has the
  least free capacity in Madrid)
- "Genera un reporte de VMs sobredimensionadas" (report of oversized VMs)

These are **cross-resource, ranked/aggregated** questions. The existing tooling is
single-resource (find one resource → read its stats / `vrops_diagnose`). This feature
adds the missing fleet layer: enumerate many resources → filter by site → bulk-fetch
metrics → aggregate/rank → return one compact report.

## Scope decisions (confirmed with user)

1. **Surface:** extend the existing Slack bot. The OpenAI/ChatGPT provider is already
   wired; no new transport. "ChatGPT → VCF" is satisfied by the existing OpenAI path.
2. **Site model:** a **local datacenter-name → location map** kept as config data
   (not derived from vROps tags). Filter resources by site through this map.
3. **Oversizing definition:** use vROps **native rightsizing** metrics (oversized flag
   + reclaimable vCPU/memory), not custom thresholds.
4. **Architecture:** Approach C — a shared internal fleet-query layer powering a small
   set of composite report tools, plus one generic escape-hatch tool. Weighted toward
   composite reports, matching the existing `vrops_diagnose` philosophy (CLAUDE.md:
   weak models make a single tool call and only narrate the verdict; aggregation done
   in Python to respect token guardrails and avoid truncation).

## Architecture

New module layout (mirrors the existing client ↔ pure-analysis ↔ action-wrapper split):

```
src/actions/builtin/vrops/
  fleet.py          # NEW: fleet-query orchestration + aggregation (shared layer)
  reports.py        # NEW: composite report action handlers + ActionDefinitions
  sites.py          # NEW: datacenter-name → location mapping, loaded from config
  vrops_client.py   # extend: list_resources_by_kind(), bulk latest stats
  analysis.py       # extend: ranking/scoring helpers (pure)
```

### Client additions (`vrops_client.py`) — HTTP only, thin

- `list_resources_by_kind(resource_kind, adapter_kind="VMWARE", page_size=1000, max_resources=20000)`
  Pages `GET /resources?resourceKind=…` with **no name filter** to enumerate an entire
  kind. Returns `{identifier, name, resourceKind, adapterKind, health, healthValue}` per
  resource. Fills the gap that `search_resources` requires a `name`.
- `get_latest_stats_bulk(resource_ids, stat_keys)`
  One `GET /resources/stats/latest?resourceId=…&resourceId=…&statKey=…` call returning
  latest values for many resources at once. Returns `{resource_id: {stat_key: value}}`.
  Chunk `resource_ids` (e.g. 100/call) to keep URLs/timeouts sane. A fleet report is then
  a handful of HTTP calls, not one-per-resource.
- Resolve a resource's parent datacenter (for site filtering) via the resource
  relationship endpoint (`GET /resources/{id}/relationships` parent traversal
  cluster → datacenter), batched/cached per run.

### Fleet layer (`fleet.py`) — pure-ish orchestration, no Slack/LLM knowledge

Pipeline, in order:

1. **Enumerate** resources of a kind via the client.
2. **Site-filter** through `sites.py`: resolve each resource's datacenter name → location,
   keep matches. **Filter happens before the stats fetch** so metrics are only pulled for
   in-scope resources.
3. **Bulk-fetch** the relevant stat keys for the filtered set.
4. **Aggregate / score** via pure helpers in `analysis.py`.
5. Return a compact Python structure, **capped to top-N rows**, so the result never
   exceeds the `MAX_TOOL_RESULT_CHARS` budget regardless of fleet size.

The client is injected (constructor arg / parameter) so `fleet.py` can be unit-tested
with a faked client and no network.

### Site mapping (`sites.py`)

- Local datacenter-name → location map, held as config data.
- Sourced from a file pointed at by env var **`VROPS_SITE_MAP_FILE`** (JSON), falling
  back to an empty map when unset.
- Shape: `{"Madrid": ["dc-mad-01", "dc-mad-02"], "Frankfurt": ["dc-fra-01"]}`.
- Matching is **case-insensitive**.
- Unknown location → an explicit result: "location not configured; known sites are: …",
  never a silent full-estate scan.

## Tools

Registered in `src/main.py` via `registry.register(...)`; exposed to both providers via
`to_openai_tools()` / `to_anthropic_tools()` (only OpenAI/ChatGPT is in active use).

### 1. `vrops_cluster_capacity_report`

- **Params:** `location` (optional; omit = all sites), `top_n` (default 5),
  `sort` (`least_free` | `most_free`, default `least_free`).
- **Behavior:** enumerate `ClusterComputeResource` → site-filter → bulk-fetch
  CPU/mem/disk capacity-remaining stats → compute a **normalized free-capacity score**
  (combine CPU/mem/disk remaining %) → rank.
- **Returns:** ranked rows of `cluster name, site, CPU/mem/disk remaining (% + absolute),
  demand, health`.
- Answers "which cluster has the fewest free resources in Madrid."

### 2. `vrops_oversized_vms_report`

- **Params:** `location` (optional), `top_n` (default 20), `min_reclaimable` (optional floor).
- **Behavior:** enumerate `VirtualMachine` → site-filter → bulk-fetch vROps **native
  rightsizing** metrics → keep oversized VMs → rank by reclaimable magnitude.
- **Returns:** rows of `VM name, site, current vs recommended vCPU/mem, reclaimable
  vCPU/memory`.
- Answers "report of oversized VMs."
- **Stat-key caveat:** exact rightsizing key names vary by Aria/vROps version.
  Implementation confirms them against the live instance via the existing
  `get_stat_keys` / statkeys endpoint rather than hardcoding blind. Candidate starting
  keys: `summary|oversized`, reclaimable CPU/memory (e.g. `cpu|reclaimable`,
  `mem|reclaimable`) and recommended-size keys. The spec records candidates; the PR
  records what was actually found.

### 3. `vrops_fleet_query` (generic escape hatch — built last / optional)

- **Params:** `resource_kind`, `location` (optional), `stat_keys[]`, `sort_by`,
  `top_n`.
- Same fleet pipeline with caller-chosen metrics, for ad-hoc fleet questions the two
  reports don't cover. Only build if real gaps appear.

## Error handling

Reuses existing conventions:

- Missing `VROPS_*` creds → standard credentials-error `ActionResult`; bot stays up.
- Unknown `location` → explicit "known sites: …" message.
- Empty fleet, or all requested stats missing → a clear "no data" result, never a
  silent empty table.
- All outputs flow through existing `_bound_raw` / `_format_tool_result` size
  guardrails; reports self-cap at `top_n` so large fleets can't cause truncation of
  the answer.

## System prompt

Add a short note to `DEFAULT_SYSTEM_PROMPT` (`src/config/settings.py`) listing the new
report tools and instructing the model to prefer them for fleet / ranking /
"which X has most/least" / rightsizing questions instead of manual per-resource
enumeration.

## Testing

Pure functions get real unit tests (no network), matching `tests/test_robustness.py`:

- **`analysis.py`** scoring/ranking: known inputs → expected order, tie-breaks,
  missing-stat handling.
- **`sites.py`**: case-insensitive match, unknown location, empty map, file load.
- **`fleet.py`**: full pipeline with a **faked/injected client** (canned resources +
  stats) → asserts filter-before-fetch, top-n capping, correct ranking. No live vROps.
- Client HTTP methods remain unit-untested (consistent with the current client),
  exercised manually against a live instance during implementation.

## Out of scope (YAGNI)

- No new transport (no Custom GPT, no standalone HTTP API).
- No site derivation from vROps tags/properties (local map only).
- No custom oversizing thresholds (native rightsizing only).
- No historical/trend fleet reports beyond what `analysis.compute_trend` already offers
  per-resource; fleet reports use latest stats.
- `vrops_fleet_query` is optional; defer unless the two reports prove insufficient.

## Open items to confirm during implementation

- Exact vROps rightsizing stat-key names for the active Aria/vROps version.
- The cheapest reliable way to resolve cluster → datacenter (relationships endpoint vs.
  a property already on the cluster resource).
- Confirm `GET /resources/stats/latest` accepts multiple `resourceId` params on the
  target instance; if not, fall back to chunked per-resource latest-stats.
