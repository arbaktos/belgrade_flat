from __future__ import annotations

from datetime import datetime, timezone

from src import filter as filt
from src.models import Listing


CFG = filt.FilterConfig(
    price_eur_max=1000, rooms_min=1.5, rooms_max=3.0, surface_m2_min=55,
    elevator_required=True, photo_required=True, freshness_days=7,
    heating_allowed=("centralno", "etazno", "podno"),
    dishwasher_required=False, pets_required=True, max_lease_months=12,
    walk_min_max=40,
)

# office gates; Sadik Enter is info-only.
GATING = ["office"]


def _l(commute: dict) -> Listing:
    return Listing(
        id="x", source="4zida", url="https://x", price_eur=900, m2=60, rooms=2.0,
        floor=3, total_floors=5, last_floor=False, elevator=True,
        furnished="yes", heating_type="centralno", pets_allowed=True,
        title="t", description="d", address=None, place_names=[], image_url=None,
        is_agency=False, created_at=datetime.now(timezone.utc),
        commute=commute,
    )


def test_office_within_40_passes():
    r = filt.apply_commute([_l({"office": 35, "Sadik Enter": 50})], CFG, gating_names=GATING)
    assert len(r.passed) == 1 and not r.rejected


def test_office_at_boundary_passes():
    r = filt.apply_commute([_l({"office": 40})], CFG, gating_names=GATING)
    assert len(r.passed) == 1


def test_office_over_40_rejected():
    r = filt.apply_commute([_l({"office": 41, "Sadik Enter": 5})], CFG, gating_names=GATING)
    assert not r.passed
    assert "office 41m > 40m walk" in r.rejected[0][1]


def test_sadik_distance_never_filters():
    # Office is fine, Sadik is far — still passes (Sadik is info-only).
    r = filt.apply_commute([_l({"office": 10, "Sadik Enter": 90})], CFG, gating_names=GATING)
    assert len(r.passed) == 1


def test_no_office_route_rejected():
    r = filt.apply_commute([_l({"office": None, "Sadik Enter": 12})], CFG, gating_names=GATING)
    assert not r.passed
    assert r.rejected[0][1] == "no walking route to office"


def test_missing_office_key_rejected():
    # Destination not computed at all (key absent) → treated as no route.
    r = filt.apply_commute([_l({"Sadik Enter": 12})], CFG, gating_names=GATING)
    assert not r.passed
    assert "no walking route to office" in r.rejected[0][1]
