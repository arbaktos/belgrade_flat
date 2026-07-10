"""Dedup + re-notify policy (spec §6).

Cascade fingerprint, in priority order:
1. Image pHash of first photo — the workhorse, since agencies syndicate the
   same photo across portals.
2. Coords (4 decimals) + m² + price-bucket (€50) — building-level cluster
   when we have coordinates.
3. Title trigram similarity ≥ 0.8 + price ±5% (fallback).

Phone-normalization (spec's 4th key) is omitted in M6 because none of the
four sources expose phone numbers reliably in listing summaries.

Re-notify rules (see notify_reason): a card is sent only when something
changed — first sighting, any price change (📉 badge on ≥5% drops), a
near-miss upgrading to a perfect match, or >14 days since the last card.
Otherwise the listing is suppressed and counted in the digest header.
"""
from __future__ import annotations

import logging
import re
import sqlite3
from datetime import datetime, timedelta, timezone
from difflib import SequenceMatcher
from io import BytesIO
from typing import Iterable

import httpx
import imagehash
from PIL import Image

from src.models import Listing

log = logging.getLogger(__name__)

PHASH_MAX_HAMMING = 6                # ≤6 bits different ≈ "same picture"
COORD_DECIMALS = 4
PRICE_BUCKET_EUR = 50
TITLE_TRIGRAM_THRESHOLD = 0.8
PRICE_SIMILARITY_PCT = 0.05
PRICE_DROP_BADGE_PCT = 0.05         # informational only — show 📉 when price drops this much
IMAGE_DOWNLOAD_TIMEOUT_S = 10


# ---------------------------------------------------------------------------
# pHash
# ---------------------------------------------------------------------------

def compute_phashes(listings: list[Listing], *, client: httpx.Client | None = None) -> int:
    """Populate `image_phash` on each listing whose `image_url` is downloadable.

    Returns the number of listings that successfully got a pHash. Failures are
    logged and the listing's image_phash stays None.
    """
    own_client = client is None
    client = client or httpx.Client(timeout=IMAGE_DOWNLOAD_TIMEOUT_S, follow_redirects=True)
    try:
        successes = 0
        for l in listings:
            if not l.image_url or l.image_phash is not None:
                continue
            try:
                resp = client.get(l.image_url)
                resp.raise_for_status()
                l.image_phash = _phash_from_bytes(resp.content)
                successes += 1
            except Exception as e:  # noqa: BLE001 — one bad image must not kill dedup
                log.warning("dedup: pHash failed for %s: %s", l.fingerprint_key, e)
        return successes
    finally:
        if own_client:
            client.close()


def _phash_from_bytes(data: bytes) -> str:
    img = Image.open(BytesIO(data))
    if img.mode != "RGB":
        img = img.convert("RGB")
    return str(imagehash.phash(img, hash_size=8))


def hamming(a: str, b: str) -> int:
    """Bit-level Hamming distance between two pHash hex strings."""
    return imagehash.hex_to_hash(a) - imagehash.hex_to_hash(b)


def cluster_duplicates(listings: list[Listing]) -> list[list[Listing]]:
    """Group listings into clusters where each pair within shares a fingerprint.

    Returns a list of clusters (each is a list of Listings). Singletons land in
    their own cluster.
    """
    clusters: list[list[Listing]] = []
    for l in listings:
        matched = None
        for cluster in clusters:
            if any(_same_flat(l, c) for c in cluster):
                matched = cluster
                break
        if matched is None:
            clusters.append([l])
        else:
            matched.append(l)
    return clusters


def is_skipped_duplicate(listing: Listing, skipped: list[Listing]) -> bool:
    """True if `listing` is the same flat as any the user has hidden.

    Uses the full cascade, so a re-listed hidden flat (new id, same photos or
    same title+price) is caught even though its fingerprint_key is new. Coords
    aren't persisted for skipped flats, so layer 2 (coord bucket) never fires
    here — pHash and title+price carry the match.
    """
    return any(_same_flat(listing, s) for s in skipped)


def _same_flat(a: Listing, b: Listing) -> bool:
    """Return True if the four-layer cascade considers a and b the same flat."""
    # 1. pHash
    if a.image_phash and b.image_phash and hamming(a.image_phash, b.image_phash) <= PHASH_MAX_HAMMING:
        return True
    # 2. Coords + m² + price bucket
    if _coord_bucket(a) and _coord_bucket(a) == _coord_bucket(b):
        if abs(a.m2 - b.m2) < 1 and _price_bucket(a.price_eur) == _price_bucket(b.price_eur):
            return True
    # 3. Title trigram + price ±5%
    if _title_similar(a.title, b.title) and _price_similar(a.price_eur, b.price_eur):
        return True
    return False


