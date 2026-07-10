# Telegram delivery

Telegram is the only interface. The pipeline sends a digest; you read the cards
and tap two buttons. There's no `/command` bot — the design is push-only, with
the buttons as the single feedback channel. `telegram_digest.py` formats and
sends; `telegram_callbacks.py` handles the taps.

## What a digest looks like

A daily digest is a sequence of messages, not one block:

1. **A header** — the date, how many matches and near-misses, a per-portal
   health line (`halooglasi 0 ⚠️` when a source failed), the month-to-date
   Google API count, the dedup tally, and R2 state size.
2. **One message per match**, best-ranked first — a photo with a caption: price,
   size, rooms, floor, walking minutes to each destination, an English summary,
   the expandable Serbian original, and any annotations (bills, and a
   re-notify badge: 📉 price drop, 💱 price changed, ⬆️ upgraded from
   near-miss, 🔁 re-surfaced). A `View on portal` link button and the two action buttons sit
   below.
3. **A near-miss section** — the same cards for listings that were close but had
   an unclear field (see [filtering-and-ranking.md](filtering-and-ranking.md#3-llm-aware-filter--filterapply_with_extraction)).

Limits keep the burst calm: 10 matches and 5 near-misses per digest. Anything
beyond that becomes a quiet `(+N more — see digests/YYYY-MM-DD.md)` line, since
the full set is committed to the repo under `digests/`. An empty day sends
`Nothing new today. All systems green.` with the health footer.

## The buttons

Each card carries two inline buttons. A tap is a Telegram *callback*; both the
CI drain and the VM webhook apply it through the same `handle_callback_query`,
so the effect is identical whichever path delivered it.

### 🙈 Hide

Records the listing in the `skipped` table and removes the card from the chat.
From then on the listing is dropped early in every run — before the LLM and the
Routes API — so it never resurfaces and never costs anything again.

The card deletion is best-effort: a Telegram message older than 48 hours can't
be deleted, but the hide is already persisted, so it still takes effect on
future digests. Only the card is removed; a separate translation follow-up, if
any, stays.

### ⭐ Favorite

Records the listing in the `favorites` table and copies the card to a separate
favorites chat (`TELEGRAM_FAVORITES_CHAT_ID`, or a forum topic via
`TELEGRAM_FAVORITES_THREAD_ID`). The copy keeps only the `View on portal` link
button — the Hide/Favorite buttons would carry stale data in the favorites chat,
so they're stripped.

Every step here soft-fails: if the favorites chat isn't configured or the copy
errors out, the listing is still saved, and the toast tells you what happened.

## How a tap reaches the database

Two paths, depending on whether the real-time webhook is deployed:

- **CI drain (default).** GitHub Actions can't host a webhook, so the pipeline
  calls `getUpdates` at the start of each run and applies any pending taps
  *before* generating the new digest. A tap on yesterday's card takes effect on
  today's run — a lag of up to two hours.
- **VM webhook (when deployed).** Telegram POSTs each tap to the always-on
  service; it takes effect in about a second. Once a webhook is set, Telegram
  blocks `getUpdates`, so the CI drain detects the webhook and no-ops. See
  [architecture.md](architecture.md#the-webhook-hetzner-vm).

## Daily digest versus instant push

The same per-listing card is reused in two delivery modes
([architecture.md](architecture.md#run-modes) explains how the mode is chosen):

- **Daily digest** — the full header, all matches, all near-misses.
- **Instant push** — silent unless a listing is *new* (never notified before).
  When something new clears the gates, it pushes just those cards behind a
  one-line `🔔 N new match…` header, with no digest framing. A perfect match and
  a near-miss can both push, both gated on the same office walking threshold so a
  push never includes a listing that fails the non-negotiable axis.

No poll cron is currently scheduled (the two daily runs are both full
digests), but the mode stays wired for any future cron. Because instant push
has no header, a portal failure during a poll would be invisible, so a poll that
hits a failing source sends a short `⚠️ Sources failed: <names>` line.
