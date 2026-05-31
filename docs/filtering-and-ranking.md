# Filtering and ranking

A raw listing passes through several gates before it reaches a Telegram card.
The gates run cheapest-first, so the work that costs money — the language model,
the Google walking-distance API — only ever touches listings that already
survived everything before it. This doc walks each gate in order. The thresholds
behind them live in `config.yaml`, documented in [configuration.md](configuration.md).

## 1. Structural filter — `filter.apply()`

The cheap pass. It reads only fields the portal card already gives us, so a
rejection here costs nothing downstream. A listing must clear all of:

- **Price** — above €0 and at or below the cap.
- **Rooms** — inside the configured range. Belgrade counts in half-rooms: 0.5 is
  a studio, 1.0 a one-bedroom, 1.5 a one-bedroom with an alcove.
- **Surface** — at or above the size floor.
- **Floor** — known, and not a basement. Ground floor is allowed.
- **Elevator** — only checked above the ground floor, and only when the source
  actually states there's no lift. A missing value isn't a rejection.
- **Freshness** — posted inside the freshness window.
- **Photo** — has an image.

The first failing rule is recorded as the rejection reason, which surfaces in
the digest's reject tally.

A design note worth knowing: the elevator rule is deliberately soft. Central
pre-war buildings rarely have lifts, so a stated absence above the ground floor
rejects, but an *unknown* passes and the lift instead becomes a ranking bonus
(see [Scoring](#6-scoring--scorepy)). STATUS.md §2 records why several thresholds are set
the way they are — read it before changing one.

## 2. LLM extraction — `extract.py`

Portal cards rarely state pets, dishwasher, heating, or lease length, but the
Serbian free-text description often does. Each structural survivor goes to
**Claude Haiku 4.5** once, via tool use, which returns a structured `Extraction`
record:

| Field | Meaning |
| --- | --- |
| `pets_allowed` | `yes` / `no` / `unknown` |
| `dishwasher` | true / false / null |
| `heating_type_confirmed` | `centralno`, `etazno`, `podno`, `TA`, `klima`, … |
| `furnishing_confirmed` | `furnished` / `semi-furnished` / `unfurnished` |
| `max_lease_months` | shortest lease the listing demands |
| `bills_estimate_eur`, `agency_or_owner`, `red_flags` | surfaced in the card |
| `summary_en`, `description_en` | English translation for the card |

Two cost disciplines keep this cheap:

- **Extract once, ever.** Results are cached in `extraction_cache` keyed by the
  listing's fingerprint. A listing already in the cache never hits the API
  again; only its first sighting does. A cache miss just re-extracts — the cache
  is an optimization, never a correctness gate.
- **Cached system prompt.** The system prompt is sent with `cache_control:
  ephemeral` so Anthropic bills it once per cache window, not once per listing.

The provider is selectable: Claude Haiku is the default; flipping `LLM_PROVIDER`
to `gemini` swaps in Gemini 2.5 Flash with its own rate pacing and circuit
breaker. STATUS.md §2 explains why the project is back on Anthropic.

A failed extraction leaves the listing's `extraction` as `None`; it isn't
cached, so the next run retries it.

## 3. LLM-aware filter — `filter.apply_with_extraction()`

Now the rules that need those extracted facts run. This is where the
**near-miss** idea matters: a definitive *no* hard-rejects, but an *unclear*
field routes the listing to a near-miss bucket instead of dropping it silently.
The digest shows near-misses in their own section so you can vet them by hand.

| Rule | Hard-reject when | Near-miss when |
| --- | --- | --- |
| Pets | the listing explicitly forbids pets | — (silence passes; see below) |
| Dishwasher | source or LLM says there's none | neither can tell |
| Heating | the type isn't on the allow-list | type is unclear |
| Furnishing | it's outside the allowed set | furnishing is unclear |
| Lease | the minimum lease exceeds the cap | — |

Two deliberate asymmetries:

- **Pets default to silence-passes.** Most Belgrade listings never mention pets,
  so treating "unknown" as a near-miss left almost nothing in the matches
  bucket. Only an explicit "no" rejects. A confirmed *yes*, by contrast, lifts
  the listing into a priority tier when ranking.
- **Structured field beats the LLM.** When a portal exposes a field directly
  (4zida's furnishing, for instance), that value wins; the LLM only fills gaps.
  Source-specific vocabularies (`namešten`, `district`, `etažno`, …) are mapped
  to a canonical set in `filter.py`.

## 4. Walking-distance filter — `route.py` + `filter.apply_commute()`

What ranks a flat here is how close it is on foot to the places that matter, not
its commute by bus. The model is **walking minutes to each named destination**.

Destinations are configured by name in `config.yaml`; their coordinates come
from environment variables (kept in GitHub Secrets, never committed — this is a
public repo). Each destination carries two flags:

- **`gates`** — does its walking time decide pass/fail? The office gates; a flat
  beyond the office walking threshold is rejected. A destination with no walking
  route at all also fails the gate.
- **`score_weight`** — how much closeness to it influences ranking. An
  info-only destination (one that doesn't gate) still contributes to the score
  and shows its walking time on the card.

Three cost controls sit in front of the Google Routes API:

- **Straight-line pre-filter.** A 10 km Haversine check discards listings
  obviously too far to walk before any API call.
- **90-day cache.** Results are keyed by a 3-decimal lat/lng bucket (~110 m
  grid) plus the destination name, so flats in the same building share a cached
  walk and each (building, destination) pair is fetched once a quarter.
- **Fail fast on config errors.** A `REQUEST_DENIED` or quota error raises
  `DirectionsConfigError`, which stops the API loop for the whole run rather
  than burning the rest of the budget on calls that will also fail. The digest
  degrades gracefully — matches keep their LLM-pass status.

The legacy Directions API isn't accepting new project enables, so this uses the
newer **Routes API**. The digest header shows an approximate month-to-date call
count from the cache.

## 5. Dedup — `dedup.py`

Agencies syndicate the same flat across portals, so the same listing can arrive
four times. Dedup collapses each cluster to one canonical card using a cascade,
in priority order:

1. **Image perceptual hash (pHash).** The workhorse: the same photo, even
   re-encoded, hashes within 6 bits. Computing it costs one image download per
   listing.
2. **Coordinates + size + price bucket.** A building-level match when
   coordinates exist — same 4-decimal coordinate, size within 1 m², price in the
   same €50 bucket.
3. **Title similarity + price.** A fallback: title trigram similarity ≥ 0.8 and
   price within 5%.

The canonical pick prefers the richest source — 4zida, then cityexpert,
nekretnine, halooglasi — and the most recent post within a source. Phone-number
matching, the spec's fourth cascade key, is dead on arrival because no source
exposes phone numbers reliably.

Suppression of already-seen listings now lives in the user's 🙈 button, not in
dedup. Dedup still stamps a listing as notified and adds a 📉 badge when the
price dropped at least 5% since it was last surfaced.

## 6. Scoring — `score.py`

The surviving matches are ordered by a composite score. Price isn't scored at
all — the hard cap already gates it, and anything under the cap is acceptable.
What differentiates listings is walking closeness:

```text
score = Σ_dest  weight_dest · (1 − walk_min_dest / 40)   # closeness to each destination
      + 0.10 · (m² / 80)                                  # size, with diminishing returns
      + 0.10 · freshness                                  # 1.0 just-posted → 0.0 at window edge
      + 0.10 · elevator                                   # 1.0 has a lift, else 0.0
```

With the default weights (office 0.45, the other destination 0.25, plus the
three 0.10 terms) the maximum sums to 1.0. The score is never shown — it only
orders the digest. Higher is better.

One override sits above the score: a **confirmed pet-friendly** listing always
ranks above any non-pet-friendly one, whatever their scores. Within each tier
the composite score breaks ties.

## Annotations (never filter)

Some signals decorate the card without affecting pass/fail or order:

- **Winter smog** — `winter_smog.py` looks each flat's location up in an offline
  PM2.5 grid built from CAMS reanalysis and motorway proximity (see
  `scripts/build_winter_smog_grid.py`). The card gets a one-line band — better /
  moderate / worse for Belgrade — and a warning when the area is in the city's
  worst third for bad-air days. Locations without coordinates are geocoded via
  Nominatim and cached in `geocode_cache`.
- **Bills, agency-or-owner, red flags** — surfaced from the LLM extraction.

## Telegram-channel side pipeline

`telegram_channel_pipeline.py` runs after the portal pipeline and is independent
of it. It reads public Telegram channels listed under `telegram_channels` in
`config.yaml`, applies only two filters — pets must be allowed and size must
clear the channel's floor — and dedupes posts against the portal `listings`
table by pHash. A channel can require a hashtag (for example `#аренда`) so a
general-interest channel doesn't flood the LLM with non-rental posts; the
hashtag check runs before the LLM call, so noise costs no tokens. Processed
posts are recorded in `seen_telegram_posts` by message id, so each post is
handled exactly once across runs.
