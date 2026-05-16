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

import logging
import os
from datetime import datetime, timezone
from typing import Iterable
from urllib.parse import quote

from src import telegram
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
    office_lat: float,
    office_lng: float,
) -> None:
    """Send the full digest. Idempotent at the Telegram level — call once per run."""
    perfect = list(result.passed)[:MAX_PERFECT]
    perfect_overflow = max(0, len(result.passed) - MAX_PERFECT)
    near = list(result.near_misses)[:MAX_NEAR_MISS]
    near_overflow = max(0, len(result.near_misses) - MAX_NEAR_MISS)

    telegram.send_message(_render_header(
        today=today,
        perfect=len(result.passed), near=len(result.near_misses),
        source_stats=source_stats, api_count=api_count, dedup_stats=dedup_stats,
        commute_config_error=commute_config_error,
        state_size_bytes=state_size_bytes, listings_tracked=listings_tracked,
        digest_path=digest_path,
    ))

    if not perfect and not near:
        telegram.send_message("Nothing new today. All systems green.")
        return

    for l in perfect:
        reason = (notify_reasons or {}).get(l.fingerprint_key)
        _send_listing(l, near_miss_reasons=None, notify_reason=reason,
                      office_lat=office_lat, office_lng=office_lng)

    if perfect_overflow:
        telegram.send_message(f"(+{perfect_overflow} more matches — see {digest_path})",
                              disable_notification=True)

    for l, reasons in near:
        _send_listing(l, near_miss_reasons=reasons, notify_reason=None,
                      office_lat=office_lat, office_lng=office_lng)

    if near_overflow:
        telegram.send_message(f"(+{near_overflow} more near-misses — see {digest_path})",
                              disable_notification=True)


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
        f"{name} {count}{' ⚠️' if err else ''}" for name, (count, err) in source_stats.items()
    )
    lines = [
        f"Belgrade rentals — {today.strftime('%Y-%m-%d')}",
        f"{perfect} matches · {near} near-misses",
        f"🩺 {src_line}",
    ]
    if commute_config_error:
        lines.append(f"🔢 Google API: ⚠️ {commute_config_error[:120]}")
    else:
        lines.append(f"🔢 Google API: {api_count}/40 000 this month")
    if dedup_stats and dedup_stats.get("clusters"):
        lines.append(
            f"🪞 Dedup: {dedup_stats['clusters']} cluster(s), "
            f"{dedup_stats.get('suppressed', 0)} suppressed"
        )
    kb = max(1, state_size_bytes // 1024)
    lines.append(f"💾 R2 state: {kb} KB · {listings_tracked} flats tracked")
    lines.append(f"📝 Archive: {digest_path}")
    return "\n".join(lines)


def _send_listing(
    l: Listing,
    *,
    near_miss_reasons: list[str] | None,
    notify_reason: str | None,
    office_lat: float,
    office_lng: float,
) -> None:
    caption = _render_listing(
        l, near_miss_reasons=near_miss_reasons, notify_reason=notify_reason,
        office_lat=office_lat, office_lng=office_lng,
    )
    if l.image_url:
        try:
            telegram.send_photo(l.image_url, caption=caption)
            return
        except Exception as e:  # noqa: BLE001
            log.warning("telegram sendPhoto failed for %s (%s); falling back to text", l.fingerprint_key, e)
    telegram.send_message(caption)


def _render_listing(
    l: Listing,
    *,
    near_miss_reasons: list[str] | None,
    notify_reason: str | None,
    office_lat: float,
    office_lng: float,
) -> str:
    head_emoji = "⚠️" if near_miss_reasons else "✅"
    notify_badge = ""
    if notify_reason == "price_drop":
        notify_badge = " · 📉 price drop"
    elif notify_reason == "reappeared":
        notify_badge = " · 🔁 reappeared"

    place = " · ".join(l.place_names[:2]) if l.place_names else ""

    commute_bits = []
    if l.walk_min is not None:
        commute_bits.append(f"{l.walk_min} min walk")
    if l.transit_min is not None:
        commute_bits.append(f"{l.transit_min} min transit")
    commute_str = " / ".join(commute_bits) if commute_bits else "no commute data"

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
        bills_line = f"\n💸 bills ≈ €{l.extraction.bills_estimate_eur}"

    lines: list[str] = [
        f"{head_emoji} €{l.price_eur:.0f}{notify_badge} · {l.rooms} rooms · {l.m2:.0f} m² · {place}",
        f"📍 {l.address or '?'} — {commute_str}",
        " · ".join(x for x in (heat, pets, dish, lift, floor_str) if x),
        f"📅 {posted_rel} · {agency}",
    ]
    if near_miss_reasons:
        lines.append("⚠️ Unconfirmed: " + "; ".join(near_miss_reasons))
    if red_flags:
        lines.append(f"🚩 {red_flags}")
    if bills_line:
        lines.append(bills_line.lstrip("\n"))
    if summary:
        lines.append("")
        lines.append(summary)

    # Links — use plain URLs (Telegram auto-detects, no parse mode needed).
    map_link = _maps_link(l)
    walk_link = _route_link(l, office_lat, office_lng, "walking")
    transit_link = _route_link(l, office_lat, office_lng, "transit")
    lines.append("")
    lines.append(f"🔗 {l.url}")
    lines.append(f"🗺 Map: {map_link}  ·  🚶 {walk_link}  ·  🚌 {transit_link}")
    return "\n".join(lines)


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
    parts = [l.address or "", *(l.place_names[:2] if l.place_names else []), "Belgrade"]
    q = quote(", ".join(p for p in parts if p))
    return f"https://www.google.com/maps?q={q}"


def _route_link(l: Listing, office_lat: float, office_lng: float, mode: str) -> str:
    if l.lat is not None and l.lng is not None:
        origin = f"{l.lat},{l.lng}"
    else:
        parts = [l.address or "", *(l.place_names[:2] if l.place_names else []), "Belgrade"]
        origin = quote(", ".join(p for p in parts if p))
    return (
        "https://www.google.com/maps/dir/?api=1"
        f"&origin={origin}&destination={office_lat},{office_lng}&travelmode={mode}"
    )
