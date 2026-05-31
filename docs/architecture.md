# Architecture

The system has two moving parts that share one piece of state. The **pipeline**
runs on a schedule, does all the scraping and filtering, and sends the digest.
The **webhook** is a tiny always-on service that handles button clicks the
moment they happen. Both read and write the same SQLite file in Cloudflare R2.

```text
                    ┌─────────────────────────┐
   GitHub Actions   │  src/main.py pipeline    │   pull → run → push
   (cron + manual)  │  scrape→filter→digest    │ ───────────────────┐
                    └─────────────────────────┘                     │
                                                                     ▼
                                                          ┌────────────────────┐
   Telegram click ─── webhook ──► vm/webhook_server.py ──►│  db.sqlite on R2   │
   (🙈 / ⭐)          (Hetzner VM)  pull → apply → push    └────────────────────┘
```

## The pipeline (GitHub Actions)

`src/main.py` is the whole run. A GitHub Actions workflow
(`.github/workflows/scrape.yml`) sets it off on two crons and on manual
dispatch. The job:

1. Starts a **FlareSolverr** sidecar container (solves halooglasi's Cloudflare
   challenge) and waits for it to answer.
2. Installs `rclone` and the Python dependencies.
3. Runs `python -m src.main`, which pulls `db.sqlite` from R2, walks the
   pipeline, and pushes the file back.
4. On a daily or manual run, commits the generated digest under `digests/` and
   pushes it. The two-hourly poll skips this commit so the repo isn't churned
   with twelve commits a day.
5. On any failure, sends a `🚨 scrape workflow failed` message to Telegram.

The runner is `ubuntu-latest` with a 25-minute timeout. The repo is public, so
Actions minutes are free. Every secret reaches the run through the workflow's
`env:` block — see [configuration.md](configuration.md#secrets).

### Run modes

The same pipeline behaves differently depending on what triggered it. `main.py`
reads the `GITHUB_EVENT_SCHEDULE` environment variable that Actions sets to the
cron expression:

| Trigger | Mode | Delivery |
| --- | --- | --- |
| Manual dispatch (no schedule) | `digest` | Full digest. |
| `30 4 * * *` (daily) | `digest` | Full digest. |
| `0 2-16/2 * * *` (every 2 h) | `instant` | Silent unless a *new* perfect match exists; pushes just that card. |
| Any other cron | `instant` | Safe default. |

Instant mode compares the current matches against a snapshot of
already-notified listings taken *before* the pipeline mutates anything, so it
only pushes what hasn't reached you yet. See
[telegram.md](telegram.md#daily-digest-versus-instant-push).

## State in R2

All memory lives in one SQLite file, `db.sqlite`, stored at `state/db.sqlite` in
the Cloudflare R2 bucket. `src/state.py` pulls it at the start of every run with
`rclone` and pushes it back at the end. R2 has no minimum charge and the file is
well under the 10 GB free tier.

The tables:

| Table | Purpose |
| --- | --- |
| `meta` | Schema version and the Telegram update offset. |
| `listings` | Every listing seen, with its dedup hash and notify timestamps. |
| `commute_cache` | Walking minutes per (location, destination), 90-day TTL. |
| `extraction_cache` | Cached LLM output, so each listing is read once ever. |
| `geocode_cache` | Nominatim lookups for the winter-smog annotation. |
| `skipped` | Listings the user hid with 🙈. |
| `favorites` | Listings the user starred with ⭐. |
| `seen_telegram_posts` | Channel posts already processed, keyed by message id. |

The schema version is **v13**. Migrations are forward-only and idempotent:
`ALTER TABLE ... ADD COLUMN` for additive changes, `DROP TABLE` when a cache
needs flushing (SQLite can't relax a `NOT NULL` constraint in place). Bump
`SCHEMA_VERSION` in `src/state.py` and add a guarded migration block for any
schema change — see the conventions in [CLAUDE.md](../CLAUDE.md).

## The webhook (Hetzner VM)

A 🙈 or ⭐ tap is a Telegram *callback*. Without the webhook, the pipeline drains
those taps with `getUpdates` on its next run — up to two hours later. The
webhook moves only that drain off CI so a tap takes effect in about a second.
Everything else stays on CI.

`vm/webhook_server.py` is a small FastAPI app. Telegram delivers each tap as an
HTTP POST; the handler pulls `db.sqlite` from R2, applies the tap through the
*same* code the CI drain uses (`telegram_callbacks.handle_callback_query`), and
pushes the file back — all under a process lock so two taps can't interleave.

Two shared secrets guard the endpoint: an unguessable URL path segment, and the
`X-Telegram-Bot-Api-Secret-Token` header that Telegram sends. Both must match.

`vm/bootstrap.sh`, run by a systemd unit, brings it up: start uvicorn, open a
Cloudflare quick tunnel to a public `https://<random>.trycloudflare.com`, wait
until that URL serves publicly, then register it with Telegram via `setWebhook`.
Quick-tunnel URLs change on every restart, so the script re-registers on each
boot. If either uvicorn or the tunnel dies, the unit restarts.

Deploy steps, smoke test, and rollback are in [deploy/README.md](../deploy/README.md).
The design rationale and the state-race analysis are in
[docs/plans/vm-webhook.md](plans/vm-webhook.md).

### When CI and the webhook both write

Both can push `db.sqlite`. A collision is rare — CI runs about two minutes every
two hours, taps are infrequent — and harmless: skips and favorites are
`INSERT OR IGNORE`, so the worst case is one lost tap the user re-taps. Once a
webhook is set, Telegram blocks `getUpdates`, so CI's drain detects the webhook
and no-ops. A stronger etag guard is noted as future work in the plan doc.
