from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable

import httpx

from src.models import Listing

log = logging.getLogger(__name__)

SOURCE_NAME = "4zida"
API_URL = "https://api.4zida.rs/v6/search/apartments"
DETAIL_URL_TMPL = "https://api.4zida.rs/v6/eds/{id}"
LISTING_URL_BASE = "https://www.4zida.rs"
BELGRADE_PLACE_ID = 2
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"
)
MAX_PAGES_SAFETY = 50


def fetch(*, freshness_days: int = 7, client: httpx.Client | None = None) -> list[Listing]:
    """Fetch Belgrade rentals from 4zida newer than freshness_days.

    Default ordering on the API is newest first; we paginate until a whole page
    is older than the cutoff, then stop.
    """
    cutoff = datetime.now(timezone.utc) - timedelta(days=freshness_days)
    own_client = client is None
    client = client or httpx.Client(
        headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
        timeout=20.0,
    )
    try:
        results: list[Listing] = []
        for page in range(1, MAX_PAGES_SAFETY + 1):
            params = {
                "for": "rent",
                "placeIds[]": BELGRADE_PLACE_ID,
                "page": page,
            }
            r = client.get(API_URL, params=params)
            r.raise_for_status()
            data = r.json()
            ads = data.get("ads", [])
            if not ads:
                break

            page_listings = [_parse_ad(ad) for ad in ads]
            page_listings = [l for l in page_listings if l is not None]
            fresh = [l for l in page_listings if l.created_at >= cutoff]
            results.extend(fresh)

            # Stop if no listing on this page is within the freshness window.
            if not fresh:
                log.info("4zida: page %d had 0 fresh listings; stopping", page)
                break
        log.info("4zida: collected %d listings within %dd window", len(results), freshness_days)
        return results
    finally:
        if own_client:
            client.close()


def fetch_full_descriptions(
    listings: Iterable[Listing], *, client: httpx.Client | None = None
) -> int:
    """Upgrade the search API's 100-char `description100` preview to the detail
    endpoint's full `desc` text.

    The preview routinely hides a "bez ljubimaca" refusal buried later in the
    description, so the LLM extraction saw nothing and returned pets "unknown"
    — which passes the filter. Call this on structural survivors that lack
    structured pets data so the LLM checks the complete text. Failures are
    per-listing: a delisted ad (404) just keeps its preview. Returns how many
    descriptions were upgraded.
    """
    listings = list(listings)
    own_client = client is None
    client = client or httpx.Client(
        headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
        timeout=20.0,
    )
    try:
        upgraded = 0
        for l in listings:
            try:
                r = client.get(DETAIL_URL_TMPL.format(id=l.id))
                r.raise_for_status()
                desc = str(r.json().get("desc") or "").strip()
            except Exception as e:  # noqa: BLE001 — one dead ad must not kill the run
                log.warning("4zida: detail fetch failed for %s: %s", l.id, e)
                continue
            if len(desc) > len(l.description or ""):
                l.description = desc
                upgraded += 1
        log.info("4zida: upgraded %d/%d descriptions via detail API", upgraded, len(listings))
        return upgraded
    finally:
        if own_client:
            client.close()


def _parse_ad(ad: dict[str, Any]) -> Listing | None:
    """Map a raw 4zida ad dict to our Listing model. Returns None on bad shape."""
    try:
        ad_id = ad["id"]
        created_at = datetime.fromisoformat(ad["createdAt"])
        url_path = ad.get("urlPath") or ""
        return Listing(
            id=ad_id,
            source=SOURCE_NAME,
            url=f"{LISTING_URL_BASE}{url_path}",
            price_eur=float(ad.get("price") or 0),
            m2=float(ad.get("m2") or 0),
            rooms=float(ad.get("roomCount") or 0),
            floor=_to_int(ad.get("redactedFloor")),
            total_floors=_to_int(ad.get("redactedTotalFloors")),
            last_floor=bool(ad.get("lastFloor", False)),
            elevator=bool((ad.get("elevator") or 0) > 0),
            furnished=ad.get("furnished"),
            heating_type=ad.get("heatingType"),
            pets_allowed=ad.get("petsAllowed"),
            title=ad.get("detailedTitle") or ad.get("title") or "",
            description=ad.get("description100") or "",
            address=ad.get("safeAddress") or ad.get("address"),
            place_names=list(ad.get("placeNames") or []),
            image_url=_pick_image_url(ad),
            is_agency=bool(ad.get("agencyUrlPath")),
            created_at=created_at,
        )
    except (KeyError, ValueError, TypeError) as e:
        log.warning("4zida: skipping malformed ad %s: %s", ad.get("id", "?"), e)
        return None


def _to_int(v: Any) -> int | None:
    if v is None:
        return None
    try:
        return int(v)
    except (ValueError, TypeError):
        return None


def _pick_image_url(ad: dict[str, Any]) -> str | None:
    img = ad.get("image") or {}
    search = img.get("search") or {}
    return search.get("380x0_fill_0_jpeg") or search.get("380x0_fill_0_webp")
