# Operations

The run book for living with the project: triggering runs, watching them,
checking spend, and resetting state. The repo is `arbaktos/belgrade_flat`.

## Run the tests

Tests must stay green before anything ships.

```bash
.venv/bin/python -m pytest tests/ -q
```

The suite mocks portal responses and the LLM tool-use shape from real captured
fixtures, so it runs offline.

## Trigger a run on CI

A manual dispatch always produces a full digest:

```bash
gh workflow run scrape.yml --repo arbaktos/belgrade_flat --ref main
gh run watch <run-id> --repo arbaktos/belgrade_flat --exit-status
```

Or use the **Run workflow** button on the
[Actions page](https://github.com/arbaktos/belgrade_flat/actions/workflows/scrape.yml).

After it finishes, check three things: the Telegram chat for the digest, the
auto-committed `digests/YYYY-MM-DD.md` (or `-test.md` for a manual run), and the
run logs for any `⚠️` source-failure lines.

The two crons run on their own — a daily digest at `30 4 * * *` and an
every-2-hour poll at `0 2-16/2 * * *` — and pick `digest` or `instant` mode
automatically ([architecture.md](architecture.md#run-modes)).

## Run locally

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
cp .env.example .env          # fill in the secrets (see configuration.md)
.venv/bin/python -m src.main
```

halooglasi needs the FlareSolverr sidecar, which only runs in CI, so locally
that source shows `⚠️` and the other three run normally.

## Check Google API spend

The digest header line `🔢 Google API: N/40 000 this month` is an approximation
from the SQLite cache — one walking call per (location, destination) bucket
written this month. For the authoritative figure, open Cloud Console → Routes
API → Metrics.

## Inspect or edit the state DB

State lives only in R2. Pull it, inspect or edit, push it back:

```bash
rclone copy r2:belgrade-flats/state/db.sqlite .
sqlite3 db.sqlite '.tables'
rclone copy db.sqlite r2:belgrade-flats/state/
```

`rclone` needs the same `R2_*` credentials the run uses, exported in the
environment (see `src/state.py` for the variable mapping).

## Reset dedup state

To force every tracked listing to surface again — clearing hides and
notify stamps:

```sql
DELETE FROM skipped;
UPDATE listings SET notified_at=NULL, notified_price=NULL;
```

Apply it through the pull → edit → push cycle above.

## Pull the latest committed digest

```bash
git pull --rebase
cat digests/$(date -u +%Y-%m-%d).md
```

## The VM webhook

Deploy, smoke test, rollback, and code-update steps for the real-time callback
service live in [deploy/README.md](../deploy/README.md). The quick rollback —
hand callbacks back to the CI drain — is one call:

```bash
source /etc/belgrade-webhook.env
curl -s -X POST "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/deleteWebhook"
```

## When something breaks

- **A workflow run failed.** The job's failure step sends a `🚨 scrape workflow
  failed` message with the run id and trigger. Open that run's logs.
- **One portal shows `⚠️` / 0 listings.** A single source failing is isolated by
  design and doesn't fail the run. If it persists, the portal likely changed its
  markup or API — check the matching module under `src/sources/`.
- **Walking distances stopped updating.** A `DirectionsConfigError`
  (`REQUEST_DENIED` / quota) stops the Routes loop for the run; the digest says
  so and matches keep their LLM-pass status. Check the API key and quota.
- **The digest is empty for days.** Re-read the filter thresholds in
  `config.yaml` against STATUS.md §2 before loosening anything.
