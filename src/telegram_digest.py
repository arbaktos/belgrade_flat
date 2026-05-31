"""Format and send the digest as a sequence of Telegram messages (spec §8).

Two stages of output:
1. A header summary message (sources, API count, dedup line, R2 stats).
2. One Telegram message per listing — match or near-miss — with photo,
   English summary, expandable Serbian original, Google Maps deep links.

Lifetime keeps the markdown archive (`digests/YYYY-MM-DD.md`) for the GitHub
record; this module is the *runtime delivery* layer.

Limits per spec §8:
- 10 perfect matches + 5 near-misses per digest.
- Overflow → "(+N more — see /digests/YYYY-MM-DD.md)".
- Empty days → "Nothing new today. All systems green." with the health footer.
"""
from __future__ import annotations

import html
import logging
import os
from datetime import datetime, timezone
from typing import Iterable
from urllib.parse import quote

from src import telegram
from src.winter_smog import format_digest_line
from src.destinations import Destination
from src.filter import FilterResult
from src.models import Listing

log = logging.getLogger(__name__)

MAX_PERFECT = 10
MAX_NEAR_MISS = 5


# ---------------------------------------------------------------------------
# Public entry
# ---------------------------------------------------------------------------

def send(
    result: FilterResult,
    *,
    today: datetime,
    source_stats: dict[str, tuple[int, str | None]],
    api_count: int,
    dedup_stats: dict | None,
    notify_reasons: dict[str, str] | None,
    commute_config_error: str | None,
    state_size_bytes: int,
    listings_tracked: int,
    digest_path: str,
    destinations: list[Destination],
) -> None:
    """Send the full digest. Idempotent at the Telegram level — call once per run."""
    perfect = list(result.passed)[:MAX_PERFECT]
    perfect_overflow = max(0, len(result.passed) - MAX_PERFECT)
    near = list(result.near_misses)[:MAX_NEAR_MISS]
    near_overflow = max(0, len(result.near_misses) - MAX_NEAR_MISS)

    telegram.send_message(
        _render_header(
            today=today,
            perfect=len(result.passed), near=len(result.near_misses),
            source_stats=source_stats, api_count=api_count, dedup_stats=dedup_stats,
            commute_config_error=commute_config_error,
            state_size_bytes=state_size_bytes, listings_tracked=listings_tracked,
            digest_path=digest_path,
        ),
        parse_mode="HTML",
    )

    if not perfect and not near:
        telegram.send_message("Nothing new today. All systems green.")
        return

    for l in perfect:
        reason = (notify_reasons or {}).get(l.fingerprint_key)
        _send_listing(l, near_miss_reasons=None, notify_reason=reason,
                      destinations=destinations)

    if perfect_overflow:
        telegram.send_message(f"(+{perfect_overflow} more matches — see {digest_path})",
                              disable_notification=True)

    for l, reasons in near:
        _send_listing(l, near_miss_reasons=reasons, notify_reason=None,
                      destinations=destinations)

    if near_overflow:
        telegram.send_message(f"(+{near_overflow} more near-misses — see {digest_path})",
                              disable_notification=True)


def send_instant_push(
    fresh_matches: list[Listing],
    *,
    fresh_near_misses: list[tuple[Listing, list[str]]] | None = None,
    notify_reasons: dict[str, str] | None,
    destinations: list[Destination],
) -> None:
    """Push newly-discovered perfect matches and near-misses between digests.

    Silent when there's nothing new — instant-push is a courtesy, not a
    heartbeat. The standard per-listing card is reused; a one-line header
    ("🔔 N new match… + M near-miss…") precedes the cards so the burst is
    recognisable against the daily digest.
    """
    fresh_near_misses = fresh_near_misses or []
    if not fresh_matches and not fresh_near_misses:
        log.info("instant-push: nothing new; staying quiet")
        return
    parts: list[str] = []
    if fresh_matches:
        parts.append(
            f"{len(fresh_matches)} new perfect match"
            f"{'es' if len(fresh_matches) != 1 else ''}"
        )
    if fresh_near_misses:
        parts.append(
            f"{len(fresh_near_misses)} near-miss"
            f"{'es' if len(fresh_near_misses) != 1 else ''}"
        )
    header = f"🔔 <b>{' + '.join(parts)}</b> since the last run."
    telegram.send_message(header, parse_mode="HTML")
    for l in fresh_matches:
        reason = (notify_reasons or {}).get(l.fingerprint_key)
        _send_listing(l, near_miss_reasons=None, notify_reason=reason,
                      destinations=destinations)
    for l, reasons in fresh_near_misses:
        _send_listing(l, near_miss_reasons=reasons, notify_reason=None,
                      destinations=destinations)


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------

