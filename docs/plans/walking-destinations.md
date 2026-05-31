# Plan: Walking-only, multi-destination commute

_Drafted: 2026-05-31. Status: agreed (design forks resolved by user)._

## What changes
Replace the single-office **walk + transit** commute model with **N named
walking destinations**. Transit is removed entirely.

Destinations (initial):
| Name | Coords source | Role |
|---|---|---|
| office | `OFFICE_LAT`/`OFFICE_LNG` secrets (existing) | **gates** the filter at ≤ 40 min walk; score weight 0.45 |
| Sadik Enter | `SADIK_LAT`/`SADIK_LNG` secrets (new) | info + score only (never filters); score weight 0.25 |

### Decisions (from user)
- **Filter:** office walk ≤ **40 min** (raised from 30). Sadik never filters.
- **Score:** office walk 0.45 + Sadik walk 0.25 + m² 0.10 + freshness 0.10 +
  elevator 0.10 = 1.0. (Sadik's 0.25 is the slot transit used to hold.)
- **Coords:** Sadik kept in GitHub Secrets (public repo). config lists
  destination *names* + which env vars hold their coords — no real locations
  committed.
- **Walking distance** = real walking time via Google Routes `WALK` mode
  (unchanged); we just drop the second `TRANSIT` call per listing → halves
  Google API spend.

## New module: `src/destinations.py`
```python
@dataclass(frozen=True)
class Destination:
    name: str
    lat: float
    lng: float
    gates: bool          # True → its walk gates the commute filter
    score_weight: float
def load(cfg) -> list[Destination]   # resolves lat/lng from the named env vars
```
Loaded once in `main`, threaded into route/filter/score/digest so no module
hard-codes "office"/"Sadik Enter".

## File-by-file

| File | Change |
|---|---|
| `config.yaml` | `filters.commute`: `walk_min_max: 40`, drop `transit_min_max`, add `destinations:` list (name + lat_env/lng_env + gates + score_weight). |
| `src/models.py` | Replace `walk_min`/`transit_min`/`transit_transfers` with `commute: dict[str, int \| None]` (destination-name → walk minutes). `to_row()` strips `commute`. |
| `src/state.py` | Schema **v13**: DROP + recreate `commute_cache` as `(bucket_key TEXT PK, walk_min INTEGER, fetched_at)`. `bucket_key` now embeds the destination so each (location, destination) pair caches separately. |
| `src/route.py` | `CommuteResult` → `{walk_min, source}`. New `compute_walk(listing, dest, conn)` — one WALK query, per-destination haversine pre-filter + cache. Delete transit query, `_count_transit_transfers`, FEWER_TRANSFERS. `monthly_api_count` → rows × 1. Keep `DirectionsConfigError` short-circuit. |
| `src/filter.py` | `FilterConfig.walk_min_max=40`, drop `transit_min_max`. `apply_commute(listings, cfg, gating_names)`: pass iff every gating destination's walk ≤ max. |
| `src/score.py` | Iterate destinations: `Σ d.score_weight · term(l.commute[d.name], CAP=40)`. Drop transit. `score`/`rank_descending` take `destinations` instead of `price_cap_eur`. |
| `src/digest.py` | Per-destination line: `🚶 N min to <name>` (or `🚶 ? to <name>`). |
| `src/telegram_digest.py` | Same, each line a clickable walking-route link to that destination's coords. Drop the transit (`🚌`) block. |
| `src/telegram_channel_pipeline.py` | Channel posts compute walk to the gating (office) destination only; show `🚶 N min`. |
| `src/main.py` | Load destinations once; compute walks per candidate × destination; gate on office; pass destinations to score + digest. Both digest and instant-push paths. |
| `.github/workflows/scrape.yml` | Add `SADIK_LAT`/`SADIK_LNG` env from secrets. |
| `.env` | Add `SADIK_LAT`/`SADIK_LNG` for local parity. |
| Secrets | `gh secret set SADIK_LAT` / `SADIK_LNG` = `44.8070245817281` / `20.464452838679918`. |
| `STATUS.md` | Update §2 (threshold 30→40, transit dropped, Sadik destination), §3 (schema v13, commute_cache shape). |

## Tests
- `test_route.py`: rewrite for walk-only + per-destination cache key; drop transit assertions.
- `test_score.py`: rewrite `_l(...)` to set `commute={...}`; new weight expectations (office 0.45 / Sadik 0.25); drop transit-specific tests; add "closer-to-office scores higher", "Sadik contributes", "office-only when Sadik missing".
- `test_filter_commute.py`: office-gates-at-40, Sadik-never-filters.
- `test_telegram_digest.py`: per-destination walk lines, no transit line.
- New `test_destinations.py`: load resolves env coords, gates/weights parsed, missing env handled.

## Migration / cost notes
- Schema v13 drop is forward-only and idempotent (same pattern as v5/v7 commute_cache drops). The cache simply rebuilds — walk re-queries are cheap and the haversine pre-filter skips far listings.
- Dropping the TRANSIT call halves Google Routes spend (was 2 calls/listing → 1 per listing × destinations; with office+Sadik that's 2 calls/listing, same as before, but no transit — net neutral, and far listings skip per-destination).

## Out of scope
- Configurable per-destination walk caps (single CAP=40 for all).
- Changing the haversine pre-filter (stays 10 km; generous for a 40-min walk).
