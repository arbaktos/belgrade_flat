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
