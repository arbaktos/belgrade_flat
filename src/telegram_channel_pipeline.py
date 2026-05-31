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
import re
import sqlite3
from io import BytesIO

import httpx
import imagehash
from PIL import Image

from src import dedup, extract, route, state, telegram
from src.destinations import Destination
from src.models import Listing
from src.sources import telegram_channel

log = logging.getLogger(__name__)

# A post needs to be at least this big to qualify (per user spec).
DEFAULT_M2_MIN = 55
# t.me/s preview pages don't list lots of metadata — placeholder values for the
# Listing fields we don't have but route.compute_walk would inspect.
_PLACEHOLDER_CREATED = None     # set per-call from post.posted_at


def run(
    channel: str,
    conn: sqlite3.Connection,
    *,
    m2_min: float = DEFAULT_M2_MIN,
    require_hashtag: str | None = None,
    office: Destination | None = None,
    llm_client: object | None = None,
    http_client: httpx.Client | None = None,
) -> dict:
    """Drive the pipeline once. Returns a counter dict for log/telemetry.

    `require_hashtag` (e.g. "#аренда") restricts processing to posts carrying
    that tag — for general-interest channels where most posts aren't rentals.
    Non-matching posts are marked seen and never reach the LLM, so the filter
    costs no tokens and produces no status-header noise.
    """
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

    if require_hashtag:
        relevant = []
        for p in posts:
            if _post_has_hashtag(p.text, require_hashtag):
                relevant.append(p)
            else:
                # Irrelevant for good — mark seen so we never re-evaluate it,
                # and don't count it as "filtered" (that's for rentals we reject).
                state.mark_telegram_message_seen(conn, p.message_id)
        log.info("telegram-channel %s: %d/%d posts carry %s",
                 channel, len(relevant), len(posts), require_hashtag)
        posts = relevant
        if not posts:
            return counts

    if not extract.llm_api_key_present():
        log.warning("telegram-channel: LLM API key missing for provider=%s; skipping",
                    extract._provider())
        return counts
    llm_client = llm_client or extract.make_client()

    own_http = http_client is None
    http_client = http_client or httpx.Client(timeout=15.0, follow_redirects=True)

    # Two-pass design so we can emit one summary line BEFORE the per-match
    # cards: pass 1 decides accept/reject and marks the rejects seen; pass 2
    # delivers the accepted posts (commute + card + mark seen).
    accepted: list[tuple] = []
    try:
        for post in posts:
            try:
                facts = extract.extract_telegram_post(post.text, client=llm_client)
            except Exception as e:  # noqa: BLE001
                log.warning("telegram-channel extract failed for %s: %s", post.message_id, e)
                counts["errors"] += 1
                state.mark_telegram_message_seen(conn, post.message_id)
                continue

            if facts.pets_allowed != "yes" or facts.m2 is None or facts.m2 <= m2_min:
                log.info("tg post %s rejected: pets=%s m2=%s",
                         post.message_id, facts.pets_allowed, facts.m2)
                counts["filtered_out"] += 1
                state.mark_telegram_message_seen(conn, post.message_id)
                continue

            phash = _compute_phash(post.photo_urls[0], http_client) if post.photo_urls else None
            if phash is not None and _is_known_listing_by_phash(conn, phash):
                log.info("tg post %s is a duplicate of an existing portal listing", post.message_id)
                counts["deduped"] += 1
                state.mark_telegram_message_seen(conn, post.message_id)
                continue

            accepted.append((post, facts))

        # Pre-card status line. Fires whenever the channel page had ANY new
        # posts (or we hit an error) — gives a per-poll heartbeat that the
        # source is alive even when all posts get filtered. Silent only on
        # genuinely empty polls (page exists but contained no unseen posts).
        if posts or counts["errors"]:
            _send_status_header(channel, counts, accepted_count=len(accepted))

        for post, facts in accepted:
            try:
                commute = None
                if office is not None and facts.address:
                    shim = _commute_shim(post, facts)
                    try:
                        commute = route.compute_walk(shim, office, conn=conn)
                    except Exception as e:  # noqa: BLE001
                        log.warning("tg post %s commute failed: %s", post.message_id, e)
                _send_card(post, facts, commute)
                counts["pushed"] += 1
            except Exception as e:  # noqa: BLE001
                log.warning("telegram-channel deliver failed for %s: %s", post.message_id, e)
                counts["errors"] += 1
            finally:
                state.mark_telegram_message_seen(conn, post.message_id)
    finally:
        if own_http:
            http_client.close()

    log.info("telegram-channel: %s", counts)
    return counts


def _send_status_header(channel: str, counts: dict, *, accepted_count: int) -> None:
    """Single-line summary preceding the per-match cards.

    Fires only when we have matches to push OR encountered errors.
    Filter/dup counts are appended only when non-zero to keep the line tight.
    """
    bits = [f"📣 <b>{html.escape(channel)}</b>",
            f"{accepted_count} new match{'es' if accepted_count != 1 else ''}"]
    extras = []
    if counts.get("filtered_out"):
        extras.append(f"{counts['filtered_out']} filtered")
    if counts.get("deduped"):
        extras.append(f"{counts['deduped']} dup of portal")
    if counts.get("errors"):
        extras.append(f"{counts['errors']} error{'s' if counts['errors'] != 1 else ''}")
    line = " · ".join(bits)
    if extras:
        line += f" ({', '.join(extras)})"
    try:
        telegram.send_message(line, parse_mode="HTML")
    except Exception as e:  # noqa: BLE001
        log.warning("telegram-channel status header failed: %s", e)


def _post_has_hashtag(text: str, hashtag: str) -> bool:
    """Whether `text` contains `hashtag` as a whole tag, case-insensitive.

    The trailing `(?!\\w)` keeps "#аренда" from matching inside a longer tag
    like "#арендаквартиры" — `\\w` is Unicode-aware for str patterns, so it
    covers Cyrillic. A leading boundary isn't needed: the literal "#" already
    anchors the tag start.
    """
    if not text:
        return False
    return re.search(re.escape(hashtag) + r"(?!\w)", text, re.IGNORECASE) is not None


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
    """Build a throwaway Listing carrying just enough for route.compute_walk."""
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
    if commute and commute.walk_min is not None:
        bits.append(f"🚶 {commute.walk_min} min")
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
