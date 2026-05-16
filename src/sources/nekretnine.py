from __future__ import annotations

import logging
import re
import time
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx
from parsel import Selector

from src.models import Listing

log = logging.getLogger(__name__)

SOURCE_NAME = "nekretnine"
LIST_URL_TMPL = (
    "https://www.nekretnine.rs/stambeni-objekti/stanovi/izdavanje-prodaja/"
    "izdavanje/grad/beograd/lista/po-stranici/30/stranica/{page}/?order=4"
)
BASE_URL = "https://www.nekretnine.rs"
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"
)
MAX_PAGES_SAFETY = 15
DETAIL_FETCH_DELAY_S = 0.3   # polite pacing between detail requests

# Serbian room-count words → numeric value.
ROOM_WORDS: dict[str, float] = {
    "garsonjera": 0.5,
    "jednosoban": 1.0,
    "jednoiposoban": 1.5,
    "dvosoban": 2.0,
    "dvoiposoban": 2.5,
    "trosoban": 3.0,
    "troiposoban": 3.5,
    "četvorosoban": 4.0,
    "cetvorosoban": 4.0,
    "petosoban": 5.0,
}


def fetch(*, freshness_days: int = 7, client: httpx.Client | None = None) -> list[Listing]:
    cutoff = datetime.now(timezone.utc) - timedelta(days=freshness_days)
    own_client = client is None
    client = client or httpx.Client(
        headers={"User-Agent": USER_AGENT, "Accept": "text/html"},
        timeout=20.0,
        follow_redirects=True,
    )
    try:
        cards: list[dict[str, Any]] = []
        for page in range(1, MAX_PAGES_SAFETY + 1):
            r = client.get(LIST_URL_TMPL.format(page=page))
            r.raise_for_status()
            page_cards = _parse_cards(r.text)
            fresh = [c for c in page_cards if c["posted_at"] >= cutoff]
            cards.extend(fresh)
            if not fresh or not page_cards:
                break

        # Enrich each card with detail-page data, skipping on individual fetch errors.
        results: list[Listing] = []
        for card in cards:
            try:
                detail = _fetch_detail(client, card["url"])
            except (httpx.HTTPError, ValueError) as e:
                log.warning("nekretnine: detail fetch failed for %s: %s", card["url"], e)
                continue
            time.sleep(DETAIL_FETCH_DELAY_S)
            listing = _to_listing(card, detail)
            if listing is not None:
                results.append(listing)

        log.info("nekretnine: %d cards fresh, %d listings enriched", len(cards), len(results))
        return results
    finally:
        if own_client:
            client.close()


def _parse_cards(html: str) -> list[dict[str, Any]]:
    sel = Selector(text=html)
    cards: list[dict[str, Any]] = []
    for card in sel.css(".offer-body"):
        title_a = card.css("h2.offer-title a")
        href = title_a.attrib.get("href")
        if not href:
            continue
        listing_id = href.rstrip("/").split("/")[-1]
        title = " ".join(title_a.css("::text").getall()).strip()

        # The card has a GA dataLayer JSON with price/m²/category fields — read it.
        onclick = title_a.attrib.get("onclick", "")
        price = _extract_int(r'"price":"(\d+)"', onclick)
        currency = _extract_str(r'"currency":"([^"]+)"', onclick) or "EUR"
        m2 = _extract_float(r'"item_category4":"(\d+(?:[.,]\d+)?)"', onclick)
        item_category2 = _extract_str(r'"item_category2":"([^"]+)"', onclick) or ""

        # First word of item_category2 like "Stan u zgradi" is too generic; grab the
        # "<X> stan" descriptor that follows it.
        rooms = _rooms_from_category(item_category2 + " " + title)

        meta = card.css(".offer-meta-info::text").get("") or ""
        date_m = re.search(r"(\d{2})\.(\d{2})\.(\d{4})", meta)
        if not date_m:
            continue
        d, m, y = (int(x) for x in date_m.groups())
        posted_at = datetime(y, m, d, tzinfo=timezone.utc)

        location = (card.css(".offer-location::text").get() or "").strip()

        cards.append({
            "id": listing_id,
            "url": f"{BASE_URL}{href}",
            "title": title,
            "price_eur": float(price or 0) if currency == "EUR" else 0.0,
            "m2": m2 or 0.0,
            "rooms": rooms or 0.0,
            "location": location,
            "posted_at": posted_at,
        })
    return cards


