import json
import pathlib
from datetime import datetime, timezone
from unittest.mock import MagicMock

import httpx

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


def _listing_with_preview(id_: str, preview: str):
    data = json.loads(FIXTURE.read_text())
    ad = dict(data["ads"][0])
    ad["id"] = id_
    ad["description100"] = preview
    return four_zida._parse_ad(ad)


def _detail_response(desc):
    resp = MagicMock()
    resp.raise_for_status.return_value = None
    resp.json.return_value = {"desc": desc}
    return resp


def test_fetch_full_descriptions_upgrades_preview():
    l = _listing_with_preview("abc", "Nov stan u blizini Kneza Miloša . Sastoji se od hodnika,")
    full = "Nov stan u blizini Kneza Miloša. Sastoji se od hodnika i kuhinje. Kućni ljubimci nisu dozvoljeni."
    client = MagicMock()
    client.get.return_value = _detail_response(full)
    upgraded = four_zida.fetch_full_descriptions([l], client=client)
    assert upgraded == 1
    assert l.description == full
    client.get.assert_called_once_with("https://api.4zida.rs/v6/eds/abc")


def test_fetch_full_descriptions_keeps_preview_when_detail_shorter_or_empty():
    l = _listing_with_preview("abc", "already the whole description")
    client = MagicMock()
    client.get.return_value = _detail_response(None)
    assert four_zida.fetch_full_descriptions([l], client=client) == 0
    assert l.description == "already the whole description"


def test_fetch_full_descriptions_survives_dead_ad():
    # A delisted ad 404s; the other listing must still be upgraded.
    dead = _listing_with_preview("dead", "preview A")
    alive = _listing_with_preview("alive", "preview B")
    err = httpx.HTTPStatusError("404", request=MagicMock(), response=MagicMock())

    def get(url):
        if "dead" in url:
            raise err
        return _detail_response("preview B plus the full remainder of the text")

    client = MagicMock()
    client.get.side_effect = get
    assert four_zida.fetch_full_descriptions([dead, alive], client=client) == 1
    assert dead.description == "preview A"
    assert alive.description.endswith("remainder of the text")
