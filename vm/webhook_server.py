"""Real-time Telegram callback webhook (runs on the Hetzner VM).

Telegram delivers each 🙈 Hide / ⭐ Favorite click here as an HTTP POST, instead
of the CI run draining them every 2 h via getUpdates. The click takes effect
in ~1 s: we pull db.sqlite from R2, apply it with the SAME dispatch the CI
drain uses (src.telegram_callbacks.handle_callback_query), and push back.

Security: two shared secrets must both match —
  * the URL path segment   (WEBHOOK_PATH_SECRET)   — keeps the URL unguessable
  * the X-Telegram-Bot-Api-Secret-Token header (WEBHOOK_SECRET_TOKEN) — set on
    the bot via setWebhook so only Telegram can post here.

Concurrency: a process-wide lock serialises pull→apply→push so two clicks
can't interleave. uvicorn runs a single worker, so this is the only writer in
this process. The remaining race — a CI run pushing state in the same ~2 s
window — is rare (CI runs ~2 min every 2 h) and benign: skips/favourites are
idempotent INSERT-OR-IGNORE, so the worst case is one lost click that the user
re-taps. (A stronger etag guard is noted in docs/plans/vm-webhook.md.)
"""
from __future__ import annotations

import logging
import os
import threading
from typing import Any, Optional

from fastapi import FastAPI, Header, HTTPException, Request

from src import state, telegram_callbacks

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("vm.webhook")

app = FastAPI()
_state_lock = threading.Lock()


def _path_secret() -> str:
    secret = os.environ.get("WEBHOOK_PATH_SECRET")
    if not secret:
        raise RuntimeError("WEBHOOK_PATH_SECRET is not set")
    return secret


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/tg/{secret}")
async def telegram_webhook(
    secret: str,
    request: Request,
    x_telegram_bot_api_secret_token: Optional[str] = Header(default=None),
) -> dict:
    # Two-factor: unguessable path + Telegram's secret-token header.
    if secret != _path_secret():
        raise HTTPException(status_code=404, detail="not found")
    expected_token = os.environ.get("WEBHOOK_SECRET_TOKEN")
    if expected_token and x_telegram_bot_api_secret_token != expected_token:
        raise HTTPException(status_code=403, detail="bad secret token")

    update = await request.json()
    cq = update.get("callback_query")
    if not cq:
        # Non-callback updates (Telegram may send others) — accept and ignore.
        return {"ok": True}

    try:
        _apply_callback(cq)
    except Exception as e:  # noqa: BLE001 — never make Telegram retry-storm us
        log.error("callback apply failed: %s", e, exc_info=True)
    # Always 200: the user already saw the toast (or we logged the failure);
    # a non-200 would make Telegram redeliver the same update repeatedly.
    return {"ok": True}


def _apply_callback(cq: dict[str, Any]) -> dict[str, int]:
    """Pull state, apply one callback, push back — serialised across requests."""
    counts = {"skipped": 0, "favorited": 0, "unknown": 0}
    with _state_lock:
        state.pull()
        conn = state.ensure_schema()
        try:
            telegram_callbacks.handle_callback_query(conn, cq, counts)
        finally:
            conn.close()
        state.push()
    log.info("applied callback: %s", counts)
    return counts
