"""Commute filter (spec §4) — walking-only, multi-destination.

Given a listing and a named destination (see src/destinations.py), compute the
walking time from the listing to that destination via the Google Routes API.
Returns the minutes, or None if Google couldn't find a walking route. Transit
was removed 2026-05-31; the model is now "minutes on foot to each destination".

Cost control:
- A 10 km Haversine pre-filter discards listings clearly too far for a walk
  before hitting the API.
- Results are cached in SQLite for 90 days, keyed by a 3-decimal lat/lng
  bucket (~110 m grid) PLUS the destination name, so each (building, dest)
  pair caches once and listings in the same building share it.
- Listings without coordinates fall back to address-string lookup (Google
  geocodes internally); cache key is then "addr:<sha1-of-address>@<dest>".
- Caller is responsible for choosing when to call this (after LLM filters
  pass) so we never spend an API call on a listing already rejected.
"""
from __future__ import annotations

import hashlib
import logging
import math
import os
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

import httpx

from src.destinations import Destination
from src.models import Listing

log = logging.getLogger(__name__)

ROUTES_URL = "https://routes.googleapis.com/directions/v2:computeRoutes"
CACHE_TTL_DAYS = 90
HAVERSINE_KM_MAX = 10
BUCKET_DECIMALS = 3


@dataclass
class CommuteResult:
    walk_min: int | None
    source: str   # "cache", "api", "haversine_skipped"


