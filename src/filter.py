from __future__ import annotations

from dataclasses import dataclass
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


@dataclass
class FilterResult:
    passed: list[Listing]
    rejected: list[tuple[Listing, str]]   # (listing, first failing rule)


def from_dict(cfg: dict) -> FilterConfig:
    f = cfg["filters"]
    return FilterConfig(
        price_eur_max=float(f["price_eur_max"]),
        rooms_min=float(f["rooms_min"]),
        rooms_max=float(f["rooms_max"]),
        surface_m2_min=float(f["surface_m2_min"]),
        elevator_required=bool(f["elevator_required"]),
        freshness_days=int(f["freshness_days"]),
    )


def apply(listings: list[Listing], cfg: FilterConfig, *, now: datetime | None = None) -> FilterResult:
    now = now or datetime.now(timezone.utc)
    cutoff = now - timedelta(days=cfg.freshness_days)
    passed: list[Listing] = []
    rejected: list[tuple[Listing, str]] = []
    for l in listings:
        reason = _check(l, cfg, cutoff)
        if reason is None:
            passed.append(l)
        else:
            rejected.append((l, reason))
    return FilterResult(passed=passed, rejected=rejected)


def _check(l: Listing, cfg: FilterConfig, cutoff: datetime) -> str | None:
    if l.price_eur <= 0 or l.price_eur > cfg.price_eur_max:
        return f"price {l.price_eur}€ > cap {cfg.price_eur_max}€"
    if l.rooms < cfg.rooms_min or l.rooms > cfg.rooms_max:
        return f"rooms {l.rooms} outside [{cfg.rooms_min}, {cfg.rooms_max}]"
    if l.m2 < cfg.surface_m2_min:
        return f"surface {l.m2}m² < {cfg.surface_m2_min}m²"
    if l.floor is None or l.floor <= 0:
        return f"ground/basement floor ({l.floor})"
    # elevator=None means "data unavailable for this source" — treat as near-miss
    # rather than a hard rejection (spec §3 pattern for ambiguous fields).
    if cfg.elevator_required and l.elevator is False:
        return "no elevator"
    if l.created_at < cutoff:
        return f"older than {cfg.freshness_days}d"
    return None
