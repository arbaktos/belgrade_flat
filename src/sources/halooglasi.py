"""halooglasi.com — currently blocked by Cloudflare Turnstile challenge.

A direct HTTP fetch returns the Cloudflare "Just a moment…" interstitial (HTTP 403)
on every path we've tried, including `/api/*`, `/realestate`, and the search list.

The spec (§2) keeps Playwright headless Chromium as the fallback for sources that
begin 403'ing. Adding Playwright to the GH Actions workflow is the next step if
we decide we need this fourth source. Until then this module returns an empty
list and the health line surfaces `halooglasi 0 ⚠️` per spec §9's example.
"""
from __future__ import annotations

import logging

from src.models import Listing

log = logging.getLogger(__name__)

SOURCE_NAME = "halooglasi"


class SourceBlockedError(Exception):
    """Raised so callers can mark the source as ⚠️ in the health line."""


def fetch(*, freshness_days: int = 7) -> list[Listing]:
    log.warning("halooglasi: blocked by Cloudflare challenge; needs Playwright fallback")
    raise SourceBlockedError("Cloudflare challenge — Playwright not yet wired up")
