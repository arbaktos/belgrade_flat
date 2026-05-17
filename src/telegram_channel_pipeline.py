"""Telegram-channel source pipeline.

Standalone mini-pipeline for `@beograd_stan`-style channels. Channel posts
are free-form text with no structured fields, so this bypasses the regular
filter cascade entirely — user said: only two filters (pets=yes AND m²>min)
plus commute, plus dedup against portal listings.

Flow per post:
    fetch → LLM-extract pets/m²/address → filter → pHash dedup against
    listings table → compute commute → push to Telegram → mark seen.

Failures on any individual post are logged and the post is still marked
seen so the next run doesn't retry forever.
"""
from __future__ import annotations

import html
import logging
import os
import sqlite3
from io import BytesIO

import anthropic
import httpx
import imagehash
from PIL import Image

from src import dedup, extract, route, state, telegram
from src.models import Listing
from src.sources import telegram_channel

log = logging.getLogger(__name__)

# A post needs to be at least this big to qualify (per user spec).
DEFAULT_M2_MIN = 55
# t.me/s preview pages don't list lots of metadata — placeholder values for the
# Listing fields we don't have but route.compute_commute would inspect.
_PLACEHOLDER_CREATED = None     # set per-call from post.posted_at


def run(
    channel: str,
    conn: sqlite3.Connection,
    *,
    m2_min: float = DEFAULT_M2_MIN,
    office_lat: float | None = None,
    office_lng: float | None = None,
    anthropic_client: anthropic.Anthropic | None = None,
    http_client: httpx.Client | None = None,
) -> dict:
    """Drive the pipeline once. Returns a counter dict for log/telemetry."""
    counts = {"fetched": 0, "filtered_out": 0, "deduped": 0, "pushed": 0, "errors": 0}
    seen = state.seen_telegram_message_ids(conn)

    try:
        posts = telegram_channel.fetch_recent_posts(channel, exclude=seen)
    except Exception as e:  # noqa: BLE001 — channel scrape failure must not break the run
        log.warning("telegram-channel: scrape failed: %s", e, exc_info=True)
        return counts

    counts["fetched"] = len(posts)
    if not posts:
        return counts

    if "ANTHROPIC_API_KEY" not in os.environ:
        log.warning("telegram-channel: ANTHROPIC_API_KEY missing; skipping")
        return counts
    anthropic_client = anthropic_client or anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])

    own_http = http_client is None
    http_client = http_client or httpx.Client(timeout=15.0, follow_redirects=True)

    try:
        for post in posts:
            try:
                _process_one(
                    post, conn, counts,
                    anthropic_client=anthropic_client,
                    http_client=http_client,
                    m2_min=m2_min,
                    office_lat=office_lat,
                    office_lng=office_lng,
                )
            except Exception as e:  # noqa: BLE001
                log.warning("telegram-channel: post %s failed: %s",
                            post.message_id, e, exc_info=True)
                counts["errors"] += 1
                # Mark seen anyway — retrying a malformed post on every run is wasteful.
                state.mark_telegram_message_seen(conn, post.message_id)
    finally:
        if own_http:
            http_client.close()

    log.info("telegram-channel: %s", counts)
    return counts


