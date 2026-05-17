from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

from src import geocode
from src.models import Listing
from src.state import ensure_schema


def _listing(**over) -> Listing:
    base = dict(
        id="x", source="4zida", url="https://example.com",
        price_eur=800, m2=60, rooms=2.0,
        floor=2, total_floors=5, last_floor=False, elevator=True,
        furnished="yes", heating_type="centralno", pets_allowed=None,
        title="Test", description="",
        address="Krunska 35", place_names=["Vračar"],
        image_url=None, is_agency=False,
        created_at=datetime.now(timezone.utc),
    )
    base.update(over)
    return Listing(**base)


def test_query_string_builds_belgrade_address():
    q = geocode.query_string(_listing())
    assert "Krunska 35" in q
    assert "Vračar" in q
    assert "Belgrade" in q


def test_resolve_uses_existing_coords_without_api():
    conn = ensure_schema()
    listing = _listing(lat=44.81, lng=20.46)
    with patch.object(geocode, "_nominatim_search") as mock_search:
        coords = geocode.resolve(listing, conn)
    assert coords == (44.81, 20.46)
    mock_search.assert_not_called()
    conn.close()


def test_resolve_caches_nominatim_result():
    conn = ensure_schema()
    conn.execute("DELETE FROM geocode_cache")
    conn.commit()
    listing = _listing()
    with patch.object(geocode, "_nominatim_search", return_value=(44.802, 20.471)) as mock_search:
        first = geocode.resolve(listing, conn, client=MagicMock())
        second = geocode.resolve(listing, conn, client=MagicMock())
    assert first == (44.802, 20.471)
    assert second == (44.802, 20.471)
    mock_search.assert_called_once()
    conn.close()
