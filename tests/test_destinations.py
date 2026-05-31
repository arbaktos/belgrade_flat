from __future__ import annotations

import pytest

from src import destinations


_CFG = {
    "filters": {
        "commute": {
            "destinations": [
                {"name": "office", "lat_env": "T_OFFICE_LAT", "lng_env": "T_OFFICE_LNG",
                 "gates": True, "score_weight": 0.45},
                {"name": "Sadik Enter", "lat_env": "T_SADIK_LAT", "lng_env": "T_SADIK_LNG",
                 "gates": False, "score_weight": 0.25},
            ]
        }
    }
}


@pytest.fixture
def coords(monkeypatch):
    monkeypatch.setenv("T_OFFICE_LAT", "44.80058")
    monkeypatch.setenv("T_OFFICE_LNG", "20.45238")
    monkeypatch.setenv("T_SADIK_LAT", "44.80702")
    monkeypatch.setenv("T_SADIK_LNG", "20.46445")


def test_load_resolves_coords_and_flags(coords):
    ds = destinations.load(_CFG)
    assert [d.name for d in ds] == ["office", "Sadik Enter"]
    office, sadik = ds
    assert (office.lat, office.lng) == (pytest.approx(44.80058), pytest.approx(20.45238))
    assert office.gates is True and office.score_weight == 0.45
    assert sadik.gates is False and sadik.score_weight == 0.25


def test_gating_returns_only_gating_destinations(coords):
    ds = destinations.load(_CFG)
    assert [d.name for d in destinations.gating(ds)] == ["office"]


def test_load_skips_destination_with_missing_env(monkeypatch):
    # Only office coords present → Sadik is skipped, run degrades gracefully.
    monkeypatch.setenv("T_OFFICE_LAT", "44.80058")
    monkeypatch.setenv("T_OFFICE_LNG", "20.45238")
    monkeypatch.delenv("T_SADIK_LAT", raising=False)
    monkeypatch.delenv("T_SADIK_LNG", raising=False)
    ds = destinations.load(_CFG)
    assert [d.name for d in ds] == ["office"]


def test_load_skips_unparseable_coords(monkeypatch):
    monkeypatch.setenv("T_OFFICE_LAT", "not-a-number")
    monkeypatch.setenv("T_OFFICE_LNG", "20.45238")
    monkeypatch.setenv("T_SADIK_LAT", "44.80702")
    monkeypatch.setenv("T_SADIK_LNG", "20.46445")
    ds = destinations.load(_CFG)
    assert [d.name for d in ds] == ["Sadik Enter"]


def test_load_empty_when_no_config():
    assert destinations.load({}) == []
    assert destinations.load({"filters": {}}) == []
