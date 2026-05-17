"""Composite score used to rank listings within the digest (spec §8).

User preference (May 2026): time-to-office matters more than price within the
allowed range — anything ≤ €1000 is acceptable, what really differentiates is
how long the commute is. Walking is preferred over transit, so the commute
weight is split: walking dominates and transit only shapes the tail.

    score = 0.30·(1 − walk_min/30)
          + 0.15·(1 − transit_min/30)
          + 0.25·(1 − price/cap)
          + 0.20·(m²/80)
          + 0.10·freshness  (1.0 = just posted; 0.0 = older than freshness window)

The score is NEVER shown to the user — only used for digest ordering. Higher
is better.
"""
from __future__ import annotations

from datetime import datetime, timezone

from src.models import Listing


WALK_WEIGHT = 0.30
TRANSIT_WEIGHT = 0.15
PRICE_WEIGHT = 0.25
SURFACE_WEIGHT = 0.20
FRESHNESS_WEIGHT = 0.10

SURFACE_CAP_M2 = 80      # diminishing returns above this
WALK_CAP_MIN = 30
TRANSIT_CAP_MIN = 30


def _commute_term(minutes: int | None, cap: int) -> float:
    if minutes is None:
        return 0.0
    return max(0.0, 1.0 - minutes / cap)


def score(l: Listing, *, price_cap_eur: float, freshness_days: int, now: datetime | None = None) -> float:
    now = now or datetime.now(timezone.utc)
    price_term = max(0.0, 1.0 - l.price_eur / max(price_cap_eur, 1))

    walk_term = _commute_term(l.walk_min, WALK_CAP_MIN)
    transit_term = _commute_term(l.transit_min, TRANSIT_CAP_MIN)

    surface_term = min(1.0, l.m2 / SURFACE_CAP_M2)

    age_hours = (now - l.created_at).total_seconds() / 3600
    freshness_term = max(0.0, 1.0 - age_hours / (freshness_days * 24))

    return (
        PRICE_WEIGHT * price_term
        + WALK_WEIGHT * walk_term
        + TRANSIT_WEIGHT * transit_term
        + SURFACE_WEIGHT * surface_term
        + FRESHNESS_WEIGHT * freshness_term
    )


def rank_descending(listings: list[Listing], *, price_cap_eur: float, freshness_days: int) -> list[Listing]:
    """Return listings sorted by composite score, highest first."""
    return sorted(
        listings,
        key=lambda l: score(l, price_cap_eur=price_cap_eur, freshness_days=freshness_days),
        reverse=True,
    )
