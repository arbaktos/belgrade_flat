from __future__ import annotations

from datetime import datetime, timezone

from src import filter as filt
from src.models import Listing


CFG = filt.FilterConfig(
    price_eur_max=1000, rooms_min=1.5, rooms_max=3.0, surface_m2_min=55,
    elevator_required=True, photo_required=True, freshness_days=7,
    heating_allowed=("centralno", "etazno", "podno"),
    dishwasher_required=False, pets_required=True, max_lease_months=12,
    walk_min_max=30, transit_min_max=30,
)


def _l(*, walk=None, transit=None) -> Listing:
    return Listing(
        id="x", source="4zida", url="https://x", price_eur=900, m2=60, rooms=2.0,
        floor=3, total_floors=5, last_floor=False, elevator=True,
        furnished="yes", heating_type="centralno", pets_allowed=True,
        title="t", description="d", address=None, place_names=[], image_url=None,
        is_agency=False, created_at=datetime.now(timezone.utc),
        walk_min=walk, transit_min=transit,
    )


def test_walk_in_window_passes():
    r = filt.apply_commute([_l(walk=22, transit=40)], CFG)
    assert len(r.passed) == 1 and not r.rejected


def test_transit_in_window_passes():
    r = filt.apply_commute([_l(walk=42, transit=18)], CFG)
    assert len(r.passed) == 1


def test_both_too_far_rejected():
    r = filt.apply_commute([_l(walk=42, transit=45)], CFG)
    assert not r.passed and r.rejected
    assert "walk 42m" in r.rejected[0][1]


def test_no_route_rejected():
    r = filt.apply_commute([_l(walk=None, transit=None)], CFG)
    assert r.rejected[0][1].startswith("no route")
