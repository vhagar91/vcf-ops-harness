"""Local datacenter-name -> location mapping, loaded from a JSON config file.

The bot filters fleet queries by physical site (e.g. 'Madrid') using this map,
because the site is not modeled in vROps directly. Map shape:

    {"Madrid": ["dc-mad-01", "dc-mad-02"], "Frankfurt": ["dc-fra-01"]}

Matching is case-insensitive on the location name.
"""

from __future__ import annotations

import json
import logging
from typing import Optional


class SiteMap:
    """Read-only lookup from a physical location to its vROps Datacenter names."""

    def __init__(self, mapping: dict[str, list[str]]):
        # Preserve display names; index by lowercased location for lookups.
        self._by_location: dict[str, tuple[str, list[str]]] = {}
        for loc, dcs in (mapping or {}).items():
            self._by_location[loc.lower()] = (loc, list(dcs))

    @classmethod
    def from_file(cls, path: Optional[str]) -> "SiteMap":
        """Load a site map from a JSON file. Missing/unset path or a bad file
        yields an empty map (no site filtering available), never an exception."""
        if not path:
            return cls({})
        try:
            with open(path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            if not isinstance(data, dict):
                logging.error("Site map %s is not a JSON object; ignoring", path)
                return cls({})
            return cls(data)
        except FileNotFoundError:
            logging.warning("Site map %s not found; no site filtering available", path)
            return cls({})
        except Exception as e:  # malformed JSON, permissions, etc.
            logging.error("Failed to load site map %s: %s", path, e)
            return cls({})

    def known_locations(self) -> list[str]:
        """Configured location display names."""
        return [display for display, _ in self._by_location.values()]

    def datacenters_for(self, location: str) -> Optional[list[str]]:
        """Datacenter names for a location, or None if the location is unknown."""
        entry = self._by_location.get((location or "").lower())
        return list(entry[1]) if entry else None
