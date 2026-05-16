"""halooglasi.com — Cloudflare-protected; fetched via FlareSolverr sidecar.

Phase 2: card-level parsing for Belgrade rentals. Each `.product-item` on the
list page carries price, m², rooms, floor (Roman numerals!), publish date,
location, and agency/owner flag — enough for five of the six hard filters.
Elevator and heating type live on the per-listing detail page, which we
defer to a follow-up (still need to capture a real detail-page HTML sample
through the bypass before writing that parser).

If FlareSolverr is unreachable (e.g. local dev without Docker), the run marks
halooglasi ⚠️ on the health line and continues with the other sources.
"""
from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from parsel import Selector

from src.models import Listing
from src.sources import _flaresolverr

log = logging.getLogger(__name__)

SOURCE_NAME = "halooglasi"
LIST_URL_TMPL = "https://www.halooglasi.com/nekretnine/izdavanje-stanova/beograd?page={page}"
BASE_URL = "https://www.halooglasi.com"
DEBUG_DIR = Path("debug")
DEBUG_PATH = DEBUG_DIR / "halooglasi-latest.html"
MAX_PAGES_SAFETY = 8


class SourceBlockedError(Exception):
    """Surface to caller so health line shows ⚠️ for halooglasi."""


def fetch(*, freshness_days: int = 7) -> list[Listing]:
    if not _flaresolverr.is_available():
        raise SourceBlockedError(
            f"FlareSolverr not reachable at {_flaresolverr.base_url()}"
        )

    cutoff = datetime.now(timezone.utc) - timedelta(days=freshness_days)
    sess = _flaresolverr.create_session()
    try:
        all_listings: list[Listing] = []
        for page in range(1, MAX_PAGES_SAFETY + 1):
            try:
                html = _flaresolverr.get(LIST_URL_TMPL.format(page=page), session=sess)
            except _flaresolverr.FlareSolverrError as e:
                if page == 1:
                    raise SourceBlockedError(str(e)) from e
                log.warning("halooglasi: page %d fetch failed, stopping: %s", page, e)
                break

            if page == 1:
                DEBUG_DIR.mkdir(exist_ok=True)
                DEBUG_PATH.write_text(html, encoding="utf-8")

            page_listings = _parse_list(html)
            fresh = [l for l in page_listings if l.created_at >= cutoff]
            all_listings.extend(fresh)

            if not fresh or not page_listings:
                break

        log.info("halooglasi: collected %d listings within %dd window", len(all_listings), freshness_days)
        return all_listings
    finally:
        _flaresolverr.destroy_session(sess)


def _parse_list(html: str) -> list[Listing]:
    sel = Selector(text=html)
    listings: list[Listing] = []
    for card in sel.css(".product-item"):
        listing = _parse_card(card)
        if listing is not None:
            listings.append(listing)
    return listings


