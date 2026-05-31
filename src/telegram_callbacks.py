"""Process Telegram callback_queries (Skip / Favorite button clicks).

GH Actions can't host a webhook, so we drain pending callbacks via getUpdates
at the start of every workflow run, *before* the new digest is generated.
That way clicks on yesterday's digest take effect today.

callback_data formats (max 64 bytes per Telegram):
- 'skip:<fingerprint_key>'  — 🙈 Hide: persist to `skipped` and drop from future digests.
- 'fav:<fingerprint_key>'   — ⭐ Favorite: persist to `favorites` and copy the original
   card to TELEGRAM_FAVORITES_CHAT_ID (if configured).
"""
from __future__ import annotations

import logging
import os
import sqlite3
from typing import Any

from src import telegram

log = logging.getLogger(__name__)

OFFSET_META_KEY = "telegram_update_offset"
SKIP_PREFIX = "skip:"
FAV_PREFIX = "fav:"


def drain(conn: sqlite3.Connection) -> dict[str, int]:
    """Pull pending updates, persist any Skip clicks, advance the offset.

    Returns a counter dict: {'fetched': N, 'skipped': M, 'unknown': K}.
    """
    counts = {"fetched": 0, "skipped": 0, "favorited": 0, "unknown": 0}
    offset = _read_offset(conn)
    # A configured webhook (the VM real-time handler) owns callbacks: Telegram
    # delivers updates there and getUpdates returns empty. Detect it and no-op
    # so this stays an intentional skip rather than a mysterious silent drain.
    try:
        info = telegram.get_webhook_info()
        url = info.get("url") or ""
        last_err = info.get("last_error_message") or ""
        log.info("webhook status: url=%s pending=%s last_error=%s",
                 url or "(no webhook)", info.get("pending_update_count"), last_err)
        if url:
            log.info("callbacks handled by webhook %s — skipping getUpdates drain", url)
            return counts
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
        handle_callback_query(conn, cq, counts)

    _write_offset(conn, max_update_id + 1)
    log.info("callbacks drained: %s", counts)
    return counts


def handle_callback_query(
    conn: sqlite3.Connection, cq: dict[str, Any], counts: dict[str, int],
) -> None:
    """Apply one Telegram callback_query (🙈 Skip / ⭐ Favorite), updating
    `counts` in place and sending the toast ack.

    Shared by the CI getUpdates drain and the VM webhook server, so a click
    has identical effect whichever path delivered it.
    """
    data = cq.get("data") or ""
    cq_id = cq.get("id")
    if data.startswith(SKIP_PREFIX):
        fp = data[len(SKIP_PREFIX):]
        _mark_skipped(conn, fp)
        counts["skipped"] = counts.get("skipped", 0) + 1
        _delete_owning_message(cq)
        _ack(cq_id, "🙈 Hidden")
    elif data.startswith(FAV_PREFIX):
        fp = data[len(FAV_PREFIX):]
        _mark_favorited(conn, fp)
        counts["favorited"] = counts.get("favorited", 0) + 1
        ack_text = _forward_to_favorites(cq)
        _ack(cq_id, ack_text)
    else:
        counts["unknown"] = counts.get("unknown", 0) + 1
        _ack(cq_id, "Unknown action")


def skipped_keys(conn: sqlite3.Connection) -> set[str]:
    """Return the full set of fingerprint_keys the user has hidden."""
    return {row[0] for row in conn.execute("SELECT fingerprint_key FROM skipped")}


def favorited_keys(conn: sqlite3.Connection) -> set[str]:
    """Return the full set of fingerprint_keys the user has starred."""
    return {row[0] for row in conn.execute("SELECT fingerprint_key FROM favorites")}


def _mark_skipped(conn: sqlite3.Connection, fingerprint_key: str) -> None:
    conn.execute(
        "INSERT OR IGNORE INTO skipped (fingerprint_key) VALUES (?)",
        (fingerprint_key,),
    )
    conn.commit()


def _delete_owning_message(cq: dict[str, Any]) -> None:
    """Remove the listing card that owns this 🙈 button from the chat.

    Soft-fails: the listing is already persisted to `skipped`, so a failed
    delete (message > 48 h old, already gone, or missing ids) just leaves the
    card on screen — the hide still takes effect on future digests. Only the
    card message is deleted; a separate translation follow-up, if any, isn't
    referenced by the callback and stays.
    """
    msg = cq.get("message") or {}
    chat_id = (msg.get("chat") or {}).get("id")
    message_id = msg.get("message_id")
    if not chat_id or not message_id:
        return
    try:
        telegram.delete_message(chat_id, int(message_id))
    except Exception as e:  # noqa: BLE001 — hide already persisted; deletion is best-effort
        log.info("hide: could not delete message %s/%s: %s", chat_id, message_id, e)


def _mark_favorited(conn: sqlite3.Connection, fingerprint_key: str) -> None:
    conn.execute(
        "INSERT OR IGNORE INTO favorites (fingerprint_key) VALUES (?)",
        (fingerprint_key,),
    )
    conn.commit()


def _forward_to_favorites(cq: dict[str, Any]) -> str:
    """Copy the message that owns this callback to the favorites chat.

    Returns the ack text to show in the Telegram toast. Soft-fails: if the
    destination isn't configured or the API call errors out, the listing is
    still persisted in `favorites` (the caller already did that) — we just
    surface the reason to the user via the toast.
    """
    dest = os.environ.get("TELEGRAM_FAVORITES_CHAT_ID")
    if not dest:
        log.info("favorite: TELEGRAM_FAVORITES_CHAT_ID unset; saved to DB only")
        return "⭐ Saved (no favorites chat configured)"
    msg = cq.get("message") or {}
    chat = msg.get("chat") or {}
    from_chat_id = chat.get("id")
    message_id = msg.get("message_id")
    if not from_chat_id or not message_id:
        log.warning("favorite: callback missing message ids; cq=%s", cq)
        return "⭐ Saved (couldn't locate source message)"
    thread_id_raw = os.environ.get("TELEGRAM_FAVORITES_THREAD_ID")
    thread_id = int(thread_id_raw) if thread_id_raw else None
    # copyMessage drops the source keyboard, and the listing URL lives only on
    # the 'View on portal' button — re-attach it so the favorite stays clickable.
    keyboard = _url_buttons_only(msg.get("reply_markup"))
    try:
        telegram.copy_message(
            from_chat_id=from_chat_id,
            message_id=int(message_id),
            to_chat_id=dest,
            message_thread_id=thread_id,
            reply_markup=keyboard,
        )
    except Exception as e:  # noqa: BLE001 - delivery failure stays soft
        log.warning("favorite: copyMessage failed: %s", e)
        return "⭐ Saved (forwarding failed — see logs)"
    return "⭐ Saved to favorites"


def _url_buttons_only(reply_markup: dict[str, Any] | None) -> dict[str, Any] | None:
    """Strip a card's keyboard down to its URL buttons (the portal link).

    The Favorite/Hide callback buttons are dropped: in the favorites chat they
    are meaningless and would carry stale callback_data. Returns None when the
    card had no URL button, so copyMessage is called without a keyboard.
    """
    if not reply_markup:
        return None
    rows = []
    for row in reply_markup.get("inline_keyboard", []):
        kept = [btn for btn in row if btn.get("url")]
        if kept:
            rows.append(kept)
    return {"inline_keyboard": rows} if rows else None


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