def haversine_km(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    """Great-circle distance in km."""
    r = 6371.0
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lng2 - lng1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2
    return r * 2 * math.asin(math.sqrt(a))


def bucket_key(listing: Listing, destination: str) -> str:
    """Stable per-(location, destination) cache key.

    Building-grid bucket for coords, hashed address otherwise, suffixed with
    the destination name so walking times to different destinations don't
    collide in the cache.
    """
    if listing.lat is not None and listing.lng is not None:
        geo = f"{round(listing.lat, BUCKET_DECIMALS)},{round(listing.lng, BUCKET_DECIMALS)}"
    else:
        parts = [listing.address or "", *(listing.place_names or [])]
        addr = " ".join(p for p in parts if p).lower().strip()
        geo = ("addr:" + hashlib.sha1(addr.encode()).hexdigest()[:16]) if addr else f"id:{listing.fingerprint_key}"
    return f"{geo}@{destination}"


def get_cached(conn: sqlite3.Connection, key: str) -> CommuteResult | None:
    cutoff = (datetime.utcnow() - timedelta(days=CACHE_TTL_DAYS)).isoformat(" ", "seconds")
    row = conn.execute(
        "SELECT walk_min FROM commute_cache WHERE bucket_key=? AND fetched_at > ?",
        (key, cutoff),
    ).fetchone()
    if row is None:
        return None
    return CommuteResult(walk_min=row[0], source="cache")


def put_cached(conn: sqlite3.Connection, key: str, walk_min: int | None) -> None:
    conn.execute(
        "INSERT INTO commute_cache (bucket_key, walk_min, fetched_at) "
        "VALUES (?, ?, datetime('now')) "
        "ON CONFLICT(bucket_key) DO UPDATE SET "
        "walk_min=excluded.walk_min, fetched_at=excluded.fetched_at",
        (key, walk_min),
    )
    conn.commit()


def compute_walk(
    listing: Listing,
    destination: Destination,
    *,
    conn: sqlite3.Connection,
    api_key: str | None = None,
    client: httpx.Client | None = None,
) -> CommuteResult:
    """Compute (or recall) walking minutes from listing to one destination.

    Returns a CommuteResult whose walk_min is the minutes on foot (None if
    Google found no walking route, or the haversine pre-filter skipped it).
    `source` tells whether we hit the API, the cache, or the pre-filter.
    """
    key = bucket_key(listing, destination.name)
    cached = get_cached(conn, key)
    if cached is not None:
        return cached

    # Haversine pre-filter — only useful when we have coords.
    if listing.lat is not None and listing.lng is not None:
        d_km = haversine_km(listing.lat, listing.lng, destination.lat, destination.lng)
        if d_km > HAVERSINE_KM_MAX:
            log.info("commute %s→%s: haversine=%.1fkm > %skm — skipping Routes",
                     listing.fingerprint_key, destination.name, d_km, HAVERSINE_KM_MAX)
            put_cached(conn, key, None)
            return CommuteResult(walk_min=None, source="haversine_skipped")

    api_key = api_key or os.environ["GOOGLE_DIRECTIONS_API_KEY"]
    own_client = client is None
    client = client or httpx.Client(timeout=15.0)
    try:
        origin_payload = _waypoint(listing.lat, listing.lng, _address_string(listing))
        destination_payload = _waypoint(destination.lat, destination.lng, None)
        walk, _ = _query(client, origin_payload, destination_payload, "WALK", api_key)
        put_cached(conn, key, walk)
        return CommuteResult(walk_min=walk, source="api")
    finally:
        if own_client:
            client.close()


def _waypoint(lat: float | None, lng: float | None, address: str | None) -> dict:
    """Build a Routes API waypoint — prefer coordinates, fall back to address."""
    if lat is not None and lng is not None:
        return {"location": {"latLng": {"latitude": lat, "longitude": lng}}}
    return {"address": address or ""}


def _address_string(listing: Listing) -> str:
    parts = [listing.address or "", *(listing.place_names[:2] if listing.place_names else []), "Beograd"]
    return ", ".join(p for p in parts if p)


class DirectionsConfigError(RuntimeError):
    """REQUEST_DENIED / 403 / quota — caller should stop trying for this run."""


def _query(
    client: httpx.Client, origin: dict, destination: dict, travel_mode: str, api_key: str,
) -> tuple[int | None, None]:
    """One Routes API call. Returns (minutes, None).

    The second tuple element is always None — kept so existing callers that
    unpack `walk, _ = _query(...)` stay valid after transit removal.

    Raises DirectionsConfigError on 401/403/429 — config or quota problems
    that don't get better by retrying within this run.
    """
    body: dict = {
        "origin": origin,
        "destination": destination,
        "travelMode": travel_mode,
        "computeAlternativeRoutes": False,
    }
    headers = {
        "Content-Type": "application/json",
        "X-Goog-Api-Key": api_key,
        "X-Goog-FieldMask": "routes.duration,routes.distanceMeters",
    }
    r = client.post(ROUTES_URL, json=body, headers=headers)
    if r.status_code in {401, 403}:
        raise DirectionsConfigError(f"Routes {travel_mode} HTTP {r.status_code}: {_error_message(r)}")
    if r.status_code == 429:
        raise DirectionsConfigError(f"Routes {travel_mode} HTTP 429: quota exhausted")
    if r.status_code >= 400:
        log.warning("Routes %s HTTP %s: %s", travel_mode, r.status_code, r.text[:200])
        return None, None

    data = r.json()
    routes = data.get("routes") or []
    if not routes:
        return None, None
    duration_raw = routes[0].get("duration")        # e.g. "1800s"
    if not duration_raw:
        return None, None
    try:
        seconds = int(str(duration_raw).rstrip("s"))
    except ValueError:
        return None, None
    return int(round(seconds / 60)), None


def _error_message(response) -> str:
    try:
        return response.json().get("error", {}).get("message") or response.text[:200]
    except Exception:
        return response.text[:200]


def monthly_api_count(conn: sqlite3.Connection) -> int:
    """Approximate # of API calls this calendar month.

    One WALK call per (location, destination) cache row written this month.
    Approximate; good enough for the digest's quota line.
    """
    first_of_month = datetime.utcnow().replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    row = conn.execute(
        "SELECT COUNT(*) FROM commute_cache WHERE fetched_at >= ?",
        (first_of_month.isoformat(" ", "seconds"),),
    ).fetchone()
    return row[0] or 0
