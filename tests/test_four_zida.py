import json
import pathlib
from datetime import datetime, timezone

from src.sources import four_zida


FIXTURE = pathlib.Path("tests/fixtures/four_zida_belgrade.json")


def test_parse_real_ads():
    data = json.loads(FIXTURE.read_text())
    listings = [four_zida._parse_ad(a) for a in data["ads"]]
    listings = [l for l in listings if l is not None]
    assert listings, "fixture should contain parseable ads"
    for l in listings:
        assert l.source == "4zida"
        assert l.url.startswith("https://www.4zida.rs")
        assert l.id
        assert l.price_eur > 0
        assert l.m2 > 0
        assert l.rooms > 0
        assert isinstance(l.created_at, datetime)
        assert l.created_at.tzinfo is not None


def test_parse_skips_malformed():
    bad = {"id": "x"}  # missing createdAt
    assert four_zida._parse_ad(bad) is None


def test_elevator_is_truthy_when_count_positive():
    data = json.loads(FIXTURE.read_text())
    ad = dict(data["ads"][0])
    ad["elevator"] = 2
    listing = four_zida._parse_ad(ad)
    assert listing.elevator is True
    ad["elevator"] = 0
    listing = four_zida._parse_ad(ad)
    assert listing.elevator is False


def test_url_path_attached_to_base():
    data = json.loads(FIXTURE.read_text())
    ad = dict(data["ads"][0])
    ad["urlPath"] = "/izdavanje-stanova/foo/bar/abc123"
    listing = four_zida._parse_ad(ad)
    assert listing.url == "https://www.4zida.rs/izdavanje-stanova/foo/bar/abc123"
