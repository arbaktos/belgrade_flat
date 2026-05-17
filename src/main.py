from __future__ import annotations

import logging
import os
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

import yaml
from dotenv import load_dotenv

from src import dedup, digest, extract, filter as filt, route, score, state, telegram, telegram_callbacks, telegram_digest
from src.models import Listing
from src.sources import cityexpert, four_zida, halooglasi, nekretnine

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("main")


@dataclass
class SourceResult:
    name: str
    listings: list[Listing]
    error: str | None = None


SOURCES: list[tuple[str, Callable[..., list[Listing]]]] = [
    ("4zida", four_zida.fetch),
    ("nekretnine", nekretnine.fetch),
    ("halooglasi", halooglasi.fetch),
    ("cityexpert", cityexpert.fetch),
]


def main() -> int:
    load_dotenv()

    schedule = os.environ.get("GITHUB_EVENT_SCHEDULE", "")
    run_id = os.environ.get("GITHUB_RUN_ID", "local")
    is_dispatch = not schedule

    cfg = yaml.safe_load(Path("config.yaml").read_text())
    pulled = state.pull()
    conn = state.ensure_schema()

    try:
        if is_dispatch:
            _run_pipeline(cfg, conn)
        else:
            stats = state.stats(conn)
            telegram.send_message(
                f"👋 Cron heartbeat ({schedule})\n"
                f"R2: {'pulled' if pulled else 'seeded'} ({stats['size_bytes']} B, "
                f"{stats['listings_tracked']} listings tracked)\n"
                f"Run: {run_id}"
            )
    finally:
        conn.close()

    state.push()
    return 0


