from __future__ import annotations

import logging
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

import yaml
from dotenv import load_dotenv

from src import digest, filter as filt, state, telegram
from src.sources import four_zida

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("main")


def main() -> int:
    load_dotenv()

    schedule = os.environ.get("GITHUB_EVENT_SCHEDULE", "")
    run_id = os.environ.get("GITHUB_RUN_ID", "local")
    is_dispatch = not schedule  # workflow_dispatch sets no schedule

    cfg = yaml.safe_load(Path("config.yaml").read_text())
    pulled = state.pull()
    conn = state.ensure_schema()

    try:
        if is_dispatch:
            result = _run_m2(cfg, conn)
            _report_m2(result, schedule_label="dispatch", run_id=run_id, pulled=pulled)
        else:
            # Cron branches still do the R2 round-trip + a hello message so we
            # can see schedules are firing. Real digest/poll work lands in M7/M8.
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


def _run_m2(cfg: dict, conn) -> dict:
    log.info("M2 dispatch: scraping 4zida → filter → digest")
    listings = four_zida.fetch(freshness_days=int(cfg["filters"]["freshness_days"]))
    state.upsert_listings(conn, listings)
    cfg_obj = filt.from_dict(cfg)
    result = filt.apply(listings, cfg_obj)
    today = datetime.now(timezone.utc)
    content = digest.render(
        result,
        source_stats={"4zida": len(listings)},
        today=today,
    )
    path = digest.write(content, today)
    log.info("M2 digest written to %s (%d matches, %d rejected)",
             path, len(result.passed), len(result.rejected))
    return {
        "fetched": len(listings),
        "matched": len(result.passed),
        "rejected": len(result.rejected),
        "digest_path": str(path),
    }


def _report_m2(summary: dict, *, schedule_label: str, run_id: str, pulled: bool) -> None:
    telegram.send_message(
        f"🧪 M2 test run ({schedule_label})\n"
        f"4zida fetched: {summary['fetched']}\n"
        f"Matched: {summary['matched']} · Rejected: {summary['rejected']}\n"
        f"Digest: {summary['digest_path']}\n"
        f"R2: {'pulled' if pulled else 'seeded'} · Run: {run_id}"
    )


if __name__ == "__main__":
    sys.exit(main())
