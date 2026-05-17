#!/usr/bin/env python3
"""Build data/belgrade_winter_smog.json from Open-Meteo + OSM Overpass.

Run locally or in CI when refreshing the winter map (once per year is enough):

    .venv/bin/python scripts/build_winter_smog_grid.py
"""
from __future__ import annotations

import json
import math
import sys
import time
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
OUT_PATH = ROOT / "data" / "belgrade_winter_smog.json"

LAT_MIN, LAT_MAX = 44.70, 44.92
LNG_MIN, LNG_MAX = 20.28, 20.68
STEP = 0.05

WINTER_START = "2019-12-01"
WINTER_END = "2025-02-28"
WINTER_MONTHS = {12, 1, 2}

OPEN_METEO_URL = "https://air-quality-api.open-meteo.com/v1/air-quality"
OVERPASS_URL = "https://overpass.kumi.systems/api/interpreter"
REQUEST_PAUSE_S = 1.2
MAX_RETRIES = 5


def _cell_centers() -> list[tuple[float, float]]:
    centers: list[tuple[float, float]] = []
    lat = LAT_MIN
    while lat <= LAT_MAX + 1e-9:
        lng = LNG_MIN
        while lng <= LNG_MAX + 1e-9:
            centers.append((round(lat, 2), round(lng, 2)))
            lng += STEP
        lat += STEP
    return centers


