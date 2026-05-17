"""LLM extraction layer (spec §5).

Given a listing whose title and description are in Serbian, ask Claude Haiku 4.5
to extract structured fields (pets, dishwasher, heating, max-lease, bills,
agency/owner, red flags) plus a 2-3 sentence English summary, then attach the
result to the listing for downstream filtering and rendering.

We use tool-use for the structured output (more reliable than free-form JSON
for small models) and prompt caching on the system prompt so repeat calls in
the same scrape don't re-bill those tokens.

Cost: per spec §5, 50–200 calls/day × ~500 tokens ≈ $3–6/month for Haiku 4.5.
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Any

import anthropic

from src.models import Extraction, Listing

log = logging.getLogger(__name__)

MODEL = "claude-haiku-4-5"
MAX_TOKENS = 600

SYSTEM_PROMPT = """You analyze Serbian-language Belgrade apartment rental listings for a renter who applies strict filters. Your job: read the title and description, and call the `record_listing_facts` tool exactly once with what the listing actually says.

Rules:
- Only mark a field true/yes if the listing clearly states it. If unclear, return `null` / `"unknown"`.
- `pets_allowed`: "yes" only if pets are explicitly allowed. "no" if there is a clear prohibition (e.g. "bez ljubimaca", "kućni ljubimci nisu dozvoljeni"). Otherwise `"unknown"`.
- `dishwasher`: true only if a dishwasher is listed (look for "sudopera", "mašina za sudove", "dishwasher"). Stove/oven/microwave do not count. Otherwise null.
- `heating_type_confirmed`: pick the most specific term the listing uses, normalized to one of: "centralno" (city/district heating), "etazno" (own gas/oil boiler for the unit), "podno" (underfloor), "TA" (electric storage heater — the night-time tile stove), "klima" (heating via air-conditioner only), "elektricni" (electric panels), or null if absent.
- `furnishing_confirmed`: "furnished" if the apartment is fully furnished ("namešten", "potpuno namešten", "kompletno namešten"); "semi-furnished" if partially ("polunamešten", "delimično opremljen"); "unfurnished" if empty or unfurnished ("prazan", "nenamešten", "neopremljen"). Null if absent or genuinely unclear.
- `max_lease_months`: integer if the listing requires a minimum lease length (e.g. "minimum 12 meseci"). Null otherwise.
- `bills_estimate_eur`: integer EUR if a monthly utility/bill estimate is given. Null otherwise. (Note: rent and bills are separate; do not double-count.)
- `agency_or_owner`: "agency" if the listing is posted by a real-estate agency, "owner" if posted by a private owner, otherwise "unknown".
- `red_flags`: short English strings for any concerning constraint (e.g. "students only", "no smoking", "shared bathroom", "deposit > 2 months", "long-term lease required"). Empty list if none.
- `summary_en`: 2-3 sentences in English describing the apartment and any unusual conditions. Tone: dry, factual, what a relocating professional would want to know.
"""

# Tool schema — the model must call this tool exactly once.
TOOL_DEF = {
    "name": "record_listing_facts",
    "description": "Record structured facts extracted from a rental listing.",
    "input_schema": {
        "type": "object",
        "properties": {
            "pets_allowed": {
                "type": "string",
                "enum": ["yes", "no", "unknown"],
                "description": "Whether pets are allowed per the listing text.",
            },
            "dishwasher": {
                "type": ["boolean", "null"],
                "description": "True only if a dishwasher is explicitly mentioned.",
            },
            "elevator_confirmed": {
                "type": ["boolean", "null"],
                "description": "True/false if the listing confirms elevator presence; null if not mentioned.",
            },
            "heating_type_confirmed": {
                "type": ["string", "null"],
                "enum": ["centralno", "etazno", "podno", "TA", "klima", "elektricni", None],
                "description": "Normalized heating type, or null if absent.",
            },
            "furnishing_confirmed": {
                "type": ["string", "null"],
                "enum": ["furnished", "semi-furnished", "unfurnished", None],
                "description": "Furnishing level inferred from the description, or null if unclear.",
            },
            "max_lease_months": {
                "type": ["integer", "null"],
                "description": "Minimum lease length in months if the listing requires one.",
            },
            "bills_estimate_eur": {
                "type": ["integer", "null"],
                "description": "Monthly utilities estimate in EUR, if given.",
            },
            "agency_or_owner": {
                "type": "string",
                "enum": ["agency", "owner", "unknown"],
            },
            "red_flags": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Short English flags for concerning constraints.",
            },
            "summary_en": {
                "type": "string",
                "description": "2-3 sentence English summary of the listing.",
            },
        },
        "required": [
            "pets_allowed", "dishwasher", "elevator_confirmed",
            "heating_type_confirmed", "furnishing_confirmed",
            "max_lease_months", "bills_estimate_eur",
            "agency_or_owner", "red_flags", "summary_en",
        ],
    },
}


def extract(listing: Listing, *, client: anthropic.Anthropic | None = None) -> Extraction:
    """Run LLM extraction on one listing. Raises on API/parse failure."""
    if not listing.description and not listing.title:
        log.info("extract: empty text for %s — skipping LLM call", listing.fingerprint_key)
        return Extraction()

    client = client or anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])

    user_text = (
        f"Source: {listing.source}\n"
        f"Title: {listing.title}\n\n"
        f"Description (Serbian):\n{listing.description}"
    )

    response = client.messages.create(
        model=MODEL,
        max_tokens=MAX_TOKENS,
        # Cached system block + tool def — repeat calls in the same scrape (~50-200/day)
        # skip the per-token cost on these prefix blocks.
        system=[
            {
                "type": "text",
                "text": SYSTEM_PROMPT,
                "cache_control": {"type": "ephemeral"},
            }
        ],
        tools=[TOOL_DEF],
        tool_choice={"type": "tool", "name": "record_listing_facts"},
        messages=[{"role": "user", "content": user_text}],
    )

    return _parse_tool_call(response)


def _parse_tool_call(response: Any) -> Extraction:
    for block in response.content:
        if getattr(block, "type", None) == "tool_use" and block.name == "record_listing_facts":
            args = block.input
            return Extraction(
                pets_allowed=args.get("pets_allowed"),
                dishwasher=args.get("dishwasher"),
                elevator_confirmed=args.get("elevator_confirmed"),
                heating_type_confirmed=args.get("heating_type_confirmed"),
                furnishing_confirmed=args.get("furnishing_confirmed"),
                max_lease_months=args.get("max_lease_months"),
                bills_estimate_eur=args.get("bills_estimate_eur"),
                agency_or_owner=args.get("agency_or_owner"),
                red_flags=list(args.get("red_flags") or []),
                summary_en=args.get("summary_en"),
            )
    raise RuntimeError(f"LLM did not call record_listing_facts tool; got: {response.content!r}")


_TG_SYSTEM_PROMPT = """You parse free-form Serbian/Russian apartment-rental posts from a Telegram channel. Return facts via the `record_telegram_post_facts` tool exactly once.

