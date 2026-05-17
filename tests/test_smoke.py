import pathlib

import yaml


def test_config_loads():
    cfg = yaml.safe_load(pathlib.Path("config.yaml").read_text())
    assert cfg["filters"]["price_eur_max"] == 1100
    assert cfg["filters"]["surface_m2_min"] == 55
    assert "centralno" in cfg["filters"]["heating_allowed"]


def test_imports():
    from src import digest, filter, main, sources, state, telegram  # noqa: F401
    from src.sources import four_zida  # noqa: F401


def test_schema_init(tmp_path, monkeypatch):
    from src import state

    monkeypatch.setattr(state, "LOCAL_DB", tmp_path / "db.sqlite")
    conn = state.ensure_schema()
    row = conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
    assert row == (str(state.SCHEMA_VERSION),)
    # listings table exists and has expected columns
    cols = {r[1] for r in conn.execute("PRAGMA table_info(listings)")}
    assert {"fingerprint_key", "price_eur", "m2", "rooms", "elevator"} <= cols
    conn.close()
