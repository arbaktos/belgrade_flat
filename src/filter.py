from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from src.models import Listing


@dataclass
class FilterConfig:
    price_eur_max: float
    rooms_min: float
    rooms_max: float
    surface_m2_min: float
    elevator_required: bool
    photo_required: bool
    freshness_days: int
    heating_allowed: tuple[str, ...]
    dishwasher_required: bool
    pets_required: bool
    max_lease_months: int
    furnishing_allowed: tuple[str, ...] = ()
    walk_min_max: int = 30
    transit_min_max: int = 30


@dataclass
class FilterResult:
    passed: list[Listing]
    rejected: list[tuple[Listing, str]]                       # (listing, first failing rule)
    near_misses: list[tuple[Listing, list[str]]] = field(default_factory=list)  # (listing, ambiguous fields)


def from_dict(cfg: dict) -> FilterConfig:
    f = cfg["filters"]
    commute = f.get("commute") or {}
    return FilterConfig(
        price_eur_max=float(f["price_eur_max"]),
        rooms_min=float(f["rooms_min"]),
        rooms_max=float(f["rooms_max"]),
        surface_m2_min=float(f["surface_m2_min"]),
        elevator_required=bool(f["elevator_required"]),
        photo_required=bool(f.get("photo_required", True)),
        freshness_days=int(f["freshness_days"]),
        heating_allowed=tuple(f.get("heating_allowed", ())),
        furnishing_allowed=tuple(f.get("furnishing_allowed", ())),
        dishwasher_required=bool(f.get("dishwasher_required", False)),
        pets_required=bool(f.get("pets_required", False)),
        max_lease_months=int(f.get("max_lease_months", 12)),
        walk_min_max=int(commute.get("walk_min_max", 30)),
        transit_min_max=int(commute.get("transit_min_max", 30)),
    )


def apply_commute(listings: list[Listing], cfg: FilterConfig) -> FilterResult:
    """Filter on walk_min/transit_min populated by src/route.py.

    Per spec §4: pass if walk ≤ 30 min OR transit ≤ 30 min. Listings with both
    fields None (Google had no route or haversine-skipped) get hard-rejected.
    """
    passed: list[Listing] = []
    rejected: list[tuple[Listing, str]] = []
    for l in listings:
        walk_ok = l.walk_min is not None and l.walk_min <= cfg.walk_min_max
        transit_ok = l.transit_min is not None and l.transit_min <= cfg.transit_min_max
        if walk_ok or transit_ok:
            passed.append(l)
        else:
            best = []
            if l.walk_min is not None:
                best.append(f"walk {l.walk_min}m")
            if l.transit_min is not None:
                best.append(f"transit {l.transit_min}m")
            reason = ("no route" if not best else " / ".join(best)) + f" > {cfg.walk_min_max}m"
            rejected.append((l, reason))
    return FilterResult(passed=passed, rejected=rejected)


def apply(listings: list[Listing], cfg: FilterConfig, *, now: datetime | None = None) -> FilterResult:
    """Structural-only filter (price/rooms/m²/floor/elevator/freshness).

    This is the cheap pass — anything failing here never gets an LLM call.
    Use apply_with_extraction() afterward for pets/dishwasher/heating/lease.
    """
    now = now or datetime.now(timezone.utc)
    cutoff = now - timedelta(days=cfg.freshness_days)
    passed: list[Listing] = []
    rejected: list[tuple[Listing, str]] = []
    for l in listings:
        reason = _check_structural(l, cfg, cutoff)
        if reason is None:
            passed.append(l)
        else:
            rejected.append((l, reason))
    return FilterResult(passed=passed, rejected=rejected)


