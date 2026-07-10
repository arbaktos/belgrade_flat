from __future__ import annotations

import json
import logging
import urllib.parse
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx

from src.models import Listing

log = logging.getLogger(__name__)

SOURCE_NAME = "cityexpert"
API_URL = "https://cityexpert.rs/api/Search"
# Canonical paths now require /{propId}/{slug}; the server validates propId and
# tolerates any non-empty slug, so we use a fixed "stan" placeholder.
LISTING_URL_RENT = "https://cityexpert.rs/izdavanje-nekretnina/beograd"
LISTING_URL_SALE = "https://cityexpert.rs/prodaja-nekretnina/beograd"
IMAGE_CDN = "https://img.cityexpert.rs/properties"
IMAGE_WIDTH = "720x"   # Telegram-friendly; CDN serves JPEG even when filename ends in .avif
BELGRADE_CITY_ID = 1
APARTMENT_PT_ID = 1
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"
)
PAGE_SIZE = 50
MAX_PAGES_SAFETY = 20

# These default fields are required by the API — empty arrays/null work.
_BASE_REQUEST = {
    "serviceType": "p",
    "ptId": [APARTMENT_PT_ID],
    "cityId": BELGRADE_CITY_ID,
    "rentOrSale": "r",
    "resultsPerPage": PAGE_SIZE,
    "avFrom": False,
    "underConstruction": False,
    "minPrice": None, "maxPrice": None,
    "minPricePerM": None, "maxPricePerM": None,
    "minInstallment": None, "maxInstallment": None,
    "minSize": None, "maxSize": None,
    "searchSource": "regular",
    "sort": "datedsc",
    "floor": [], "furnished": [], "furnishingArray": [], "heatingArray": [],
    "parkingArray": [], "petsArray": [], "polygonsArray": [], "propIds": [],
    "structure": [], "ceiling": [], "bldgOptsArray": [], "yearOfConstruction": [],
    "joineryArray": [], "otherArray": [], "bedroomsArray": [], "bathroomArray": [],
    "renovationArray": [], "minLeaseArray": [], "distanceCenterArray": [],
    "isSalonac": False, "isNotLastFloor": False, "isNoElevatorButLow": False,
    "newDevelopment": False, "isFeatured": False, "isLux": False, "isRecommended": False,
}


def fetch(*, freshness_days: int = 7, client: httpx.Client | None = None) -> list[Listing]:
    cutoff = datetime.now(timezone.utc) - timedelta(days=freshness_days)
    own_client = client is None
    client = client or httpx.Client(
        headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
        timeout=20.0,
    )
    try:
        results: list[Listing] = []
        for page in range(1, MAX_PAGES_SAFETY + 1):
            req = {**_BASE_REQUEST, "currentPage": page}
            r = client.get(
                f"{API_URL}?req={urllib.parse.quote(json.dumps(req))}"
            )
            r.raise_for_status()
            data = r.json()
            props = data.get("result", [])
            if not props:
                break

            page_listings = [_parse(p) for p in props]
            page_listings = [l for l in page_listings if l is not None]
            fresh = [l for l in page_listings if l.created_at >= cutoff]
            results.extend(fresh)

            if not fresh:
                break

            info = data.get("info") or {}
            if info.get("isLastPage"):
                break
        log.info("cityexpert: collected %d listings within %dd window", len(results), freshness_days)
        return results
    finally:
        if own_client:
            client.close()


def _parse(prop: dict[str, Any]) -> Listing | None:
    try:
        unique_id = prop["uniqueID"]
        prop_id = prop.get("propId")
        if prop_id is None:
            return None
        first_pub = prop.get("firstPublished") or prop.get("availableFrom")
        if not first_pub:
            return None
        created_at = datetime.fromisoformat(first_pub.replace("Z", "+00:00"))
        base = LISTING_URL_SALE if prop.get("rentOrSale") == "s" else LISTING_URL_RENT

        lat, lng = _parse_location(prop.get("location"))
        # "floor" = "2_4" → floor 2, total 4. "PR"/"VPR"/"SU" = ground/high-ground/basement.
        floor, total = _parse_floor(prop.get("floor"))
        size = float(prop.get("size") or 0)
        price = float(prop.get("price") or 0)
        rooms = _to_float(prop.get("structure"))
        furnishing = prop.get("furnishingArray") or []
        bldg_opts = prop.get("bldgOptsArray") or []
        # Elevator: not explicitly exposed as a structured feature in this API,
        # but `isNoElevatorButLow` set to True implies there is no elevator.
        # Otherwise we assume one is present (best-effort; refine later).
        no_elevator = bool(prop.get("isNoElevatorButLow"))
        elevator = not no_elevator

        return Listing(
            id=unique_id,
            source=SOURCE_NAME,
            url=f"{base}/{int(prop_id)}/stan",
            price_eur=price,
            m2=size,
            rooms=rooms,
            floor=floor,
            total_floors=total,
            last_floor=bool(prop.get("isNotLastFloor") is False and floor and total and floor == total),
            elevator=elevator,
            furnished=_furnished_label(prop.get("furnished")),
            heating_type=_heating_label(prop.get("heatingArray")),
            pets_allowed=_pets_allowed(prop.get("petsArray")),
            title=f"{prop.get('structure', '')} apartment, {prop.get('street', '')}".strip(", "),
            description=f"{prop.get('municipality', '')} · {', '.join(prop.get('polygons') or [])}",
            address=prop.get("street"),
            place_names=list(prop.get("polygons") or []),
            image_url=_image_url(prop),
            is_agency=True,  # cityexpert is itself an agency
            created_at=created_at,
            dishwasher=("furDishWasher" in furnishing),
            lat=lat,
            lng=lng,
        )
    except (KeyError, ValueError, TypeError) as e:
        log.warning("cityexpert: skipping malformed prop %s: %s", prop.get("uniqueID", "?"), e)
        return None


