from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

import pytest

from src import state
from src.models import Extraction, Listing


@pytest.fixture
def conn():
    db = sqlite3.connect(":memory:")
    db.execute(
        """
        CREATE TABLE extraction_cache (
            fingerprint_key TEXT PRIMARY KEY,
            payload         TEXT NOT NULL,
            extracted_at    TEXT NOT NULL DEFAULT (datetime('now'))
        )
        """
    )
    return db


def _listing(source: str, id_: str, extraction: Extraction | None = None) -> Listing:
    l = Listing(
        id=id_, source=source, url="http://x", price_eur=800, m2=60, rooms=2.0,
        floor=3, total_floors=5, last_floor=False, elevator=True, furnished=None,
        heating_type=None, pets_allowed=None, title="t", description="d",
        address=None, place_names=[], image_url=None, is_agency=False,
        created_at=datetime.now(timezone.utc),
    )
    l.extraction = extraction
    return l


def test_save_then_load_round_trips_all_fields(conn):
    ex = Extraction(
        pets_allowed="yes", dishwasher=True, elevator_confirmed=False,
        heating_type_confirmed="centralno", furnishing_confirmed="furnished",
        max_lease_months=12, bills_estimate_eur=50, agency_or_owner="owner",
        red_flags=["no smoking"], summary_en="Nice flat.",
        description_en="A nice flat in Vračar.",
    )
    saved = state.save_extractions(conn, [_listing("4zida", "a1", ex)])
    assert saved == 1

    loaded = state.load_extractions(conn, ["4zida:a1"])
    assert loaded["4zida:a1"] == ex  # dataclass equality covers every field


def test_load_returns_only_known_keys(conn):
    state.save_extractions(conn, [_listing("halo", "h1", Extraction(pets_allowed="no"))])
    loaded = state.load_extractions(conn, ["halo:h1", "halo:missing"])
    assert set(loaded) == {"halo:h1"}


def test_load_empty_keys_is_empty(conn):
    assert state.load_extractions(conn, []) == {}


def test_save_skips_listings_without_extraction(conn):
    n = state.save_extractions(conn, [_listing("4zida", "nope", None)])
    assert n == 0
    assert state.load_extractions(conn, ["4zida:nope"]) == {}


def test_save_is_idempotent_upsert(conn):
    state.save_extractions(conn, [_listing("4zida", "a1", Extraction(summary_en="v1"))])
    state.save_extractions(conn, [_listing("4zida", "a1", Extraction(summary_en="v2"))])
    loaded = state.load_extractions(conn, ["4zida:a1"])
    assert loaded["4zida:a1"].summary_en == "v2"
    assert conn.execute("SELECT COUNT(*) FROM extraction_cache").fetchone()[0] == 1


def test_load_drops_corrupt_row_as_miss(conn):
    conn.execute(
        "INSERT INTO extraction_cache (fingerprint_key, payload) VALUES (?, ?)",
        ("4zida:bad", "{not valid json"),
    )
    conn.commit()
    assert state.load_extractions(conn, ["4zida:bad"]) == {}


def test_load_tolerates_unknown_payload_fields(conn):
    # A payload written by a future schema with an extra field must still load.
    conn.execute(
        "INSERT INTO extraction_cache (fingerprint_key, payload) VALUES (?, ?)",
        ("4zida:fut", '{"pets_allowed": "yes", "some_new_field": 123}'),
    )
    conn.commit()
    loaded = state.load_extractions(conn, ["4zida:fut"])
    assert loaded["4zida:fut"].pets_allowed == "yes"


def test_load_chunks_beyond_sqlite_variable_limit(conn):
    keys = [f"4zida:{i}" for i in range(1200)]
    state.save_extractions(conn, [_listing("4zida", str(i), Extraction()) for i in range(1200)])
    loaded = state.load_extractions(conn, keys)
    assert len(loaded) == 1200
