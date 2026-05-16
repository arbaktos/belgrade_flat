from __future__ import annotations

from datetime import datetime
from pathlib import Path

from src.filter import FilterResult
from src.models import Listing

DIGESTS_DIR = Path("digests")


def render(result: FilterResult, *, source_stats: dict[str, int], today: datetime) -> str:
    lines: list[str] = []
    lines.append(f"# Belgrade rentals — {today.strftime('%Y-%m-%d')} (M2 TEST)\n")
    lines.append(
        f"**{len(result.passed)} matches · {len(result.rejected)} rejected** "
        f"(scope: 4zida, structural filters only — no LLM / commute / dedup yet)\n"
    )
    src_line = " · ".join(f"{k} {v}" for k, v in source_stats.items())
    lines.append(f"🩺 Sources: {src_line}\n")
    lines.append("---\n")

    if not result.passed:
        lines.append("_No matches today._\n")
    else:
        lines.append(f"## Matches ({len(result.passed)})\n")
        for l in result.passed:
            lines.append(_listing_block(l))

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


def _listing_block(l: Listing) -> str:
    floor_str = f"floor {l.floor}" if l.floor is not None else "floor ?"
    if l.total_floors:
        floor_str += f"/{l.total_floors}"
    lift = "🛗 lift" if l.elevator else "no lift"
    heat = f"🔥 {l.heating_type}" if l.heating_type else ""
    pets = "🐾 pets OK" if l.pets_allowed else ("🚫🐾" if l.pets_allowed is False else "")
    agency = "🏢 agency" if l.is_agency else "👤 owner/unknown"
    place = " · ".join(l.place_names[:2]) if l.place_names else ""

    block = [
        f"### ✅ €{l.price_eur:.0f} · {l.rooms} rooms · {l.m2:.0f} m² · {place}",
        f"- 📍 {l.address or '?'} · {floor_str} · {lift}",
        f"- {heat} · {pets} · {agency} · 📅 {l.created_at.strftime('%Y-%m-%d %H:%M UTC')}",
        f"- 🔗 [{l.title}]({l.url})",
    ]
    if l.image_url:
        block.append(f"- ![photo]({l.image_url})")
    if l.description:
        block.append(f"\n  > {l.description}")
    return "\n".join(block) + "\n"


def write(content: str, today: datetime) -> Path:
    DIGESTS_DIR.mkdir(parents=True, exist_ok=True)
    path = DIGESTS_DIR / f"{today.strftime('%Y-%m-%d')}-test.md"
    path.write_text(content)
    return path
