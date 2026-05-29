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

from src import dedup, digest, extract, filter as filt, route, score, state, telegram, telegram_callbacks, telegram_channel_pipeline, telegram_digest, winter_smog
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


# Cron expressions in .github/workflows/scrape.yml. Anything else maps to digest.
DIGEST_CRON = "30 4 * * *"
POLL_CRON = "0 */2 * * *"


def main() -> int:
    load_dotenv()

    schedule = os.environ.get("GITHUB_EVENT_SCHEDULE", "")
    run_id = os.environ.get("GITHUB_RUN_ID", "local")
    # Mode selection:
    #   workflow_dispatch (no schedule)     → full digest (manual run)
    #   schedule == DIGEST_CRON             → full digest
    #   schedule == POLL_CRON               → instant push of NEW perfect matches only
    #   any other cron value                → instant push (safe default)
    if not schedule or schedule == DIGEST_CRON:
        mode = "digest"
    else:
        mode = "instant"
    log.info("main: mode=%s schedule=%r run=%s", mode, schedule, run_id)

    cfg = yaml.safe_load(Path("config.yaml").read_text())
    state.pull()
    conn = state.ensure_schema()

    try:
        _run_pipeline(cfg, conn, mode=mode)
    finally:
        conn.close()

    state.push()
    return 0


def _run_pipeline(cfg: dict, conn, *, mode: str = "digest") -> dict:
    log.info("dispatch: drain callbacks → scrape → structural → LLM → final → %s",
             "digest" if mode == "digest" else "instant push")
    freshness_days = int(cfg["filters"]["freshness_days"])
    cfg_obj = filt.from_dict(cfg)

    # Snapshot "already-notified" before any pipeline mutation so instant-push
    # can identify genuinely new matches even though dedup.mark_notified runs
    # mid-pipeline (pre-Telegram delivery, a long-standing quirk).
    previously_notified = state.notified_keys(conn) if mode == "instant" else set()

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
    # Cached extractions are reused so each listing hits the LLM exactly once
    # across runs — keeps us under the Gemini free-tier caps and cuts cost.
    extraction_failures = 0
    if extract.llm_api_key_present() and structural.passed:
        cached = state.load_extractions(
            conn, [l.fingerprint_key for l in structural.passed]
        )
        to_extract = []
        for l in structural.passed:
            hit = cached.get(l.fingerprint_key)
            if hit is not None:
                l.extraction = hit
            else:
                to_extract.append(l)
        log.info("extraction: %d cached, %d to fetch via %s",
                 len(structural.passed) - len(to_extract), len(to_extract),
                 extract._provider())
        if to_extract:
            _, extraction_failures = extract.extract_many(to_extract)
            # Persist only successes; failed (extraction=None) listings retry next run.
            state.save_extractions(conn, to_extract)
    elif not structural.passed:
        log.info("no structural survivors — skipping LLM extraction")
    else:
        log.warning("LLM API key not set for provider=%s — skipping extraction",
                    extract._provider())

    # Stage 3: post-LLM filter — pets, dishwasher, heating, max-lease + near-miss split.
    llm_filtered = filt.apply_with_extraction(structural.passed, cfg_obj)
    log.info("LLM filter: %d passed, %d near-miss, %d rejected",
             len(llm_filtered.passed), len(llm_filtered.near_misses), len(llm_filtered.rejected))

    # Stage 4: commute filter. Compute walk+transit minutes for both passed and
    # near-miss candidates (so even near-misses get the commute info surfaced),
    # then hard-filter the matches by walk≤30m OR transit≤30m.
    candidates_for_commute = llm_filtered.passed + [l for l, _ in llm_filtered.near_misses]
    if candidates_for_commute:
        winter_smog.enrich_many(candidates_for_commute, conn)

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
        surfaced: list[Listing] = []
        for cluster in clusters:
            canonical = dedup.pick_canonical(cluster)
            surfaced.append(canonical)
            reason = dedup.price_drop_reason(canonical, conn)
            if reason:
                notify_reasons[canonical.fingerprint_key] = reason
            dedup.mark_notified(canonical, conn)
        log.info("dedup: %d→%d canonical", len(matched), len(surfaced))
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

    stats_after = state.stats(conn)
    office_lat = float(os.environ.get("OFFICE_LAT", 0))
    office_lng = float(os.environ.get("OFFICE_LNG", 0))
    try:
        if mode == "instant":
            # Surface source errors here — instant-push has no digest header,
            # so a portal failure would otherwise be invisible to the user.
            # Daily/manual runs already render errors in the digest header.
            failed = [sr.name for sr in source_results if sr.error]
            if failed:
                try:
                    telegram.send_message(
                        f"⚠️ Sources failed during poll: {', '.join(failed)}"
                    )
                except Exception as e:  # noqa: BLE001
                    log.warning("source-error alert failed: %s", e)

            fresh = [l for l in matched if l.fingerprint_key not in previously_notified]

            # Near-misses also get instant-pushed, gated on the same commute
            # requirement as matches (walk≤30m OR transit≤30m) so we don't
            # spam listings that fail the non-negotiable axis. If the Routes
            # API errored out this run, we surface near-misses ungated —
            # matching the digest's degradation behaviour above.
            near_with_reasons = llm_filtered.near_misses
            if commute_config_error:
                commute_ok_keys = {l.fingerprint_key for l, _ in near_with_reasons}
            else:
                near_commute = filt.apply_commute(
                    [l for l, _ in near_with_reasons], cfg_obj,
                )
                commute_ok_keys = {l.fingerprint_key for l in near_commute.passed}
            fresh_near = [
                (l, reasons) for l, reasons in near_with_reasons
                if l.fingerprint_key in commute_ok_keys
                and l.fingerprint_key not in previously_notified
            ]

            log.info(
                "instant-push: %d/%d matches new, %d/%d near-misses new",
                len(fresh), len(matched),
                len(fresh_near), len(near_with_reasons),
            )
            telegram_digest.send_instant_push(
                fresh,
                fresh_near_misses=fresh_near,
                notify_reasons=notify_reasons,
                office_lat=office_lat,
                office_lng=office_lng,
            )

            # Matches were stamped notified inside the dedup stage; near-misses
            # were not (they bypass dedup). Stamp them here so the next poll
            # doesn't re-send the same near-miss listing every 2 hours.
            for l, _ in fresh_near:
                dedup.mark_notified(l, conn)
        else:
            # Spec §8 Telegram delivery — header summary + per-listing messages.
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
                office_lat=office_lat,
                office_lng=office_lng,
            )
    except Exception as e:  # noqa: BLE001 - delivery failure shouldn't lose state
        log.error("telegram delivery failed (mode=%s): %s", mode, e, exc_info=True)

    # Standalone Telegram-channel pipeline. Independent of the portal pipeline;
    # uses its own pets+m² filter, dedups against portal listings by pHash, and
    # tracks processed posts in seen_telegram_posts. Errors here must not break
    # state push for the main run.
    tg_channels = cfg.get("telegram_channels") or []
    for ch_cfg in tg_channels:
        ch_name = ch_cfg.get("name")
        if not ch_name:
            continue
        try:
            telegram_channel_pipeline.run(
                ch_name,
                conn,
                m2_min=float(ch_cfg.get("m2_min", telegram_channel_pipeline.DEFAULT_M2_MIN)),
                require_hashtag=ch_cfg.get("require_hashtag"),
                office_lat=office_lat or None,
                office_lng=office_lng or None,
            )
        except Exception as e:  # noqa: BLE001
            log.error("telegram-channel pipeline (%s) crashed: %s", ch_name, e, exc_info=True)

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
