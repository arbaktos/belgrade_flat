from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import patch

from src import telegram_digest
from src.destinations import Destination
from src.filter import FilterResult
from src.models import Extraction, Listing, WinterSmog


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


def test_render_body_includes_winter_smog_line():
    smog = WinterSmog(
        band="moderate", pm25_winter_mean=40.0, pm25_best=20.0, pm25_worse=65.0,
        smog_warning=False, motorway_m=None, score=0.5, cell_lat=44.8, cell_lng=20.5,
    )
    body = telegram_digest._render_body(_l(winter_smog=smog), near_miss_reasons=None, notify_reason=None)
    assert "Winter smog" in body
    assert "best ≈ 20" in body
    assert "worse days ≈ 65" in body


def test_render_body_omits_smog_warning_line():
    # The "worst third" warning fired on ~87% of central listings, so it
    # stopped being a signal. We still render the neutral smog data line.
    smog = WinterSmog(
        band="worse", pm25_winter_mean=55.0, pm25_best=30.0, pm25_worse=90.0,
        smog_warning=True, motorway_m=None, score=0.9, cell_lat=44.8, cell_lng=20.5,
    )
    body = telegram_digest._render_body(_l(winter_smog=smog), near_miss_reasons=None, notify_reason=None)
    assert "Winter smog warning" not in body
    assert "worst third" not in body
    # Sanity: the neutral data line is still present.
    assert "Winter smog" in body
    assert "worse days ≈ 90" in body


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


def test_send_listing_sends_translation_followup_after_photo():
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
    photo_mock.assert_called_once()
    msg_mock.assert_called_once()
    followup = msg_mock.call_args.args[0]
    assert "Fully furnished two-bedroom flat" in followup
    assert followup.startswith("<i>") and followup.endswith("</i>")
    # Translation must not steal focus — already-delivered card has the alert.
    assert msg_mock.call_args.kwargs.get("disable_notification") is True
    # No inline keyboard on the follow-up; buttons belong on the card.
    assert "reply_markup" not in msg_mock.call_args.kwargs


def test_send_listing_skips_translation_when_no_description_en():
    listing = _l()      # default fixture has description_en=None
    with patch("src.telegram_digest.telegram.send_photo") as photo_mock, \
         patch("src.telegram_digest.telegram.send_message") as msg_mock:
        telegram_digest._send_listing(
            listing, near_miss_reasons=None, notify_reason=None,
            destinations=DESTS,
        )
    photo_mock.assert_called_once()
    msg_mock.assert_not_called()


def test_send_listing_translation_after_text_fallback():
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
    # First call = card (text fallback), second call = translation follow-up.
    assert msg_mock.call_count == 2
    card_text = msg_mock.call_args_list[0].args[0]
    followup_text = msg_mock.call_args_list[1].args[0]
    assert "Studio apartment" in followup_text
    assert "Studio apartment" not in card_text
    # Card has keyboard; follow-up does not.
    assert "reply_markup" in msg_mock.call_args_list[0].kwargs
    assert "reply_markup" not in msg_mock.call_args_list[1].kwargs


def test_send_listing_clips_overlong_translation():
    huge = "Detalj o stanu " * 1000     # ~16k chars
    listing = _l(extraction=Extraction(
        summary_en="x", pets_allowed="yes", description_en=huge,
    ))
    with patch("src.telegram_digest.telegram.send_photo"), \
         patch("src.telegram_digest.telegram.send_message") as msg_mock:
        telegram_digest._send_listing(
            listing, near_miss_reasons=None, notify_reason=None,
            destinations=DESTS,
        )
    followup = msg_mock.call_args.args[0]
    # Telegram sendMessage hard limit is 4096; we leave headroom for the <i> wrapper.
    assert len(followup) < 4096
    assert followup.endswith("…</i>")


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
