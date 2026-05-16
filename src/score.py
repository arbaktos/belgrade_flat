"""Composite score used to rank listings within the digest (spec §8).

    score = 0.4·(1 − price/cap)
          + 0.3·(1 − best_commute/30)
          + 0.2·(m²/80)
          + 0.1·freshness  (1.0 = just posted; 0.0 = older than freshness window)

The score is NEVER shown to the user — only used for digest ordering. Higher
is better.
"""
from __future__ import annotations

from datetime import datetime, timezone

from src.models import Listing


PRICE_WEIGHT = 0.4
COMMUTE_WEIGHT = 0.3
SURFACE_WEIGHT = 0.2
FRESHNESS_WEIGHT = 0.1

SURFACE_CAP_M2 = 80      # diminishing returns above this
COMMUTE_CAP_MIN = 30


def score(l: Listing, *, price_cap_eur: float, freshness_days: int, now: datetime | None = None) -> float:
    now = now or datetime.now(timezone.utc)
    price_term = max(0.0, 1.0 - l.price_eur / max(price_cap_eur, 1))

    best_commute = min(
        l.walk_min if l.walk_min is not None else 10**9,
        l.transit_min if l.transit_min is not None else 10**9,
    )
    if best_commute >= 10**9:
        commute_term = 0.0
    else:
        commute_term = max(0.0, 1.0 - best_commute / COMMUTE_CAP_MIN)

    surface_term = min(1.0, l.m2 / SURFACE_CAP_M2)

    age_hours = (now - l.created_at).total_seconds() / 3600
    freshness_term = max(0.0, 1.0 - age_hours / (freshness_days * 24))

    return (
        PRICE_WEIGHT * price_term
        + COMMUTE_WEIGHT * commute_term
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
