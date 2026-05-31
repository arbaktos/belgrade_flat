# Plan: Real-time 🙈 Hide via webhook on a Hetzner VM

_Drafted: 2026-05-31. Status: agreed; ready to implement._

## Goal
Telegram callback clicks (🙈 Hide / ⭐ Favorite) take effect **within ~1 s**
instead of waiting up to 2 h for the next CI drain cycle. CI continues to own
everything else (scrape → extract → filter → digest → state push).

## Architecture (one paragraph)
A tiny always-on Python service on the Hetzner VM exposes
`POST /tg/<WEBHOOK_PATH_SECRET>`. Telegram delivers each click as JSON; the
handler pulls `db.sqlite` from R2, calls the existing
`src/telegram_callbacks.py` helpers (`_mark_skipped`, `_mark_favorited`,
`_forward_to_favorites`, `_ack`) to update the DB and forward to favorites,
then pushes the DB back. CI keeps owning scraping, extraction, digests, and
its end-of-run state push — only the **callback drain** moves to the VM.

## HTTPS path: Cloudflare Tunnel (quick / `*.trycloudflare.com`)

Chosen for zero-domain setup. `cloudflared` opens an outbound connection to
Cloudflare and routes a public HTTPS URL → `127.0.0.1:8000` on the VM. No port
443 exposed, no certificates to manage, no public IP needed in the webhook URL.

**Trade-off acknowledged:** quick-tunnel URLs are ephemeral — each
`cloudflared tunnel --url …` invocation gets a fresh
`https://<random>.trycloudflare.com`. The bootstrap script handles this by
**re-registering the webhook on every startup** (see "Bootstrap" below).
Swapping to a stable named tunnel later is a one-command change.

## Files to add

| Path | What |
|---|---|
| `vm/webhook_server.py` | ~80-line FastAPI app. One route: `POST /tg/{secret}`. Validates `X-Telegram-Bot-Api-Secret-Token` header + path secret, parses the update, dispatches to the existing callback helpers under a per-process lock. |
| `vm/bootstrap.sh` | On service start: launch `cloudflared tunnel --url http://127.0.0.1:8000` in the background, tail its log until it reports the public URL, `setWebhook` with that URL + `secret_token`, then `exec uvicorn …`. Handles URL rotation across restarts. |
| `vm/requirements.txt` | `fastapi`, `uvicorn[standard]` on top of the existing project deps (shared venv). |
| `deploy/belgrade-webhook.service` | systemd unit running `vm/bootstrap.sh` as a dedicated `belgrade` user, `Restart=always`, environment loaded from `/etc/belgrade-webhook.env`. |
| `deploy/README.md` | Step-by-step Hetzner bootstrap (apt, user, clone, venv, env file, install cloudflared, systemd enable, smoke test, register webhook, rollback). |

### Code that also changes (small refactors, no behaviour drift)

- `src/telegram_callbacks.py`
  - Extract a `handle_callback_query(conn, cq, counts) -> None` helper from inside `drain()`. Both the CI drain loop and the VM webhook server call it. The 22 existing tests stay green; covered by behaviour-equivalence.
  - When `getWebhookInfo` reports a configured webhook, `drain()` logs once and returns zeros. Today it already calls `getUpdates` and returns empty (Telegram blocks `getUpdates` while a webhook is set), so this is a clarity fix, not a behaviour change.

## Concurrency / state safety

Two writers can race on `db.sqlite` in R2:
- CI run finishing its push
- Webhook handler pushing after a click

Real collision odds are tiny (CI ≈ 2 min every 2 h; clicks rare). Defences:

1. **In-process lock** in the VM service (`threading.Lock`) around the
   pull → apply → push critical section. uvicorn default is one worker, so
   this is defence-in-depth — but it makes a future multi-worker move safe.
2. **Etag check on push.** Store `meta.state_etag` (timestamp + actor:
   `"ci-<runid>"` or `"vm-<uuid>"`). On push, re-fetch the remote etag; if it
   differs from what we pulled, re-pull, re-apply the click, retry once.
   Loser retries cleanly; no clobber.
3. **CI side already idempotent.** `INSERT OR IGNORE INTO skipped/favorites`
   means a duplicate write is harmless even if the etag guard fails.

## Cutover (zero-downtime)

1. Deploy `vm/*` and `deploy/*` to the VM; start the service. **Do not** call
   `setWebhook` yet — CI keeps draining as today.
2. Smoke test: `curl -X POST https://<trycloudflare-url>/tg/<secret> -d '<crafted update>'`. Confirm DB update + Telegram toast.
3. Cut over: the bootstrap script's `setWebhook` call registers the URL. From
   this moment CI's `drain()` is a no-op (logged once) and clicks are instant.
4. Watch one digest cycle end-to-end. Done.

## Rollback (≤ 10 s)

```
curl -X POST "https://api.telegram.org/bot$TG_TOKEN/deleteWebhook"
```

Telegram immediately resumes queuing updates for `getUpdates`; the next CI run
drains them as before. The VM service can keep running idle, or
`systemctl stop belgrade-webhook`.

## Validation

- **Tests**
  - All 22 existing callback tests stay green (refactor preserves behaviour).
  - ~4 new tests: `handle_callback_query` happy path (skip + favorite),
    webhook endpoint rejects bad path secret, rejects bad header secret,
    accepts a well-formed update and increments counters.
- **Live**
  - Click 🙈 on a fresh digest card → next digest no longer includes that
    listing (within 5 s of click; cache is in memory until the next CI run
    overwrites `skipped_set`).
  - `journalctl -u belgrade-webhook -f` shows `200 OK` per click.
  - Click ⭐ → card appears in `fav belg flats` with the portal link button
    (existing behaviour, just now instant).

## Open issues

- **Visible-message edit on click.** Today the source card stays in chat
  after 🙈 click (only the toast confirms it). Editing the message to grey it
  out is a future polish, intentionally out of scope here.
- **Domain swap.** If/when we want a stable URL, run:
  `cloudflared tunnel route dns belgrade-webhook webhook.<domain>` after
  pointing the domain at Cloudflare, then drop the bootstrap's `setWebhook`
  call (register once, keep forever).

## Out of scope

- Migrating the scraper itself to the VM (separate decision; CI continues to
  run the pipeline).
- Bot-side features beyond the existing 🙈 / ⭐ buttons.
