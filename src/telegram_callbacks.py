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

import html
import logging
import os
import sqlite3
from datetime import datetime, timezone
from typing import Any

from src import telegram

log = logging.getLogger(__name__)

OFFSET_META_KEY = "telegram_update_offset"
SKIP_PREFIX = "skip:"
FAV_PREFIX = "fav:"
UNFAV_PREFIX = "unfav:"


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
    elif data.startswith(UNFAV_PREFIX):
        fp = data[len(UNFAV_PREFIX):]
        _mark_unfavorited(conn, fp)
        counts["unfavorited"] = counts.get("unfavorited", 0) + 1
        if _is_favorites_chat(cq):
            # Tapped on the saved card in the favorites chat → drop it from the list.
            _delete_owning_message(cq)
            _ack(cq_id, "❌ Removed from favorites")
        else:
            # Tapped on the digest card in the bot chat → just un-mark it in place.
            _set_chat_fav_state(cq, fp, favorited=False)
            _ack(cq_id, "Removed from favorites")
    elif data.startswith(FAV_PREFIX):
        fp = data[len(FAV_PREFIX):]
        _mark_favorited(conn, fp)
        counts["favorited"] = counts.get("favorited", 0) + 1
        ack_text = _forward_to_favorites(cq, fp)
        # Mark the original digest card in the bot chat as favorited.
        _set_chat_fav_state(cq, fp, favorited=True)
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


def _is_favorites_chat(cq: dict[str, Any]) -> bool:
    """Whether the callback's message lives in the favorites chat (vs the main
    bot chat). Used so Unfavorite deletes the saved card there, but only
    un-marks the digest card here."""
    dest = os.environ.get("TELEGRAM_FAVORITES_CHAT_ID")
    chat_id = ((cq.get("message") or {}).get("chat") or {}).get("id")
    return bool(dest) and str(chat_id) == dest


def _set_chat_fav_state(cq: dict[str, Any], fingerprint_key: str, *, favorited: bool) -> None:
    """Toggle the ⭐ button on the digest card in place (no message deleted).

    favorited=True  → '⭐ Favorite' (fav:…)  becomes '⭐ Favorited ✓' (unfav:…)
    favorited=False → reverts it. Soft-fails: the favorite is already persisted,
    so a failed edit just leaves the button label stale.
    """
    msg = cq.get("message") or {}
    chat_id = (msg.get("chat") or {}).get("id")
    message_id = msg.get("message_id")
    if not chat_id or not message_id:
        return
    keyboard = _toggle_fav_button(msg.get("reply_markup"), fingerprint_key, favorited)
    if keyboard is None:
        return
    try:
        telegram.edit_message_reply_markup(chat_id, int(message_id), keyboard)
    except Exception as e:  # noqa: BLE001 — favorite already persisted; mark is cosmetic
        log.info("favorite: could not update card markup %s/%s: %s", chat_id, message_id, e)


def _toggle_fav_button(
    reply_markup: dict[str, Any] | None, fingerprint_key: str, favorited: bool,
) -> dict[str, Any] | None:
    """Return a copy of the card keyboard with the favorite button flipped to
    the favorited / un-favorited state. None if there's no keyboard to edit."""
    if not reply_markup:
        return None
    want_old = (f"{FAV_PREFIX}{fingerprint_key}" if favorited
                else f"{UNFAV_PREFIX}{fingerprint_key}")
    new_text = "⭐ Favorited ✓" if favorited else "⭐ Favorite"
    new_cb = (f"{UNFAV_PREFIX}{fingerprint_key}" if favorited
              else f"{FAV_PREFIX}{fingerprint_key}")
    rows = []
    for row in reply_markup.get("inline_keyboard", []):
        new_row = []
        for btn in row:
            if btn.get("callback_data") == want_old:
                new_row.append({"text": new_text, "callback_data": new_cb})
            else:
                new_row.append(btn)
        rows.append(new_row)
    return {"inline_keyboard": rows}


def _mark_favorited(conn: sqlite3.Connection, fingerprint_key: str) -> None:
    conn.execute(
        "INSERT OR IGNORE INTO favorites (fingerprint_key) VALUES (?)",
        (fingerprint_key,),
    )
    conn.commit()


def _mark_unfavorited(conn: sqlite3.Connection, fingerprint_key: str) -> None:
    conn.execute("DELETE FROM favorites WHERE fingerprint_key=?", (fingerprint_key,))
    conn.commit()


def _forward_to_favorites(cq: dict[str, Any], fingerprint_key: str) -> str:
    """Copy the message that owns this callback to the favorites chat, restyled
    as a saved-favorite: a '⭐ Favorited · <date>' header above the original
    details, the portal-link button kept, and an Unfavorite button added.

    Returns the ack text for the Telegram toast. Soft-fails: if the destination
    isn't configured or the API call errors out, the listing is still persisted
    in `favorites` (the caller already did that).
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

    # Keyboard: portal-link button(s) + an Unfavorite button.
    keyboard = _url_buttons_only(msg.get("reply_markup")) or {"inline_keyboard": []}
    keyboard["inline_keyboard"].append(
        [{"text": "❌ Unfavorite", "callback_data": f"{UNFAV_PREFIX}{fingerprint_key}"}]
    )

    # Caption: prepend a ⭐ Favorited header. copyMessage's caption override is
    # plain-vs-HTML, and the original caption's text comes through cq verbatim
    # (links in it become plain text — the portal button stays clickable). Only
    # photo cards carry a caption; text-only cards copy without the header.
    caption = parse_mode = None
    original = msg.get("caption")
    if original is not None:
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        header = f"⭐ <b>Favorited</b> · {today}\n\n"
        body = html.escape(original)
        caption = (header + body)[:1024]
        parse_mode = "HTML"

    try:
        telegram.copy_message(
            from_chat_id=from_chat_id,
            message_id=int(message_id),
            to_chat_id=dest,
            message_thread_id=thread_id,
            reply_markup=keyboard,
            caption=caption,
            parse_mode=parse_mode,
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
