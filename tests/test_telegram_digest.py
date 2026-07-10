from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import patch

from src import telegram_digest
from src.destinations import Destination
from src.filter import FilterResult
from src.models import Extraction, Listing


OFFICE = Destination(name="office", lat=44.806, lng=20.460, gates=True, score_weight=0.45)
SADIK = Destination(name="Sadik Enter", lat=44.807, lng=20.464, gates=False, score_weight=0.25)
DESTS = [OFFICE, SADIK]


def _l(**over) -> Listing:
    commute = over.pop("commute", {"office": 18, "Sadik Enter": 26})
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
        commute=commute, lat=44.80, lng=20.47,
        extraction=Extraction(summary_en="Two-bedroom flat in Vračar.", pets_allowed="yes",
                              heating_type_confirmed="centralno"),
    )
    base.update(over)
    return Listing(**base)


def test_render_body_includes_listing_facts_and_summary():
    listing = _l()
    body = telegram_digest._render_body(
        listing, near_miss_reasons=None, notify_reason=None, destinations=DESTS,
    )
    assert "€890" in body
    assert "Vračar" in body
    assert "Krunska 35" in body
    assert "18 min to office" in body
    assert "26 min to Sadik Enter" in body
    assert "transit" not in body          # transit removed entirely
    assert "centralno" in body
    assert "🐾 pets OK" in body
    assert "Two-bedroom flat" in body                       # LLM summary


def test_render_body_shows_pets_unclear_when_unknown():
    # No structured pets data AND the LLM couldn't confirm → say so on the
    # card instead of omitting the fact, so the user knows to check manually.
    listing = _l(
        pets_allowed=None,
        extraction=Extraction(summary_en="s", pets_allowed="unknown"),
    )
    body = telegram_digest._render_body(listing, near_miss_reasons=None, notify_reason=None)
    assert "🐾❓ pets unclear" in body


def test_render_body_shows_pets_refused():
    listing = _l(
        pets_allowed=False,
        extraction=Extraction(summary_en="s", pets_allowed="no"),
    )
    body = telegram_digest._render_body(listing, near_miss_reasons=None, notify_reason=None)
    assert "🚫🐾" in body
    assert "pets unclear" not in body


def test_listing_keyboard_has_view_favorite_hide():
    kb = telegram_digest._listing_keyboard(_l())
    rows = kb["inline_keyboard"]
    # Row 1: View link on its own.
    assert rows[0][0]["url"] == "https://www.4zida.rs/abc"
    assert "View on 4zida" in rows[0][0]["text"]
    # Row 2: Favorite + Hide callbacks.
    assert rows[1][0]["callback_data"] == "fav:4zida:x"
    assert "Favorite" in rows[1][0]["text"]
    assert rows[1][1]["callback_data"] == "skip:4zida:x"
    assert "Hide" in rows[1][1]["text"]


def test_send_listing_text_fallback_has_keyboard_no_link_line():
    listing = _l(image_url=None)
    with patch("src.telegram_digest.telegram.send_photo") as photo_mock, \
         patch("src.telegram_digest.telegram.send_message") as msg_mock:
        telegram_digest._send_listing(
            listing, near_miss_reasons=None, notify_reason=None,
            destinations=DESTS,
        )
    photo_mock.assert_not_called()
    msg_mock.assert_called_once()
    body = msg_mock.call_args.args[0]
    assert "View on 4zida" not in body
    assert msg_mock.call_args.kwargs["reply_markup"]["inline_keyboard"][0][0]["url"] == listing.url


def test_send_listing_puts_keyboard_on_photo_not_followup():
    listing = _l()
    with patch("src.telegram_digest.telegram.send_photo") as photo_mock, \
         patch("src.telegram_digest.telegram.send_message") as msg_mock:
        telegram_digest._send_listing(
            listing, near_miss_reasons=None, notify_reason=None,
            destinations=DESTS,
        )
    photo_mock.assert_called_once()
    assert photo_mock.call_args.kwargs["reply_markup"] is not None
    assert photo_mock.call_args.kwargs["reply_markup"]["inline_keyboard"][0][0]["url"] == listing.url
    msg_mock.assert_not_called()


