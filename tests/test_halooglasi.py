import pathlib
from datetime import datetime
from unittest.mock import patch

import pytest

from src.sources import _flaresolverr, halooglasi

LIST_HTML = pathlib.Path("tests/fixtures/halooglasi_list.html").read_text()


def test_blocked_when_flaresolverr_unreachable():
    with patch.object(_flaresolverr, "is_available", return_value=False):
        with pytest.raises(halooglasi.SourceBlockedError):
            halooglasi.fetch(freshness_days=7)


def test_parse_list_skips_banners_and_yields_listings():
    listings = halooglasi._parse_list(LIST_HTML)
    assert listings, "fixture should yield product listings"
    for l in listings:
        assert l.source == "halooglasi"
        assert l.url.startswith("https://www.halooglasi.com")
        assert "?" not in l.url, "tracking query string must be stripped"
        assert l.id
        assert isinstance(l.created_at, datetime)


def test_listing_fields_populated():
    listings = halooglasi._parse_list(LIST_HTML)
    first = listings[0]
    assert first.price_eur > 0
    assert first.m2 > 0
    assert first.rooms > 0
    assert first.floor is not None
    assert first.elevator is None  # card-only — detail page would set this


def test_price_thousands_separator():
    assert halooglasi._parse_price("900") == 900.0
    assert halooglasi._parse_price("1.000") == 1000.0
    assert halooglasi._parse_price("2.500") == 2500.0
    assert halooglasi._parse_price(None) == 0.0


def test_roman_floor_parsing():
    assert halooglasi._parse_floor("VI/7") == (6, 7)
    assert halooglasi._parse_floor("V/6") == (5, 6)
    assert halooglasi._parse_floor("XII/15") == (12, 15)
    assert halooglasi._parse_floor("PR/4") == (0, 4)
    assert halooglasi._parse_floor("SUT/4") == (-1, 4)
    assert halooglasi._parse_floor("") == (None, None)


def test_date_with_trailing_dot():
    d = halooglasi._parse_date("16.05.2026.")
    assert d is not None
    assert (d.year, d.month, d.day) == (2026, 5, 16)