def _render_header(
    *, today: datetime, perfect: int, near: int,
    source_stats: dict, api_count: int, dedup_stats: dict | None,
    commute_config_error: str | None, state_size_bytes: int, listings_tracked: int,
    digest_path: str,
) -> str:
    src_line = " · ".join(
        f"{html.escape(name)} {count}{' ⚠️' if err else ''}" for name, (count, err) in source_stats.items()
    )
    lines = [
        f"<b>Belgrade rentals — {today.strftime('%Y-%m-%d')}</b>",
        f"{perfect} matches · {near} near-misses",
        f"🩺 {src_line}",
    ]
    if commute_config_error:
        lines.append(f"🔢 Google API: ⚠️ {html.escape(commute_config_error[:120])}")
    else:
        lines.append(f"🔢 Google API: {api_count}/40 000 this month")
    if dedup_stats and dedup_stats.get("clusters"):
        lines.append(
            f"🪞 Dedup: {dedup_stats['clusters']} cluster(s), "
            f"{dedup_stats.get('suppressed', 0)} suppressed"
        )
    kb = max(1, state_size_bytes // 1024)
    lines.append(f"💾 R2 state: {kb} KB · {listings_tracked} flats tracked")
    # Archive link to the GitHub blob if we can guess the repo from env.
    repo = os.environ.get("GITHUB_REPOSITORY")
    if repo:
        archive_url = f"https://github.com/{repo}/blob/main/{digest_path}"
        lines.append(f'📝 <a href="{html.escape(archive_url, quote=True)}">Archive</a>')
    else:
        lines.append(f"📝 Archive: {html.escape(digest_path)}")
    return "\n".join(lines)


def _listing_keyboard(l: Listing) -> dict:
    """Inline buttons: open portal, save to favorites, or hide.

    Two rows so the labels stay readable on narrow screens — Telegram
    lays out three inline buttons in one row by squashing the text.
    """
    source_label = l.source
    return {
        "inline_keyboard": [
            [{"text": f"🔗 View on {source_label}", "url": l.url}],
            [
                {"text": "⭐ Favorite", "callback_data": f"fav:{l.fingerprint_key}"},
                {"text": "🙈 Hide", "callback_data": f"skip:{l.fingerprint_key}"},
            ],
        ]
    }


def _send_listing(
    l: Listing,
    *,
    near_miss_reasons: list[str] | None,
    notify_reason: str | None,
    destinations: list[Destination],
) -> None:
    """Send one listing: photo caption (or text) with inline View / Hide buttons.

    Map and commute links stay in the caption body; the portal URL is only on
  the View button — no separate follow-up message with a redundant link line.
    """
    body = _render_body(
        l, near_miss_reasons=near_miss_reasons, notify_reason=notify_reason,
        destinations=destinations,
    )
    keyboard = _listing_keyboard(l)

    if l.image_url:
        try:
            telegram.send_photo(
                l.image_url, caption=body, parse_mode="HTML", reply_markup=keyboard,
            )
            _send_translation_followup(l)
            return
        except Exception as e:  # noqa: BLE001
            log.warning("telegram sendPhoto failed for %s (%s); falling back to text",
                        l.fingerprint_key, e)
    telegram.send_message(body, parse_mode="HTML", reply_markup=keyboard)
    _send_translation_followup(l)


# Telegram sendMessage text limit; leave a safety margin for the wrapper tags.
_TRANSLATION_MAX_CHARS = 3900


def _send_translation_followup(l: Listing) -> None:
    """Send a separate text message with the full English translation of the
    listing description, when the LLM produced one. Skipped silently when no
    translation is available — keeps the photo card the sole notification.
    Failures here are logged but do not propagate; the card has already been
    delivered, so a missing translation must not look like a delivery error.
    """
    translation = l.extraction.description_en if l.extraction else None
    if not translation or not translation.strip():
        return
    if len(translation) > _TRANSLATION_MAX_CHARS:
        translation = translation[:_TRANSLATION_MAX_CHARS].rstrip() + "…"
    text = f"<i>{html.escape(translation)}</i>"
    try:
        telegram.send_message(text, parse_mode="HTML", disable_notification=True)
    except Exception as e:  # noqa: BLE001 — card already delivered
        log.warning("telegram translation follow-up failed for %s: %s",
                    l.fingerprint_key, e)


def _render_body(
    l: Listing,
    *,
    near_miss_reasons: list[str] | None,
    notify_reason: str | None,
    destinations: list[Destination] | None = None,
) -> str:
    """Listing details + LLM summary — fits in a 1024-byte caption.

    When destinations are given, the address line and each per-destination
    walking line become clickable Google Maps links (map pin + walk directions).
    """
    destinations = destinations or []
    head_emoji = "⚠️" if near_miss_reasons else "✅"
    notify_badge = " · 📉 price drop" if notify_reason == "price_drop" else ""

    place = " · ".join(l.place_names[:2]) if l.place_names else ""

    floor_str = f"floor {l.floor}" if l.floor is not None else "floor ?"
    if l.total_floors:
        floor_str += f"/{l.total_floors}"
    if l.elevator is True:
        lift = "🛗 lift"
    elif l.elevator is False:
        lift = "no lift"
    else:
        lift = "🛗?"

    heating_label = (l.extraction.heating_type_confirmed if l.extraction else None) or l.heating_type
    heat = f"🔥 {heating_label}" if heating_label else "🔥?"

    pets_state = _pets_string(l)
    if pets_state == "yes":
        pets = "🐾 pets OK"
    elif pets_state == "no":
        pets = "🚫🐾"
    else:
        pets = ""

    dish = ""
    if l.dishwasher is True or (l.extraction and l.extraction.dishwasher is True):
        dish = "🍽 dishwasher"

    agency = "🏢 agency" if l.is_agency else "👤 owner/unknown"
    posted_rel = _relative_time(l.created_at)

    summary = l.extraction.summary_en if l.extraction and l.extraction.summary_en else ""
    red_flags = "; ".join(l.extraction.red_flags) if l.extraction and l.extraction.red_flags else ""
    bills_line = ""
    if l.extraction and l.extraction.bills_estimate_eur and l.extraction.bills_estimate_eur > 200:
        bills_line = f"💸 bills ≈ €{l.extraction.bills_estimate_eur}"

    # All text is HTML-escaped; links use <a href> so URLs hide behind labels.
    place_esc = html.escape(place)
    address_esc = html.escape(l.address or "?")
    red_flags_esc = html.escape(red_flags)

    # Address + walking lines become clickable when destinations are known.
    if destinations:
        addr_line = f'📍 <a href="{html.escape(_maps_link(l), quote=True)}">{address_esc}</a>'
    else:
        addr_line = f"📍 {address_esc}"

    # One fact per line — much easier to skim than dense ` · ` separators.
    lines: list[str] = [
        f"{head_emoji} €{l.price_eur:.0f}{notify_badge} · {l.rooms} rooms · {l.m2:.0f} m² · {place_esc}",
        addr_line,
    ]
    any_walk = False
    for d in destinations:
        mins = l.commute.get(d.name)
        if mins is None:
            continue
        any_walk = True
        text = f"🚶 {mins} min to {html.escape(d.name)}"
        walk_url = _route_link(l, d.lat, d.lng, "walking")
        text = f'<a href="{html.escape(walk_url, quote=True)}">{text}</a>'
        lines.append(text)
    if destinations and not any_walk:
        lines.append("🚶 no walking data")
    for fact in (heat, pets, dish, lift, floor_str):
        if fact:
            lines.append(html.escape(fact))
    lines.append(f"📅 {posted_rel}")
    lines.append(agency)
    if near_miss_reasons:
        lines.append("⚠️ Unconfirmed: " + html.escape("; ".join(near_miss_reasons)))
    if red_flags_esc:
        lines.append(f"🚩 {red_flags_esc}")
    if bills_line:
        lines.append(bills_line)
    if l.winter_smog:
        lines.append(html.escape(format_digest_line(l.winter_smog)))
    body = "\n".join(lines)

    # Append the summary last, clipped to fit the remaining caption budget so
    # the <i>...</i> wrapper stays intact (was: byte-clipping the whole body
    # cut the body mid-tag → Telegram 400 'Can't find end tag for <i>').
    if summary:
        wrapper_overhead = len("\n\n<i></i>…".encode("utf-8"))
        budget = 1000 - len(body.encode("utf-8")) - wrapper_overhead
        if budget > 30:
            clipped_raw = _byte_clip(summary, max_bytes=budget)
            clipped_esc = html.escape(clipped_raw)
            # html.escape can inflate (& → &amp;); shave more if needed.
            while len(clipped_esc.encode("utf-8")) > budget and len(clipped_raw) > 20:
                clipped_raw = clipped_raw[:-10]
                clipped_esc = html.escape(clipped_raw)
            body += f"\n\n<i>{clipped_esc}</i>"

    return body


def _byte_clip(s: str, *, max_bytes: int) -> str:
    """Clip `s` so its UTF-8 byte length stays ≤ max_bytes, breaking on whitespace."""
    if len(s.encode("utf-8")) <= max_bytes:
        return s
    # Walk back from the end, dropping characters until we fit, then break on whitespace.
    trimmed = s
    while len(trimmed.encode("utf-8")) > max_bytes - 1:
        trimmed = trimmed[:-1]
    cut = trimmed.rfind(" ")
    if cut > 0 and (len(trimmed) - cut) < 40:
        trimmed = trimmed[:cut]
    return trimmed + "…"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _pets_string(l: Listing) -> str:
    if l.pets_allowed is True:
        return "yes"
    if l.extraction and l.extraction.pets_allowed in {"yes", "no", "unknown"}:
        return l.extraction.pets_allowed
    if l.pets_allowed is False:
        return "no"
    return "unknown"


def _relative_time(then: datetime) -> str:
    now = datetime.now(timezone.utc)
    diff = now - then
    h = int(diff.total_seconds() / 3600)
    if h < 1:
        return "just now"
    if h < 24:
        return f"{h} h ago"
    d = h // 24
    if d == 1:
        return "yesterday"
    return f"{d} days ago"


def _maps_link(l: Listing) -> str:
    if l.lat is not None and l.lng is not None:
        return f"https://www.google.com/maps?q={l.lat},{l.lng}"
    q = quote(_short_address_for_url(l))
    return f"https://www.google.com/maps?q={q}"


def _route_link(l: Listing, dest_lat: float, dest_lng: float, mode: str) -> str:
    if l.lat is not None and l.lng is not None:
        origin = f"{l.lat},{l.lng}"
    else:
        origin = quote(_short_address_for_url(l))
    return (
        "https://www.google.com/maps/dir/?api=1"
        f"&origin={origin}&destination={dest_lat},{dest_lng}&travelmode={mode}"
    )


def _short_address_for_url(l: Listing) -> str:
    """A compact 'street, Belgrade' for URL params.

    Source addresses can run 70+ chars ('… kompleks B5 kula 7 stan 142 …'),
    and we encode them into THREE URLs per listing (map + walk + transit),
    blowing past Telegram's 1024-byte caption cap. Take just the street
    (first comma-separated chunk) + the top neighborhood + Belgrade — enough
    for Google to geocode while keeping each URL under 200 bytes.
    """
    parts: list[str] = []
    if l.address:
        parts.append(l.address.split(",", 1)[0].strip())
    if l.place_names:
        # Add the smallest place (last in the chain — usually street/quarter).
        place = l.place_names[-1].strip()
        if place and place not in parts[0:1]:
            parts.append(place)
    parts.append("Belgrade")
    return ", ".join(p for p in parts if p)
