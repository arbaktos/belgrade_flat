import json
import pathlib
from datetime import datetime

from src.sources import cityexpert

FIXTURE = pathlib.Path("tests/fixtures/cityexpert_search.json")


def test_parse_real_properties():
    data = json.loads(FIXTURE.read_text())
    listings = [cityexpert._parse(p) for p in data["result"]]
    listings = [l for l in listings if l is not None]
    assert listings
    for l in listings:
        assert l.source == "cityexpert"
        assert l.url.startswith("https://cityexpert.rs/izdavanje-nekretnina/beograd/")
        assert l.url.endswith("/stan")
        assert l.price_eur >= 0
        assert l.m2 >= 0
        assert isinstance(l.created_at, datetime)


def test_floor_parsing():
    assert cityexpert._parse_floor("2_4") == (2, 4)
    assert cityexpert._parse_floor("PR") == (0, None)
    assert cityexpert._parse_floor("VPR") == (0, None)
    assert cityexpert._parse_floor("SU") == (-1, None)
    assert cityexpert._parse_floor("10") == (10, None)
    assert cityexpert._parse_floor(None) == (None, None)


def test_image_url_uses_new_cdn_path():
    data = json.loads(FIXTURE.read_text())
    prop = data["result"][0]
    url = cityexpert._image_url(prop)
    assert url is not None
    assert url.startswith("https://img.cityexpert.rs/properties/720x/")
    assert f"/{prop['propId']}/slike/" in url
    assert url.endswith(prop["coverPhoto"])


def test_image_url_none_without_cover_photo():
    assert cityexpert._image_url({"propId": 1}) is None


def test_no_elevator_but_low_implies_no_elevator():
    data = json.loads(FIXTURE.read_text())
    sample = dict(data["result"][0])
    sample["isNoElevatorButLow"] = True
    l = cityexpert._parse(sample)
    assert l.elevator is False
    sample["isNoElevatorButLow"] = False
    l = cityexpert._parse(sample)
    assert l.elevator is True