def _parse_location(raw: Any) -> tuple[float | None, float | None]:
    """cityexpert's location field is 'lat, lng' as a single string."""
    if not raw or not isinstance(raw, str):
        return None, None
    try:
        a, b = raw.split(",", 1)
        return float(a.strip()), float(b.strip())
    except (ValueError, TypeError):
        return None, None


def _parse_floor(raw: Any) -> tuple[int | None, int | None]:
    if raw is None:
        return None, None
    if isinstance(raw, (int, float)):
        return int(raw), None
    s = str(raw).upper()
    # Special codes
    if s in {"SU", "POL"}:    # suteren/poluukopan = basement
        return -1, None
    if s in {"PR", "VPR"}:    # prizemlje / visoko prizemlje = ground
        return 0, None
    if "_" in s:
        a, b = s.split("_", 1)
        try:
            return int(a), int(b)
        except ValueError:
            return None, None
    try:
        return int(s), None
    except ValueError:
        return None, None


def _to_float(v: Any) -> float:
    if v is None:
        return 0.0
    try:
        return float(str(v).replace(",", "."))
    except (ValueError, TypeError):
        return 0.0


def _furnished_label(code: Any) -> str | None:
    """cityexpert furnished: 1 fully, 2 semi, 3 empty (best guess)."""
    mapping = {1: "yes", 2: "semi", 3: "no", 0: None}
    if isinstance(code, int):
        return mapping.get(code)
    return None


# CityExpert heating codes mapped to the canonical vocabulary used by
# filter.canonicalize_heating. Confirmed against the cityexpert.rs filter
# sidebar in 2026-05. Code 26 (heat pump) has no exact canonical match —
# we surface it as "klima" since both are refrigerant-cycle systems; if a
# dedicated "toplotna_pumpa" category is ever added, update this and
# _HEATING_MAP in filter.py together.
_HEATING_CODES: dict[int, str] = {
    1: "centralno",   # district / city central
    4: "elektricni",  # electric panels
    10: "TA",         # electric storage heater (TA peć)
    21: "podno",      # underfloor
    26: "klima",      # heat pump
    99: "etazno",     # own boiler (per unit / storey)
}
# When a listing reports several systems, surface the most informative one
# downstream consumers (filter + telegram render) only see a single value.
_HEATING_PRIORITY = ("centralno", "etazno", "podno", "TA", "klima", "elektricni")


def _heating_label(heating_array: Any) -> str | None:
    if not heating_array:
        return None
    mapped: set[str] = set()
    for code in heating_array:
        try:
            key = _HEATING_CODES.get(int(code))
        except (TypeError, ValueError):
            continue
        if key:
            mapped.add(key)
    if not mapped:
        return None
    for k in _HEATING_PRIORITY:
        if k in mapped:
            return k
    return next(iter(mapped))


# petsArray codes, verified against the PropertyView API and the rendered
# "Allowed pets" tags: 1 = Dog, 2 = Cat, 3 = Aquarium pets, 4 = Small cage
# pets, 5 = Terrarium pets.
_PET_CODE_CAT = 2


def _pets_allowed(pets_array: Any) -> bool | None:
    """The user's pet is a cat, so only code 2 counts as "pets OK".

    Unlike the portals, cityexpert inventories every property itself, so the
    pets field is always answered: an EMPTY array renders as "Allowed pets:
    No" on the listing page (verified on 42262/33078/61054) — a definitive
    refusal, not "unspecified". There is no separate code for "Ne". A
    non-empty array without the cat code (e.g. [3, 4, 5] = aquarium/cage/
    terrarium only) is likewise a refusal for a cat.
    """
    if pets_array is None:
        return None
    try:
        codes = {int(c) for c in pets_array}
    except (TypeError, ValueError):
        return None
    return _PET_CODE_CAT in codes


def _image_url(prop: dict[str, Any]) -> str | None:
    """Build CDN URL for coverPhoto.

    CityExpert's image pipeline (2024+) uses
    /properties/{width}x/{bucket}/{propId}/slike/{filename} where bucket is
    propId rounded down to thousands. The legacy /properties/620/{filename}
    path now 404s — which made Telegram fall back to text-only messages.
    """
    photo = prop.get("coverPhoto")
    prop_id = prop.get("propId")
    if not photo or prop_id is None:
        return None
    try:
        pid = int(prop_id)
    except (TypeError, ValueError):
        return None
    bucket = (pid // 1000) * 1000
    return f"{IMAGE_CDN}/{IMAGE_WIDTH}/{bucket}/{pid}/slike/{photo}"
