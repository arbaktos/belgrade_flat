from __future__ import annotations

import sqlite3

import pytest

from src import state


@pytest.fixture
def conn():
    db = sqlite3.connect(":memory:")
    db.execute("CREATE TABLE skipped (fingerprint_key TEXT PRIMARY KEY, skipped_at TEXT)")
    db.execute(
        """CREATE TABLE listings (
            fingerprint_key TEXT PRIMARY KEY, source TEXT, id TEXT, url TEXT,
            price_eur REAL, m2 REAL, rooms REAL, title TEXT, image_phash TEXT
        )"""
    )
    return db


def _add_listing(conn, fk, source, id_, price, m2, title, phash):
    conn.execute(
        "INSERT INTO listings (fingerprint_key, source, id, url, price_eur, m2, rooms, title, image_phash) "
        "VALUES (?,?,?,?,?,?,?,?,?)",
        (fk, source, id_, "http://x", price, m2, 2.0, title, phash),
    )
    conn.commit()


def test_skipped_listings_recovers_data_via_join(conn):
    _add_listing(conn, "4zida:a1", "4zida", "a1", 900, 60, "Vračar stan", "ff00ff00ffff0000")
    conn.execute("INSERT INTO skipped (fingerprint_key) VALUES ('4zida:a1')")
    conn.commit()

    recs = state.skipped_listings(conn)
    assert len(recs) == 1
    r = recs[0]
    assert r.fingerprint_key == "4zida:a1"
    assert r.price_eur == 900 and r.m2 == 60
    assert r.title == "Vračar stan"
    assert r.image_phash == "ff00ff00ffff0000"


def test_skipped_listings_omits_skips_with_no_listing_row(conn):
    # Hidden flat whose listing row was never persisted → simply absent.
    conn.execute("INSERT INTO skipped (fingerprint_key) VALUES ('halo:ghost')")
    conn.commit()
    assert state.skipped_listings(conn) == []


def test_skipped_listings_empty_when_nothing_hidden(conn):
    _add_listing(conn, "4zida:a1", "4zida", "a1", 900, 60, "x", None)
    assert state.skipped_listings(conn) == []
