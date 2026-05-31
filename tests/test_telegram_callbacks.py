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
    db.execute("CREATE TABLE favorites (fingerprint_key TEXT PRIMARY KEY, favorited_at TEXT DEFAULT (datetime('now')))")
    return db


def test_drain_no_updates_returns_zero(conn):
    with patch("src.telegram_callbacks.telegram.get_updates", return_value=[]):
        counts = telegram_callbacks.drain(conn)
    assert counts == {"fetched": 0, "skipped": 0, "favorited": 0, "unknown": 0}


def test_drain_noops_when_webhook_is_set(conn):
    # When the VM webhook owns callbacks, drain must skip getUpdates entirely.
    with patch("src.telegram_callbacks.telegram.get_webhook_info",
               return_value={"url": "https://x.trycloudflare.com/tg/s"}), \
         patch("src.telegram_callbacks.telegram.get_updates") as get_mock:
        counts = telegram_callbacks.drain(conn)
    get_mock.assert_not_called()
    assert counts == {"fetched": 0, "skipped": 0, "favorited": 0, "unknown": 0}


def test_handle_callback_query_dispatches_skip(conn):
    counts = {"fetched": 0, "skipped": 0, "favorited": 0, "unknown": 0}
    with patch("src.telegram_callbacks.telegram.answer_callback_query"):
        telegram_callbacks.handle_callback_query(
            conn, {"id": "c1", "data": "skip:4zida:zzz"}, counts,
        )
    assert counts["skipped"] == 1
    assert list(conn.execute("SELECT fingerprint_key FROM skipped")) == [("4zida:zzz",)]


def test_hide_deletes_owning_message(conn):
    counts = {"skipped": 0, "favorited": 0, "unknown": 0}
    cq = {
        "id": "c1", "data": "skip:4zida:zzz",
        "message": {"message_id": 555, "chat": {"id": -100777}},
    }
    with patch("src.telegram_callbacks.telegram.answer_callback_query"), \
         patch("src.telegram_callbacks.telegram.delete_message") as del_mock:
        telegram_callbacks.handle_callback_query(conn, cq, counts)
    del_mock.assert_called_once_with(-100777, 555)
    assert counts["skipped"] == 1
    assert list(conn.execute("SELECT fingerprint_key FROM skipped")) == [("4zida:zzz",)]


def test_hide_soft_fails_when_delete_errors(conn):
    # A >48h-old message can't be deleted; the hide must still persist.
    counts = {"skipped": 0, "favorited": 0, "unknown": 0}
    cq = {
        "id": "c1", "data": "skip:4zida:zzz",
        "message": {"message_id": 5, "chat": {"id": 1}},
    }
    with patch("src.telegram_callbacks.telegram.answer_callback_query"), \
         patch("src.telegram_callbacks.telegram.delete_message",
               side_effect=RuntimeError("message to delete not found")):
        telegram_callbacks.handle_callback_query(conn, cq, counts)
    assert counts["skipped"] == 1
    assert list(conn.execute("SELECT fingerprint_key FROM skipped")) == [("4zida:zzz",)]


def test_favorite_does_not_delete_message(conn):
    # ⭐ Favorite keeps the card visible in the main feed — only 🙈 deletes.
    counts = {"skipped": 0, "favorited": 0, "unknown": 0}
    cq = {
        "id": "c1", "data": "fav:4zida:zzz",
        "message": {"message_id": 5, "chat": {"id": 1}},
    }
    with patch("src.telegram_callbacks.telegram.answer_callback_query"), \
         patch("src.telegram_callbacks._forward_to_favorites", return_value="⭐ Saved"), \
         patch("src.telegram_callbacks.telegram.delete_message") as del_mock:
        telegram_callbacks.handle_callback_query(conn, cq, counts)
    del_mock.assert_not_called()
    assert counts["favorited"] == 1


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

    assert counts == {"fetched": 1, "skipped": 1, "favorited": 0, "unknown": 0}
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
    assert counts == {"fetched": 1, "skipped": 0, "favorited": 0, "unknown": 1}


def test_skipped_keys_returns_set(conn):
    conn.executemany("INSERT INTO skipped (fingerprint_key) VALUES (?)",
                     [("4zida:a",), ("halo:b",)])
    keys = telegram_callbacks.skipped_keys(conn)
    assert keys == {"4zida:a", "halo:b"}


def test_drain_swallows_get_updates_failure(conn):
    with patch("src.telegram_callbacks.telegram.get_updates", side_effect=RuntimeError("net")):
        counts = telegram_callbacks.drain(conn)
    assert counts == {"fetched": 0, "skipped": 0, "favorited": 0, "unknown": 0}


