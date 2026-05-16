from unittest.mock import patch

import pytest

from src.sources import halooglasi
from src.sources import _flaresolverr


def test_blocked_when_flaresolverr_unreachable():
    """If FlareSolverr is not running, we mark the source ⚠️ instead of crashing."""
    with patch.object(_flaresolverr, "is_available", return_value=False):
        with pytest.raises(halooglasi.SourceBlockedError):
            halooglasi.fetch(freshness_days=7)


def test_fetch_writes_debug_html_when_solver_ok(tmp_path, monkeypatch):
    """Phase 1 contract: when FlareSolverr returns HTML, we save it for inspection."""
    sample_html = "<html><body><div class='listing'>sample</div></body></html>"

    monkeypatch.setattr(halooglasi, "DEBUG_DIR", tmp_path)
    monkeypatch.setattr(halooglasi, "DEBUG_PATH", tmp_path / "halooglasi-latest.html")

    fake_session = _flaresolverr.Session(id="s1")
    with patch.object(_flaresolverr, "is_available", return_value=True), \
         patch.object(_flaresolverr, "create_session", return_value=fake_session), \
         patch.object(_flaresolverr, "destroy_session"), \
         patch.object(_flaresolverr, "get", return_value=sample_html) as mock_get:
        listings = halooglasi.fetch(freshness_days=7)

    mock_get.assert_called_once_with(halooglasi.LIST_URL, session=fake_session)
    assert listings == []  # phase 1: no parser yet
    assert (tmp_path / "halooglasi-latest.html").read_text() == sample_html