def _parse_card(card: Selector) -> Listing | None:
    """Extract a Listing from a single .product-item element. Returns None for
    promotional inserts that lack data-id."""
    listing_id = card.attrib.get("data-id")
    href = card.css("a.a-images::attr(href)").get()
    if not listing_id or not href:
        return None

    try:
        price = _parse_price(card.css('.central-feature span::attr(data-value)').get())
        title = (card.css("h3.product-title a::text").get() or "").strip()
        places = [t.strip() for t in card.css("ul.subtitle-places li::text").getall() if t.strip()]

        feats: dict[str, str] = {}
        for li in card.css("ul.product-features li"):
            legend = (li.css(".legend::text").get() or "").strip()
            val = (li.css(".value-wrapper::text").get() or "").strip()
            feats[legend] = val

        m2 = _parse_int(feats.get("Kvadratura", ""))
        rooms = _parse_float(feats.get("Broj soba", ""))
        floor, total_floors = _parse_floor(feats.get("Spratnost", ""))

        date_str = card.css("span.publish-date::text").get() or ""
        posted_at = _parse_date(date_str)
        if posted_at is None:
            return None

        owner = card.css('span[data-field-name="oglasivac_nekretnine_s"]::attr(data-field-value)').get()
        image_url = card.css("a.a-images img::attr(src)").get()
        description = (card.css(".text-description-list::text").get() or "").strip()

        # Strip query params from the listing URL — they're tracking ids and
        # change on every render.
        clean_href = href.split("?", 1)[0]

        return Listing(
            id=listing_id,
            source=SOURCE_NAME,
            url=f"{BASE_URL}{clean_href}",
            price_eur=price,
            m2=float(m2) if m2 else 0.0,
            rooms=rooms or 0.0,
            floor=floor,
            total_floors=total_floors,
            last_floor=bool(floor is not None and total_floors is not None and floor == total_floors),
            elevator=None,                 # detail page only — pending phase 3
            furnished=None,                # detail page only
            heating_type=None,             # detail page only
            pets_allowed=None,
            title=title,
            description=description,
            address=places[-1] if places else None,
            place_names=places,
            image_url=image_url,
            is_agency=(owner == "agencija"),
            created_at=posted_at,
        )
    except (ValueError, KeyError, TypeError) as e:
        log.warning("halooglasi: skipping card %s: %s", listing_id, e)
        return None


# ---- parsers ----------------------------------------------------------------

# Serbian price format uses '.' as thousands separator ("1.000" = 1000).
_THOUSANDS_RE = re.compile(r"[ \s]+")


def _parse_price(raw: Any) -> float:
    if not raw:
        return 0.0
    s = _THOUSANDS_RE.sub("", str(raw)).replace(".", "")
    try:
        return float(s)
    except ValueError:
        return 0.0


def _parse_int(raw: str) -> int | None:
    if not raw:
        return None
    m = re.search(r"\d+", raw.replace(" ", " "))
    return int(m.group(0)) if m else None


def _parse_float(raw: str) -> float | None:
    if not raw:
        return None
    s = raw.replace(" ", " ").replace(",", ".").strip()
    m = re.search(r"\d+(?:\.\d+)?", s)
    return float(m.group(0)) if m else None


def _parse_date(raw: str) -> datetime | None:
    raw = raw.strip().rstrip(".")
    m = re.match(r"(\d{1,2})\.(\d{1,2})\.(\d{4})", raw)
    if not m:
        return None
    d, mo, y = (int(x) for x in m.groups())
    return datetime(y, mo, d, tzinfo=timezone.utc)


_ROMAN_MAP = [
    ("XX", 20), ("XIX", 19), ("XVIII", 18), ("XVII", 17), ("XVI", 16), ("XV", 15),
    ("XIV", 14), ("XIII", 13), ("XII", 12), ("XI", 11), ("X", 10),
    ("IX", 9), ("VIII", 8), ("VII", 7), ("VI", 6), ("V", 5),
    ("IV", 4), ("III", 3), ("II", 2), ("I", 1),
]


def _roman_to_int(token: str) -> int | None:
    token = token.upper().strip()
    for sym, val in _ROMAN_MAP:
        if token == sym:
            return val
    return None


def _floor_token_to_int(tok: str) -> int | None:
    if not tok:
        return None
    t = tok.upper().strip()
    if t in {"SUT", "SU"}:
        return -1
    if t in {"PR", "PRZ", "VPR", "PRIZEMLJE"}:
        return 0
    roman = _roman_to_int(t)
    if roman is not None:
        return roman
    try:
        return int(t)
    except ValueError:
        return None


def _parse_floor(raw: str) -> tuple[int | None, int | None]:
    if not raw:
        return None, None
    parts = [p for p in re.split(r"[/\s]+", raw.strip()) if p]
    floor = _floor_token_to_int(parts[0]) if parts else None
    total = _floor_token_to_int(parts[1]) if len(parts) > 1 else None
    return floor, total
