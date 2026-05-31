from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from src import extract
from src.models import Listing


def _listing(description: str = "Dvosoban stan, kućni ljubimci dozvoljeni") -> Listing:
    return Listing(
        id="x", source="4zida", url="https://x", price_eur=900, m2=60, rooms=2.0,
        floor=3, total_floors=5, last_floor=False, elevator=True,
        furnished="yes", heating_type="district", pets_allowed=None,
        title="Test listing", description=description,
        address=None, place_names=["Vračar"], image_url=None, is_agency=False,
        created_at=datetime.now(timezone.utc),
    )


def _mock_response(tool_args: dict) -> SimpleNamespace:
    """Build a fake anthropic.Message-like response with one tool_use block."""
    tool_use = SimpleNamespace(
        type="tool_use", name="record_listing_facts", input=tool_args
    )
    return SimpleNamespace(content=[tool_use])


def test_parse_tool_call_happy_path():
    response = _mock_response({
        "pets_allowed": "yes",
        "dishwasher": True,
        "elevator_confirmed": True,
        "heating_type_confirmed": "centralno",
        "max_lease_months": 12,
        "bills_estimate_eur": 150,
        "agency_or_owner": "agency",
        "red_flags": [],
        "summary_en": "Two-room flat in Vračar, fully furnished, agency-listed.",
        "description_en": "Two-bedroom flat in central Vračar, fully furnished, balcony, district heating, pet-friendly owner.",
    })
    ex = extract._parse_tool_call(response)
    assert ex.pets_allowed == "yes"
    assert ex.dishwasher is True
    assert ex.heating_type_confirmed == "centralno"
    assert ex.max_lease_months == 12
    assert ex.red_flags == []
    assert "Vračar" in ex.summary_en
    assert ex.description_en is not None and "balcony" in ex.description_en


def test_parse_tool_call_accepts_null_description_en():
    response = _mock_response({
        "pets_allowed": "unknown", "dishwasher": None, "elevator_confirmed": None,
        "heating_type_confirmed": None, "furnishing_confirmed": None,
        "max_lease_months": None, "bills_estimate_eur": None,
        "agency_or_owner": "unknown", "red_flags": [],
        "summary_en": "Short ad with no body.",
        "description_en": None,
    })
    ex = extract._parse_tool_call(response)
    assert ex.description_en is None


def test_parse_tool_call_raises_when_no_tool_use():
    response = SimpleNamespace(content=[SimpleNamespace(type="text", text="hello")])
    with pytest.raises(RuntimeError, match="did not call"):
        extract._parse_tool_call(response)


def test_extract_calls_messages_create_with_tool_use():
    fake_client = MagicMock()
    fake_client.messages.create.return_value = _mock_response({
        "pets_allowed": "no", "dishwasher": None, "elevator_confirmed": None,
        "heating_type_confirmed": None, "max_lease_months": None,
        "bills_estimate_eur": None, "agency_or_owner": "owner",
        "red_flags": ["no pets"], "summary_en": "Apartment forbids pets.",
    })

    ex = extract.extract(_listing(), client=fake_client)

    assert ex.pets_allowed == "no"
    assert "no pets" in ex.red_flags

    # Verify the request shape: tool definition + cached system prompt
    call_kwargs = fake_client.messages.create.call_args.kwargs
    assert call_kwargs["model"] == extract.MODEL
    assert call_kwargs["system"][0]["cache_control"] == {"type": "ephemeral"}
    assert call_kwargs["tools"][0]["name"] == "record_listing_facts"
    assert call_kwargs["tool_choice"]["name"] == "record_listing_facts"


def test_extract_skips_when_no_text():
    fake_client = MagicMock()
    listing = _listing(description="")
    listing.title = ""
    ex = extract.extract(listing, client=fake_client)
    assert isinstance(ex, extract.Extraction)
    fake_client.messages.create.assert_not_called()