def test_translation_folded_into_photo_caption_no_followup():
    listing = _l(extraction=Extraction(
        summary_en="Two-bedroom flat in Vračar.", pets_allowed="yes",
        heating_type_confirmed="centralno",
        description_en="Fully furnished two-bedroom flat in central Vračar. Recent renovation, district heating, dishwasher, pet-friendly owner.",
    ))
    with patch("src.telegram_digest.telegram.send_photo") as photo_mock, \
         patch("src.telegram_digest.telegram.send_message") as msg_mock:
        telegram_digest._send_listing(
            listing, near_miss_reasons=None, notify_reason=None,
            destinations=DESTS,
        )
    # Single message: the photo card. No separate translation follow-up.
    photo_mock.assert_called_once()
    msg_mock.assert_not_called()
    caption = photo_mock.call_args.kwargs["caption"]
    assert "Fully furnished two-bedroom flat" in caption     # translation in caption
    assert photo_mock.call_args.kwargs["reply_markup"] is not None


def test_caption_prefers_translation_over_summary():
    # When both exist, the full translation wins; the short summary is dropped.
    listing = _l(extraction=Extraction(
        summary_en="Short summary.", pets_allowed="yes",
        description_en="The full translated description of the flat.",
    ))
    body = telegram_digest._render_body(
        listing, near_miss_reasons=None, notify_reason=None, destinations=DESTS,
    )
    assert "full translated description" in body
    assert "Short summary" not in body


def test_caption_falls_back_to_summary_when_no_translation():
    listing = _l(extraction=Extraction(
        summary_en="Two-bedroom flat in Vračar.", pets_allowed="yes",
        description_en=None,
    ))
    with patch("src.telegram_digest.telegram.send_photo") as photo_mock, \
         patch("src.telegram_digest.telegram.send_message") as msg_mock:
        telegram_digest._send_listing(
            listing, near_miss_reasons=None, notify_reason=None,
            destinations=DESTS,
        )
    photo_mock.assert_called_once()
    msg_mock.assert_not_called()
    assert "Two-bedroom flat in Vračar" in photo_mock.call_args.kwargs["caption"]


def test_text_fallback_card_includes_translation_in_one_message():
    listing = _l(image_url=None, extraction=Extraction(
        summary_en="Compact studio.", pets_allowed="yes",
        description_en="Studio apartment, 30 m², close to Trg Slavija. Furnished.",
    ))
    with patch("src.telegram_digest.telegram.send_photo") as photo_mock, \
         patch("src.telegram_digest.telegram.send_message") as msg_mock:
        telegram_digest._send_listing(
            listing, near_miss_reasons=None, notify_reason=None,
            destinations=DESTS,
        )
    photo_mock.assert_not_called()
    # One message total — card + translation together, with the keyboard.
    msg_mock.assert_called_once()
    card_text = msg_mock.call_args.args[0]
    assert "Studio apartment" in card_text
    assert "reply_markup" in msg_mock.call_args.kwargs


def test_overlong_translation_clipped_to_caption_budget():
    huge = "Detalj o stanu " * 1000     # ~16k chars
    listing = _l(extraction=Extraction(
        summary_en="x", pets_allowed="yes", description_en=huge,
    ))
    body = telegram_digest._render_body(
        listing, near_miss_reasons=None, notify_reason=None, destinations=DESTS,
    )
    # Caption must fit Telegram's 1024-char photo-caption limit.
    assert len(body) <= 1024
    assert "…" in body


def test_render_body_links_address_and_walk_per_destination():
    body = telegram_digest._render_body(
        _l(), near_miss_reasons=None, notify_reason=None,
        destinations=DESTS,
    )
    # Address text wrapped in <a href> to Maps
    assert "google.com/maps?q=44.8,20.47" in body
    # Each destination gets a walking-directions link to its own coords
    assert "travelmode=walking" in body
    assert "18 min to office" in body
    assert "26 min to Sadik Enter" in body
    assert "destination=44.806,20.46" in body       # office coords
    assert "destination=44.807,20.464" in body      # Sadik coords
    # Transit is gone.
    assert "travelmode=transit" not in body


def test_render_body_walk_line_shows_none_destinations_gracefully():
    # A gating destination with no route shows the fallback, not a crash.
    body = telegram_digest._render_body(
        _l(commute={"office": None, "Sadik Enter": None}),
        near_miss_reasons=None, notify_reason=None, destinations=DESTS,
    )
    assert "no walking data" in body


def test_render_body_near_miss_marks_unconfirmed():
    body = telegram_digest._render_body(
        _l(), near_miss_reasons=["pets unclear"], notify_reason=None,
    )
    assert body.startswith("⚠️")
    assert "Unconfirmed: pets unclear" in body


def test_render_body_price_drop_badge():
    body = telegram_digest._render_body(
        _l(), near_miss_reasons=None, notify_reason="price_drop",
    )
    assert "📉 price drop" in body


