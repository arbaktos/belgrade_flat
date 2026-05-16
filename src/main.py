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

from src import digest, filter as filt, state, telegram
from src.models import Listing
from src.sources import cityexpert, four_zida, halooglasi, nekretnine

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("main")


@dataclass
class SourceResult:
    name: str
    listings: list[Listing]
    error: str | None = None  # set if the source raised; count then shows ⚠️


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
            summary = _run_m3(cfg, conn)
            _report_m3(summary, schedule_label="dispatch", run_id=run_id, pulled=pulled)
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


def _run_m3(cfg: dict, conn) -> dict:
    log.info("M3 dispatch: scraping all sources → filter → digest")
    freshness_days = int(cfg["filters"]["freshness_days"])
    source_results = [_fetch_source(name, fn, freshness_days) for name, fn in SOURCES]

    all_listings: list[Listing] = []
    for sr in source_results:
        all_listings.extend(sr.listings)

    state.upsert_listings(conn, all_listings)

    cfg_obj = filt.from_dict(cfg)
    filter_result = filt.apply(all_listings, cfg_obj)

    today = datetime.now(timezone.utc)
    source_stats = {sr.name: (len(sr.listings), sr.error) for sr in source_results}
    content = digest.render(
        filter_result,
        source_stats=source_stats,
        today=today,
    )
    path = digest.write(content, today)
    log.info("M3 digest written to %s (%d matches, %d rejected, %d total scraped)",
             path, len(filter_result.passed), len(filter_result.rejected), len(all_listings))

    return {
        "source_results": source_results,
        "fetched": len(all_listings),
        "matched": len(filter_result.passed),
        "rejected": len(filter_result.rejected),
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


def _report_m3(summary: dict, *, schedule_label: str, run_id: str, pulled: bool) -> None:
    src_line = " · ".join(
        f"{sr.name} {len(sr.listings)}{' ⚠️' if sr.error else ''}"
        for sr in summary["source_results"]
    )
    telegram.send_message(
        f"🧪 M3 test run ({schedule_label})\n"
        f"🩺 {src_line}\n"
        f"Total fetched: {summary['fetched']}\n"
        f"Matched: {summary['matched']} · Rejected: {summary['rejected']}\n"
        f"Digest: {summary['digest_path']}\n"
        f"R2: {'pulled' if pulled else 'seeded'} · Run: {run_id}"
    )


if __name__ == "__main__":
    sys.exit(main())
