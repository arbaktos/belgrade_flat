# Belgrade Rental Notifier — Execution Spec

A personal scraper that delivers filtered Belgrade rental listings to Telegram once a day at 11:00 Asia/Bishkek, with optional instant pushes for perfect matches throughout the day.

---

## 1. Goals & non-goals

**Goals**
- Aggregate fresh Belgrade rentals across the four major portals into one filtered feed.
- Apply strict structural filters + LLM-extracted text filters before sending.
- Compute walk/transit commute time from a fixed office anchor and filter by it.
- Deliver a calm daily digest at 11:00 Bishkek time; push perfect matches instantly (within 2 h).
- Run for ≈ $5/month all-in.

**Non-goals (v1)**
- Facebook groups (deferred to v2).
- Interactive Telegram commands (`/pause`, `/status`, etc. — design assumes push-only).
- Multi-user. Single-recipient personal tool.
- Real-time (<1 min) listing detection.

---

## 2. Sources

| Portal | Method |
|---|---|
| `4zida.rs` | JSON API used by their frontend |
| `nekretnine.rs` | HTML scraping |
| `halooglasi.com` | HTML scraping |
| `cityexpert.rs` | API-ish, agency listings |

Lightweight-first scraping (`httpx` + `parsel`). Playwright headless Chromium kept in the toolbox as fallback for any source that begins 403'ing.

---

## 3. Filter spec

### Hard filters (must pass)

| Filter | Rule |
|---|---|
| Price (rent only) | ≤ €1000 / month |
| Rooms | 1.5 – 3.0 (Serbian system: 0.5 studio, 1.0 1BR, 1.5 1BR+alcove, …) |
| Surface | ≥ 55 m² |
| Heating | one of `централно` (district) / `етажно` (own boiler) / `подно` (underfloor). Reject TA peć, climas-only, electric-panels-only. |
| Pets | allowed (hard requirement; ambiguous from text → routed to near-miss section, not silently rejected) |
| Furnishing | furnished or semi-furnished (unfurnished rejected) |
| Floor | not ground, not basement |
| Elevator | required |
| Max lease commitment | ≤ 12 months (reject listings demanding 24-month+ contracts) |
| Dishwasher | required |
| Freshness | posted ≤ 7 days ago |
| Commute | walk ≤ 30 min **OR** transit ≤ 30 min from `Kneza Miloša 88A, Beograd` |
| Agencies | allowed |

### Annotations (don't filter, just surface)

- Flag `bills > €200` in digest when LLM detects it in description.
- Surface `agency-or-owner` classification per listing.
- Surface LLM `red_flags` (e.g. "students only", "no smoking", "shared bathroom").

---

## 4. Commute computation

- **Anchor:** `Kneza Miloša 88A, Beograd, Serbia` → geocoded once, lat/lng stored in GH Secrets (`OFFICE_LAT`, `OFFICE_LNG`).
- **Semantics:** flat passes if walk ≤ 30 min OR transit ≤ 30 min (union of two isochrones). Driving excluded.
- **Engine:** Google Directions API.
- **Cost control:**
  1. Cheap 10 km Haversine pre-filter.
  2. Routing only after every other hard filter passes.
  3. Spatial-bucket cache keyed by lat/lng rounded to 3 decimals (~110 m grid). Positive + negative cache. 90-day TTL.
  4. Hard daily cap (500 calls) configured in Google Cloud as a runaway-loop safety.
- **Reporting:** every digest includes `Google API: <used>/40 000 this month`.

---

## 5. LLM extraction layer

Three filters (pets, dishwasher, max-lease) and several annotations (bills, agency, heating-confirmed, red-flags) are not reliably exposed as structured fields. After structural filters pass, each candidate's free-text description goes to **Claude Haiku 4.5** with a single prompt returning a JSON blob:

```json
{
  "pets_allowed": "yes" | "no" | "unknown",
  "dishwasher": true | false | null,
  "elevator_confirmed": true | false | null,
  "heating_type_confirmed": "centralno" | "etazno" | "podno" | "TA" | "...",
  "max_lease_months": 12 | 24 | null,
  "bills_estimate_eur": 150 | null,
  "agency_or_owner": "agency" | "owner" | "unknown",
  "red_flags": ["..."]
}
```

Listings that pass all filters also get an English summary (2–3 sentences) generated in the same call or a follow-up call.

Budget: 50–200 candidates/day × ~500 tokens ≈ $3–6/month.