def apply_with_extraction(listings: list[Listing], cfg: FilterConfig) -> FilterResult:
    """Run the LLM-derived rules on listings that already cleared the structural pass.

    Per spec §3, ambiguous fields (e.g. pets "unknown") route to near-misses
    rather than silent rejection. Definitive "no" still hard-rejects.
    """
    passed: list[Listing] = []
    near_misses: list[tuple[Listing, list[str]]] = []
    rejected: list[tuple[Listing, str]] = []
    for l in listings:
        hard, soft = _check_extraction(l, cfg)
        if hard:
            rejected.append((l, hard[0]))
        elif soft:
            near_misses.append((l, soft))
        else:
            passed.append(l)
    return FilterResult(passed=passed, rejected=rejected, near_misses=near_misses)


def _check_structural(l: Listing, cfg: FilterConfig, cutoff: datetime) -> str | None:
    if l.price_eur <= 0 or l.price_eur > cfg.price_eur_max:
        return f"price {l.price_eur}€ > cap {cfg.price_eur_max}€"
    if l.rooms < cfg.rooms_min or l.rooms > cfg.rooms_max:
        return f"rooms {l.rooms} outside [{cfg.rooms_min}, {cfg.rooms_max}]"
    if l.m2 < cfg.surface_m2_min:
        return f"surface {l.m2}m² < {cfg.surface_m2_min}m²"
    if l.floor is None:
        return "floor unknown"
    if l.floor < 0:
        return f"basement floor ({l.floor})"
    # Ground floor (0) is allowed; user accepts walk-up flats there. For
    # floor 1+ we still require an elevator when configured.
    # elevator=None means the source did not expose the field — near-miss-only.
    if cfg.elevator_required and l.floor > 0 and l.elevator is False:
        return "no elevator"
    if l.created_at < cutoff:
        return f"older than {cfg.freshness_days}d"
    if cfg.photo_required and not _has_photo(l):
        return "no photo"
    return None


def _has_photo(l: Listing) -> bool:
    return bool(l.image_url and str(l.image_url).strip())


def _check_extraction(l: Listing, cfg: FilterConfig) -> tuple[list[str], list[str]]:
    """Apply the LLM-aware rules. Each source's structured data is preferred
    over the LLM's confirmation; the LLM is fallback only.

    Returns (hard_rejects, near_miss_reasons).
    """
    e = l.extraction
    hard: list[str] = []
    soft: list[str] = []

    # ---- pets ---------------------------------------------------------------
    # Most Serbian listings don't mention pets at all, so "unknown" is the
    # default state, not a signal. We only hard-reject when the listing
    # explicitly forbids pets; silence passes cleanly.
    if cfg.pets_required:
        pets = _resolve_pets(l, e)
        if pets is False:
            hard.append("pets not allowed")

    # ---- dishwasher ---------------------------------------------------------
    # Same logic as pets: structured True = positive signal, structured False
    # = "not on the source's checkbox list" (unknown, not no). Trust the LLM
    # to find an explicit absence in the description text.
    if cfg.dishwasher_required:
        if l.dishwasher is True:
            dish: bool | None = True
        else:
            dish = e.dishwasher if e is not None else None
        if dish is False:
            hard.append("no dishwasher")
        elif dish is None:
            soft.append("dishwasher unclear")

    # ---- heating ------------------------------------------------------------
    if cfg.heating_allowed:
        canonical = canonicalize_heating(l.heating_type)
        if canonical is None and e is not None:
            canonical = canonicalize_heating(e.heating_type_confirmed)
        if canonical is None:
            soft.append("heating unclear")
        elif canonical not in cfg.heating_allowed:
            hard.append(f"heating={canonical} not in {list(cfg.heating_allowed)}")

    # ---- furnishing ---------------------------------------------------------
    # Structured field on 4zida/nekretnine/cityexpert; halooglasi never exposes
    # it (detail-page only). Fall back to the LLM-confirmed value so halooglasi
    # listings aren't permanently stuck at "furnishing unclear" near-miss.
    if cfg.furnishing_allowed:
        canonical = canonicalize_furnishing(l.furnished)
        if canonical is None and e is not None:
            canonical = canonicalize_furnishing(e.furnishing_confirmed)
        if canonical is None:
            soft.append("furnishing unclear")
        elif canonical not in cfg.furnishing_allowed:
            hard.append(f"furnishing={canonical} not in {list(cfg.furnishing_allowed)}")

    # ---- lease length (LLM-only — sources don't expose this) ---------------
    if cfg.max_lease_months and e is not None and e.max_lease_months is not None:
        if e.max_lease_months > cfg.max_lease_months:
            hard.append(f"min lease {e.max_lease_months}mo > {cfg.max_lease_months}mo")

    return hard, soft


