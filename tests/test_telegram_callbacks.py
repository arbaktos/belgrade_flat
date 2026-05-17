from __future__ import annotations

import sqlite3
from unittest.mock import patch

import pytest

from src import telegram_callbacks


@pytest.fixture
def conn():
    db = sqlite3.connect(":memory:")
    db.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
    db.execute("CREATE TABLE skipped (fingerprint_key TEXT PRIMARY KEY, skipped_at TEXT DEFAULT (datetime('now')))")
    return db


def test_drain_no_updates_returns_zero(conn):
    with patch("src.telegram_callbacks.telegram.get_updates", return_value=[]):
        counts = telegram_callbacks.drain(conn)
    assert counts == {"fetched": 0, "skipped": 0, "unknown": 0}


def test_drain_records_skip_click_and_acks(conn):
    update = {
        "update_id": 42,
        "callback_query": {
            "id": "cb1",
            "data": "skip:4zida:abc123",
            "from": {"id": 1},
        },
    }
    with patch("src.telegram_callbacks.telegram.get_updates", return_value=[update]) as get_mock, \
         patch("src.telegram_callbacks.telegram.answer_callback_query") as ack_mock:
        counts = telegram_callbacks.drain(conn)

    assert counts == {"fetched": 1, "skipped": 1, "unknown": 0}
    get_mock.assert_called_once_with(offset=0)
    ack_mock.assert_called_once()
    # DB has the skip
    rows = list(conn.execute("SELECT fingerprint_key FROM skipped"))
    assert rows == [("4zida:abc123",)]
    # Offset advanced past update_id 42
    off = telegram_callbacks._read_offset(conn)
    assert off == 43


def test_drain_advances_offset_so_we_dont_replay(conn):
    """Second drain should request offset = max(update_id) + 1 from the first."""
    update = {"update_id": 100, "callback_query": {"id": "x", "data": "skip:halo:42"}}
    with patch("src.telegram_callbacks.telegram.get_updates", return_value=[update]), \
         patch("src.telegram_callbacks.telegram.answer_callback_query"):
        telegram_callbacks.drain(conn)
    # Now drain again — should pass offset=101
    with patch("src.telegram_callbacks.telegram.get_updates", return_value=[]) as get_mock:
        telegram_callbacks.drain(conn)
    get_mock.assert_called_once_with(offset=101)


def test_drain_ignores_unrelated_callbacks(conn):
    update = {"update_id": 7, "callback_query": {"id": "y", "data": "pause:something"}}
    with patch("src.telegram_callbacks.telegram.get_updates", return_value=[update]), \
         patch("src.telegram_callbacks.telegram.answer_callback_query"):
        counts = telegram_callbacks.drain(conn)
    assert counts == {"fetched": 1, "skipped": 0, "unknown": 1}


def test_skipped_keys_returns_set(conn):
    conn.executemany("INSERT INTO skipped (fingerprint_key) VALUES (?)",
                     [("4zida:a",), ("halo:b",)])
    keys = telegram_callbacks.skipped_keys(conn)
    assert keys == {"4zida:a", "halo:b"}


def test_drain_swallows_get_updates_failure(conn):
    with patch("src.telegram_callbacks.telegram.get_updates", side_effect=RuntimeError("net")):
        counts = telegram_callbacks.drain(conn)
    assert counts == {"fetched": 0, "skipped": 0, "unknown": 0}
