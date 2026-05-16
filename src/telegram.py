from __future__ import annotations

import logging
import os
import time

import httpx

log = logging.getLogger(__name__)

BASE_URL = "https://api.telegram.org"
DEFAULT_TIMEOUT_S = 20


def send_message(
    text: str,
    *,
    disable_notification: bool = False,
    parse_mode: str | None = None,
) -> None:
    _call("sendMessage", {
        "chat_id": _chat_id(),
        "text": text,
        "disable_notification": disable_notification,
        **({"parse_mode": parse_mode} if parse_mode else {}),
        "link_preview_options": {"is_disabled": True},
    })


def send_photo(
    photo_url: str,
    *,
    caption: str | None = None,
    disable_notification: bool = False,
    parse_mode: str | None = None,
) -> None:
    """Send a single photo with caption. Caption max 1024 chars."""
    if caption and len(caption) > 1024:
        caption = caption[:1020] + "…"
    _call("sendPhoto", {
        "chat_id": _chat_id(),
        "photo": photo_url,
        "caption": caption or "",
        "disable_notification": disable_notification,
        **({"parse_mode": parse_mode} if parse_mode else {}),
    })


def _call(method: str, payload: dict, *, max_retries: int = 3) -> dict:
    """POST to Telegram, respecting 429 retry-after."""
    token = os.environ["TELEGRAM_BOT_TOKEN"]
    url = f"{BASE_URL}/bot{token}/{method}"
    for attempt in range(max_retries):
        r = httpx.post(url, json=payload, timeout=DEFAULT_TIMEOUT_S)
        if r.status_code == 429:
            retry_after = int(r.json().get("parameters", {}).get("retry_after", 1))
            log.warning("Telegram %s 429 — sleeping %ds", method, retry_after)
            time.sleep(retry_after + 1)
            continue
        if r.status_code >= 400:
            # Surface Telegram's actual error reason ('description' field) so a
            # bad caption/parse_mode/photo URL doesn't fail silently.
            try:
                desc = r.json().get("description", "")[:300]
            except Exception:
                desc = r.text[:300]
            log.warning("Telegram %s HTTP %s: %s", method, r.status_code, desc)
            r.raise_for_status()
        return r.json()
    raise RuntimeError(f"Telegram {method} failed after {max_retries} retries")


def _chat_id() -> str:
    return os.environ["TELEGRAM_CHAT_ID"]
