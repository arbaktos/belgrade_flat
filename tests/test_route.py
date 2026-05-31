from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from unittest.mock import MagicMock

import pytest

from src import route
from src.destinations import Destination
from src.models import Listing


OFFICE = Destination(name="office", lat=44.806, lng=20.460, gates=True, score_weight=0.45)
SADIK = Destination(name="Sadik Enter", lat=44.807, lng=20.464, gates=False, score_weight=0.25)


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
def conn():
    db = sqlite3.connect(":memory:")
    db.execute(
        """CREATE TABLE commute_cache (
            bucket_key TEXT PRIMARY KEY,
            walk_min INTEGER,
            fetched_at TEXT NOT NULL DEFAULT (datetime('now'))
        )"""
    )
    return db


def _walk_client(minutes: int):
    """Fake httpx client returning a single WALK route of `minutes`."""
    client = MagicMock()
    client.post.return_value = MagicMock(
        status_code=200,
        json=MagicMock(return_value={"routes": [{"duration": f"{minutes * 60}s"}]}),
    )
    return client


def test_haversine_belgrade_landmark_distance():
    km = route.haversine_km(44.8225, 20.4513, 44.7984, 20.4694)
    assert 2.0 < km < 3.5


def test_haversine_self_is_zero():
    assert route.haversine_km(44.0, 20.0, 44.0, 20.0) == pytest.approx(0.0, abs=1e-6)


def test_bucket_key_includes_destination():
    l = _listing(lat=44.81234, lng=20.45678)
    # Same location, different destination → different cache key.
    assert route.bucket_key(l, "office") != route.bucket_key(l, "Sadik Enter")
    assert route.bucket_key(l, "office").endswith("@office")


def test_bucket_key_coords_use_3_decimal_grid():
    l1 = _listing(lat=44.81234, lng=20.45678)
    l2 = _listing(lat=44.81244, lng=20.45688)   # ~10m — same bucket
    l3 = _listing(lat=44.81500, lng=20.45678)   # ~300m — different bucket
    assert route.bucket_key(l1, "office") == route.bucket_key(l2, "office")
    assert route.bucket_key(l1, "office") != route.bucket_key(l3, "office")


def test_bucket_key_falls_back_to_address_hash():
    l = _listing(lat=None, lng=None, address="Krunska 35", place_names=["Vračar"])
    key = route.bucket_key(l, "office")
    assert key.startswith("addr:") and key.endswith("@office")


def test_haversine_prefilter_caches_skip(conn):
    far = _listing(lat=45.0, lng=22.0)   # well outside Belgrade
    result = route.compute_walk(far, OFFICE, conn=conn, api_key="fake", client=MagicMock())
    assert result.source == "haversine_skipped"
    assert result.walk_min is None
    # Second call hits the cache, no API.
    result2 = route.compute_walk(far, OFFICE, conn=conn, api_key="fake", client=MagicMock())
    assert result2.source == "cache"


def test_compute_walk_hits_api_then_cache(conn):
    near = _listing(lat=44.810, lng=20.465)
    client = _walk_client(20)
    r = route.compute_walk(near, OFFICE, conn=conn, api_key="fake", client=client)
    assert r.source == "api"
    assert r.walk_min == 20
    assert client.post.call_count == 1
    # Only a WALK request is made — no transit.
    assert client.post.call_args.kwargs["json"]["travelMode"] == "WALK"

    r2 = route.compute_walk(near, OFFICE, conn=conn, api_key="fake", client=client)
    assert r2.source == "cache"
    assert client.post.call_count == 1     # no new API hit


def test_compute_walk_per_destination_caches_separately(conn):
    near = _listing(lat=44.810, lng=20.465)
    office_client = _walk_client(20)
    sadik_client = _walk_client(31)
    r_office = route.compute_walk(near, OFFICE, conn=conn, api_key="fake", client=office_client)
    r_sadik = route.compute_walk(near, SADIK, conn=conn, api_key="fake", client=sadik_client)
    assert r_office.walk_min == 20
    assert r_sadik.walk_min == 31
    # Both cached independently.
    assert route.compute_walk(near, OFFICE, conn=conn, api_key="fake", client=MagicMock()).walk_min == 20
    assert route.compute_walk(near, SADIK, conn=conn, api_key="fake", client=MagicMock()).walk_min == 31


def test_compute_walk_handles_no_route(conn):
    near = _listing(lat=44.810, lng=20.465)
    client = MagicMock()
    client.post.return_value = MagicMock(
        status_code=200, json=MagicMock(return_value={"routes": []}),
    )
    r = route.compute_walk(near, OFFICE, conn=conn, api_key="fake", client=client)
    assert r.walk_min is None
    assert r.source == "api"


def test_compute_walk_raises_on_403(conn):
    near = _listing(lat=44.810, lng=20.465)
    client = MagicMock()
    client.post.return_value = MagicMock(
        status_code=403,
        text='{"error": {"message": "Routes API not enabled"}}',
        json=MagicMock(return_value={"error": {"message": "Routes API not enabled"}}),
    )
    with pytest.raises(route.DirectionsConfigError):
        route.compute_walk(near, OFFICE, conn=conn, api_key="fake", client=client)


def test_monthly_api_count_one_per_row(conn):
    near = _listing(lat=44.810, lng=20.465)
    route.compute_walk(near, OFFICE, conn=conn, api_key="fake", client=_walk_client(20))
    route.compute_walk(near, SADIK, conn=conn, api_key="fake", client=_walk_client(31))
    assert route.monthly_api_count(conn) == 2
