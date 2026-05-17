# Belgrade Rental Notifier — Project State

_Last updated: 2026-05-17 (snapshot after 🙈 Hide button feature)_

A personal Belgrade-rentals scraper that delivers filtered Telegram messages
from four portals, behind LLM extraction, commute time, and dedup. Runs as a
GitHub Actions workflow with state in Cloudflare R2.

Spec source of truth: [belgrade-rental-notifier-SPEC.md](belgrade-rental-notifier-SPEC.md).

---

## 1. What is currently working

### End-to-end pipeline (workflow_dispatch trigger)

```
1. drain Skip-button callbacks    →  src/telegram_callbacks.py
2. scrape 4 sources               →  src/sources/{four_zida,nekretnine,halooglasi,cityexpert}.py
3. drop user-skipped listings     →  in-pipeline filter
4. upsert to SQLite               →  src/state.py
5. structural pre-filter (cheap)  →  src/filter.apply()           (price / rooms / m² / floor / elevator / freshness)
6. LLM extract on survivors       →  src/extract.py               (Claude Haiku 4.5, tool-use, cached system prompt)
7. LLM-aware filter               →  src/filter.apply_with_extraction()  (pets / dishwasher / heating / max-lease)
8. commute filter                 →  src/route.py + src/filter.apply_commute()  (Google Routes API + Haversine 10 km pre-filter + 90-day SQLite cache)
9. dedup + cluster matches        →  src/dedup.py                 (image pHash workhorse + coord/m²/price + title trigram)
10. composite-score order         →  src/score.py                 (walk 0.45 + transit 0.25 + m² 0.10 + freshness 0.10 + elevator 0.10; price not scored)
11. write markdown digest         →  src/digest.py + git commit to digests/YYYY-MM-DD.md
12. send Telegram digest          →  src/telegram_digest.py       (header + photo+caption per listing + follow-up link line + 🙈 Hide button)
13. push state to R2              →  src/state.push()
```

### Spec milestone status (§15)

| # | Milestone | Status |
|---|---|---|
| M1 | Skeleton + secrets wired | ✅ done |
| M2 | One source end-to-end (4zida) | ✅ done |
| M3 | All four sources scraping | ✅ done (halooglasi via FlareSolverr sidecar) |
| M4 | LLM extraction layer | ✅ done (Claude Haiku 4.5 + tool-use + prompt caching) |
| M5 | Commute filter + caching | ✅ done (Routes API, not legacy Directions API) |
| M6 | Dedup + fingerprint cascade | ✅ done (pHash + coord + trigram; phone skipped — no source exposes it) |
| M7 | Digest format complete | ✅ done + 🙈 Hide button (off-spec but better UX) |
| M8 | Polling + perfect-match instant push | ✅ done (daily 04:30 cron = full digest; every-2h cron = silent unless new perfect match) |
| M9 | Error routing + cold-start silent seed | ⏳ pending |
| M10 | One-week production observation | ⏳ pending |

### Test coverage

90 tests, all green:
- `test_filter.py` — structural rules (ground floor, basement, elevator exemption)
- `test_filter_extraction.py` — LLM-aware rules (pets, dishwasher, heating, max-lease)
- `test_filter_commute.py` — walk-or-transit ≤ 30 min
- `test_four_zida.py`, `test_nekretnine.py`, `test_cityexpert.py`, `test_halooglasi.py` — per-source parsing
- `test_route.py` — Haversine math, cache hit/miss, FEWER_TRANSFERS request shape, 403 → DirectionsConfigError
- `test_extract.py` — Anthropic tool-use response parsing, per-listing failure isolation
- `test_dedup.py` — pHash compute, hamming, clustering, re-notify policy
- `test_score.py` — composite ordering invariants
- `test_telegram_digest.py` — body / links / header / overflow / empty-day
- `test_telegram_callbacks.py` — drain, offset advance, ignore unrelated callbacks

---

## 2. Key design decisions (and why they diverge from spec)

