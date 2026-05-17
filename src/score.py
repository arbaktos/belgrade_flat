"""Composite score used to rank listings within the digest (spec §8).

User preference (May 2026): price is acceptable anywhere ≤ €1100 — what truly
differentiates listings is commute. Walk and transit dominate the score; price
contributes nothing (the hard cap in config.yaml does the gating). Elevator
moved from hard-required to a soft 0.10 bonus so central pre-war buildings
can enter the pool.

    score = 0.45·(1 − walk_min/40)
          + 0.25·(1 − transit_min/30)
          + 0.10·(m²/80)
          + 0.10·freshness  (1.0 = just posted; 0.0 = older than freshness window)
          + 0.10·elevator   (1.0 = has lift; 0.0 = unknown or none)

The score is NEVER shown to the user — only used for digest ordering. Higher
is better. `price_cap_eur` is kept in the signature for call-site stability;
it's unused.
"""
from __future__ import annotations

from datetime import datetime, timezone

from src.models import Listing


WALK_WEIGHT = 0.45
TRANSIT_WEIGHT = 0.25
SURFACE_WEIGHT = 0.10
FRESHNESS_WEIGHT = 0.10
ELEVATOR_WEIGHT = 0.10

SURFACE_CAP_M2 = 80      # diminishing returns above this
WALK_CAP_MIN = 40        # 35-40 min walks still earn partial credit
TRANSIT_CAP_MIN = 30


def _commute_term(minutes: int | None, cap: int) -> float:
    if minutes is None:
        return 0.0
    return max(0.0, 1.0 - minutes / cap)


def score(l: Listing, *, price_cap_eur: float, freshness_days: int, now: datetime | None = None) -> float:
    del price_cap_eur  # unused — price doesn't enter the score (hard-capped upstream)
    now = now or datetime.now(timezone.utc)

    walk_term = _commute_term(l.walk_min, WALK_CAP_MIN)
    transit_term = _commute_term(l.transit_min, TRANSIT_CAP_MIN)

    surface_term = min(1.0, l.m2 / SURFACE_CAP_M2)

    age_hours = (now - l.created_at).total_seconds() / 3600
    freshness_term = max(0.0, 1.0 - age_hours / (freshness_days * 24))

    elevator_term = 1.0 if l.elevator else 0.0

    return (
        WALK_WEIGHT * walk_term
        + TRANSIT_WEIGHT * transit_term
        + SURFACE_WEIGHT * surface_term
        + FRESHNESS_WEIGHT * freshness_term
        + ELEVATOR_WEIGHT * elevator_term
    )


def rank_descending(listings: list[Listing], *, price_cap_eur: float, freshness_days: int) -> list[Listing]:
    """Return listings sorted by composite score, highest first."""
    return sorted(
        listings,
        key=lambda l: score(l, price_cap_eur=price_cap_eur, freshness_days=freshness_days),
        reverse=True,
    )
