# VM Placement Recommendation (vrops_placement_recommendation)

**Date:** 2026-06-11
**Status:** Design approved, pending spec review
**Branch:** continues on `feat/ops-assistant-fleet-queries` (or a follow-up branch)

## Goal

Let the team ask "where should I place a VM of N vCPU and M GB in site X — which is the
best host?" and get a concrete recommendation. Example:

> "Necesito colocar una VM de 4 vCPU y 12 GB en lab — ¿cuál es el mejor host?"

This is distinct from the capacity *reports*: it takes a **requested VM size** and finds
the best **fit**, rather than just ranking current free capacity.

## Decisions (confirmed with user)

1. **Target:** cluster → host. Pick the best cluster for the site, then the best host
   within it. Mirrors how placement actually works (you target a cluster; a host is the
   concrete landing spot).
2. **Fit basis:** capacity-engine primary, raw shown too. The fit decision uses vROps
   `capacityRemaining` (post-HA/buffer, safe); raw headroom (total − usage) is computed
   and reported alongside for context.
3. **Best = most free after placement.** Among candidates that fit, pick the one with the
   most bottleneck headroom remaining once the VM lands — spreads load, avoids hotspots.

## Tool

`vrops_placement_recommendation`

- **Params:**
  - `vcpu` (integer, required) — requested vCPU count.
  - `memory_gb` (number, required) — requested memory in GB.
  - `location` (string, optional) — physical site; omit = whole estate.
  - `top_n` (integer, default 3) — how many host candidates to return.
- **Handler:** `src/actions/builtin/vrops/placement.py`.

## Flow (reuses the fleet layer)

1. **Scope clusters** in `location` via `fleet.resolve_scope(client, site_map, location,
   "ClusterComputeResource")`. Unknown location → the standard "known sites: …" error.
2. **Cluster stage:** bulk-fetch the placement keys for the clusters; compute fit + score;
   keep clusters that fit; rank by headroom-after-placement; pick the best fitting cluster.
   If no cluster fits, still pick the *closest* (highest score) so the host stage can
   report specifics, but mark `fits=false`.
3. **Host stage:** enumerate `HostSystem` beneath the chosen cluster
   (`fleet.collect_descendants(client, [cluster_id], "HostSystem")`), compute fit + score,
   rank, return up to `top_n`.
4. Return the recommended cluster → host with fit breakdowns and alternatives.

## Stat keys (verified on the live instance; both clusters and hosts expose them)

- `cpu|capacity_provisioned` — total CPU MHz.
- `cpu|corecount_provisioned` — core/vCPU count (for MHz-per-vCPU).
- `OnlineCapacityAnalytics|cpu|demand|capacityRemaining` — free CPU MHz (capacity-engine).
- `cpu|capacity_usagepct_average` — raw CPU usage %.
- `OnlineCapacityAnalytics|mem|demand|capacityRemaining` — free memory KB (capacity-engine).
- `mem|usage_average` — raw memory usage %.
- `mem|host_provisioned` (hosts) / `mem|demand|usableCapacity` (clusters) — total memory KB
  (for raw headroom). Use whichever is present.

## Fit math (pure helpers in `analysis.py`)

- **MHz per vCPU:** `mhz_per_vcpu(cpu_capacity_mhz, corecount)` = `cpu_capacity_mhz /
  corecount` (None if either missing / corecount ≤ 0). If a *cluster* lacks corecount,
  derive the ratio from the first host beneath it.
- **CPU fit:** `required_mhz = vcpu * mhz_per_vcpu`. Fits if
  `cpu_capacityRemaining_mhz ≥ required_mhz`. `cpu_free_after_mhz = remaining − required`.
- **Memory fit:** `required_kb = memory_gb * 1048576`. Fits if
  `mem_capacityRemaining_kb ≥ required_kb`. `mem_free_after_kb = remaining − required`.
- **Candidate fits** ⇔ CPU fits AND memory fits (capacity-engine basis).
- **Score (headroom after placement):** express CPU and memory free-after as a % of the
  candidate's total capacity, take the **minimum** (bottleneck). Higher = better. Used to
  rank both clusters and hosts. `free_capacity_score` (existing min-helper) is reused.
- **Raw headroom (context only):** `raw_free = total * (1 − usage%/100)` for CPU and
  memory; reported but not used for the fit decision.

## Output (`ActionResult.raw`)

```
{
  "request": {"vcpu": 4, "memory_gb": 12},
  "location": "lab",
  "recommended": {
    "cluster": "cluster-01a",
    "host": "esx-02a.corp.local",          # null if nothing fits
    "fits": true
  },
  "candidates": [                            # up to top_n hosts, ranked
    {
      "host": "...", "cluster": "...", "fits": true,
      "headroom_after_pct": 18.3,            # ranking score (bottleneck % after)
      "cpu":    {"free_mhz": 10292, "required_mhz": 8400, "free_after_mhz": 1892,
                 "raw_free_mhz": 13000, "fits": true},
      "memory": {"free_gb": 0.0, "required_gb": 12, "free_after_gb": -12.0,
                 "raw_free_gb": 2.0, "fits": false}
    }, ...
  ]
}
```

Summary (headline) names the recommendation or, when nothing fits, the limiting dimension
and closest option, e.g. *"No host in lab can fit 4 vCPU / 12 GB — memory is the blocker
(max free ≈ 0 GB capacity-engine / 2 GB raw on esx-02a); CPU would fit."*

## Error handling

- Missing creds → standard credentials error.
- Unknown location → "known sites: …".
- `vcpu`/`memory_gb` missing or non-positive → `ActionResult(success=False, ...)`.
- No clusters/hosts in scope, or all sizing stats absent → clear "no data / cannot
  evaluate" result; never a silent empty recommendation.
- Handler never raises (broad try/except, matching the other report handlers).

## System prompt

Add a note: "where can I place / where should I put a VM of N vCPU and M GB (optionally in
a site)" → `vrops_placement_recommendation`; the site is the `location` param.

## Testing

Pure helpers (no network) in `tests/`:
- `mhz_per_vcpu`: normal, missing inputs, zero corecount.
- CPU/memory fit + free-after + score: fits, doesn't-fit, bottleneck selection, raw vs
  capacity-engine.
Handler via monkeypatched fake client + `asyncio.run`:
- happy path (a host fits) → recommended cluster+host, ranked candidates.
- nothing fits (12 GB in a memory-bound site) → `fits=false`, blocker named, closest shown.
- unknown location → error with known sites.
Live verify: "4 vCPU / 12 GB in lab" against the real instance.

## Out of scope (YAGNI)

- No actual provisioning / vMotion — recommendation only.
- No multi-VM / batch placement.
- No storage/datastore placement (CPU + memory only; storage capacity isn't reliably
  published per host here).
- No anti-affinity / DRS-rule awareness.