---

## 6. Identity & dedup

Cascade fingerprint — any of these collisions means "same flat":

1. Normalized phone (if visible)
2. Coords (4 decimals) + m² + price-bucket (€50)
3. Image pHash of first photo (**workhorse** — same agency photos appear on every portal)
4. Title trigram similarity ≥ 0.8 + price ±5% (fallback)

Stored per-listing in SQLite. Every new candidate checks all four keys against the index.

**Re-notify policy:**
- Send again if price drops ≥ 5%.
- Send again if listing reappears after > 14 days of silence.
- Otherwise stay quiet on duplicates.

**Daily re-check:** existing active listings are re-fetched once daily so an edit ("added dishwasher") can move a previously-rejected flat into eligibility.

---

## 7. Runtime

- **Host:** GitHub Actions, public repo (unlimited minutes).
- **Language:** Python.
- **State:** SQLite file in Cloudflare R2 (free tier). Each run starts by `rclone copy r2:state/db.sqlite .`, ends by `rclone copy db.sqlite r2:state/`.
- **No long-running process.** No interactive bot. Configuration via `config.yaml` committed to the repo; pause via "Disable workflow" in GH UI; force run via "Run workflow" button.

### Schedules (single workflow, two cron entries)

```yaml
on:
  schedule:
    - cron: '30 4 * * *'    # 04:30 UTC = 10:30 Asia/Bishkek — daily digest
    - cron: '0 */2 * * *'   # every 2h — perfect-match poll
  workflow_dispatch: {}
```

The job branches on `github.event.schedule`:

- **Digest run:** scrape → filter → LLM extract → translate → build & send digest → commit to `/digests/YYYY-MM-DD.md` → upload state.
- **Poll run:** scrape → filter → LLM extract → for each *new perfect match* not already in SQLite, push immediately to Telegram → upload state. No digest, no "nothing new" message.

**Quiet hours:** poll runs continue executing 23:00–08:00 KGT but suppress Telegram pushes. Anything found overnight surfaces in the next morning digest.

---

## 8. Digest format

### Header message

```
Belgrade rentals — 2026-05-16
3 new perfect matches · 5 near-misses
🩺 4zida 47 · nekretnine 12 · halooglasi 0 ⚠️ · cityexpert 3
🔢 Google API: 47/40 000 this month
💾 R2 state: 312 KB · 1 847 flats tracked
```

### One message per listing

```
✅ €890 · 2.0 rooms · 62 m² · Vračar
📍 Krunska 35 — 18 min walk / 22 min transit
🔥 Centralno · 🐾 pets OK · 🍽 dishwasher · 🛗 lift · floor 3/5
📅 Posted 2 hours ago · 🏢 agency (½-month fee)

[English summary, 2–3 sentences from LLM]

🇷🇸 Original (Serbian, expandable)

🔗 <portal link>
🗺 Google Maps · 🚶 Walk route · 🚌 Transit route

[Photo media-group, first 5 photos]
```

### Limits & ordering

- **10 perfect matches + 5 near-misses max** per digest. Overflow → `(+12 more — see /digests/2026-05-16.md)`.
- **Sort by composite score:**
  `0.4·(1 − price/cap) + 0.3·(1 − commute_min/30) + 0.2·(m²/80) + 0.1·freshness_hours`
- **Score is not shown to user** — used only for ordering.

### Near-miss section

Flats failing one or two soft criteria are shown below perfect matches, each labeled with what they missed:

```
⚠️ Missing: dishwasher
⚠️ 52 m² (cap 55)
```

### Empty days

If 0 matches, send `Nothing new today. All systems green.` with the same health-summary footer. User uses this as the dead-man's-switch: no message by ~11:15 KGT = something is wrong, check GH Actions logs.

---

## 9. Failures & errors

- **Errors → same Telegram chat, prefixed with 🚨** and the source name. One inbox, one place to look.
- **Catastrophic errors** (R2 fetch failed → no state available) fire immediately regardless of quiet hours.
- **Source-level breakage** surfaces in the daily health line (`halooglasi: 0 ⚠️`), not as a separate alert, so degradation is visible without being noisy.
- **No external dead-man's-switch (healthchecks.io)** — user is the watcher (silence by 11:15 KGT = check logs).

---

## 10. Cold start

First run sees ~hundreds of currently-on-market listings, all "new" to an empty SQLite.