def _process_one(
    post, conn, counts,
    *, anthropic_client, http_client, m2_min, office_lat, office_lng,
) -> None:
    facts = extract.extract_telegram_post(post.text, client=anthropic_client)

    # Filter 1: pets must be explicitly allowed.
    if facts.pets_allowed != "yes":
        log.info("tg post %s rejected: pets=%s", post.message_id, facts.pets_allowed)
        counts["filtered_out"] += 1
        state.mark_telegram_message_seen(conn, post.message_id)
        return

    # Filter 2: m² must be > threshold. Unknown m² is treated as a reject —
    # user's spec is strict on size.
    if facts.m2 is None or facts.m2 <= m2_min:
        log.info("tg post %s rejected: m2=%s", post.message_id, facts.m2)
        counts["filtered_out"] += 1
        state.mark_telegram_message_seen(conn, post.message_id)
        return

    # Dedup against portal listings via pHash of the cover photo.
    phash = _compute_phash(post.photo_urls[0], http_client) if post.photo_urls else None
    if phash is not None and _is_known_listing_by_phash(conn, phash):
        log.info("tg post %s is a duplicate of an existing portal listing", post.message_id)
        counts["deduped"] += 1
        state.mark_telegram_message_seen(conn, post.message_id)
        return

    # Commute via the existing route module — build a minimal Listing shim
    # because route.compute_commute reads .lat/.lng/.address/.fingerprint_key.
    commute = None
    if office_lat is not None and office_lng is not None and facts.address:
        shim = _commute_shim(post, facts)
        try:
            commute = route.compute_commute(
                shim, office_lat=office_lat, office_lng=office_lng, conn=conn,
            )
        except Exception as e:  # noqa: BLE001
            log.warning("tg post %s: commute failed: %s", post.message_id, e)

    _send_card(post, facts, commute)
    counts["pushed"] += 1
    state.mark_telegram_message_seen(conn, post.message_id)


def _compute_phash(url: str, http_client: httpx.Client) -> str | None:
    try:
        r = http_client.get(url)
        r.raise_for_status()
        img = Image.open(BytesIO(r.content))
        if img.mode != "RGB":
            img = img.convert("RGB")
        return str(imagehash.phash(img, hash_size=8))
    except Exception as e:  # noqa: BLE001
        log.warning("tg pHash: %s -> %s", url, e)
        return None


def _is_known_listing_by_phash(conn: sqlite3.Connection, phash: str) -> bool:
    """True if any listing in the DB has a pHash within hamming threshold."""
    target = imagehash.hex_to_hash(phash)
    for (row_hash,) in conn.execute(
        "SELECT image_phash FROM listings WHERE image_phash IS NOT NULL"
    ):
        try:
            if (imagehash.hex_to_hash(row_hash) - target) <= dedup.PHASH_MAX_HAMMING:
                return True
        except ValueError:
            continue
    return False


def _commute_shim(post, facts) -> Listing:
    """Build a throwaway Listing carrying just enough for route.compute_commute."""
    from datetime import datetime, timezone
    return Listing(
        id=str(post.message_id),
        source="telegram_channel",
        url=post.permalink,
        price_eur=0.0, m2=facts.m2 or 0.0, rooms=0.0,
        floor=None, total_floors=None, last_floor=False, elevator=None,
        furnished=None, heating_type=None, pets_allowed=True,
        title=(post.text or "")[:60],
        description=post.text or "",
        address=facts.address,
        place_names=[],
        image_url=post.photo_urls[0] if post.photo_urls else None,
        is_agency=False,
        created_at=post.posted_at or datetime.now(timezone.utc),
    )


def _send_card(post, facts, commute) -> None:
    """Brief Telegram card per match. Photo + caption, no inline buttons —
    posts don't have a stable fingerprint_key the Hide button needs."""
    bits = [
        f"📣 <b>Telegram channel match</b> · {facts.m2:.0f} m²",
        f"🐾 pets OK",
    ]
    if commute:
        if commute.walk_min is not None:
            bits.append(f"🚶 {commute.walk_min} min")
        if commute.transit_min is not None:
            bits.append(f"🚌 {commute.transit_min} min")
    if facts.address:
        bits.append(f"📍 {html.escape(facts.address)}")
    bits.append(f'🔗 <a href="{html.escape(post.permalink, quote=True)}">Source post</a>')
    if facts.summary_en:
        bits.append("")
        bits.append(f"<i>{html.escape(facts.summary_en)}</i>")
    caption = "\n".join(bits)

    photo_url = post.photo_urls[0] if post.photo_urls else None
    if photo_url:
        try:
            telegram.send_photo(photo_url, caption=caption, parse_mode="HTML")
            return
        except Exception as e:  # noqa: BLE001
            log.warning("tg-channel sendPhoto failed (%s); falling back to text", e)
    telegram.send_message(caption, parse_mode="HTML")