# Source-specific terms → canonical values from spec §3.
_HEATING_MAP: dict[str, str] = {
    # 4zida raw values
    "district": "centralno",
    "central": "centralno",
    "gas": "etazno",
    "tapec": "TA",
    # nekretnine raw values
    "centralno": "centralno",
    "etazno": "etazno",
    "etažno": "etazno",
    "podno": "podno",
    "ta": "TA",
    "klima": "klima",
    "klimatizacija": "klima",
    "elektricno": "elektricni",
    "električno": "elektricni",
    "elektricni": "elektricni",
    "podno grejanje": "podno",
    "centralno grejanje": "centralno",
    "etažno grejanje": "etazno",
    "etazno grejanje": "etazno",
}


# Source-specific furnishing terms → canonical {furnished, semi-furnished, unfurnished}.
# Each source uses its own vocabulary: 4zida 'yes'/'no'/'semi', cityexpert
# 'yes'/'semi'/'no' (via _furnished_label), nekretnine Serbian labels from
# the "Opremljenost" field.
_FURNISHING_MAP: dict[str, str] = {
    # 4zida / cityexpert
    "yes": "furnished",
    "furnished": "furnished",
    "semi": "semi-furnished",
    "semi-furnished": "semi-furnished",
    "partially furnished": "semi-furnished",
    "no": "unfurnished",
    "empty": "unfurnished",
    "unfurnished": "unfurnished",
    # nekretnine Serbian labels (both with and without diacritics)
    "namešten": "furnished",
    "namesten": "furnished",
    "polunamešten": "semi-furnished",
    "polunamesten": "semi-furnished",
    "prazan": "unfurnished",
    "nenamešten": "unfurnished",
    "nenamesten": "unfurnished",
}


def canonicalize_furnishing(raw: str | None) -> str | None:
    """Map a source-specific furnishing string to the spec vocabulary.

    Returns one of {furnished, semi-furnished, unfurnished}, or None if the
    input is empty/unknown (caller routes None to near-miss).
    """
    if not raw:
        return None
    key = raw.strip().lower()
    if key in _FURNISHING_MAP:
        return _FURNISHING_MAP[key]
    if key in {"furnished", "semi-furnished", "unfurnished"}:
        return key
    return None


def canonicalize_heating(raw: str | None) -> str | None:
    """Normalize a source-specific heating term to the spec's vocabulary.

    Returns one of {centralno, etazno, podno, TA, klima, elektricni}, or None
    if the input is unknown / unmappable.
    """
    if not raw:
        return None
    key = raw.strip().lower()
    if key in _HEATING_MAP:
        return _HEATING_MAP[key]
    # Also accept inputs that are already canonical (case-insensitive).
    if key in {"centralno", "etazno", "podno", "ta", "klima", "elektricni"}:
        return "TA" if key == "ta" else key
    return None


def _resolve_pets(l: Listing, e) -> bool | None:
    """Resolve pets_allowed from the structured field, falling back to the LLM.

    Sources that expose pets as a tri-state (4zida, post-fix cityexpert) only
    surface a definitive value when the owner ticked yes or no on the listing
    form — None means "not specified" and we defer to the LLM. We trust the
    structured False as a hard signal so explicit "no pets" listings don't
    silently leak into matches or near-misses.
    """
    if l.pets_allowed is True:
        return True
    if l.pets_allowed is False:
        return False
    if e is None:
        return None
    if e.pets_allowed == "yes":
        return True
    if e.pets_allowed == "no":
        return False
    return None