def _mock_gemini_response(args: dict, name: str = "record_listing_facts") -> SimpleNamespace:
    """Mimic google-genai's GenerateContentResponse exposing function_calls."""
    fc = SimpleNamespace(name=name, args=args)
    return SimpleNamespace(function_calls=[fc], candidates=[])


def test_gemini_provider_routes_through_gemini_client(monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "gemini")
    monkeypatch.setenv("GEMINI_API_KEY", "fake")

    fake_client = MagicMock()
    fake_client.models.generate_content.return_value = _mock_gemini_response({
        "pets_allowed": "yes", "dishwasher": True, "elevator_confirmed": True,
        "heating_type_confirmed": "centralno", "furnishing_confirmed": "furnished",
        "max_lease_months": 12, "bills_estimate_eur": 120,
        "agency_or_owner": "owner", "red_flags": [],
        "summary_en": "Furnished 2BR in Vračar, pets ok.",
        "description_en": "Spacious furnished two-bedroom in Vračar with balcony and dishwasher.",
    })

    ex = extract.extract(_listing(), client=fake_client)

    assert ex.pets_allowed == "yes"
    assert ex.heating_type_confirmed == "centralno"
    assert ex.description_en is not None and "balcony" in ex.description_en
    # Verify Gemini-shaped call, not Anthropic's messages.create
    fake_client.models.generate_content.assert_called_once()
    kw = fake_client.models.generate_content.call_args.kwargs
    assert kw["model"] == extract.GEMINI_MODEL
    cfg = kw["config"]
    assert cfg["tools"][0]["function_declarations"][0]["name"] == "record_listing_facts"
    assert cfg["tool_config"]["function_calling_config"]["mode"] == "ANY"


def test_gemini_telegram_post_extraction(monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "gemini")
    monkeypatch.setenv("GEMINI_API_KEY", "fake")

    fake_client = MagicMock()
    fake_client.models.generate_content.return_value = _mock_gemini_response(
        {"m2": 65, "pets_allowed": "yes", "address": "Krunska 35, Vračar",
         "summary_en": "Two-room flat near Slavija."},
        name="record_telegram_post_facts",
    )

    facts = extract.extract_telegram_post("Izdajem stan, 65m2 ...", client=fake_client)
    assert facts.m2 == 65.0
    assert facts.pets_allowed == "yes"
    assert facts.address == "Krunska 35, Vračar"


def test_gemini_parse_raises_when_no_function_call():
    response = SimpleNamespace(function_calls=[], candidates=[])
    with pytest.raises(RuntimeError, match="did not call"):
        extract._parse_gemini_function_call(response, "record_listing_facts")


def test_provider_dispatch_defaults_to_anthropic(monkeypatch):
    monkeypatch.delenv("LLM_PROVIDER", raising=False)
    assert extract._provider() == "anthropic"
    monkeypatch.setenv("LLM_PROVIDER", "GEMINI")
    assert extract._provider() == "gemini"


def test_llm_api_key_present_follows_provider(monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "anthropic")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "x")
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    assert extract.llm_api_key_present()

    monkeypatch.setenv("LLM_PROVIDER", "gemini")
    assert not extract.llm_api_key_present()
    monkeypatch.setenv("GEMINI_API_KEY", "y")
    assert extract.llm_api_key_present()


def test_extract_many_isolates_individual_failures():
    fake_client = MagicMock()
    fake_client.messages.create.side_effect = [
        _mock_response({
            "pets_allowed": "yes", "dishwasher": True, "elevator_confirmed": None,
            "heating_type_confirmed": "centralno", "max_lease_months": None,
            "bills_estimate_eur": None, "agency_or_owner": "agency",
            "red_flags": [], "summary_en": "Good one.",
        }),
        RuntimeError("boom"),
        _mock_response({
            "pets_allowed": "unknown", "dishwasher": False, "elevator_confirmed": False,
            "heating_type_confirmed": "TA", "max_lease_months": None,
            "bills_estimate_eur": 250, "agency_or_owner": "owner",
            "red_flags": ["TA stove"], "summary_en": "Has TA heating.",
        }),
    ]

    listings = [_listing(f"desc {i}") for i in range(3)]
    updated, failures = extract.extract_many(listings, client=fake_client)
    assert failures == 1
    assert updated[0].extraction.pets_allowed == "yes"
    assert updated[1].extraction is None        # failed
    assert updated[2].extraction.heating_type_confirmed == "TA"


