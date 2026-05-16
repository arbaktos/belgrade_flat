from __future__ import annotations

from datetime import datetime, timezone

from src import filter as filt
from src.models import Extraction, Listing


CFG = filt.FilterConfig(
    price_eur_max=1000, rooms_min=1.5, rooms_max=3.0, surface_m2_min=55,
    elevator_required=True, freshness_days=7,
    heating_allowed=("centralno", "etazno", "podno"),
    dishwasher_required=True, pets_required=True, max_lease_months=12,
)


def _listing(extraction: Extraction | None) -> Listing:
    return Listing(
        id="x", source="4zida", url="https://x", price_eur=900, m2=60, rooms=2.0,
        floor=3, total_floors=5, last_floor=False, elevator=True,
        furnished="yes", heating_type=None, pets_allowed=None,
        title="t", description="d", address=None, place_names=[], image_url=None,
        is_agency=False, created_at=datetime.now(timezone.utc),
        extraction=extraction,
    )


def test_passes_when_all_llm_clear():
    e = Extraction(
        pets_allowed="yes", dishwasher=True, heating_type_confirmed="centralno",
        max_lease_months=12, agency_or_owner="agency",
    )
    r = filt.apply_with_extraction([_listing(e)], CFG)
    assert len(r.passed) == 1 and not r.near_misses and not r.rejected


def test_pets_no_is_hard_reject():
    e = Extraction(pets_allowed="no", dishwasher=True, heating_type_confirmed="centralno")
    r = filt.apply_with_extraction([_listing(e)], CFG)
    assert not r.passed and r.rejected and "pets" in r.rejected[0][1]


def test_pets_unknown_is_near_miss():
    e = Extraction(pets_allowed="unknown", dishwasher=True, heating_type_confirmed="centralno")
    r = filt.apply_with_extraction([_listing(e)], CFG)
    assert not r.passed and r.near_misses and "pets" in r.near_misses[0][1][0]


def test_no_dishwasher_hard_reject():
    e = Extraction(pets_allowed="yes", dishwasher=False, heating_type_confirmed="centralno")
    r = filt.apply_with_extraction([_listing(e)], CFG)
    assert "dishwasher" in r.rejected[0][1]


def test_bad_heating_hard_reject():
    e = Extraction(pets_allowed="yes", dishwasher=True, heating_type_confirmed="TA")
    r = filt.apply_with_extraction([_listing(e)], CFG)
    assert "heating" in r.rejected[0][1]


def test_long_lease_hard_reject():
    e = Extraction(
        pets_allowed="yes", dishwasher=True, heating_type_confirmed="centralno",
        max_lease_months=24,
    )
    r = filt.apply_with_extraction([_listing(e)], CFG)
    assert "lease" in r.rejected[0][1]


def test_missing_extraction_lands_in_near_miss_not_hard_reject():
    """If the LLM failed AND no structured data, treat as near-miss not silent pass."""
    r = filt.apply_with_extraction([_listing(None)], CFG)
    assert not r.passed and not r.rejected
    assert len(r.near_misses) == 1
    reasons = r.near_misses[0][1]
    assert any("pets" in x for x in reasons)


def test_structured_data_prefers_over_llm():
    """4zida exposes heatingType='district' structurally → should be canonicalized
    to 'centralno' without needing LLM confirmation."""
    e = Extraction(pets_allowed="unknown", dishwasher=True, heating_type_confirmed=None)
    listing = _listing(e)
    listing.heating_type = "district"
    listing.pets_allowed = True
    listing.dishwasher = None  # let LLM handle this one
    r = filt.apply_with_extraction([listing], CFG)
    # pets=True structurally → pass; dishwasher=True (LLM) → pass; heating=district→centralno → pass
    assert len(r.passed) == 1


def test_canonicalize_heating():
    assert filt.canonicalize_heating("district") == "centralno"
    assert filt.canonicalize_heating("gas") == "etazno"
    assert filt.canonicalize_heating("Centralno") == "centralno"
    assert filt.canonicalize_heating("TA") == "TA"
    assert filt.canonicalize_heating("TA peć") is None  # diacritic; we lowercase but don't normalize spaces/special chars yet
    assert filt.canonicalize_heating(None) is None
    assert filt.canonicalize_heating("Centralno grejanje") == "centralno"