def _haversine_m(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    r = 6371000.0
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lng2 - lng1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2
    return r * 2 * math.asin(math.sqrt(a))


def _percentile(sorted_vals: list[float], pct: float) -> float:
    if not sorted_vals:
        raise ValueError("empty values")
    if len(sorted_vals) == 1:
        return sorted_vals[0]
    k = (len(sorted_vals) - 1) * pct / 100.0
    lo = int(math.floor(k))
    hi = int(math.ceil(k))
    if lo == hi:
        return sorted_vals[lo]
    return sorted_vals[lo] + (sorted_vals[hi] - sorted_vals[lo]) * (k - lo)


def _winter_pm25_stats(hourly: dict) -> tuple[float, float, float]:
    """Return (mean, P10 best, P90 worse) for Dec–Feb hours."""
    times = hourly["time"]
    pm = hourly["pm2_5"]
    vals = sorted(
        v for t, v in zip(times, pm)
        if v is not None and int(t[5:7]) in WINTER_MONTHS
    )
    if not vals:
        raise ValueError("no winter PM2.5 values in response")
    mean = sum(vals) / len(vals)
    return mean, _percentile(vals, 10), _percentile(vals, 90)


def fetch_pm25(centers: list[tuple[float, float]], client: httpx.Client) -> list[tuple[float, float, float]]:
    """One coordinate per request — avoids Open-Meteo 429 on large batch windows."""
    stats: list[tuple[float, float, float]] = []
    for i, (lat, lng) in enumerate(centers, start=1):
        for attempt in range(MAX_RETRIES):
            resp = client.get(
                OPEN_METEO_URL,
                params={
                    "latitude": lat,
                    "longitude": lng,
                    "hourly": "pm2_5",
                    "start_date": WINTER_START,
                    "end_date": WINTER_END,
                    "timezone": "Europe/Belgrade",
                },
                timeout=120.0,
            )
            if resp.status_code == 429:
                wait = 15 * (attempt + 1)
                print(f"  open-meteo 429 at {lat},{lng} — sleeping {wait}s", flush=True)
                time.sleep(wait)
                continue
            resp.raise_for_status()
            stats.append(_winter_pm25_stats(resp.json()["hourly"]))
            break
        else:
            raise RuntimeError(f"Open-Meteo rate-limited after {MAX_RETRIES} retries at {lat},{lng}")
        if i % 5 == 0 or i == len(centers):
            print(f"  open-meteo: {i}/{len(centers)} cells", flush=True)
        time.sleep(REQUEST_PAUSE_S)
    return stats


def fetch_highway_points(client: httpx.Client) -> list[tuple[float, float]]:
    query = f"""
    [out:json][timeout:90];
    way["highway"~"motorway|trunk"]({LAT_MIN},{LNG_MIN},{LAT_MAX},{LNG_MAX});
    out center;
    """
    resp = client.post(
        OVERPASS_URL,
        content=query.encode("utf-8"),
        headers={"Content-Type": "text/plain; charset=utf-8", "Accept": "application/json"},
        timeout=120.0,
    )
    resp.raise_for_status()
    data = resp.json()
    points: list[tuple[float, float]] = []
    for el in data.get("elements", []):
        if el.get("type") != "way":
            continue
        center = el.get("center")
        if center:
            points.append((float(center["lat"]), float(center["lon"])))
    print(f"  overpass: {len(points)} motorway/trunk way centers", flush=True)
    return points


def min_motorway_distance(lat: float, lng: float, highway_pts: list[tuple[float, float]]) -> float | None:
    if not highway_pts:
        return None
    return min(_haversine_m(lat, lng, hlat, hlng) for hlat, hlng in highway_pts)


def _normalize(values: list[float]) -> list[float]:
    lo, hi = min(values), max(values)
    if hi <= lo:
        return [0.5] * len(values)
    return [(v - lo) / (hi - lo) for v in values]


def main() -> int:
    centers = _cell_centers()
    print(f"Building {len(centers)} grid cells (step={STEP}°)…", flush=True)

    with httpx.Client() as client:
        pm25_stats = fetch_pm25(centers, client)
        try:
            highway_pts = fetch_highway_points(client)
        except Exception as e:  # noqa: BLE001 — offline build should still succeed on PM2.5 alone
            print(f"  overpass failed ({e}); scoring PM2.5 only", flush=True)
            highway_pts = []

    pm25_means = [s[0] for s in pm25_stats]
    pm25_norm = _normalize(pm25_means)
    motorway_ms = [min_motorway_distance(lat, lng, highway_pts) for lat, lng in centers]
    # Closer motorway → higher score; cap influence at 2 km.
    motorway_norm = [
        max(0.0, 1.0 - (d / 2000.0)) if d is not None else 0.0
        for d in motorway_ms
    ]

    scores = [0.7 * p + 0.3 * m for p, m in zip(pm25_norm, motorway_norm)]
    sorted_scores = sorted(scores)
    n = len(sorted_scores)
    band_lo = sorted_scores[max(0, n // 3 - 1)]
    band_hi = sorted_scores[min(n - 1, (2 * n) // 3)]

    worse_vals = [round(s[2], 1) for s in pm25_stats]
    sorted_worse = sorted(worse_vals)
    n = len(sorted_worse)
    worse_warning_min = sorted_worse[(2 * n) // 3] if n else 0.0

    cells = [
        {
            "lat": lat,
            "lng": lng,
            "pm25_winter_mean": round(mean, 1),
            "pm25_best": round(best, 1),
            "pm25_worse": round(worse, 1),
            "motorway_m": round(mway, 0) if mway is not None else None,
            "score": round(sc, 4),
        }
        for (lat, lng), (mean, best, worse), mway, sc in zip(
            centers, pm25_stats, motorway_ms, scores
        )
    ]

    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    doc = {
        "version": 2,
        "bbox": {"lat_min": LAT_MIN, "lat_max": LAT_MAX, "lng_min": LNG_MIN, "lng_max": LNG_MAX},
        "step": STEP,
        "sources": ["open-meteo-cams-reanalysis", "osm-overpass-motorway-trunk"],
        "winter_window": {"start": WINTER_START, "end": WINTER_END, "months": [12, 1, 2]},
        "bands": {"better_max": band_lo, "worse_min": band_hi},
        "worse_warning_min": worse_warning_min,
        "cells": cells,
    }
    OUT_PATH.write_text(json.dumps(doc, indent=2) + "\n")
    print(f"Wrote {OUT_PATH} ({len(cells)} cells)", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
