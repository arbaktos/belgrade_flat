"""Named walking destinations (replaces the single-office commute anchor).

Each destination has a display name, coordinates resolved from environment
variables (kept in GitHub Secrets so real locations stay out of this public
repo), a `gates` flag (does its walking time gate the commute filter?), and a
`score_weight` (how much closeness to it influences digest ordering).

Config lives under `filters.commute.destinations` in config.yaml. Coordinates
are NEVER stored in config — only the names of the env vars that hold them.
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Destination:
    name: str
    lat: float
    lng: float
    gates: bool          # True → walking time to here gates the commute filter
    score_weight: float  # contribution to the composite digest-ordering score


def load(cfg: dict) -> list[Destination]:
    """Build the destination list from config + environment.

    A destination whose lat/lng env vars are unset or unparseable is skipped
    with a warning (so a missing SADIK_LAT degrades to office-only rather than
    crashing the run). Returns destinations in config order.
    """
    raw = (((cfg.get("filters") or {}).get("commute") or {}).get("destinations")) or []
    out: list[Destination] = []
    for entry in raw:
        name = entry.get("name")
        lat = _env_float(entry.get("lat_env"))
        lng = _env_float(entry.get("lng_env"))
        if not name or lat is None or lng is None:
            log.warning(
                "destinations: skipping %r — missing name or coords (%s/%s)",
                name, entry.get("lat_env"), entry.get("lng_env"),
            )
            continue
        out.append(Destination(
            name=name,
            lat=lat,
            lng=lng,
            gates=bool(entry.get("gates", False)),
            score_weight=float(entry.get("score_weight", 0.0)),
        ))
    return out


def gating(destinations: list[Destination]) -> list[Destination]:
    """The subset whose walking time gates the commute filter."""
    return [d for d in destinations if d.gates]


def _env_float(var: str | None) -> float | None:
    if not var:
        return None
    val = os.environ.get(var)
    if val is None or val.strip() == "":
        return None
    try:
        return float(val)
    except ValueError:
        log.warning("destinations: env %s=%r is not a float", var, val)
        return None
