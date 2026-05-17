from __future__ import annotations

from pathlib import Path

from src.models import WinterSmog
from src.winter_smog import (
    BAND_BETTER,
    BAND_WORSE,
    format_digest_line,
    format_smog_warning,
    load_grid,
    lookup,
)

FIXTURE = Path(__file__).parent / "fixtures" / "belgrade_winter_smog_mini.json"


def test_lookup_picks_nearest_cell_band_and_warning():
    grid = load_grid(FIXTURE)
    low = lookup(44.801, 20.451, grid)
    high = lookup(44.799, 20.549, grid)
    assert low.band == BAND_BETTER
    assert high.band == BAND_WORSE
    assert low.pm25_best == 12.0
    assert high.pm25_worse == 85.0
    assert not low.smog_warning
    assert high.smog_warning


def test_format_digest_line_includes_best_and_worse():
    smog = WinterSmog(
        band=BAND_WORSE,
        pm25_winter_mean=55.0,
        pm25_best=30.0,
        pm25_worse=85.0,
        smog_warning=True,
        motorway_m=200.0,
        score=0.85,
        cell_lat=44.8,
        cell_lng=20.55,
    )
    line = format_digest_line(smog)
    assert "best ≈ 30" in line
    assert "worse days ≈ 85" in line


def test_format_smog_warning_only_for_worst_third():
    warned = WinterSmog(
        band=BAND_WORSE, pm25_winter_mean=55.0, pm25_best=30.0, pm25_worse=85.0,
        smog_warning=True, motorway_m=None, score=0.85, cell_lat=44.8, cell_lng=20.55,
    )
    ok = WinterSmog(
        band=BAND_BETTER, pm25_winter_mean=25.0, pm25_best=12.0, pm25_worse=45.0,
        smog_warning=False, motorway_m=None, score=0.2, cell_lat=44.8, cell_lng=20.45,
    )
    assert format_smog_warning(warned) is not None
    assert "worst third" in format_smog_warning(warned)
    assert format_smog_warning(ok) is None
