"""Process Telegram callback_queries (Skip-this-listing button clicks).

GH Actions can't host a webhook, so we drain pending callbacks via getUpdates
at the start of every workflow run, *before* the new digest is generated.
That way clicks on yesterday's digest take effect today.

callback_data format: 'skip:<fingerprint_key>' (max 64 bytes per Telegram).
"""
from __future__ import annotations

import logging
import sqlite3
from typing import Any

from src import telegram

log = logging.getLogger(__name__)

OFFSET_META_KEY = "telegram_update_offset"
SKIP_PREFIX = "skip:"


def drain(conn: sqlite3.Connection) -> dict[str, int]:
    """Pull pending updates, persist any Skip clicks, advance the offset.

    Returns a counter dict: {'fetched': N, 'skipped': M, 'unknown': K}.
    """
    counts = {"fetched": 0, "skipped": 0, "unknown": 0}
    offset = _read_offset(conn)
    # Diagnostic: log webhook status — a set webhook silently steals callbacks
    # before getUpdates can drain them.
    try:
        info = telegram.get_webhook_info()
        url = info.get("url") or "(no webhook)"
        last_err = info.get("last_error_message") or ""
        log.info("webhook status: url=%s pending=%s last_error=%s",
                 url, info.get("pending_update_count"), last_err)
    except Exception as e:  # noqa: BLE001
        log.warning("getWebhookInfo failed: %s", e)
    log.info("drain start: offset=%d", offset)
    try:
        updates = telegram.get_updates(offset=offset)
    except Exception as e:  # noqa: BLE001 - polling failure mustn't break the run
        log.warning("getUpdates failed: %s", e)
        return counts

    counts["fetched"] = len(updates)
    if not updates:
        log.info("callbacks drained: %s (no pending updates)", counts)
        return counts

    max_update_id = offset - 1 if offset > 0 else -1
    for u in updates:
        max_update_id = max(max_update_id, int(u.get("update_id", 0)))
        cq = u.get("callback_query")
        if not cq:
            counts["unknown"] += 1
            continue
        data = cq.get("data") or ""
        cq_id = cq.get("id")
        if data.startswith(SKIP_PREFIX):
            fp = data[len(SKIP_PREFIX):]
            _mark_skipped(conn, fp)
            counts["skipped"] += 1
            _ack(cq_id, "🙈 Hidden from future digests")
        else:
            counts["unknown"] += 1
            _ack(cq_id, "Unknown action")

    _write_offset(conn, max_update_id + 1)
    log.info("callbacks drained: %s", counts)
    return counts


def skipped_keys(conn: sqlite3.Connection) -> set[str]:
    """Return the full set of fingerprint_keys the user has hidden."""
    return {row[0] for row in conn.execute("SELECT fingerprint_key FROM skipped")}


def _mark_skipped(conn: sqlite3.Connection, fingerprint_key: str) -> None:
    conn.execute(
        "INSERT OR IGNORE INTO skipped (fingerprint_key) VALUES (?)",
        (fingerprint_key,),
    )
    conn.commit()


def _ack(callback_id: str | None, text: str) -> None:
    if not callback_id:
        return
    try:
        telegram.answer_callback_query(callback_id, text=text)
    except Exception as e:  # noqa: BLE001
        log.warning("answerCallbackQuery failed: %s", e)


def _read_offset(conn: sqlite3.Connection) -> int:
    row = conn.execute(
        "SELECT value FROM meta WHERE key=?", (OFFSET_META_KEY,)
    ).fetchone()
    return int(row[0]) if row else 0


def _write_offset(conn: sqlite3.Connection, offset: int) -> None:
    conn.execute(
        "INSERT INTO meta (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (OFFSET_META_KEY, str(offset)),
    )
    conn.commit()