def _run_pipeline(cfg: dict, conn) -> dict:
    log.info("dispatch: drain callbacks → scrape → structural → LLM → final → digest")
    freshness_days = int(cfg["filters"]["freshness_days"])
    cfg_obj = filt.from_dict(cfg)

    # Stage 0: drain any 🙈 Skip button clicks the user made since the last run.
    # Has to happen BEFORE the new digest so new skips take effect immediately.
    callback_counts = telegram_callbacks.drain(conn)
    skipped_set = telegram_callbacks.skipped_keys(conn)
    if skipped_set:
        log.info("user has skipped %d listings cumulatively", len(skipped_set))

    source_results = [_fetch_source(name, fn, freshness_days) for name, fn in SOURCES]
    all_listings: list[Listing] = []
    for sr in source_results:
        all_listings.extend(sr.listings)
    state.upsert_listings(conn, all_listings)

    # Drop user-skipped listings BEFORE the expensive stages (LLM, Routes API).
    if skipped_set:
        before = len(all_listings)
        all_listings = [l for l in all_listings if l.fingerprint_key not in skipped_set]
        log.info("skipped filter: dropped %d/%d listings", before - len(all_listings), before)

    # Stage 1: cheap structural filter — anything failing here gets no LLM call.
    structural = filt.apply(all_listings, cfg_obj)
    log.info("structural: %d passed, %d rejected", len(structural.passed), len(structural.rejected))

    # Stage 2: LLM extraction on structural survivors. Skip if no API key (e.g. local dev).
    extraction_failures = 0
    if "ANTHROPIC_API_KEY" in os.environ and structural.passed:
        log.info("extracting LLM facts for %d listings", len(structural.passed))
        _, extraction_failures = extract.extract_many(structural.passed)
    elif not structural.passed:
        log.info("no structural survivors — skipping LLM extraction")
    else:
        log.warning("ANTHROPIC_API_KEY not set — skipping LLM extraction")

    # Stage 3: post-LLM filter — pets, dishwasher, heating, max-lease + near-miss split.
    llm_filtered = filt.apply_with_extraction(structural.passed, cfg_obj)
    log.info("LLM filter: %d passed, %d near-miss, %d rejected",
             len(llm_filtered.passed), len(llm_filtered.near_misses), len(llm_filtered.rejected))

    # Stage 4: commute filter. Compute walk+transit minutes for both passed and
    # near-miss candidates (so even near-misses get the commute info surfaced),
    # then hard-filter the matches by walk≤30m OR transit≤30m.
    candidates_for_commute = llm_filtered.passed + [l for l, _ in llm_filtered.near_misses]
    commute_rejected: list[tuple[Listing, str]] = []
    commute_config_error: str | None = None
    if "GOOGLE_DIRECTIONS_API_KEY" in os.environ and candidates_for_commute:
        office_lat = float(os.environ["OFFICE_LAT"])
        office_lng = float(os.environ["OFFICE_LNG"])
        log.info("computing commute for %d candidates", len(candidates_for_commute))
        for l in candidates_for_commute:
            try:
                r = route.compute_commute(
                    l, office_lat=office_lat, office_lng=office_lng, conn=conn
                )
                l.walk_min = r.walk_min
                l.transit_min = r.transit_min
                l.transit_transfers = r.transit_transfers
            except route.DirectionsConfigError as e:
                # Config failure (REQUEST_DENIED / OVER_QUERY_LIMIT) won't get better
                # by retrying — stop calling the API for the rest of this run.
                log.error("commute aborted — Directions config error: %s", e)
                commute_config_error = str(e)
                break
            except Exception as e:  # noqa: BLE001
                log.warning("commute failed for %s: %s", l.fingerprint_key, e)

        if commute_config_error:
            log.warning("Skipping commute filter due to API config error; matches keep LLM-pass status")
            matched = llm_filtered.passed
        else:
            commute_passed = filt.apply_commute(llm_filtered.passed, cfg_obj)
            commute_rejected = commute_passed.rejected
            matched = commute_passed.passed
    else:
        log.warning("Skipping commute filter (no API key or no candidates)")
        matched = llm_filtered.passed

    # Stage 5: dedup — cross-portal duplicates. Compute pHash for matches +
    # near-misses (~50 image downloads), cluster matches by the cascade, pick
    # one canonical per cluster, then apply the re-notify policy. Near-misses
    # are not deduped in M6 — they're already a manual-vet bucket.
    candidates_for_phash = matched + [l for l, _ in llm_filtered.near_misses]
    dedup_stats = {"phashed": 0, "clusters": 0, "suppressed": 0}
    notify_reasons: dict[str, str] = {}
    if candidates_for_phash:
        dedup_stats["phashed"] = dedup.compute_phashes(candidates_for_phash)
        dedup.persist_phashes(candidates_for_phash, conn)

        clusters = dedup.cluster_duplicates(matched)
        dedup_stats["clusters"] = len(clusters)
        always_surface = bool(cfg.get("dedup", {}).get("always_surface_matches", False))
        surfaced: list[Listing] = []
        for cluster in clusters:
            canonical = dedup.pick_canonical(cluster)
            ok, reason = dedup.should_notify(canonical, conn)
            if ok or always_surface:
                surfaced.append(canonical)
                # If we're surfacing anyway despite a 'already_notified' verdict,
                # show that visibly so the user knows we've sent this before.
                effective_reason = reason if ok else "already_notified"
                notify_reasons[canonical.fingerprint_key] = effective_reason
                if ok:
                    dedup.mark_notified(canonical, conn)
            else:
                dedup_stats["suppressed"] += 1
        log.info("dedup: %d→%d canonical, %d suppressed (always_surface=%s)",
                 len(matched), len(surfaced), dedup_stats["suppressed"], always_surface)
        matched = surfaced

    # Composite score ordering: highest score first (best commute / cheapest / biggest / freshest).
    matched = score.rank_descending(
        matched, price_cap_eur=cfg_obj.price_eur_max, freshness_days=cfg_obj.freshness_days
    )
    # Near-misses also get sorted so the top 5 shown in Telegram are the best
    # of the bunch, not the first to be scraped.
    near_listings = [l for l, _ in llm_filtered.near_misses]
    near_listings = score.rank_descending(
        near_listings, price_cap_eur=cfg_obj.price_eur_max, freshness_days=cfg_obj.freshness_days
    )
    reasons_by_key = {l.fingerprint_key: r for l, r in llm_filtered.near_misses}
    llm_filtered = filt.FilterResult(
        passed=llm_filtered.passed,
        near_misses=[(l, reasons_by_key[l.fingerprint_key]) for l in near_listings],
        rejected=llm_filtered.rejected,
    )

    today = datetime.now(timezone.utc)
    source_stats = {sr.name: (len(sr.listings), sr.error) for sr in source_results}
    api_count = route.monthly_api_count(conn)

    # Merge all rejections (structural + LLM + commute) into final.rejected.
    all_rejected = structural.rejected + llm_filtered.rejected + commute_rejected
    full_result = filt.FilterResult(
        passed=matched,
        near_misses=llm_filtered.near_misses,
        rejected=all_rejected,
    )
    content = digest.render(
        full_result, source_stats=source_stats, today=today, api_count=api_count,
        commute_config_error=commute_config_error,
        notify_reasons=notify_reasons,
        dedup_stats=dedup_stats,
    )
    path = digest.write(content, today)
    log.info("digest written to %s", path)

    # Spec §8 Telegram delivery — header summary + per-listing messages.
    stats_after = state.stats(conn)
    try:
        telegram_digest.send(
            full_result,
            today=today,
            source_stats=source_stats,
            api_count=api_count,
            dedup_stats=dedup_stats,
            notify_reasons=notify_reasons,
            commute_config_error=commute_config_error,
            state_size_bytes=stats_after["size_bytes"],
            listings_tracked=stats_after["listings_tracked"],
            digest_path=f"digests/{today.strftime('%Y-%m-%d')}.md",
            office_lat=float(os.environ.get("OFFICE_LAT", 0)),
            office_lng=float(os.environ.get("OFFICE_LNG", 0)),
        )
    except Exception as e:  # noqa: BLE001 - delivery failure shouldn't lose state
        log.error("telegram digest send failed: %s", e, exc_info=True)

    return {
        "source_results": source_results,
        "fetched": len(all_listings),
        "structural_passed": len(structural.passed),
        "matched": len(matched),
        "near_miss": len(llm_filtered.near_misses),
        "rejected": len(all_rejected),
        "extraction_failures": extraction_failures,
        "api_count": api_count,
        "digest_path": str(path),
    }


def _fetch_source(name: str, fn: Callable[..., list[Listing]], freshness_days: int) -> SourceResult:
    try:
        listings = fn(freshness_days=freshness_days)
        log.info("source %s: %d listings", name, len(listings))
        return SourceResult(name=name, listings=listings)
    except Exception as e:  # noqa: BLE001 - one bad source must not kill the whole run
        log.warning("source %s failed: %s", name, e, exc_info=True)
        return SourceResult(name=name, listings=[], error=str(e))


if __name__ == "__main__":
    sys.exit(main())