# --- Gemini free-tier throttle + retry -------------------------------------

def _gemini_client(side_effect):
    """A fake google-genai client whose models.generate_content is scripted."""
    client = MagicMock()
    client.models.generate_content.side_effect = side_effect
    return client


def test_gemini_generate_retries_503_then_succeeds(monkeypatch):
    # 503 UNAVAILABLE is genuinely transient ("high demand"), so we retry it.
    monkeypatch.delenv("GEMINI_MIN_INTERVAL_S", raising=False)
    monkeypatch.setattr(extract.time, "sleep", lambda _s: None)
    client = _gemini_client([
        RuntimeError("503 UNAVAILABLE ... Please retry in 2.5s"),
        "OK",
    ])
    assert extract._gemini_generate(client, model="m", contents="c") == "OK"
    assert client.models.generate_content.call_count == 2


def test_gemini_generate_does_not_retry_resource_exhausted(monkeypatch):
    # 429 RESOURCE_EXHAUSTED won't clear within a single run — retrying just
    # burns the job timeout waiting for a 60s "retry in" hint that won't help.
    monkeypatch.delenv("GEMINI_MIN_INTERVAL_S", raising=False)
    monkeypatch.setattr(extract.time, "sleep", lambda _s: None)
    client = _gemini_client(
        RuntimeError("429 RESOURCE_EXHAUSTED ... PerDay ... retry in 58s")
    )
    with pytest.raises(RuntimeError):
        extract._gemini_generate(client, model="m", contents="c")
    assert client.models.generate_content.call_count == 1


def test_gemini_generate_does_not_retry_non_transient(monkeypatch):
    monkeypatch.delenv("GEMINI_MIN_INTERVAL_S", raising=False)
    monkeypatch.setattr(extract.time, "sleep", lambda _s: None)
    client = _gemini_client(ValueError("400 invalid argument"))
    with pytest.raises(ValueError):
        extract._gemini_generate(client, model="m", contents="c")
    assert client.models.generate_content.call_count == 1


def test_gemini_generate_gives_up_after_max_attempts(monkeypatch):
    monkeypatch.delenv("GEMINI_MIN_INTERVAL_S", raising=False)
    monkeypatch.setattr(extract.time, "sleep", lambda _s: None)
    client = _gemini_client(RuntimeError("503 UNAVAILABLE"))
    with pytest.raises(RuntimeError):
        extract._gemini_generate(client, model="m", contents="c")
    assert client.models.generate_content.call_count == extract._GEMINI_MAX_ATTEMPTS


def test_gemini_generate_throttles_when_interval_set(monkeypatch):
    monkeypatch.setenv("GEMINI_MIN_INTERVAL_S", "13")
    slept: list[float] = []
    monkeypatch.setattr(extract.time, "sleep", lambda s: slept.append(s))
    monkeypatch.setattr(extract.time, "monotonic", lambda: 1000.0)
    monkeypatch.setattr(extract, "_gemini_last_call", 1000.0)  # 0s since "last" call
    extract._gemini_generate(_gemini_client(["OK"]), model="m", contents="c")
    assert slept and slept[0] == pytest.approx(13.0, abs=0.5)


def test_gemini_retry_delay_honors_server_hint():
    assert extract._gemini_retry_delay("Please retry in 9.5s.", 0) == pytest.approx(10.5)


