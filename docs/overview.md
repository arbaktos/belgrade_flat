# Overview

Looking for a flat in Belgrade means watching several portals at once, each with
its own search, its own duplicates, and its own ten-listings-an-hour churn. This
project collapses that into one feed: it watches the portals for you, throws out
everything that fails your rules, and sends only what's worth a look — once a
day, plus an instant ping when something genuinely good shows up.

It's a single-recipient personal tool. There is no web UI and no interactive
bot beyond two buttons (see [telegram.md](telegram.md)). Everything runs on a
schedule with no machine to keep alive — apart from one tiny webhook covered in
[architecture.md](architecture.md).

## Where listings come from

Four rental portals, scraped on every run:

| Portal | How it's read |
| --- | --- |
| `4zida.rs` | The JSON API its own frontend calls — the richest structured data. |
| `nekretnine.rs` | HTML scraping. |
| `halooglasi.com` | HTML behind a Cloudflare challenge, solved by a FlareSolverr sidecar. |
| `cityexpert.rs` | An API-style endpoint; mostly agency listings. |

Each portal lives in its own module under `src/sources/`. One failing portal
never sinks the run — a scrape that throws is caught, logged, and reported in
the digest's health line, while the other three carry on.

A separate, optional path watches public **Telegram channels** (for example
`@beograd_stan`). It runs after the portal pipeline with a looser filter — pets
must be allowed and the flat must clear a size floor — and dedupes its posts
against the portal listings. See [filtering-and-ranking.md](filtering-and-ranking.md#telegram-channel-side-pipeline).

## What happens to a listing

Every run walks one pipeline. Each stage is cheaper to run than the one after
it, so the expensive work (the language model, the Google walking-distance API)
only ever touches listings that already passed everything before it.

1. **Drain button clicks.** Apply any 🙈 Hide / ⭐ Favorite taps since the last
   run, so a freshly hidden listing is gone before this run can resurface it.
2. **Scrape** all four portals and record every listing in SQLite.
3. **Drop hidden listings** before any paid stage runs.
4. **Structural filter** — price, rooms, size, floor, freshness, and the like.
   Cheap, and it rejects the bulk of listings.
5. **LLM extraction** — send each survivor to Claude Haiku once, ever, to read
   the Serbian description for facts the card doesn't expose (pets, heating,
   dishwasher, lease length). Results are cached so a listing is never sent twice.
6. **LLM-aware filter** — apply the rules that need those extracted facts, and
   split borderline listings into a *near-miss* bucket instead of dropping them.
7. **Walking-distance filter** — compute minutes on foot to each named
   destination; reject anything beyond the gating threshold to the office.
8. **Dedup** — collapse the same flat listed across portals into one card.
9. **Rank** — order by a composite score (walking closeness dominates).
10. **Deliver** — render a Markdown digest, commit it to `digests/`, and send it
    to Telegram.

Stages 4–9 are detailed in [filtering-and-ranking.md](filtering-and-ranking.md).

## The daily rhythm

Two schedules drive the same pipeline in two modes:

- **Daily digest** at 04:30 UTC (10:30 in Asia/Bishkek). The full digest — a
  header, the ranked matches, and a near-miss section.
- **Instant push** every two hours through the day. Silent unless a *new*
  perfect match turned up since the last run; then it pushes just that card,
  with no digest framing.

Quiet hours (23:00–08:00 Bishkek time) suppress the instant pushes; anything
found overnight waits for the morning digest. A manual run always produces a
full digest. The mode is chosen from the cron expression that triggered the run
— see [architecture.md](architecture.md#run-modes).

## What it costs

| Item | Monthly |
| --- | --- |
| GitHub Actions (public repo) | $0 |
| Cloudflare R2 (10 GB free tier) | $0 |
| Google Routes API | $0 (inside the $200/mo free credit) |
| Anthropic Claude Haiku 4.5 | ~$1–3 |
| Telegram | $0 |
| **Total** | **~$2/month** |

The cost discipline behind those numbers — calling the LLM once per listing,
caching walking distances for 90 days, pre-filtering by straight-line distance —
is explained in [filtering-and-ranking.md](filtering-and-ranking.md).
