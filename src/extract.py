"""LLM extraction layer (spec §5).

Given a listing whose title and description are in Serbian, call an LLM to
extract structured fields (pets, dishwasher, heating, max-lease, bills,
agency/owner, red flags) plus a 2-3 sentence English summary, then attach the
result to the listing for downstream filtering and rendering.

Provider switch: `LLM_PROVIDER=anthropic` (default) uses Claude Haiku 4.5 with
prompt caching on the system block. `LLM_PROVIDER=gemini` routes the same
tool-call shape through Gemini 2.5 Flash on the free tier. Both providers
return the same `Extraction` / `TelegramPostFacts` records — the rest of the
pipeline is provider-agnostic.

Cost: per spec §5, 50-200 calls/day. Anthropic Haiku ≈ $3-6/month. Gemini free
tier absorbs the load at zero cost (2.5-flash; 2.0-flash has no free quota in
our region).
"""
from __future__ import annotations

import logging
import os
import re
import time
from dataclasses import dataclass
from typing import Any

import anthropic

from src.models import Extraction, Listing

log = logging.getLogger(__name__)

# Gemini free-tier pacing. gemini-2.5-flash free tier allows 5 requests/minute;
# GEMINI_MIN_INTERVAL_S spaces calls so we stay under it (set to ~13 in CI;
# defaults to 0 = no throttle for local/tests where the client is mocked).
_GEMINI_MAX_ATTEMPTS = 3
_RETRY_HINT_RE = re.compile(r"retry in ([0-9.]+)s")
_gemini_last_call = 0.0


def _gemini_generate(client: Any, **kwargs: Any) -> Any:
    """Call Gemini with free-tier-friendly pacing + retry on transient errors.

    Paces successive calls at least GEMINI_MIN_INTERVAL_S apart so a backfill
    burst stays under the per-minute cap, and retries 503 UNAVAILABLE with
    backoff. 429 RESOURCE_EXHAUSTED is NOT retried — daily-cap exhaustion
    won't clear within a single CI run, and the server's "retry in 58s" hint
    blows the job timeout fast. The caller (extract_many) circuit-breaks on
    the first daily-cap error so the rest of the run completes quickly.
    """
    global _gemini_last_call
    try:
        interval = float(os.environ.get("GEMINI_MIN_INTERVAL_S", "0"))
    except ValueError:
        interval = 0.0
    last_exc: Exception | None = None
    for attempt in range(_GEMINI_MAX_ATTEMPTS):
        if interval > 0:
            wait = interval - (time.monotonic() - _gemini_last_call)
            if wait > 0:
                time.sleep(wait)
        _gemini_last_call = time.monotonic()
        try:
            return client.models.generate_content(**kwargs)
        except Exception as e:  # noqa: BLE001
            msg = str(e)
            # Only retry genuinely transient errors. Daily-cap 429s and any
            # other status surface immediately to extract_many.
            transient = ("UNAVAILABLE" in msg or "503" in msg)
            if not transient or attempt == _GEMINI_MAX_ATTEMPTS - 1:
                raise
            last_exc = e
            time.sleep(_gemini_retry_delay(msg, attempt))
    raise last_exc  # pragma: no cover — loop either returns or raises above


def is_daily_cap_error(exc: BaseException) -> bool:
    """Detect a Gemini per-day quota exhaustion (vs. per-minute or transient).

    The per-minute cap can clear within a run; the per-day cap can't, and
    that's the signal extract_many uses to circuit-break and let the run
    finish so state can still be pushed.
    """
    msg = str(exc)
    if "RESOURCE_EXHAUSTED" not in msg and "429" not in msg:
        return False
    return "PerDay" in msg or "RequestsPerDay" in msg


def _gemini_retry_delay(msg: str, attempt: int) -> float:
    """Seconds to wait before retrying: the server's hint if present (capped),
    else exponential backoff."""
    m = _RETRY_HINT_RE.search(msg)
    if m:
        return min(float(m.group(1)) + 1.0, 65.0)
    return min(5.0 * (2 ** attempt), 65.0)

MODEL = "claude-haiku-4-5"
MAX_TOKENS = 600
# gemini-2.0-flash has no free-tier quota for our project's region (every call
# 429s with `limit: 0`); 2.5-flash does. See STATUS.md provider notes.
GEMINI_MODEL = "gemini-2.5-flash"


def _provider() -> str:
    return (os.environ.get("LLM_PROVIDER") or "anthropic").strip().lower()


def llm_api_key_present() -> bool:
    """Whether the currently selected provider has its API key configured."""
    if _provider() == "gemini":
        return "GEMINI_API_KEY" in os.environ
    return "ANTHROPIC_API_KEY" in os.environ


