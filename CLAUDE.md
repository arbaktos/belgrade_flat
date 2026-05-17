# CLAUDE.md

> Context for Claude Code sessions in this repo. Read [STATUS.md](STATUS.md) first.

## Where to find things

- **[STATUS.md](STATUS.md)** — current state, what's working, what's pending, design decisions, run book. Single source of truth for "where are we?".
- **[belgrade-rental-notifier-SPEC.md](belgrade-rental-notifier-SPEC.md)** — the original execution spec. STATUS.md tracks deviations.
- **`src/`** — all production code. One module per concern:
  - `sources/` — per-portal scrapers (`four_zida`, `nekretnine`, `halooglasi`, `cityexpert`, `_flaresolverr`)
  - `filter.py` — structural + LLM-aware filters with near-miss bucket
  - `extract.py` — Claude Haiku 4.5 tool-use extraction
  - `route.py` — Google Routes API + haversine pre-filter + 90-day SQLite cache
  - `dedup.py` — pHash + coord + trigram cascade, re-notify policy
  - `telegram.py` / `telegram_digest.py` / `telegram_callbacks.py` — delivery + 🙈 Hide button
  - `state.py` — SQLite + R2 sync, schema migrations
- **`tests/`** — 90 tests with fixtures from real-world responses

## How to validate changes

1. `.venv/bin/python -m pytest tests/ -q` — must stay green
2. Push to `main`, then `gh workflow run scrape.yml --repo arbaktos/belgrade_flat --ref main`
3. `gh run watch <id> --repo arbaktos/belgrade_flat --exit-status`
4. Check Telegram + the auto-committed `digests/YYYY-MM-DD-test.md`

## Conventions

- Python 3.12 target on CI, 3.9 locally → use `from __future__ import annotations` everywhere for PEP 604 unions
- Schema changes bump `SCHEMA_VERSION` in `src/state.py` with a forward-only, idempotent migration
- New external dependencies go in `requirements.txt` with pinned versions
- Source-isolated failures: one bad portal must never kill the run (see `_fetch_source` pattern)
- LLM cost discipline: only call Anthropic on listings that passed structural filter; system prompt is `cache_control: ephemeral`
- Routes API cost discipline: haversine pre-filter (10 km) before the API; 3-decimal lat/lng bucket cache; the `DirectionsConfigError` short-circuits the loop on REQUEST_DENIED so we don't burn cache slots
- Test mocks against tool-use response shape live in `tests/test_extract.py`

## When in doubt

Re-read STATUS.md §2 (design decisions) before changing a filter threshold or score weight — most of them have a documented reason.
