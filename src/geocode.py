"""Geocode listing addresses via Nominatim (OSM).

Used when a listing has no lat/lng from the scraper. Results are cached in
SQLite keyed by a normalized address string so repeat runs don't hammer the
public Nominatim instance (max 1 req/s per usage policy).
"""
from __future__ import annotations

import hashlib
import logging
import sqlite3
import time
from datetime import datetime, timedelta
from typing import Any

import httpx

from src.models import Listing

log = logging.getLogger(__name__)

NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"
USER_AGENT = "belgrade-flat-notifier/1.0 (rental digest; github.com/arbaktos/belgrade_flat)"
CACHE_TTL_DAYS = 90
MIN_REQUEST_INTERVAL_S = 1.1

_last_request_at: float = 0.0


def query_string(listing: Listing) -> str:
    """Compact geocoder query: street chunk + neighborhood + Belgrade."""
    parts: list[str] = []
    if listing.address:
        parts.append(listing.address.split(",", 1)[0].strip())
    if listing.place_names:
        place = listing.place_names[-1].strip()
        if place and place not in parts:
            parts.append(place)
    parts.extend(["Belgrade", "Serbia"])
    return ", ".join(p for p in parts if p)


def cache_key(listing: Listing) -> str:
    if listing.lat is not None and listing.lng is not None:
        return f"coord:{round(listing.lat, 5)},{round(listing.lng, 5)}"
    q = query_string(listing).lower().strip()
    if not q:
        return f"id:{listing.fingerprint_key}"
    return "addr:" + hashlib.sha1(q.encode()).hexdigest()[:16]


def get_cached(conn: sqlite3.Connection, key: str) -> tuple[float, float] | None:
    cutoff = (datetime.utcnow() - timedelta(days=CACHE_TTL_DAYS)).isoformat(" ", "seconds")
    row = conn.execute(
        "SELECT lat, lng FROM geocode_cache WHERE cache_key=? AND fetched_at > ?",
        (key, cutoff),
    ).fetchone()
    if row is None:
        return None
    return float(row[0]), float(row[1])


def put_cached(conn: sqlite3.Connection, key: str, lat: float, lng: float) -> None:
    conn.execute(
        "INSERT INTO geocode_cache (cache_key, lat, lng, fetched_at) "
        "VALUES (?, ?, ?, datetime('now')) "
        "ON CONFLICT(cache_key) DO UPDATE SET lat=excluded.lat, lng=excluded.lng, "
        "fetched_at=excluded.fetched_at",
        (key, lat, lng),
    )
    conn.commit()


def _throttle() -> None:
    global _last_request_at
    elapsed = time.monotonic() - _last_request_at
    if elapsed < MIN_REQUEST_INTERVAL_S:
        time.sleep(MIN_REQUEST_INTERVAL_S - elapsed)
    _last_request_at = time.monotonic()


def _nominatim_search(query: str, *, client: httpx.Client) -> tuple[float, float] | None:
    _throttle()
    resp = client.get(
        NOMINATIM_URL,
        params={"q": query, "format": "json", "limit": 1, "countrycodes": "rs"},
        headers={"User-Agent": USER_AGENT},
        timeout=30.0,
    )
    resp.raise_for_status()
    data: list[dict[str, Any]] = resp.json()
    if not data:
        return None
    hit = data[0]
    return float(hit["lat"]), float(hit["lon"])


def resolve(
    listing: Listing,
    conn: sqlite3.Connection,
    *,
    client: httpx.Client | None = None,
) -> tuple[float, float] | None:
    """Return WGS84 coords for a listing, using cache + Nominatim when needed."""
    if listing.lat is not None and listing.lng is not None:
        return listing.lat, listing.lng

    key = cache_key(listing)
    cached = get_cached(conn, key)
    if cached is not None:
        return cached

    query = query_string(listing)
    if not query or query == "Belgrade, Serbia":
        return None

    own_client = client is None
    if own_client:
        client = httpx.Client()
    try:
        coords = _nominatim_search(query, client=client)  # type: ignore[arg-type]
    finally:
        if own_client:
            client.close()

    if coords is None:
        log.info("geocode: no result for %s (%s)", listing.fingerprint_key, query[:60])
        return None

    put_cached(conn, key, coords[0], coords[1])
    return coords


def resolve_many(
    listings: list[Listing],
    conn: sqlite3.Connection,
) -> dict[str, tuple[float, float]]:
    """Resolve coords for listings; returns fingerprint_key → (lat, lng)."""
    out: dict[str, tuple[float, float]] = {}
    with httpx.Client() as client:
        for listing in listings:
            coords = resolve(listing, conn, client=client)
            if coords is not None:
                out[listing.fingerprint_key] = coords
    return out
