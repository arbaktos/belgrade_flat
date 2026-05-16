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
    freshness_days: int
    heating_allowed: tuple[str, ...]
    dishwasher_required: bool
    pets_required: bool
    max_lease_months: int


@dataclass
class FilterResult:
    passed: list[Listing]
    rejected: list[tuple[Listing, str]]                       # (listing, first failing rule)
    near_misses: list[tuple[Listing, list[str]]] = field(default_factory=list)  # (listing, ambiguous fields)


def from_dict(cfg: dict) -> FilterConfig:
    f = cfg["filters"]
    return FilterConfig(
        price_eur_max=float(f["price_eur_max"]),
        rooms_min=float(f["rooms_min"]),
        rooms_max=float(f["rooms_max"]),
        surface_m2_min=float(f["surface_m2_min"]),
        elevator_required=bool(f["elevator_required"]),
        freshness_days=int(f["freshness_days"]),
        heating_allowed=tuple(f.get("heating_allowed", ())),
        dishwasher_required=bool(f.get("dishwasher_required", False)),
        pets_required=bool(f.get("pets_required", False)),
        max_lease_months=int(f.get("max_lease_months", 12)),
    )


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
    if l.floor is None or l.floor <= 0:
        return f"ground/basement floor ({l.floor})"
    # elevator=None means the source did not expose this field — near-miss not hard-reject
    if cfg.elevator_required and l.elevator is False:
        return "no elevator"
    if l.created_at < cutoff:
        return f"older than {cfg.freshness_days}d"
    return None


def _check_extraction(l: Listing, cfg: FilterConfig) -> tuple[list[str], list[str]]:
    """Apply the LLM-aware rules. Each source's structured data is preferred
    over the LLM's confirmation; the LLM is fallback only.

    Returns (hard_rejects, near_miss_reasons).
    """
    e = l.extraction
    hard: list[str] = []
    soft: list[str] = []

    # ---- pets ---------------------------------------------------------------
    if cfg.pets_required:
        pets = _resolve_pets(l, e)
        if pets is False:
            hard.append("pets not allowed")
        elif pets is None:
            soft.append("pets unclear")

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
    """Prefer structured Listing.pets_allowed when *positive*; otherwise consult the LLM.

    Sources expose pets as a boolean checkbox, and `False` in 4zida/cityexpert
    typically means "owner didn't tick yes" — not a definitive prohibition. We
    only trust structured True as a positive signal; for structured False we
    still let the LLM read the description text to find an explicit ban.
    """
    if l.pets_allowed is True:
        return True
    if e is None:
        return None
    if e.pets_allowed == "yes":
        return True
    if e.pets_allowed == "no":
        return False
    return None