def pick_canonical(cluster: list[Listing]) -> Listing:
    """Pick one representative listing from a cluster.

    Preference order: 4zida (richest structured data) > cityexpert > nekretnine
    > halooglasi. Within a source, prefer the most recent createdAt.
    """
    source_rank = {"4zida": 0, "cityexpert": 1, "nekretnine": 2, "halooglasi": 3}
    return min(cluster, key=lambda l: (source_rank.get(l.source, 99), -l.created_at.timestamp()))


# ---------------------------------------------------------------------------
# Re-notify policy (restored 2026-07-10: only send a card when something changed)
# ---------------------------------------------------------------------------

RENOTIFY_AFTER_DAYS = 14    # a card older than this may be sent again


def notify_reason(
    listing: Listing,
    conn: sqlite3.Connection,
    *,
    stage: str = "match",
    renotify_after_days: int = RENOTIFY_AFTER_DAYS,
) -> str | None:
    """Why this listing deserves a Telegram card this run — or None to suppress.

    Reasons, in priority order:
      "new"           never notified before
      "price_drop"    ≥ PRICE_DROP_BADGE_PCT cheaper than the last card (📉)
      "price_change"  any other price change since the last card
      "upgraded"      last card was a near-miss, now a confirmed perfect match
      "relisted"      last card is > renotify_after_days old (re-listed, or a
                      still-available reminder for a long-running listing)
    """
    row = conn.execute(
        "SELECT notified_at, notified_price, notified_stage "
        "FROM listings WHERE fingerprint_key=?",
        (listing.fingerprint_key,),
    ).fetchone()
    if row is None or row[0] is None:
        return "new"
    notified_at, last_price, last_stage = row
    if last_price is not None:
        last_price = float(last_price)
        if listing.price_eur < last_price * (1 - PRICE_DROP_BADGE_PCT):
            return "price_drop"
        if listing.price_eur != last_price:
            return "price_change"
    if stage == "match" and last_stage == "near_miss":
        return "upgraded"
    # sqlite datetime('now') writes naive UTC ("YYYY-MM-DD HH:MM:SS").
    then = datetime.fromisoformat(notified_at).replace(tzinfo=timezone.utc)
    if datetime.now(timezone.utc) - then > timedelta(days=renotify_after_days):
        return "relisted"
    return None


def mark_notified(listing: Listing, conn: sqlite3.Connection, *, stage: str = "match") -> None:
    """Stamp the canonical listing as notified at the current price and stage."""
    conn.execute(
        "UPDATE listings SET notified_at=datetime('now'), notified_price=?, "
        "notified_stage=? WHERE fingerprint_key=?",
        (listing.price_eur, stage, listing.fingerprint_key),
    )
    conn.commit()


def persist_phashes(listings: list[Listing], conn: sqlite3.Connection) -> None:
    """Write computed pHashes back to the listings table."""
    rows = [(l.image_phash, l.fingerprint_key) for l in listings if l.image_phash]
    if not rows:
        return
    conn.executemany(
        "UPDATE listings SET image_phash=? WHERE fingerprint_key=?", rows
    )
    conn.commit()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _coord_bucket(l: Listing) -> str | None:
    if l.lat is None or l.lng is None:
        return None
    return f"{round(l.lat, COORD_DECIMALS)},{round(l.lng, COORD_DECIMALS)}"


def _price_bucket(price: float) -> int:
    return int(price // PRICE_BUCKET_EUR)


def _title_similar(a: str, b: str) -> bool:
    return SequenceMatcher(None, _norm_title(a), _norm_title(b)).ratio() >= TITLE_TRIGRAM_THRESHOLD


def _norm_title(t: str) -> str:
    t = (t or "").lower()
    t = re.sub(r"[^\w\s]", " ", t, flags=re.UNICODE)
    return re.sub(r"\s+", " ", t).strip()


def _price_similar(a: float, b: float) -> bool:
    if a <= 0 or b <= 0:
        return False
    return abs(a - b) / max(a, b) <= PRICE_SIMILARITY_PCT


