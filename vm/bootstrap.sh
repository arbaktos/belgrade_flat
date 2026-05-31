#!/usr/bin/env bash
#
# Boot the real-time Telegram webhook on the VM:
#   1. start uvicorn (the FastAPI app) on 127.0.0.1:8000
#   2. open a Cloudflare quick tunnel → public https://<rand>.trycloudflare.com
#   3. register that URL with Telegram via setWebhook (+ secret token)
#   4. supervise both; if either dies, exit so systemd restarts the unit
#
# Quick-tunnel URLs are ephemeral — they change on every (re)start — so we
# re-register the webhook here on each boot. Swap to a named tunnel later to
# get a stable URL and drop the setWebhook step (see deploy/README.md).
#
# Required env (from /etc/belgrade-webhook.env, loaded by systemd):
#   TELEGRAM_BOT_TOKEN, WEBHOOK_PATH_SECRET, WEBHOOK_SECRET_TOKEN,
#   R2_* (for state pull/push). PORT optional (default 8000).
set -euo pipefail

PORT="${PORT:-8000}"
PYTHON="${PYTHON:-.venv/bin/python}"
LOG_DIR="${LOG_DIR:-/tmp}"
CF_LOG="${LOG_DIR}/cloudflared.log"

: "${TELEGRAM_BOT_TOKEN:?set TELEGRAM_BOT_TOKEN}"
: "${WEBHOOK_PATH_SECRET:?set WEBHOOK_PATH_SECRET}"
: "${WEBHOOK_SECRET_TOKEN:?set WEBHOOK_SECRET_TOKEN}"

cleanup() {
  [[ -n "${UVICORN_PID:-}" ]] && kill "$UVICORN_PID" 2>/dev/null || true
  [[ -n "${CF_PID:-}" ]] && kill "$CF_PID" 2>/dev/null || true
}
trap cleanup EXIT INT TERM

echo "[bootstrap] starting uvicorn on 127.0.0.1:${PORT}"
"$PYTHON" -m uvicorn vm.webhook_server:app --host 127.0.0.1 --port "$PORT" &
UVICORN_PID=$!

# Wait for the app to answer /health before exposing it.
for _ in $(seq 1 30); do
  if curl -fsS "http://127.0.0.1:${PORT}/health" >/dev/null 2>&1; then
    echo "[bootstrap] uvicorn healthy"
    break
  fi
  sleep 1
done

echo "[bootstrap] opening Cloudflare quick tunnel"
: > "$CF_LOG"
cloudflared tunnel --no-autoupdate --url "http://127.0.0.1:${PORT}" \
  >"$CF_LOG" 2>&1 &
CF_PID=$!

# cloudflared prints the public URL to its log a second or two after start.
TUNNEL_URL=""
for _ in $(seq 1 30); do
  TUNNEL_URL="$(grep -oE 'https://[a-z0-9-]+\.trycloudflare\.com' "$CF_LOG" | head -n1 || true)"
  [[ -n "$TUNNEL_URL" ]] && break
  sleep 1
done
if [[ -z "$TUNNEL_URL" ]]; then
  echo "[bootstrap] ERROR: could not determine tunnel URL; cloudflared log:" >&2
  cat "$CF_LOG" >&2
  exit 1
fi
echo "[bootstrap] tunnel up: ${TUNNEL_URL}"

# Wait until the tunnel actually serves our app from the PUBLIC side. This is
# the real readiness signal: it confirms the trycloudflare hostname is globally
# resolvable AND proxying to uvicorn, both of which lag a few seconds behind
# cloudflared printing the URL. Registering the webhook before this is ready is
# what caused Telegram's "Failed to resolve host" 400.
echo "[bootstrap] waiting for tunnel to serve /health publicly"
for _ in $(seq 1 30); do
  if curl -fsS "${TUNNEL_URL}/health" >/dev/null 2>&1; then
    echo "[bootstrap] tunnel serving publicly"
    break
  fi
  sleep 2
done

WEBHOOK_URL="${TUNNEL_URL}/tg/${WEBHOOK_PATH_SECRET}"
echo "[bootstrap] registering webhook"
registered=""
for attempt in 1 2 3 4 5; do
  RESP="$(curl -sS -X POST "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/setWebhook" \
    --data-urlencode "url=${WEBHOOK_URL}" \
    --data-urlencode "secret_token=${WEBHOOK_SECRET_TOKEN}" \
    --data-urlencode 'allowed_updates=["callback_query"]' \
    --data-urlencode 'drop_pending_updates=false' || true)"
  echo "[bootstrap] setWebhook attempt ${attempt}: ${RESP}"
  case "$RESP" in
    *'"ok":true'*) registered="yes"; break ;;
  esac
  sleep 5
done
if [[ -z "$registered" ]]; then
  echo "[bootstrap] ERROR: setWebhook never succeeded; restarting to retry" >&2
  exit 1
fi

echo "[bootstrap] ready — supervising uvicorn(${UVICORN_PID}) + cloudflared(${CF_PID})"
# Exit (and let systemd restart us) the moment either child dies.
wait -n "$UVICORN_PID" "$CF_PID"
echo "[bootstrap] a child exited — shutting down for restart" >&2
exit 1