def test_byte_clip_handles_emoji():
    """Don't truncate mid-byte on UTF-8 emoji (each is 4 bytes)."""
    s = "x" * 100 + "🏠" * 50
    clipped = telegram_digest._byte_clip(s, max_bytes=200)
    # Should be valid UTF-8 and within budget.
    assert clipped.encode("utf-8")
    assert len(clipped.encode("utf-8")) <= 200


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
            destinations=DESTS,
        )
    assert any("Nothing new today" in t for t in sent_texts)
    photo_mock.assert_not_called()


def test_overflow_line_when_too_many_matches():
    """Beyond MAX_PERFECT, we send an overflow note pointing to the archive."""
    sent_texts: list[str] = []
    sent_photos: list[str] = []
    listings = [_l(id=str(i)) for i in range(12)]
    with patch("src.telegram_digest.telegram.send_message", side_effect=lambda txt, **_: sent_texts.append(txt)), \
         patch("src.telegram_digest.telegram.send_photo", side_effect=lambda url, **_: sent_photos.append(url)):
        telegram_digest.send(
            FilterResult(passed=listings, near_misses=[], rejected=[]),
            today=datetime(2026, 5, 16, tzinfo=timezone.utc),
            source_stats={"4zida": (12, None)},
            api_count=0, dedup_stats={}, notify_reasons={},
            commute_config_error=None,
            state_size_bytes=1024, listings_tracked=12,
            digest_path="digests/2026-05-16.md",
            destinations=DESTS,
        )
    assert len(sent_photos) == telegram_digest.MAX_PERFECT
    assert any("+2 more matches" in t for t in sent_texts)


def test_instant_push_is_silent_when_no_fresh_matches():
    sent_texts: list[str] = []
    sent_photos: list[str] = []
    with patch("src.telegram_digest.telegram.send_message", side_effect=lambda txt, **_: sent_texts.append(txt)), \
         patch("src.telegram_digest.telegram.send_photo", side_effect=lambda url, **_: sent_photos.append(url)):
        telegram_digest.send_instant_push(
            [], notify_reasons={},
            destinations=DESTS,
        )
    assert sent_texts == [] and sent_photos == []


def test_instant_push_sends_header_then_one_card_per_fresh_match():
    sent_texts: list[str] = []
    sent_photos: list[str] = []
    fresh = [_l(id="a"), _l(id="b")]
    with patch("src.telegram_digest.telegram.send_message", side_effect=lambda txt, **_: sent_texts.append(txt)), \
         patch("src.telegram_digest.telegram.send_photo", side_effect=lambda url, **_: sent_photos.append(url)):
        telegram_digest.send_instant_push(
            fresh, notify_reasons={},
            destinations=DESTS,
        )
    # Exactly one header text, plus one photo card per listing.
    assert len(sent_texts) == 1
    assert "2 new perfect matches" in sent_texts[0]
    assert len(sent_photos) == 2


def test_instant_push_sends_near_misses_with_header_split():
    sent_texts: list[str] = []
    sent_photos: list[str] = []
    matches = [_l(id="m1")]
    near = [(_l(id="n1"), ["pets unknown"]), (_l(id="n2"), ["dishwasher unknown"])]
    with patch("src.telegram_digest.telegram.send_message", side_effect=lambda txt, **_: sent_texts.append(txt)), \
         patch("src.telegram_digest.telegram.send_photo", side_effect=lambda url, **_: sent_photos.append(url)):
        telegram_digest.send_instant_push(
            matches, fresh_near_misses=near, notify_reasons={},
            destinations=DESTS,
        )
    assert len(sent_texts) == 1
    header = sent_texts[0]
    assert "1 new perfect match" in header
    assert "2 near-misses" in header
    # One photo card per listing: 1 match + 2 near-misses.
    assert len(sent_photos) == 3


def test_instant_push_sends_only_near_misses_when_no_matches():
    sent_texts: list[str] = []
    sent_photos: list[str] = []
    near = [(_l(id="n1"), ["pets unknown"])]
    with patch("src.telegram_digest.telegram.send_message", side_effect=lambda txt, **_: sent_texts.append(txt)), \
         patch("src.telegram_digest.telegram.send_photo", side_effect=lambda url, **_: sent_photos.append(url)):
        telegram_digest.send_instant_push(
            [], fresh_near_misses=near, notify_reasons={},
            destinations=DESTS,
        )
    assert len(sent_texts) == 1
    header = sent_texts[0]
    assert "perfect match" not in header
    assert "1 near-miss" in header
    assert len(sent_photos) == 1