def _fetch_detail(client: httpx.Client, url: str) -> dict[str, Any]:
    r = client.get(url)
    r.raise_for_status()
    return _parse_detail(r.text)


def _parse_detail(html: str) -> dict[str, Any]:
    # The detail page packs spec rows as `<span>Label:</span><br/>Value</span>`.
    pairs = dict(re.findall(r'<span>([^<]+):</span><br\s*/?>(.*?)</span>', html, re.DOTALL))
    floor_raw = _clean_text(pairs.get("Sprat", ""))
    floor, total_floors = _parse_floor(floor_raw)
    heating = _clean_text(pairs.get("Grejanje", "")) or None
    furnishing = _clean_text(pairs.get("Opremljenost", "")) or None

    # Elevator is in the "Dodatna opremljenost" section (item list) rather than the spec rows.
    elevator = bool(re.search(r"Dodatna opremljenost.{0,5000}>\s*Lift\s*<", html, re.DOTALL))

    sel = Selector(text=html)
    description = " ".join(sel.css(".description ::text").getall()).strip()
    image_url = sel.css('meta[property="og:image"]::attr(content)').get()
    address = _clean_text(pairs.get("Adresa", ""))

    return {
        "floor": floor,
        "total_floors": total_floors,
        "elevator": elevator,
        "heating": heating,
        "furnishing": furnishing,
        "description": description[:400],
        "image_url": image_url,
        "address": address or None,
    }


def _to_listing(card: dict[str, Any], detail: dict[str, Any]) -> Listing | None:
    try:
        return Listing(
            id=card["id"],
            source=SOURCE_NAME,
            url=card["url"],
            price_eur=card["price_eur"],
            m2=card["m2"],
            rooms=card["rooms"],
            floor=detail["floor"],
            total_floors=detail["total_floors"],
            last_floor=bool(detail["floor"] is not None
                            and detail["total_floors"] is not None
                            and detail["floor"] == detail["total_floors"]),
            elevator=detail["elevator"],
            furnished=detail["furnishing"],
            heating_type=detail["heating"],
            pets_allowed=None,            # not exposed in nekretnine card or detail spec rows
            title=card["title"],
            description=detail["description"],
            address=detail["address"],
            place_names=[p.strip() for p in card["location"].split(",") if p.strip()][:3],
            image_url=detail["image_url"],
            is_agency=False,              # not reliably distinguished here; refine later
            created_at=card["posted_at"],
        )
    except (KeyError, TypeError) as e:
        log.warning("nekretnine: skipping %s: %s", card.get("id"), e)
        return None


def _parse_floor(raw: str) -> tuple[int | None, int | None]:
    if not raw:
        return None, None
    raw = raw.strip()
    # Common forms: "10 / -", "10/12", "PR / 4", "SU / 4"
    parts = [p.strip() for p in re.split(r"[/\s]+", raw) if p.strip()]
    floor = _floor_token_to_int(parts[0]) if parts else None
    total = _floor_token_to_int(parts[1]) if len(parts) > 1 else None
    return floor, total


def _floor_token_to_int(tok: str) -> int | None:
    tok_l = tok.upper()
    if tok_l in {"SU", "POL"}:
        return -1
    if tok_l in {"PR", "VPR", "PRIZEMLJE"}:
        return 0
    if tok_l in {"-", ""}:
        return None
    try:
        return int(tok)
    except ValueError:
        return None


def _rooms_from_category(s: str) -> float | None:
    s_lower = s.lower()
    for word, val in ROOM_WORDS.items():
        if word in s_lower:
            return val
    return None


def _extract_str(pattern: str, text: str) -> str | None:
    m = re.search(pattern, text)
    return m.group(1) if m else None


def _extract_int(pattern: str, text: str) -> int | None:
    m = re.search(pattern, text)
    return int(m.group(1)) if m else None


def _extract_float(pattern: str, text: str) -> float | None:
    m = re.search(pattern, text)
    if not m:
        return None
    return float(m.group(1).replace(",", "."))


def _clean_text(s: str) -> str:
    s = re.sub(r"<[^>]+>", " ", s)
    return re.sub(r"\s+", " ", s).strip()
