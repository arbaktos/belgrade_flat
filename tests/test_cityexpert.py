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


def test_heating_codes_mapped_to_canonical():
    assert cityexpert._heating_label([1]) == "centralno"
    assert cityexpert._heating_label([4]) == "elektricni"
    assert cityexpert._heating_label([10]) == "TA"
    assert cityexpert._heating_label([21]) == "podno"
    assert cityexpert._heating_label([26]) == "klima"
    assert cityexpert._heating_label([99]) == "etazno"


def test_heating_multi_code_picks_highest_priority():
    # centralno beats anything else; etazno beats elektricni; TA beats elektricni.
    assert cityexpert._heating_label([1, 4]) == "centralno"
    assert cityexpert._heating_label([4, 4, 99]) == "etazno"
    assert cityexpert._heating_label([4, 4, 10]) == "TA"


def test_heating_empty_or_unknown_returns_none():
    assert cityexpert._heating_label([]) is None
    assert cityexpert._heating_label(None) is None
    assert cityexpert._heating_label([12345]) is None
    assert cityexpert._heating_label(["bogus"]) is None


def test_pets_allowed_from_petsarray():
    # Real API returns integer codes (1=cats, etc.) — any code means pets are allowed.
    # Regression: the old check was `"petAllowed" in petsArray`, which always
    # evaluated False and mislabeled cat-friendly listings as no-pets.
    assert cityexpert._pets_allowed([1, 2]) is True
    assert cityexpert._pets_allowed([1]) is True
    assert cityexpert._pets_allowed([1, 2, 3, 4, 5]) is True
    assert cityexpert._pets_allowed([]) is None
    assert cityexpert._pets_allowed(None) is None


def test_no_elevator_but_low_implies_no_elevator():
    data = json.loads(FIXTURE.read_text())
    sample = dict(data["result"][0])
    sample["isNoElevatorButLow"] = True
    l = cityexpert._parse(sample)
    assert l.elevator is False
    sample["isNoElevatorButLow"] = False
    l = cityexpert._parse(sample)
    assert l.elevator is True
