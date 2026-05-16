from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest

from src import route
from src.models import Listing


OFFICE_LAT = 44.806
OFFICE_LNG = 20.460


def _listing(**over) -> Listing:
    base = dict(
        id="x", source="4zida", url="https://x",
        price_eur=900, m2=60, rooms=2.0,
        floor=3, total_floors=5, last_floor=False, elevator=True,
        furnished="yes", heating_type="district", pets_allowed=None,
        title="t", description="d", address=None, place_names=["Vračar"],
        image_url=None, is_agency=False, created_at=datetime.now(timezone.utc),
    )
    base.update(over)
    return Listing(**base)


@pytest.fixture
def conn(tmp_path):
    db = sqlite3.connect(":memory:")
    db.execute(
        """CREATE TABLE commute_cache (
            bucket_key TEXT PRIMARY KEY,
            walk_min INTEGER, transit_min INTEGER,
            fetched_at TEXT NOT NULL DEFAULT (datetime('now'))
        )"""
    )
    return db


def test_haversine_belgrade_landmark_distance():
    # Kalemegdan to Hram Svetog Save ≈ 2.5 km
    km = route.haversine_km(44.8225, 20.4513, 44.7984, 20.4694)
    assert 2.0 < km < 3.5


def test_haversine_self_is_zero():
    assert route.haversine_km(44.0, 20.0, 44.0, 20.0) == pytest.approx(0.0, abs=1e-6)


def test_bucket_key_coords_use_3_decimal_grid():
    l1 = _listing(lat=44.81234, lng=20.45678)
    l2 = _listing(lat=44.81244, lng=20.45688)   # 10m away — same bucket
    l3 = _listing(lat=44.81500, lng=20.45678)   # ~300m — different bucket
    assert route.bucket_key(l1) == route.bucket_key(l2)
    assert route.bucket_key(l1) != route.bucket_key(l3)


def test_bucket_key_falls_back_to_address_hash():
    l = _listing(lat=None, lng=None, address="Krunska 35", place_names=["Vračar"])
    key = route.bucket_key(l)
    assert key.startswith("addr:")


def test_haversine_prefilter_caches_skip(conn):
    """A listing > 10 km from office should skip the API and cache a null result."""
    far = _listing(lat=45.0, lng=22.0)   # well outside Belgrade
    result = route.compute_commute(
        far, office_lat=OFFICE_LAT, office_lng=OFFICE_LNG,
        conn=conn, api_key="fake", client=MagicMock(),
    )
    assert result.source == "haversine_skipped"
    assert result.walk_min is None and result.transit_min is None
    # On second call we hit the cache, no API.
    result2 = route.compute_commute(
        far, office_lat=OFFICE_LAT, office_lng=OFFICE_LNG,
        conn=conn, api_key="fake", client=MagicMock(),
    )
    assert result2.source == "cache"


def test_compute_commute_hits_api_then_cache(conn):
    near = _listing(lat=44.810, lng=20.465)
    fake_client = MagicMock()

    def fake_get(url, params=None, **_):
        mode = params["mode"]
        # walking 20 min, transit 12 min
        duration = 1200 if mode == "walking" else 720
        return MagicMock(
            raise_for_status=MagicMock(),
            json=MagicMock(return_value={
                "status": "OK",
                "routes": [{"legs": [{"duration": {"value": duration}}]}],
            }),
        )

    fake_client.get.side_effect = fake_get

    r = route.compute_commute(
        near, office_lat=OFFICE_LAT, office_lng=OFFICE_LNG,
        conn=conn, api_key="fake", client=fake_client,
    )
    assert r.source == "api"
    assert r.walk_min == 20
    assert r.transit_min == 12
    assert fake_client.get.call_count == 2     # one walk, one transit

    # Second call uses cache.
    r2 = route.compute_commute(
        near, office_lat=OFFICE_LAT, office_lng=OFFICE_LNG,
        conn=conn, api_key="fake", client=fake_client,
    )
    assert r2.source == "cache"
    assert fake_client.get.call_count == 2     # unchanged — no new API hits


def test_compute_commute_handles_no_route(conn):
    """Google returning ZERO_RESULTS should land in cache as (None, None)."""
    near = _listing(lat=44.810, lng=20.465)
    fake_client = MagicMock()
    fake_client.get.return_value = MagicMock(
        raise_for_status=MagicMock(),
        json=MagicMock(return_value={"status": "ZERO_RESULTS"}),
    )
    r = route.compute_commute(
        near, office_lat=OFFICE_LAT, office_lng=OFFICE_LNG,
        conn=conn, api_key="fake", client=fake_client,
    )
    assert r.walk_min is None and r.transit_min is None
    assert r.source == "api"
