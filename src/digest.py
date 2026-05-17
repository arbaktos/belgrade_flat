from __future__ import annotations

from datetime import datetime
from pathlib import Path

from src.filter import FilterResult
from src.models import Listing

DIGESTS_DIR = Path("digests")


def render(
    result: FilterResult,
    *,
    source_stats: dict[str, tuple[int, str | None]],
    today: datetime,
    api_count: int = 0,
    commute_config_error: str | None = None,
    notify_reasons: dict[str, str] | None = None,
    dedup_stats: dict | None = None,
) -> str:
    """Render the digest.

    `source_stats` maps source name → (listing_count, error_msg_or_None). A
    non-None error renders as `⚠️` on the health line per spec §9.
    """
    lines: list[str] = []
    lines.append(f"# Belgrade rentals — {today.strftime('%Y-%m-%d')} (test run)\n")
    near_miss_count = len(result.near_misses)
    near_miss_blurb = f" · {near_miss_count} near-miss" if near_miss_count else ""
    lines.append(
        f"**{len(result.passed)} matches{near_miss_blurb} · "
        f"{len(result.rejected)} rejected**\n"
    )
    src_line = " · ".join(
        f"{name} {count}{' ⚠️' if err else ''}" for name, (count, err) in source_stats.items()
    )
    lines.append(f"🩺 Sources: {src_line}")
    if commute_config_error:
        lines.append(f"🔢 Google API: ⚠️ {commute_config_error}")
    else:
        lines.append(f"🔢 Google API: {api_count}/40 000 this month")
    if dedup_stats and dedup_stats.get("clusters"):
        lines.append(
            f"🪞 Dedup: {dedup_stats['clusters']} cluster(s), "
            f"{dedup_stats['suppressed']} suppressed by re-notify policy"
        )
    lines.append("")

    error_lines = [
        f"- ⚠️ {name}: {err}" for name, (_c, err) in source_stats.items() if err
    ]
    if error_lines:
        lines.append("**Source errors:**")
        lines.extend(error_lines)
        lines.append("")

    lines.append("---\n")

    if result.near_misses:
        lines.append(f"## Near-misses ({len(result.near_misses)})\n")
        lines.append("Cleared structural filters but the LLM couldn't confirm one or more soft requirements. Vet these manually.\n")
        for l, reasons in result.near_misses:
            lines.append(_listing_block(l, near_miss_reasons=reasons))
        lines.append("\n---\n")

    if not result.passed:
        lines.append("_No matches today._\n")
    else:
        lines.append(f"## Matches ({len(result.passed)})\n")
        for l in result.passed:
            reason = (notify_reasons or {}).get(l.fingerprint_key)
            lines.append(_listing_block(l, notify_reason=reason))

    if result.rejected:
        lines.append(f"\n## Rejected ({len(result.rejected)})\n")
        for l, reason in result.rejected[:30]:
            lines.append(
                f"- ❌ {reason} — [{l.title or l.id}]({l.url}) "
                f"({l.price_eur}€ · {l.rooms} rooms · {l.m2}m²)"
            )
        if len(result.rejected) > 30:
            lines.append(f"- _…and {len(result.rejected) - 30} more_")

    return "\n".join(lines) + "\n"


def _listing_block(
    l: Listing,
    near_miss_reasons: list[str] | None = None,
    notify_reason: str | None = None,
) -> str:
    floor_str = f"floor {l.floor}" if l.floor is not None else "floor ?"
    if l.total_floors:
        floor_str += f"/{l.total_floors}"
    if l.elevator is True:
        lift = "🛗 lift"
    elif l.elevator is False:
        lift = "no lift"
    else:
        lift = "🛗?"           # data not exposed by this source — verify on the listing
    # Prefer LLM-confirmed heating if it differs from the raw source value.
    heating_label = (l.extraction.heating_type_confirmed if l.extraction else None) or l.heating_type
    heat = f"🔥 {heating_label}" if heating_label else "🔥?"
    # Pets: prefer a *positive* signal from either side over "unknown".
    pets_state = _pets_string(l.pets_allowed)
    if pets_state == "unknown" and l.extraction and l.extraction.pets_allowed:
        pets_state = l.extraction.pets_allowed
    if pets_state == "yes":
        pets = "🐾 pets OK"
    elif pets_state == "no":
        pets = "🚫🐾"
    else:
        pets = ""
    agency = "🏢 agency" if l.is_agency else "👤 owner/unknown"
    place = " · ".join(l.place_names[:2]) if l.place_names else ""

    head_emoji = "⚠️" if near_miss_reasons else "✅"
    notify_badge = " · 📉 price drop" if notify_reason == "price_drop" else ""
    commute_bits: list[str] = []
    if l.walk_min is not None:
        commute_bits.append(f"🚶 {l.walk_min} min")
    if l.transit_min is not None:
        if l.transit_transfers is not None:
            label = "direct" if l.transit_transfers == 0 else f"{l.transit_transfers} transfer{'s' if l.transit_transfers != 1 else ''}"
            commute_bits.append(f"🚌 {l.transit_min} min ({label})")
        else:
            commute_bits.append(f"🚌 {l.transit_min} min")
    commute_str = " · ".join(commute_bits) if commute_bits else ""

    block = [
        f"### {head_emoji} €{l.price_eur:.0f}{notify_badge} · {l.rooms} rooms · {l.m2:.0f} m² · {place}",
        f"- 📍 {l.address or '?'} · {floor_str} · {lift}",
        f"- {heat} · {pets} · {agency} · 📅 {l.created_at.strftime('%Y-%m-%d %H:%M UTC')}",
    ]
    if commute_str:
        block.append(f"- {commute_str}")
    block.append(f"- 🔗 [{l.title}]({l.url})")
    if near_miss_reasons:
        block.append("- ⚠️ Unconfirmed: " + "; ".join(near_miss_reasons))
    if l.winter_smog:
        from src.winter_smog import format_digest_line, format_smog_warning

        block.append(f"- {format_digest_line(l.winter_smog)}")
        warning = format_smog_warning(l.winter_smog)
        if warning:
            block.append(f"- {warning}")
    if l.extraction:
        e = l.extraction
        extras: list[str] = []
        if e.dishwasher is True:
            extras.append("🍽 dishwasher")
        elif e.dishwasher is False:
            extras.append("no dishwasher")
        if e.bills_estimate_eur and e.bills_estimate_eur > 200:
            extras.append(f"💸 bills ≈ €{e.bills_estimate_eur}")
        if e.max_lease_months:
            extras.append(f"📝 min lease {e.max_lease_months}mo")
        if extras:
            block.append("- " + " · ".join(extras))
        if e.red_flags:
            block.append("- 🚩 " + "; ".join(e.red_flags))
        if e.summary_en:
            block.append(f"\n  _{e.summary_en}_")
    if l.image_url:
        block.append(f"- ![photo]({l.image_url})")
    if l.description and not (l.extraction and l.extraction.summary_en):
        block.append(f"\n  > {l.description}")
    return "\n".join(block) + "\n"


def _pets_string(b: bool | None) -> str:
    return "yes" if b is True else "no" if b is False else "unknown"


def write(content: str, today: datetime) -> Path:
    DIGESTS_DIR.mkdir(parents=True, exist_ok=True)
    path = DIGESTS_DIR / f"{today.strftime('%Y-%m-%d')}-test.md"
    path.write_text(content)
    return path
