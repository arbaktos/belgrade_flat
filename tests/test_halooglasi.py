import pytest

from src.sources import halooglasi


def test_halooglasi_is_blocked():
    """Documented stub: halooglasi raises until Playwright fallback is wired."""
    with pytest.raises(halooglasi.SourceBlockedError):
        halooglasi.fetch(freshness_days=7)
