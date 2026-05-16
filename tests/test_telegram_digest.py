from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import patch

from src import telegram_digest
from src.filter import FilterResult
from src.models import Extraction, Listing


OFFICE_LAT = 44.806
OFFICE_LNG = 20.460


def _l(**over) -> Listing:
    base = dict(
        id="x", source="4zida", url="https://www.4zida.rs/abc",
        price_eur=890, m2=62, rooms=2.0,
        floor=3, total_floors=5, last_floor=False, elevator=True,
        furnished="yes", heating_type="centralno", pets_allowed=True,
        title="Krunska 35", description="...",
        address="Krunska 35", place_names=["Vračar"],
        image_url="https://example.com/p.jpg",
        is_agency=True,
        created_at=datetime.now(timezone.utc),
        walk_min=18, transit_min=22, lat=44.80, lng=20.47,
        extraction=Extraction(summary_en="Two-bedroom flat in Vračar.", pets_allowed="yes",
                              heating_type_confirmed="centralno"),
    )
    base.update(over)
    return Listing(**base)


def test_render_listing_includes_all_spec_fields():
    listing = _l()
    body = telegram_digest._render_listing(
        listing, near_miss_reasons=None, notify_reason=None,
        office_lat=OFFICE_LAT, office_lng=OFFICE_LNG,
    )
    assert "€890" in body
    assert "Vračar" in body
    assert "Krunska 35" in body
    assert "18 min walk" in body
    assert "22 min transit" in body
    assert "centralno" in body
    assert "🐾 pets OK" in body
    assert "Two-bedroom flat" in body                       # LLM summary
    assert "https://www.4zida.rs/abc" in body                # portal link
    assert "google.com/maps?q=44.8,20.47" in body            # map link
    assert "travelmode=walking" in body
    assert "travelmode=transit" in body


def test_render_listing_near_miss_marks_unconfirmed():
    body = telegram_digest._render_listing(
        _l(), near_miss_reasons=["pets unclear"], notify_reason=None,
        office_lat=OFFICE_LAT, office_lng=OFFICE_LNG,
    )
    assert body.startswith("⚠️")
    assert "Unconfirmed: pets unclear" in body


def test_render_listing_price_drop_badge():
    body = telegram_digest._render_listing(
        _l(), near_miss_reasons=None, notify_reason="price_drop",
        office_lat=OFFICE_LAT, office_lng=OFFICE_LNG,
    )
    assert "📉 price drop" in body


def test_render_header_basic():
    out = telegram_digest._render_header(
        today=datetime(2026, 5, 16, tzinfo=timezone.utc),
        perfect=3, near=5,
        source_stats={"4zida": (47, None), "halooglasi": (0, "blocked")},
        api_count=47, dedup_stats={"clusters": 4, "suppressed": 1},
        commute_config_error=None, state_size_bytes=312_000, listings_tracked=1847,
        digest_path="digests/2026-05-16.md",
    )
    assert "Belgrade rentals — 2026-05-16" in out
    assert "3 matches · 5 near-misses" in out
    assert "4zida 47" in out and "halooglasi 0 ⚠️" in out
    assert "47/40 000" in out
    assert "🪞 Dedup: 4 cluster" in out
    assert "304 KB" in out                       # 312_000 // 1024
    assert "1847 flats tracked" in out


def test_empty_day_sends_all_systems_green():
    """0 matches + 0 near-misses → header + 'Nothing new today' message."""
    sent_texts: list[str] = []
    with patch("src.telegram_digest.telegram.send_message", side_effect=lambda txt, **_: sent_texts.append(txt)), \
         patch("src.telegram_digest.telegram.send_photo") as photo_mock:
        telegram_digest.send(
            FilterResult(passed=[], near_misses=[], rejected=[]),
            today=datetime(2026, 5, 16, tzinfo=timezone.utc),
            source_stats={"4zida": (0, None)},
            api_count=0, dedup_stats={}, notify_reasons={},
            commute_config_error=None,
            state_size_bytes=1024, listings_tracked=0,
            digest_path="digests/2026-05-16.md",
            office_lat=OFFICE_LAT, office_lng=OFFICE_LNG,
        )
    assert any("Nothing new today" in t for t in sent_texts)
    photo_mock.assert_not_called()


def test_overflow_line_when_too_many_matches():
    """Beyond MAX_PERFECT, we send an overflow note pointing to the archive."""
    sent_texts: list[str] = []
    sent_photos: list[tuple[str, str]] = []
    listings = [_l(id=str(i)) for i in range(12)]
    with patch("src.telegram_digest.telegram.send_message", side_effect=lambda txt, **_: sent_texts.append(txt)), \
         patch("src.telegram_digest.telegram.send_photo", side_effect=lambda url, caption=None, **_: sent_photos.append((url, caption))):
        telegram_digest.send(
            FilterResult(passed=listings, near_misses=[], rejected=[]),
            today=datetime(2026, 5, 16, tzinfo=timezone.utc),
            source_stats={"4zida": (12, None)},
            api_count=0, dedup_stats={}, notify_reasons={},
            commute_config_error=None,
            state_size_bytes=1024, listings_tracked=12,
            digest_path="digests/2026-05-16.md",
            office_lat=OFFICE_LAT, office_lng=OFFICE_LNG,
        )
    assert len(sent_photos) == telegram_digest.MAX_PERFECT
    assert any("+2 more matches" in t for t in sent_texts)