**Behavior: silent seed.** All listings marked as seen, no digest sent on day 1 beyond a short "Initialized: tracked N listings as baseline" message. Real notifications start from day 2 forward.

---

## 11. Repo layout

```
.
├── .github/workflows/scrape.yml
├── config.yaml                 # filter values (non-sensitive)
├── digests/
│   └── YYYY-MM-DD.md           # auto-committed daily archive
├── src/
│   ├── scrape/                 # per-portal modules
│   ├── filter/                 # structural filtering
│   ├── extract/                # LLM extraction + translation
│   ├── route/                  # Google Directions + cache
│   ├── dedup/                  # fingerprint logic
│   ├── digest/                 # message building
│   ├── telegram/               # send
│   ├── state/                  # SQLite + R2 sync
│   └── main.py                 # entry point, branches on schedule
├── tests/
└── README.md
```

`config.yaml` is committed (non-sensitive filter values). Office coordinates live in GH Secrets, not in the repo.

---

## 12. Secrets

All in GitHub Actions Secrets, never in repo:

| Secret | Source |
|---|---|
| `TELEGRAM_BOT_TOKEN` | @BotFather `/newbot` |
| `TELEGRAM_CHAT_ID` | from one-time `/start` message capture |
| `GOOGLE_DIRECTIONS_API_KEY` | Google Cloud, restricted to Directions API only |
| `ANTHROPIC_API_KEY` | console.anthropic.com |
| `R2_ACCESS_KEY_ID` | Cloudflare R2 |
| `R2_SECRET_ACCESS_KEY` | Cloudflare R2 |
| `R2_BUCKET` | Cloudflare R2 |
| `OFFICE_LAT` | geocoded from Kneza Miloša 88A |
| `OFFICE_LNG` | geocoded from Kneza Miloša 88A |

---

## 13. Cost ceiling

| Item | Monthly |
|---|---|
| GitHub Actions (public repo) | $0 |
| Cloudflare R2 (10 GB free) | $0 |
| Google Directions API (within $200 free credit) | $0 |
| Anthropic Haiku 4.5 | $3 – 6 |
| Telegram | $0 |
| **Total** | **≈ $5/month** |

---

## 14. Pre-implementation checklist (user actions)

Before code can be written:

1. Create new **public GitHub repo**.
2. **Telegram bot:** DM `@BotFather`, run `/newbot`, save bot token; then DM the new bot once so we can capture your chat ID.
3. **Google Cloud:** create project, enable Directions API, create restricted API key, set hard daily cap (500 calls).
4. **Anthropic API key** from `console.anthropic.com`.
5. **Cloudflare account + R2 bucket** (free tier).
6. **Confirm office geocode:** `Kneza Miloša 88A, Beograd` → verify pin location before locking coords.

---

## 15. Implementation milestones

| # | Milestone | Definition of done |
|---|---|---|
| M1 | Skeleton + secrets wired | Empty workflow runs, reads/writes R2, sends "hello" to Telegram |
| M2 | One source end-to-end (4zida) | Scrape → SQLite → filter → simulate digest. No dedup yet. |
| M3 | All four sources scraping | Health line shows non-zero per source |
| M4 | LLM extraction layer | JSON extraction working, translation working on passing listings |
| M5 | Commute filter + caching | Google Directions integrated with spatial cache |
| M6 | Dedup + fingerprint cascade | Image pHash live, no cross-portal duplicates |
| M7 | Digest format complete | Header + per-listing messages + near-miss + photos |
| M8 | Polling (every-2h) + perfect-match instant push | Two cron triggers branching correctly |
| M9 | Error routing + cold-start logic | Silent seed verified, 🚨 errors routed to chat |
| M10 | One-week production observation | Adjust filter thresholds based on real signal |

---

## 16. Known limitations (v1) and v2 candidates

- **No false-positive feedback loop.** If LLM hallucinates "dishwasher: yes" and a viewing reveals none, there's no learning signal. v2: `/feedback/<id>/false-positive` URL that retro-tags and nudges prompt.
- **No price-history visibility.** Data is captured in SQLite but not surfaced. v2: `📉 was €1100 7 days ago` line per listing.
- **Facebook groups not covered.** v2: Playwright + dedicated burner FB account, run on a separate cadence.
- **Up to 2 h latency on perfect matches.** Lower latency requires moving to an always-on host (Hetzner ~€4.59/mo) and replacing GH Actions with a daemon.
- **Single recipient.** Adding husband's chat as a second Telegram target is a one-line change but not implemented in v1.