Rules:
- Only mark fields when the post clearly states them. Prefer null over guessing.
- `m2`: numeric square-meter size if mentioned (e.g. "60m2", "65 кв.м", "70 кв"). Null otherwise.
- `pets_allowed`: "yes" only if pets are explicitly allowed ("са кућним љубимцима", "ljubimci dozvoljeni", "разрешены животные", "pets ok"). "no" if explicitly prohibited. Otherwise "unknown".
- `address`: the most specific Belgrade street/neighborhood mentioned (e.g. "Krunska 35, Vračar" or "Beograd, Centar"). Null if no location info is given.
- `summary_en`: 1-2 plain English sentences describing the flat (what a renter cares about).
"""

_TG_TOOL_DEF = {
    "name": "record_telegram_post_facts",
    "description": "Record structured facts extracted from a Telegram-channel apartment post.",
    "input_schema": {
        "type": "object",
        "properties": {
            "m2": {
                "type": ["number", "null"],
                "description": "Square meters if stated in the post; null otherwise.",
            },
            "pets_allowed": {
                "type": "string",
                "enum": ["yes", "no", "unknown"],
            },
            "address": {
                "type": ["string", "null"],
                "description": "Most specific Belgrade location string in the post; null if absent.",
            },
            "summary_en": {
                "type": "string",
                "description": "1-2 sentence English summary.",
            },
        },
        "required": ["m2", "pets_allowed", "address", "summary_en"],
    },
}


@dataclass
class TelegramPostFacts:
    m2: float | None
    pets_allowed: str            # "yes" | "no" | "unknown"
    address: str | None
    summary_en: str | None


def extract_telegram_post(
    text: str, *, client: anthropic.Anthropic | None = None
) -> TelegramPostFacts:
    """Run LLM extraction on a single Telegram-channel post body."""
    if not text.strip():
        return TelegramPostFacts(m2=None, pets_allowed="unknown", address=None, summary_en=None)

    client = client or anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
    response = client.messages.create(
        model=MODEL,
        max_tokens=300,
        system=[{
            "type": "text",
            "text": _TG_SYSTEM_PROMPT,
            "cache_control": {"type": "ephemeral"},
        }],
        tools=[_TG_TOOL_DEF],
        tool_choice={"type": "tool", "name": "record_telegram_post_facts"},
        messages=[{"role": "user", "content": text}],
    )
    for block in response.content:
        if getattr(block, "type", None) == "tool_use" and block.name == "record_telegram_post_facts":
            args = block.input
            m2 = args.get("m2")
            return TelegramPostFacts(
                m2=float(m2) if m2 is not None else None,
                pets_allowed=args.get("pets_allowed") or "unknown",
                address=args.get("address"),
                summary_en=args.get("summary_en"),
            )
    raise RuntimeError(f"LLM did not call record_telegram_post_facts tool; got: {response.content!r}")


def extract_many(
    listings: list[Listing],
    *,
    client: anthropic.Anthropic | None = None,
) -> tuple[list[Listing], int]:
    """Attach extraction to each listing; return (updated_listings, failure_count).

    A single LLM error is logged and the listing keeps `extraction=None` — that
    flows through to the filter as "data unknown" and lands the listing in the
    near-miss bucket rather than aborting the whole run.
    """
    client = client or anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
    failures = 0
    for l in listings:
        try:
            l.extraction = extract(l, client=client)
        except Exception as e:  # noqa: BLE001 - one LLM failure must not kill the run
            log.warning("extract: failed for %s: %s", l.fingerprint_key, e)
            failures += 1
    return listings, failures
