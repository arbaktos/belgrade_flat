"""halooglasi.com — Cloudflare-protected; fetched via FlareSolverr sidecar.

Phase 1 (this commit): wire up the bypass and dump the rendered list-page HTML
to debug/halooglasi-latest.html so we can inspect the real structure once and
write a proper parser for phase 2.

If FlareSolverr is unreachable (e.g. running locally without Docker), we raise
SourceBlockedError so the run marks halooglasi ⚠️ on the health line instead
of crashing the whole pipeline.
"""
from __future__ import annotations

import logging
from pathlib import Path

from src.models import Listing
from src.sources import _flaresolverr

log = logging.getLogger(__name__)

SOURCE_NAME = "halooglasi"
LIST_URL = "https://www.halooglasi.com/nekretnine/izdavanje-stanova/beograd"
DEBUG_DIR = Path("debug")
DEBUG_PATH = DEBUG_DIR / "halooglasi-latest.html"


class SourceBlockedError(Exception):
    """Surface to caller so health line shows ⚠️ for halooglasi."""


def fetch(*, freshness_days: int = 7) -> list[Listing]:
    if not _flaresolverr.is_available():
        raise SourceBlockedError(
            f"FlareSolverr not reachable at {_flaresolverr.base_url()}"
        )

    log.info("halooglasi: fetching list page via FlareSolverr (Phase 1 capture)")
    sess = _flaresolverr.create_session()
    try:
        html = _flaresolverr.get(LIST_URL, session=sess)
    except _flaresolverr.FlareSolverrError as e:
        raise SourceBlockedError(str(e)) from e
    finally:
        _flaresolverr.destroy_session(sess)

    DEBUG_DIR.mkdir(exist_ok=True)
    DEBUG_PATH.write_text(html, encoding="utf-8")
    log.info("halooglasi: captured %d bytes → %s (parser TBD)", len(html), DEBUG_PATH)

    # Parser lands in phase 2 once we have a real HTML sample committed.
    return []
