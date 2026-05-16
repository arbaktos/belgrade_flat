"""Thin client for FlareSolverr's /v1 endpoint.

FlareSolverr (https://github.com/FlareSolverr/FlareSolverr) is a sidecar that
solves Cloudflare anti-bot challenges by driving headless Chromium and returns
the rendered HTML + cookies. We use it for halooglasi.com, which is behind a
Cloudflare Turnstile challenge that no plain-httpx fetch can bypass.

Lifecycle:
    sess = create_session()
    html = get(url, session=sess)   # may include CF interstitial solve
    destroy_session(sess)

A session bundles a browser context, so cookies persist across calls. This
matters because solving the Turnstile gives us a `cf_clearance` cookie valid
for ~30 minutes — reusing it across a scrape avoids repeated solves.
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Any

import httpx

log = logging.getLogger(__name__)

DEFAULT_URL = "http://localhost:8191/v1"
DEFAULT_TIMEOUT_MS = 60_000


class FlareSolverrError(RuntimeError):
    pass


def base_url() -> str:
    return os.environ.get("FLARESOLVERR_URL", DEFAULT_URL)


def is_available() -> bool:
    """Quick check whether the FlareSolverr endpoint responds at all."""
    try:
        r = httpx.post(
            base_url(),
            json={"cmd": "sessions.list"},
            timeout=5.0,
        )
        return r.status_code == 200
    except httpx.HTTPError:
        return False


@dataclass
class Session:
    id: str


def create_session() -> Session:
    r = httpx.post(base_url(), json={"cmd": "sessions.create"}, timeout=60.0)
    r.raise_for_status()
    data = r.json()
    if data.get("status") != "ok":
        raise FlareSolverrError(f"create_session failed: {data}")
    return Session(id=data["session"])


def destroy_session(sess: Session) -> None:
    try:
        httpx.post(
            base_url(),
            json={"cmd": "sessions.destroy", "session": sess.id},
            timeout=10.0,
        )
    except httpx.HTTPError as e:
        log.warning("FlareSolverr destroy_session failed: %s", e)


def get(url: str, *, session: Session | None = None, max_timeout_ms: int = DEFAULT_TIMEOUT_MS) -> str:
    """Fetch a URL through FlareSolverr. Returns rendered HTML."""
    payload: dict[str, Any] = {
        "cmd": "request.get",
        "url": url,
        "maxTimeout": max_timeout_ms,
    }
    if session is not None:
        payload["session"] = session.id

    r = httpx.post(base_url(), json=payload, timeout=max_timeout_ms / 1000 + 10)
    r.raise_for_status()
    data = r.json()
    if data.get("status") != "ok":
        raise FlareSolverrError(f"request.get failed: {data.get('message')} ({data})")
    solution = data.get("solution") or {}
    status = solution.get("status")
    if status != 200:
        raise FlareSolverrError(f"upstream HTTP {status} for {url}")
    return solution.get("response", "")