def test_drain_records_favorite_and_copies_to_favorites_chat(conn, monkeypatch):
    monkeypatch.setenv("TELEGRAM_FAVORITES_CHAT_ID", "-5057252591")
    monkeypatch.delenv("TELEGRAM_FAVORITES_THREAD_ID", raising=False)
    update = {
        "update_id": 55,
        "callback_query": {
            "id": "cb2",
            "data": "fav:4zida:xyz789",
            "message": {
                "message_id": 1234,
                "chat": {"id": -100200300},
                "caption": "€890 · 2 rooms · Vračar\nKrunska 35",
                "reply_markup": {
                    "inline_keyboard": [
                        [{"text": "🔗 View on 4zida", "url": "https://4zida.rs/123"}],
                        [
                            {"text": "⭐ Favorite", "callback_data": "fav:4zida:xyz789"},
                            {"text": "🙈 Hide", "callback_data": "skip:4zida:xyz789"},
                        ],
                    ]
                },
            },
        },
    }
    with patch("src.telegram_callbacks.telegram.get_updates", return_value=[update]), \
         patch("src.telegram_callbacks.telegram.copy_message") as copy_mock, \
         patch("src.telegram_callbacks.telegram.answer_callback_query") as ack_mock:
        counts = telegram_callbacks.drain(conn)

    assert counts == {"fetched": 1, "skipped": 0, "favorited": 1, "unknown": 0}
    kw = copy_mock.call_args.kwargs
    assert kw["from_chat_id"] == -100200300 and kw["message_id"] == 1234
    assert kw["to_chat_id"] == "-5057252591"
    # Caption gains the ⭐ Favorited header above the original details.
    assert kw["caption"].startswith("⭐ <b>Favorited</b> · ")
    assert "Krunska 35" in kw["caption"]
    assert kw["parse_mode"] == "HTML"
    # Keyboard: portal link kept, Favorite/Hide stripped, Unfavorite added.
    kb = kw["reply_markup"]["inline_keyboard"]
    assert kb[0] == [{"text": "🔗 View on 4zida", "url": "https://4zida.rs/123"}]
    assert kb[-1] == [{"text": "❌ Unfavorite", "callback_data": "unfav:4zida:xyz789"}]
    # Toast text confirms the save.
    args, kwargs = ack_mock.call_args
    assert "Saved to favorites" in kwargs.get("text", args[1] if len(args) > 1 else "")
    # DB persisted the favorite.
    rows = list(conn.execute("SELECT fingerprint_key FROM favorites"))
    assert rows == [("4zida:xyz789",)]


def test_unfavorite_removes_from_db_and_deletes_message(conn):
    conn.execute("INSERT INTO favorites (fingerprint_key) VALUES ('4zida:xyz789')")
    conn.commit()
    counts = {"skipped": 0, "favorited": 0, "unknown": 0}
    cq = {
        "id": "u1", "data": "unfav:4zida:xyz789",
        "message": {"message_id": 42, "chat": {"id": -100200300}},
    }
    with patch("src.telegram_callbacks.telegram.answer_callback_query") as ack_mock, \
         patch("src.telegram_callbacks.telegram.delete_message") as del_mock:
        telegram_callbacks.handle_callback_query(conn, cq, counts)
    # Removed from the favorites table and the card deleted from the fav chat.
    assert list(conn.execute("SELECT * FROM favorites")) == []
    del_mock.assert_called_once_with(-100200300, 42)
    assert counts["unfavorited"] == 1
    args, kwargs = ack_mock.call_args
    assert "Removed from favorites" in kwargs.get("text", "")


def test_drain_favorite_without_chat_env_still_persists(conn, monkeypatch):
    monkeypatch.delenv("TELEGRAM_FAVORITES_CHAT_ID", raising=False)
    update = {
        "update_id": 88,
        "callback_query": {
            "id": "cb3",
            "data": "fav:halo:111",
            "message": {"message_id": 5, "chat": {"id": -1}},
        },
    }
    with patch("src.telegram_callbacks.telegram.get_updates", return_value=[update]), \
         patch("src.telegram_callbacks.telegram.copy_message") as copy_mock, \
         patch("src.telegram_callbacks.telegram.answer_callback_query"):
        counts = telegram_callbacks.drain(conn)

    assert counts["favorited"] == 1
    copy_mock.assert_not_called()
    rows = list(conn.execute("SELECT fingerprint_key FROM favorites"))
    assert rows == [("halo:111",)]


def test_url_buttons_only_keeps_link_drops_callbacks():
    markup = {
        "inline_keyboard": [
            [{"text": "🔗 View on halo", "url": "https://halooglasi.com/5"}],
            [
                {"text": "⭐ Favorite", "callback_data": "fav:halo:5"},
                {"text": "🙈 Hide", "callback_data": "skip:halo:5"},
            ],
        ]
    }
    assert telegram_callbacks._url_buttons_only(markup) == {
        "inline_keyboard": [
            [{"text": "🔗 View on halo", "url": "https://halooglasi.com/5"}],
        ]
    }


def test_url_buttons_only_none_when_no_url_button():
    callbacks_only = {"inline_keyboard": [[{"text": "🙈 Hide", "callback_data": "skip:x"}]]}
    assert telegram_callbacks._url_buttons_only(callbacks_only) is None
    assert telegram_callbacks._url_buttons_only(None) is None
    assert telegram_callbacks._url_buttons_only({}) is None


def test_favorited_keys_returns_set(conn):
    conn.executemany("INSERT INTO favorites (fingerprint_key) VALUES (?)",
                     [("4zida:a",), ("city:b",)])
    keys = telegram_callbacks.favorited_keys(conn)
    assert keys == {"4zida:a", "city:b"}