def test_gemini_retry_delay_caps_long_hint():
    assert extract._gemini_retry_delay("retry in 120s", 0) == 65.0


def test_gemini_retry_delay_backoff_without_hint():
    assert extract._gemini_retry_delay("boom", 0) == 5.0
    assert extract._gemini_retry_delay("boom", 1) == 10.0


# --- Daily-cap classifier + extract_many circuit breaker --------------------

_DAILY_CAP_MSG = (
    "429 RESOURCE_EXHAUSTED. quotaId: "
    "GenerateRequestsPerDayPerProjectPerModel-FreeTier, limit: 20"
)
_PER_MINUTE_MSG = (
    "429 RESOURCE_EXHAUSTED. quotaId: "
    "GenerateRequestsPerMinutePerProjectPerModel-FreeTier, limit: 5"
)


def test_is_daily_cap_error_recognises_perday_quota():
    assert extract.is_daily_cap_error(RuntimeError(_DAILY_CAP_MSG)) is True


def test_is_daily_cap_error_rejects_per_minute_quota():
    # PerMinute caps can clear within a run, so they're not the circuit signal.
    assert extract.is_daily_cap_error(RuntimeError(_PER_MINUTE_MSG)) is False


def test_is_daily_cap_error_rejects_non_429():
    assert extract.is_daily_cap_error(ValueError("400 invalid argument")) is False
    assert extract.is_daily_cap_error(RuntimeError("503 UNAVAILABLE")) is False


def test_extract_many_circuit_breaks_after_daily_cap(monkeypatch):
    # Two successes, then a daily-cap error — the 4th listing must not call
    # the LLM at all so the run can finish and push state in time.
    fake_client = MagicMock()
    fake_client.messages.create.side_effect = [
        _mock_response({
            "pets_allowed": "yes", "dishwasher": True, "elevator_confirmed": None,
            "heating_type_confirmed": "centralno", "max_lease_months": None,
            "bills_estimate_eur": None, "agency_or_owner": "agency",
            "red_flags": [], "summary_en": "ok 1.",
        }),
        _mock_response({
            "pets_allowed": "no", "dishwasher": None, "elevator_confirmed": None,
            "heating_type_confirmed": None, "max_lease_months": None,
            "bills_estimate_eur": None, "agency_or_owner": "owner",
            "red_flags": [], "summary_en": "ok 2.",
        }),
        RuntimeError(_DAILY_CAP_MSG),
        # No fourth response — the 4th listing must be skipped, not requested.
    ]

    listings = [_listing(f"desc {i}") for i in range(4)]
    updated, failures = extract.extract_many(listings, client=fake_client)
    assert fake_client.messages.create.call_count == 3
    assert failures == 2                               # the cap-hit + the skipped one
    assert updated[0].extraction.pets_allowed == "yes"
    assert updated[1].extraction.pets_allowed == "no"
    assert updated[2].extraction is None               # raised
    assert updated[3].extraction is None               # circuit-broken, never tried


def test_extract_many_per_minute_error_does_not_circuit_break(monkeypatch):
    # A PerMinute 429 (or any non-PerDay failure) must NOT trip the breaker —
    # only daily-cap exhaustion does. Subsequent listings keep extracting.
    fake_client = MagicMock()
    fake_client.messages.create.side_effect = [
        RuntimeError(_PER_MINUTE_MSG),
        _mock_response({
            "pets_allowed": "yes", "dishwasher": False, "elevator_confirmed": True,
            "heating_type_confirmed": "etazno", "max_lease_months": 12,
            "bills_estimate_eur": None, "agency_or_owner": "owner",
            "red_flags": [], "summary_en": "ok.",
        }),
    ]
    listings = [_listing(f"desc {i}") for i in range(2)]
    updated, failures = extract.extract_many(listings, client=fake_client)
    assert fake_client.messages.create.call_count == 2
    assert failures == 1
    assert updated[1].extraction.pets_allowed == "yes"
