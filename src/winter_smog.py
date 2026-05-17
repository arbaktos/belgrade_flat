"""Winter smog exposure from a pre-built Belgrade grid (annotate only, never filter).

The grid is built offline from Open-Meteo CAMS reanalysis (winter PM2.5) and
OSM motorway/trunk proximity. See scripts/build_winter_smog_grid.py.
"""
from __future__ import annotations

import json
import logging
import math
import sqlite3
from dataclasses import dataclass
from pathlib import Path

from src import geocode
from src.models import Listing, WinterSmog

log = logging.getLogger(__name__)

DEFAULT_GRID_PATH = Path(__file__).resolve().parent.parent / "data" / "belgrade_winter_smog.json"

BAND_BETTER = "better"
BAND_MODERATE = "moderate"
BAND_WORSE = "worse"


@dataclass(frozen=True)
class _Cell:
    lat: float
    lng: float
    pm25_winter_mean: float
    pm25_best: float
    pm25_worse: float
    motorway_m: float | None
    score: float


@dataclass
class _Grid:
    cells: list[_Cell]
    band_lo: float               # score <= band_lo → better
    band_hi: float               # score >= band_hi → worse
    worse_warning_min: float     # pm25_worse >= this → worst third of city


def _haversine_m(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    r = 6371000.0
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lng2 - lng1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2
    return r * 2 * math.asin(math.sqrt(a))


def _cell_pm25_stats(c: dict) -> tuple[float, float, float]:
    mean = float(c["pm25_winter_mean"])
    best = float(c["pm25_best"]) if "pm25_best" in c else mean * 0.55
    worse = float(c["pm25_worse"]) if "pm25_worse" in c else mean * 1.50
    return mean, best, worse


def _worse_warning_threshold(cells: list[_Cell], raw: dict) -> float:
    if "worse_warning_min" in raw:
        return float(raw["worse_warning_min"])
    worse_vals = sorted(c.pm25_worse for c in cells)
    n = len(worse_vals)
    return worse_vals[(2 * n) // 3] if n else 0.0


def load_grid(path: Path | None = None) -> _Grid:
    path = path or DEFAULT_GRID_PATH
    raw = json.loads(path.read_text())
    cells: list[_Cell] = []
    for c in raw["cells"]:
        mean, best, worse = _cell_pm25_stats(c)
        cells.append(
            _Cell(
                lat=c["lat"],
                lng=c["lng"],
                pm25_winter_mean=mean,
                pm25_best=best,
                pm25_worse=worse,
                motorway_m=float(c["motorway_m"]) if c.get("motorway_m") is not None else None,
                score=float(c["score"]),
            )
        )
    bands = raw.get("bands", {})
    return _Grid(
        cells=cells,
        band_lo=float(bands.get("better_max", 0.33)),
        band_hi=float(bands.get("worse_min", 0.67)),
        worse_warning_min=_worse_warning_threshold(cells, raw),
    )


def lookup(lat: float, lng: float, grid: _Grid) -> WinterSmog:
    nearest = min(grid.cells, key=lambda c: _haversine_m(lat, lng, c.lat, c.lng))
    if nearest.score <= grid.band_lo:
        band = BAND_BETTER
    elif nearest.score >= grid.band_hi:
        band = BAND_WORSE
    else:
        band = BAND_MODERATE
    smog_warning = nearest.pm25_worse >= grid.worse_warning_min
    return WinterSmog(
        band=band,
        pm25_winter_mean=nearest.pm25_winter_mean,
        pm25_best=nearest.pm25_best,
        pm25_worse=nearest.pm25_worse,
        smog_warning=smog_warning,
        motorway_m=nearest.motorway_m,
        score=nearest.score,
        cell_lat=nearest.lat,
        cell_lng=nearest.lng,
    )


def format_digest_line(smog: WinterSmog) -> str:
    """Single-line Telegram/markdown annotation (plain text, escaped by caller)."""
    band_label = {
        BAND_BETTER: "relatively better for Belgrade",
        BAND_MODERATE: "moderate for Belgrade",
        BAND_WORSE: "often worse in winter",
    }[smog.band]
    parts = [
        f"🌫️ Winter smog: {band_label}",
        f"best ≈ {smog.pm25_best:.0f}",
        f"typical ≈ {smog.pm25_winter_mean:.0f}",
        f"worse days ≈ {smog.pm25_worse:.0f} µg/m³",
    ]
    if smog.motorway_m is not None and smog.motorway_m < 800:
        parts.append(f"near motorway ({int(smog.motorway_m)} m)")
    return " · ".join(parts)


def format_smog_warning(smog: WinterSmog) -> str | None:
    """Extra Telegram line when the cell is in the city's worst third for bad-air days."""
    if not smog.smog_warning:
        return None
    return (
        "⚠️ Winter smog warning: this area is in Belgrade's worst third for "
        f"bad-air days (P90 ≈ {smog.pm25_worse:.0f} µg/m³)"
    )


def enrich_listing(
    listing: Listing,
    coords: tuple[float, float],
    grid: _Grid,
) -> None:
    listing.winter_smog = lookup(coords[0], coords[1], grid)


def enrich_many(
    listings: list[Listing],
    conn: sqlite3.Connection,
    *,
    grid_path: Path | None = None,
) -> int:
    """Attach winter_smog to listings we can geolocate. Returns count enriched."""
    path = grid_path or DEFAULT_GRID_PATH
    if not path.exists():
        log.warning("winter_smog: grid file missing at %s — skipping", path)
        return 0

    grid = load_grid(path)
    enriched = 0
    for listing in listings:
        coords: tuple[float, float] | None
        if listing.lat is not None and listing.lng is not None:
            coords = listing.lat, listing.lng
        else:
            coords = geocode.resolve(listing, conn)
        if coords is None:
            continue
        enrich_listing(listing, coords, grid)
        enriched += 1
    log.info("winter_smog: enriched %d/%d listings", enriched, len(listings))
    return enriched