| Decision | Spec says | We do | Why |
|---|---|---|---|
| Dishwasher | hard requirement | soft preference (annotated in digest) | Serbian listings rarely mention `mašina za sudove` even when present; hard reject lost ~50/51 candidates |
| Ground floor | reject (`not ground`) | allowed | User explicitly opted in; relaxes the candidate pool |
| Elevator on floor 0 | required | waived | No lift needed when you're on the ground floor |
| Score weights | 0.4 price / 0.3 commute / 0.2 m² / 0.1 freshness | 0.45 walk / 0.25 transit / 0.10 m² / 0.10 freshness / 0.10 elevator; price not scored (hard cap €1100) | User: "anything ≤ €1100 is OK, walk and transit are most important" |
| Elevator | hard requirement (waived on floor 0) | soft preference — 0.10 score bonus instead of reject | User: central pre-war buildings rarely have lifts; we'd rather see them and let the score rank lift-equipped ones higher |
| Re-notify policy | suppress already-notified | always-surface mode (with 📌 seen-before badge) for now | Testing phase; flip `dedup.always_surface_matches: false` for production |
| 🙈 Hide button | spec says interactive bot is non-goal | implemented as inline-keyboard callback | Better UX than implicit auto-suppression; user requested |
| Halooglasi | scrape via plain httpx | via FlareSolverr sidecar | Their site is Cloudflare-Turnstile-protected, no other approach works without paid proxies |
| Transit routing | spec doesn't specify | `FEWER_TRANSFERS` preference, transfer count surfaced | User: "minimum transport change" |
| Directions API | spec says "Google Directions" | Routes API (new product) | Legacy Directions API isn't accepting new project enables; Google steered us to Routes |
| Pets unclear | spec hard-requires pets | silent pass on "unknown"; only explicit "no" hard-rejects | Most Serbian listings don't mention pets at all — demoting "unknown" to near-miss left ~0 perfect matches |
| Pet-friendly ranking | not in spec | listings with confirmed pets-allowed form a priority tier ABOVE non-pet-friendly, regardless of composite score | User: "if there IS info that the flat is pet friendly, push it on top always" |
| Cron behaviour | spec only mentions a daily digest | 04:30 UTC = full digest; every-2h polls 02-16 UTC (08-22 KGT) = silent instant push of NEW perfect matches | M8; "new" = no `notified_at` yet; polls skip nighttime (KGT 23-08) per `config.yaml.quiet_hours_kgt`; poll runs commit nothing to git to avoid churning commits |
| Source-error visibility in instant-push | M9 only spec'd "error routing" abstractly | every poll sends a brief `⚠️ Sources failed: <names>` if any portal returned an error | Instant-push has no digest header, so portal failures would otherwise be silent; alert fires every run a source is failing (no rate-limit yet) |

---

## 3. Persistent state (`db.sqlite` in R2)

| Table | Purpose |
|---|---|
| `meta(key, value)` | Schema version, telegram_update_offset |
| `listings(fingerprint_key, …, image_phash, notified_at, notified_price)` | Every listing we've seen, with timestamps and dedup hash |
| `commute_cache(bucket_key, walk_min, transit_min, transit_transfers, fetched_at)` | 90-day TTL per spatial bucket (3-decimal lat/lng or addr hash) |
| `skipped(fingerprint_key, skipped_at)` | User-clicked 🙈 Hide; suppresses listings before LLM/Routes |

Schema version is at **v8**. Migrations are forward-only and idempotent
(`ALTER TABLE` for adds, `DROP TABLE` for invalidations).

---

## 4. Infrastructure

- **CI runner**: GitHub Actions, public repo `arbaktos/belgrade_flat`, no minute limit
- **Sidecar service**: FlareSolverr (`ghcr.io/flaresolverr/flaresolverr:latest`) inside the same job — solves Cloudflare Turnstile for halooglasi
- **State**: `db.sqlite` in Cloudflare R2 bucket `belgrade-flats`, fetched at job start and pushed at end via `rclone`
- **Trigger**: `workflow_dispatch` only (manual). Cron entries (`30 4 * * *`, `0 */2 * * *`) currently emit heartbeat-only messages on schedule
- **Secrets** (in GH Actions Secrets, never in repo):
  `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`, `GOOGLE_DIRECTIONS_API_KEY`, `ANTHROPIC_API_KEY`,
  `R2_ACCESS_KEY_ID`, `R2_SECRET_ACCESS_KEY`, `R2_BUCKET`, `R2_ACCOUNT_ID`, `OFFICE_LAT`, `OFFICE_LNG`

### Cost ceiling (actual)

| Item | Monthly |
|---|---|
| GitHub Actions (public repo) | $0 |
| Cloudflare R2 (10 GB free) | $0 |
| Google Routes API | $0 (well within $200/mo free credit) |
| Anthropic Haiku 4.5 | ~$1–3 |
| Telegram | $0 |
| **Total** | **~$2/month so far** |

---

## 5. Known limitations / open issues

