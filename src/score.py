"""Composite score used to rank listings within the digest (spec §8).

User preference (May 2026): price is acceptable anywhere ≤ €1100 — what truly
differentiates listings is how close they are, on foot, to the destinations
that matter. Each destination contributes its own walking-closeness term,
weighted per its config `score_weight`; price contributes nothing (the hard
cap in config.yaml does the gating). Elevator is a soft 0.10 bonus so central
pre-war buildings can enter the pool.

    score = Σ_dest  dest.score_weight · (1 − walk_min[dest]/40)
          + 0.10·(m²/80)
          + 0.10·freshness  (1.0 = just posted; 0.0 = older than freshness window)
          + 0.10·elevator   (1.0 = has lift; 0.0 = unknown or none)

With the default config (office 0.45 + Sadik Enter 0.25 + 0.10·3) the weights
sum to 1.0. The score is NEVER shown to the user — only used for digest
ordering. Higher is better.
"""
from __future__ import annotations

from datetime import datetime, timezone

from src.destinations import Destination
from src.models import Listing


SURFACE_WEIGHT = 0.10
FRESHNESS_WEIGHT = 0.10
ELEVATOR_WEIGHT = 0.10

SURFACE_CAP_M2 = 80      # diminishing returns above this
WALK_CAP_MIN = 40        # walks approaching the cap earn near-zero credit


def _commute_term(minutes: int | None, cap: int) -> float:
    if minutes is None:
        return 0.0
    return max(0.0, 1.0 - minutes / cap)


def score(
    l: Listing, *, destinations: list[Destination], freshness_days: int,
    now: datetime | None = None,
) -> float:
    now = now or datetime.now(timezone.utc)

    walk_score = sum(
        d.score_weight * _commute_term(l.commute.get(d.name), WALK_CAP_MIN)
        for d in destinations
    )

    surface_term = min(1.0, l.m2 / SURFACE_CAP_M2)

    age_hours = (now - l.created_at).total_seconds() / 3600
    freshness_term = max(0.0, 1.0 - age_hours / (freshness_days * 24))

    elevator_term = 1.0 if l.elevator else 0.0

    return (
        walk_score
        + SURFACE_WEIGHT * surface_term
        + FRESHNESS_WEIGHT * freshness_term
        + ELEVATOR_WEIGHT * elevator_term
    )


def is_pet_friendly(l: Listing) -> bool:
    """A listing is pet-friendly when *explicitly* confirmed.

    Structured True wins. LLM "yes" counts. False/None do not, since most
    Belgrade listings simply don't mention pets and silence is not consent.
    """
    if l.pets_allowed is True:
        return True
    if l.extraction is not None and l.extraction.pets_allowed == "yes":
        return True
    return False


def rank_descending(
    listings: list[Listing], *, destinations: list[Destination], freshness_days: int,
) -> list[Listing]:
    """Return listings sorted by composite score, highest first.

    Pets-friendly listings form a priority tier — they always rank above any
    non-pet-friendly listing, regardless of composite score. Within each tier
    the composite score is the tiebreaker.
    """
    def sort_key(l: Listing) -> tuple[int, float]:
        s = score(l, destinations=destinations, freshness_days=freshness_days)
        return (1 if is_pet_friendly(l) else 0, s)
    return sorted(listings, key=sort_key, reverse=True)
