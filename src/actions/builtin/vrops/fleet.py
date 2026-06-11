"""Fleet-query orchestration: scope a resource set, then bulk-fetch metrics.

Free of Slack/LLM concerns and takes an injected client, so it is unit-tested with
a fake client and no network. The client must provide: list_resources_by_kind,
search_resources, get_child_resources, get_latest_stats_bulk.
"""

from __future__ import annotations

import logging
from typing import Optional

from .sites import SiteMap


class UnknownLocation(Exception):
    """Raised when a location filter is not present in the site map."""

    def __init__(self, location: str, known: list[str]):
        self.location = location
        self.known = known
        super().__init__(f"Unknown location '{location}'")


# Container kinds we descend through when collecting a target kind beneath a site.
# vROps relationships are single-hop, so reaching VMs under a Datacenter means
# walking these intermediate containers level by level.
DEFAULT_CONTAINER_KINDS = (
    "Datacenter", "HostFolder", "VMFolder",
    "ClusterComputeResource", "ResourcePool", "HostSystem",
)


def collect_descendants(client, root_ids: list[str], target_kind: str,
                        container_kinds=DEFAULT_CONTAINER_KINDS,
                        max_depth: int = 8) -> list[dict]:
    """BFS via single-hop CHILD relationships from root_ids, descending only into
    container kinds, collecting resources of target_kind. Deduped and depth-capped.

    Never recurses into the target kind itself (e.g. when collecting clusters we
    don't walk down into each cluster's hosts).
    """
    containers = set(container_kinds)
    found: dict[str, dict] = {}
    visited: set[str] = set()
    frontier = list(dict.fromkeys(root_ids))
    depth = 0
    while frontier and depth < max_depth:
        nxt: list[str] = []
        for rid in frontier:
            if rid in visited:
                continue
            visited.add(rid)
            for child in client.get_child_resources(rid):
                cid = child.get("identifier")
                kind = child.get("resourceKind")
                if not cid:
                    continue
                if kind == target_kind:
                    found.setdefault(cid, child)
                elif kind in containers and cid not in visited:
                    nxt.append(cid)
        frontier = nxt
        depth += 1
    return list(found.values())


def resolve_scope(client, site_map: SiteMap, location: Optional[str],
                  resource_kind: str, adapter_kind: str = "VMWARE") -> list[dict]:
    """In-scope resources of `resource_kind`.

    No location -> the whole estate (one enumeration call). With a location ->
    resources of that kind beneath the location's datacenters, found via the
    recursive CHILD walk. Raises UnknownLocation when the location is not
    configured, so callers never silently scan everything.
    """
    if not location:
        return client.list_resources_by_kind(resource_kind, adapter_kind=adapter_kind)

    dc_names = site_map.datacenters_for(location)
    if dc_names is None:
        raise UnknownLocation(location, site_map.known_locations())

    dc_ids: list[str] = []
    for dc_name in dc_names:
        for dc in client.search_resources(name=dc_name, resource_kind="Datacenter"):
            did = dc.get("identifier")
            if did:
                dc_ids.append(did)
    if not dc_ids:
        # Location is configured but none of its datacenter names matched a vROps
        # resource — likely a stale/misconfigured site map. Empty, but log why.
        logging.warning("Location '%s' has no matching vROps datacenters (names=%s)",
                        location, dc_names)
        return []
    # NOTE: adapter_kind is not forwarded here — site scoping descends via
    # get_child_resources (CHILD relationships), which has no adapter filter.
    return collect_descendants(client, dc_ids, resource_kind)


def attach_stats(client, resources: list[dict], stat_keys: list[str]) -> list[dict]:
    """Bulk-fetch stat_keys for the given (already-scoped) resources and attach them
    as row['stats']. Stats are fetched ONLY for in-scope resources."""
    ids = [r["identifier"] for r in resources if r.get("identifier")]
    stats_by_id = client.get_latest_stats_bulk(ids, stat_keys)
    rows = []
    for r in resources:
        rid = r.get("identifier")
        if not rid:
            continue  # malformed resource with no id — can't fetch stats or key it
        rows.append({
            "id": rid,
            "name": r.get("name"),
            "health": r.get("health"),
            "stats": stats_by_id.get(rid, {}),
        })
    return rows


def build_rows(client, site_map: SiteMap, location: Optional[str],
               resource_kind: str, stat_keys: list[str],
               adapter_kind: str = "VMWARE") -> list[dict]:
    """Full pipeline: resolve scope (filter) THEN fetch stats."""
    resources = resolve_scope(client, site_map, location, resource_kind, adapter_kind)
    return attach_stats(client, resources, stat_keys)
