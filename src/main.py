import os
import sys
from datetime import datetime, timezone

from dotenv import load_dotenv

from src import state, telegram


def main() -> int:
    load_dotenv()

    schedule = os.environ.get("GITHUB_EVENT_SCHEDULE", "manual")
    run_id = os.environ.get("GITHUB_RUN_ID", "local")

    pulled = state.pull()
    conn = state.ensure_schema()
    db_stats = state.stats(conn)
    conn.close()

    state.push()

    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    telegram.send_message(
        "👋 Belgrade rental notifier — M1 skeleton alive\n"
        f"Schedule: {schedule}\n"
        f"Run: {run_id}\n"
        f"R2 state: {'pulled' if pulled else 'seeded fresh'} ({db_stats['size_bytes']} B)\n"
        f"Time: {now}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