def make_client() -> Any:
    """Build a provider-appropriate client. Lazy-imports the Gemini SDK so the
    Anthropic-only path doesn't require google-genai to be installed."""
    if _provider() == "gemini":
        from google import genai  # type: ignore[import-not-found]
        return genai.Client(api_key=os.environ["GEMINI_API_KEY"])
    return anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])

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
- `description_en`: a full English translation of the listing description text. Translate, do not summarize: preserve every concrete claim, room name, address detail, price/utility figure, and condition. Strip boilerplate marketing fluff ("dream apartment!", agency contact spam, repeated calls to action) but keep all factual content the renter would care about. Use plain natural English. If the listing has no usable description (empty, just a phone number, just a price line), return `null`.
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
            "description_en": {
                "type": ["string", "null"],
                "description": "Full English translation of the listing body, or null if the listing has no usable description.",
            },
        },
        "required": [
            "pets_allowed", "dishwasher", "elevator_confirmed",
            "heating_type_confirmed", "furnishing_confirmed",
            "max_lease_months", "bills_estimate_eur",
            "agency_or_owner", "red_flags", "summary_en", "description_en",
        ],
    },
}


def extract(listing: Listing, *, client: Any | None = None) -> Extraction:
    """Run LLM extraction on one listing. Raises on API/parse failure.

    Dispatches to the Anthropic or Gemini backend per `LLM_PROVIDER`. The
    `client` arg is optional and only used to inject a fake in tests; in
    production we lazy-build the right one via `make_client()`.
    """
    if not listing.description and not listing.title:
        log.info("extract: empty text for %s — skipping LLM call", listing.fingerprint_key)
        return Extraction()

    user_text = (
        f"Source: {listing.source}\n"
        f"Title: {listing.title}\n\n"
        f"Description (Serbian):\n{listing.description}"
    )

    if _provider() == "gemini":
        return _extract_gemini_listing(user_text, client)
    return _extract_anthropic_listing(user_text, client)


def _extract_anthropic_listing(user_text: str, client: Any | None) -> Extraction:
    client = client or anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
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


def _extract_gemini_listing(user_text: str, client: Any | None) -> Extraction:
    if client is None:
        from google import genai  # type: ignore[import-not-found]
        client = genai.Client(api_key=os.environ["GEMINI_API_KEY"])
    response = _gemini_generate(
        client,
        model=GEMINI_MODEL,
        contents=user_text,
        config={
            "system_instruction": SYSTEM_PROMPT,
            "tools": [{"function_declarations": [GEMINI_TOOL_DEF]}],
            "tool_config": {
                "function_calling_config": {
                    "mode": "ANY",
                    "allowed_function_names": ["record_listing_facts"],
                }
            },
            "temperature": 0.0,
        },
    )
    args = _parse_gemini_function_call(response, "record_listing_facts")
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
        description_en=args.get("description_en"),
    )


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
                description_en=args.get("description_en"),
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
    text: str, *, client: Any | None = None
) -> TelegramPostFacts:
    """Run LLM extraction on a single Telegram-channel post body."""
    if not text.strip():
        return TelegramPostFacts(m2=None, pets_allowed="unknown", address=None, summary_en=None)

    if _provider() == "gemini":
        return _extract_telegram_post_gemini(text, client)
    return _extract_telegram_post_anthropic(text, client)


def _extract_telegram_post_anthropic(text: str, client: Any | None) -> TelegramPostFacts:
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


def _extract_telegram_post_gemini(text: str, client: Any | None) -> TelegramPostFacts:
    if client is None:
        from google import genai  # type: ignore[import-not-found]
        client = genai.Client(api_key=os.environ["GEMINI_API_KEY"])
    response = _gemini_generate(
        client,
        model=GEMINI_MODEL,
        contents=text,
        config={
            "system_instruction": _TG_SYSTEM_PROMPT,
            "tools": [{"function_declarations": [_GEMINI_TG_TOOL_DEF]}],
            "tool_config": {
                "function_calling_config": {
                    "mode": "ANY",
                    "allowed_function_names": ["record_telegram_post_facts"],
                }
            },
            "temperature": 0.0,
        },
    )
    args = _parse_gemini_function_call(response, "record_telegram_post_facts")
    m2 = args.get("m2")
    return TelegramPostFacts(
        m2=float(m2) if m2 is not None else None,
        pets_allowed=args.get("pets_allowed") or "unknown",
        address=args.get("address"),
        summary_en=args.get("summary_en"),
    )