1. **Truncated 4zida descriptions** — the API returns a 100-char preview, not the full description. The LLM frequently returns "unknown" for pets/dishwasher/heating because the snippet doesn't mention them. Fix would be to fetch the detail page for survivors (adds ~50 requests per run).
2. **Halooglasi card-only data** — pHash works, but elevator/heating/furnishing aren't on the card. Detail-page enrichment via FlareSolverr would be expensive (CF challenge per page).
3. **No phone-normalization fingerprint** — none of the four sources expose phone numbers reliably in listing summaries; spec §6's phone-cascade key is dead-on-arrival for our sources.
4. **No photo carousel** — Telegram supports media groups (up to 10 photos), but sources only expose 1 image_url on the card; we'd need detail-page fetches for the rest.
5. **`already_notified` listings re-spend the 🚌 Routes API call** — should_notify check runs in dedup *after* commute; with `always_surface_matches: true` we still call Routes for repeats. Cache hits make this near-free but not zero.
6. **Cron crons fire on the old workflow if a push lands mid-run** — GH Actions checks out at trigger time, not push time. Mostly cosmetic given test-mode behavior.
7. **No empty-state Telegram message during cron** — cron heartbeat is a separate path that bypasses `telegram_digest`. Spec §8's "Nothing new today. All systems green." is only sent on dispatch runs.

---

## 6. Future plan

### M8 — Polling + perfect-match instant push (spec §7)

- Wire the every-2h cron to actually run the pipeline (not just heartbeat)
- For perfect matches not already notified, push immediately (skipping the
  digest header / overflow / near-miss block)
- Daily digest at 04:30 UTC (10:30 KGT) keeps the spec §8 format
- Quiet hours 23:00–08:00 KGT: poll runs continue, but Telegram pushes are
  suppressed; anything found overnight surfaces in the morning digest
- Implementation: branch on `GITHUB_EVENT_SCHEDULE` env var in `src/main.py`,
  reuse the existing pipeline, swap `telegram_digest.send()` for an
  `instant_push()` mode on the 2h cron

### M9 — Error routing + cold-start silent seed (spec §9, §10)

- **Cold start**: first ever run sees hundreds of listings. Mark them all as
  seen, send only "Initialized: tracked N listings as baseline". No noise.
  Implementation: detect empty `listings` table at run start.
- **Error routing**: prefix every uncaught failure with 🚨 + source name and
  send to the same Telegram chat. Currently the workflow's failure-step in
  `scrape.yml` already does this; bring per-source / per-stage error wrapping
  into the pipeline too (e.g. `🚨 LLM extraction crashed on listing X`).
- **Catastrophic errors** (R2 pull failed → no state): fire immediately
  regardless of quiet hours.
- **Source-level breakage** already surfaces in the health line
  (`halooglasi 0 ⚠️`); keep it that way (don't double-alert).

### M10 — Production observation week (spec §15)

- Flip `always_surface_matches` to `false`
- Disable workflow_dispatch (cron only)
- Run for 7 days
- Tune thresholds based on real signal (probably: m² floor at 50 instead of
  55? rooms cap at 3.5? heating allow-list?)

### Backlog (post-M10)

- **Detail-page enrichment** for 4zida (gets us full description for the
  LLM; fixes the truncation problem)
- **Multi-photo carousel** via Telegram `sendMediaGroup` (requires detail
  page for additional photos)
- **Per-listing notes** via Telegram bot reply (`/note <id> too loud`)
- **Price-history line** per listing (we have the data in SQLite, just need
  to surface)
- **Tighten halooglasi to detail pages** when CF challenge solving is stable
  (currently card-only; missing elevator/heating)
- **`/feedback/<id>/false-positive`** webhook from spec §16 — closes the
  loop on LLM hallucinations
- **Facebook groups** (spec §16) — Playwright + dedicated burner account
- **Multi-recipient** — currently `TELEGRAM_CHAT_ID` is one value; a
  comma-split would unlock husband-also-gets-pinged

---

## 7. Run book

### Trigger a dispatch run

```
gh workflow run scrape.yml --repo arbaktos/belgrade_flat --ref main
gh run watch <run-id> --repo arbaktos/belgrade_flat --exit-status
```

Or click "Run workflow" at
<https://github.com/arbaktos/belgrade_flat/actions/workflows/scrape.yml>.

### Local dev (no Docker available → halooglasi will be ⚠️)

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
cp .env.example .env  # fill in the 10 secrets
.venv/bin/python -m src.main
```

### Run tests

```
.venv/bin/python -m pytest tests/ -q
```

### Pull the latest committed digest

```
git pull --rebase
cat digests/$(date -u +%Y-%m-%d)-test.md
```

### Check Google API spend this month

The digest header line `🔢 Google API: N/40 000 this month` is the
SQLite-cached approximation. For the authoritative number, check Cloud
Console → Routes API → Metrics.

### Reset all dedup state (force re-notify of everything)

```sql
DELETE FROM skipped;
UPDATE listings SET notified_at=NULL, notified_price=NULL;
```

Run via `rclone copy r2:belgrade-flats/state/db.sqlite . && sqlite3 db.sqlite '...' && rclone copy db.sqlite r2:belgrade-flats/state/`.
