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
    })
    ex = extract._parse_tool_call(response)
    assert ex.pets_allowed == "yes"
    assert ex.dishwasher is True
    assert ex.heating_type_confirmed == "centralno"
    assert ex.max_lease_months == 12
    assert ex.red_flags == []
    assert "Vračar" in ex.summary_en


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
