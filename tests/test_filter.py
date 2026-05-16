from datetime import datetime, timedelta, timezone

from src import filter as filt
from src.models import Listing


def _listing(**overrides) -> Listing:
    base = dict(
        id="x",
        source="4zida",
        url="https://x",
        price_eur=900,
        m2=60,
        rooms=2.0,
        floor=3,
        total_floors=5,
        last_floor=False,
        elevator=True,
        furnished="yes",
        heating_type="district",
        pets_allowed=True,
        title="t",
        description="d",
        address=None,
        place_names=["Vračar"],
        image_url=None,
        is_agency=False,
        created_at=datetime.now(timezone.utc) - timedelta(days=1),
    )
    base.update(overrides)
    return Listing(**base)


CFG = filt.FilterConfig(
    price_eur_max=1000,
    rooms_min=1.5,
    rooms_max=3.0,
    surface_m2_min=55,
    elevator_required=True,
    freshness_days=7,
)


def test_baseline_passes():
    result = filt.apply([_listing()], CFG)
    assert len(result.passed) == 1
    assert not result.rejected


def test_price_over_cap_rejected():
    result = filt.apply([_listing(price_eur=1500)], CFG)
    assert not result.passed
    assert "price" in result.rejected[0][1]


def test_too_small_rejected():
    result = filt.apply([_listing(m2=40)], CFG)
    assert "surface" in result.rejected[0][1]


def test_ground_floor_rejected():
    result = filt.apply([_listing(floor=0)], CFG)
    assert "ground" in result.rejected[0][1]


def test_no_elevator_rejected():
    result = filt.apply([_listing(elevator=False)], CFG)
    assert "elevator" in result.rejected[0][1]


def test_unknown_elevator_passes_as_near_miss():
    """elevator=None means the source did not expose this field — don't hard-reject."""
    result = filt.apply([_listing(elevator=None)], CFG)
    assert result.passed and not result.rejected


def test_rooms_out_of_range_rejected():
    result = filt.apply([_listing(rooms=4.0)], CFG)
    assert "rooms" in result.rejected[0][1]


def test_stale_rejected():
    old = datetime.now(timezone.utc) - timedelta(days=14)
    result = filt.apply([_listing(created_at=old)], CFG)
    assert "older" in result.rejected[0][1]
