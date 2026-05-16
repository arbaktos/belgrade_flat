import pathlib

from src.sources import nekretnine

LIST_HTML = pathlib.Path("tests/fixtures/nekretnine_list.html").read_text()
DETAIL_HTML = pathlib.Path("tests/fixtures/nekretnine_detail.html").read_text()


def test_parse_cards_basic():
    cards = nekretnine._parse_cards(LIST_HTML)
    assert cards, "fixture should yield at least one card"
    first = cards[0]
    assert first["id"]
    assert first["url"].startswith("https://www.nekretnine.rs")
    assert first["price_eur"] > 0
    assert first["m2"] > 0
    assert first["rooms"] > 0
    assert first["posted_at"] is not None


def test_rooms_word_mapping():
    assert nekretnine._rooms_from_category("Dvosoban stan") == 2.0
    assert nekretnine._rooms_from_category("Trosoban") == 3.0
    assert nekretnine._rooms_from_category("Garsonjera") == 0.5
    assert nekretnine._rooms_from_category("nothing") is None


def test_parse_detail_extracts_spec_rows():
    detail = nekretnine._parse_detail(DETAIL_HTML)
    assert detail["floor"] == 10
    assert detail["heating"] == "Centralno"
    assert detail["elevator"] is True  # fixture lists "Lift" in features


def test_parse_floor_tokens():
    assert nekretnine._parse_floor("10 / -") == (10, None)
    assert nekretnine._parse_floor("3/5") == (3, 5)
    assert nekretnine._parse_floor("PR / 4") == (0, 4)
    assert nekretnine._parse_floor("SU / 4") == (-1, 4)
    assert nekretnine._parse_floor("") == (None, None)
