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

from src import digest, extract, filter as filt, route, state, telegram
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
            summary = _run_pipeline(cfg, conn)
            _report(summary, schedule_label="dispatch", run_id=run_id, pulled=pulled)
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
    log.info("dispatch: scrape → structural filter → LLM extract → final filter → digest")
    freshness_days = int(cfg["filters"]["freshness_days"])
    cfg_obj = filt.from_dict(cfg)

    source_results = [_fetch_source(name, fn, freshness_days) for name, fn in SOURCES]
    all_listings: list[Listing] = []
    for sr in source_results:
        all_listings.extend(sr.listings)
    state.upsert_listings(conn, all_listings)

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
    )
    path = digest.write(content, today)
    log.info("digest written to %s", path)

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


def _report(summary: dict, *, schedule_label: str, run_id: str, pulled: bool) -> None:
    src_line = " · ".join(
        f"{sr.name} {len(sr.listings)}{' ⚠️' if sr.error else ''}"
        for sr in summary["source_results"]
    )
    llm_line = (
        f"\nLLM failures: {summary['extraction_failures']}"
        if summary['extraction_failures'] else ""
    )
    telegram.send_message(
        f"🧪 Test run ({schedule_label})\n"
        f"🩺 {src_line}\n"
        f"Fetched: {summary['fetched']} · structural pass: {summary['structural_passed']}\n"
        f"Matched: {summary['matched']} · near-miss: {summary['near_miss']} · rejected: {summary['rejected']}"
        f"{llm_line}\n"
        f"Digest: {summary['digest_path']}\n"
        f"R2: {'pulled' if pulled else 'seeded'} · Run: {run_id}"
    )


if __name__ == "__main__":
    sys.exit(main())