def extract_many(
    listings: list[Listing],
    *,
    client: Any | None = None,
) -> tuple[list[Listing], int]:
    """Attach extraction to each listing; return (updated_listings, failure_count).

    A single LLM error is logged and the listing keeps `extraction=None` — that
    flows through to the filter as "data unknown" and lands the listing in the
    near-miss bucket rather than aborting the whole run.

    Circuit breaker: the first daily-cap quota error stops all further LLM
    calls for this run (remaining listings count as failures with no API call).
    Without this, every leftover listing would wait the server's ~60s "retry
    in" hint and the job would time out before state could be pushed. The
    skipped listings are not cached, so they retry naturally on the next run
    once the per-day budget resets.
    """
    client = client or make_client()
    failures = 0
    daily_cap_hit = False
    for l in listings:
        if daily_cap_hit:
            failures += 1
            continue
        try:
            l.extraction = extract(l, client=client)
        except Exception as e:  # noqa: BLE001 - one LLM failure must not kill the run
            log.warning("extract: failed for %s: %s", l.fingerprint_key, e)
            failures += 1
            if is_daily_cap_error(e):
                log.warning(
                    "extract: Gemini daily cap reached after %d successes; "
                    "skipping %d remaining listings to let state push",
                    sum(1 for x in listings if x.extraction is not None),
                    sum(1 for x in listings if x.extraction is None) - 1,
                )
                daily_cap_hit = True
    return listings, failures


# Gemini tool schemas mirror the Anthropic tool defs above. Gemini's Schema
# uses uppercase type names and `nullable: True` instead of union types — but
# the field set, enum values, and required list are identical so the two
# providers produce interchangeable Extraction / TelegramPostFacts records.
GEMINI_TOOL_DEF = {
    "name": "record_listing_facts",
    "description": "Record structured facts extracted from a rental listing.",
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "pets_allowed": {
                "type": "STRING",
                "enum": ["yes", "no", "unknown"],
            },
            "dishwasher": {"type": "BOOLEAN", "nullable": True},
            "elevator_confirmed": {"type": "BOOLEAN", "nullable": True},
            "heating_type_confirmed": {
                "type": "STRING",
                "enum": ["centralno", "etazno", "podno", "TA", "klima", "elektricni"],
                "nullable": True,
            },
            "furnishing_confirmed": {
                "type": "STRING",
                "enum": ["furnished", "semi-furnished", "unfurnished"],
                "nullable": True,
            },
            "max_lease_months": {"type": "INTEGER", "nullable": True},
            "bills_estimate_eur": {"type": "INTEGER", "nullable": True},
            "agency_or_owner": {
                "type": "STRING",
                "enum": ["agency", "owner", "unknown"],
            },
            "red_flags": {"type": "ARRAY", "items": {"type": "STRING"}},
            "summary_en": {"type": "STRING"},
            "description_en": {"type": "STRING", "nullable": True},
        },
        "required": [
            "pets_allowed", "dishwasher", "elevator_confirmed",
            "heating_type_confirmed", "furnishing_confirmed",
            "max_lease_months", "bills_estimate_eur",
            "agency_or_owner", "red_flags", "summary_en", "description_en",
        ],
    },
}

_GEMINI_TG_TOOL_DEF = {
    "name": "record_telegram_post_facts",
    "description": "Record structured facts extracted from a Telegram-channel apartment post.",
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "m2": {"type": "NUMBER", "nullable": True},
            "pets_allowed": {
                "type": "STRING",
                "enum": ["yes", "no", "unknown"],
            },
            "address": {"type": "STRING", "nullable": True},
            "summary_en": {"type": "STRING"},
        },
        "required": ["m2", "pets_allowed", "address", "summary_en"],
    },
}


def _parse_gemini_function_call(response: Any, name: str) -> dict:
    """Return the args dict of the first function call matching `name`.

    google-genai exposes function calls both as a `response.function_calls`
    convenience list and as `function_call` parts on the candidate content.
    We try the convenience accessor first, then fall back to walking parts,
    so mocked test responses can populate whichever shape is simpler.
    """
    fcs = getattr(response, "function_calls", None)
    if fcs:
        for fc in fcs:
            if getattr(fc, "name", None) == name:
                return dict(fc.args or {})
    for cand in (getattr(response, "candidates", None) or []):
        parts = getattr(getattr(cand, "content", None), "parts", None) or []
        for part in parts:
            fc = getattr(part, "function_call", None)
            if fc and getattr(fc, "name", None) == name:
                return dict(getattr(fc, "args", None) or {})
    raise RuntimeError(f"Gemini did not call {name} tool; got: {response!r}")
