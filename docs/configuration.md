# Configuration

Two kinds of configuration drive a run. **`config.yaml`** holds the tunable
behaviour — thresholds, weights, schedules — and is committed to the repo.
**Secrets** hold credentials and the private coordinates, and never touch the
repo: CI reads them from GitHub Actions Secrets, local runs from a `.env` file.

## `config.yaml`

### `filters`

The structural and LLM-aware rules from
[filtering-and-ranking.md](filtering-and-ranking.md).

| Key | Meaning |
| --- | --- |
| `price_eur_max` | Rent cap. A hard gate; price isn't otherwise scored. |
| `rooms_min` / `rooms_max` | Room range, Belgrade half-room counting. |
| `surface_m2_min` | Size floor in m². |
| `heating_allowed` | Heating types that pass (e.g. `centralno`, `etazno`, `podno`). |
| `furnishing_allowed` | Furnishing states that pass. |
| `floor_exclude` | Floors rejected outright (`ground`, `basement`). |
| `elevator_required` | A soft preference now — scored, not hard-rejected. |
| `photo_required` | Reject listings with no image. |
| `dishwasher_required` | Soft: Serbian listings rarely state it, so absence is a near-miss. |
| `pets_required` | Only an explicit "no" rejects; silence passes. |
| `max_lease_months` | Reject listings demanding a longer minimum lease. |
| `freshness_days` | Reject listings older than this. |

### `filters.commute`

| Key | Meaning |
| --- | --- |
| `walk_min_max` | Office walking-minute gate. Beyond this, reject. |
| `haversine_prefilter_km` | Straight-line cutoff before any API call. |
| `destinations` | Named walking destinations (below). |

Each destination is a name plus the *names of the environment variables* that
hold its coordinates — never the coordinates themselves, since this is a public
repo. `gates: true` makes its walking time decide pass/fail; `score_weight` sets
its pull on ranking.

```yaml
destinations:
  - name: office
    lat_env: OFFICE_LAT
    lng_env: OFFICE_LNG
    gates: true
    score_weight: 0.45
  - name: Sadik Enter
    lat_env: SADIK_LAT
    lng_env: SADIK_LNG
    gates: false        # info + score only, never filters
    score_weight: 0.25
```

A destination whose env vars are unset is skipped with a warning, so a missing
`SADIK_LAT` degrades to office-only rather than crashing the run.

### Other sections

| Section | Key knobs |
| --- | --- |
| `routing` | `cache_grid_decimals` (cache bucket precision), `cache_ttl_days` (90), `daily_call_cap`. |
| `dedup` | `price_drop_badge_pct` (📉 threshold), and the fingerprint `coord_decimals` / `price_bucket_eur` / `title_trigram_threshold`. |
| `digest` | `max_perfect` (10), `max_near_miss` (5). |
| `schedule` | The two cron expressions and `quiet_hours_kgt`. |
| `llm` | `model` — the Claude Haiku model id. |
| `state` | `r2_key` — the SQLite object key in R2. |
| `telegram_channels` | The channel side pipeline: per-channel `m2_min` and optional `require_hashtag`. |

Some keys are vestigial — `digest.score_weights`, for one, predates the
walking-distance scoring in `score.py` and isn't read. Treat `src/` as the
source of truth when config and code disagree, and check STATUS.md §2 for the
reasoning behind a threshold before changing it.

## Secrets

Set in **GitHub Actions Secrets** for CI (wired into the run through the `env:`
block in `.github/workflows/scrape.yml`) and in a local **`.env`** for
development (`cp .env.example .env`). They never appear in the repo.

| Secret | Purpose |
| --- | --- |
| `TELEGRAM_BOT_TOKEN` | Bot that sends the digest and answers callbacks. |
| `TELEGRAM_CHAT_ID` | Where the digest is delivered. |
| `GOOGLE_DIRECTIONS_API_KEY` | Google Routes API (walking distances). |
| `ANTHROPIC_API_KEY` | Claude Haiku extraction. |
| `R2_ACCESS_KEY_ID`, `R2_SECRET_ACCESS_KEY`, `R2_BUCKET`, `R2_ACCOUNT_ID` | Cloudflare R2 state storage. |
| `OFFICE_LAT`, `OFFICE_LNG` | Office coordinates (the gating destination). |
| `SADIK_LAT`, `SADIK_LNG` | The second walking destination. |

Optional:

| Secret | Purpose |
| --- | --- |
| `TELEGRAM_FAVORITES_CHAT_ID` | Destination for ⭐ Favorite copies. Unset → favorites persist to the DB only. |
| `TELEGRAM_FAVORITES_THREAD_ID` | Forum-topic id when the favorites chat is a supergroup topic. |
| `LLM_PROVIDER` | `anthropic` (default) or `gemini`. |
| `GEMINI_API_KEY` | Needed when `LLM_PROVIDER=gemini`. |
| `GEMINI_MIN_INTERVAL_S` | Pacing to stay under Gemini's free-tier rate cap. |

The VM webhook reads a smaller subset plus its own two secrets
(`WEBHOOK_PATH_SECRET`, `WEBHOOK_SECRET_TOKEN`) from
`/etc/belgrade-webhook.env` — see [deploy/README.md](../deploy/README.md).
