# Real-time Telegram webhook — Hetzner VM deploy

Runs `vm/webhook_server.py` behind a Cloudflare **quick tunnel** so 🙈 Hide /
⭐ Favorite clicks take effect in ~1 s instead of waiting for the next CI run.
See `docs/plans/vm-webhook.md` for the design.

The CI scraper keeps doing everything else. Once a webhook is set, CI's
`telegram_callbacks.drain()` detects it and no-ops (logged), so the two never
fight over callbacks.

---

## 0. Prereqs on the VM (Ubuntu, run as root once)

```bash
adduser --disabled-password --gecos "" belgrade
apt-get update && apt-get install -y python3-venv git curl

# cloudflared (Cloudflare Tunnel client)
curl -fsSL https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-amd64 \
  -o /usr/local/bin/cloudflared
chmod +x /usr/local/bin/cloudflared
cloudflared --version
```

## 1. Clone + venv (as `belgrade`)

```bash
sudo -iu belgrade
git clone https://github.com/arbaktos/belgrade_flat.git ~/belgrade_flat
cd ~/belgrade_flat
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt -r vm/requirements.txt
# rclone is needed for R2 state sync:
curl -fsSL https://rclone.org/install.sh | sudo bash
```

## 2. Secrets file (as root)

Generate the two webhook secrets and write the env file. Both must be hard to
guess; the path secret hides the URL, the token authenticates Telegram.

```bash
PATH_SECRET=$(openssl rand -hex 16)
TOKEN=$(openssl rand -hex 32)

cat >/etc/belgrade-webhook.env <<EOF
TELEGRAM_BOT_TOKEN=<the bot token>
WEBHOOK_PATH_SECRET=${PATH_SECRET}
WEBHOOK_SECRET_TOKEN=${TOKEN}
TELEGRAM_FAVORITES_CHAT_ID=-5057252591
R2_ACCESS_KEY_ID=<...>
R2_SECRET_ACCESS_KEY=<...>
R2_BUCKET=belgrade-flats
R2_ACCOUNT_ID=<...>
EOF
chown belgrade:belgrade /etc/belgrade-webhook.env
chmod 600 /etc/belgrade-webhook.env
```

> The same secret values live nowhere else — the bootstrap script reads them
> from here and registers the webhook with the token on every start.

## 3. Install + start the service (as root)

```bash
cp /home/belgrade/belgrade_flat/deploy/belgrade-webhook.service \
   /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now belgrade-webhook
journalctl -u belgrade-webhook -f
```

Healthy startup logs look like:

```
[bootstrap] uvicorn healthy
[bootstrap] tunnel up: https://<rand>.trycloudflare.com
[bootstrap] setWebhook "ok":true
[bootstrap] ready — supervising ...
```

## 4. Smoke test

Tap 🙈 Hide on any digest card in Telegram. Within a second or two you should
see the "🙈 Hidden from future digests" toast, and the journal logs
`applied callback: {'skipped': 1, ...}`. Confirm the webhook is the one Telegram
is using:

```bash
source /etc/belgrade-webhook.env
curl -s "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/getWebhookInfo" | python3 -m json.tool
# expect: "url": "https://<rand>.trycloudflare.com/tg/<path-secret>", pending_update_count: 0
```

The next CI run will log `callbacks handled by webhook … — skipping getUpdates
drain`, confirming the cutover.

---

## Rollback (≤ 10 s)

Hand callbacks back to the CI drain:

```bash
source /etc/belgrade-webhook.env
curl -s -X POST "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/deleteWebhook"
systemctl stop belgrade-webhook        # optional; can also leave it idle
```

Telegram immediately re-queues updates for `getUpdates`; the next CI run drains
them as before. Re-deploy by starting the service again (it re-registers the
webhook on boot).

---

## Upgrading to a stable URL (optional, later)

Quick-tunnel URLs rotate on every restart (we re-register each boot to cope).
For a permanent URL, create a **named tunnel** bound to a Cloudflare domain:

```bash
cloudflared tunnel login
cloudflared tunnel create belgrade-webhook
cloudflared tunnel route dns belgrade-webhook webhook.<your-domain>
# point ingress at http://127.0.0.1:8000, run `cloudflared tunnel run` via its
# own systemd unit, then setWebhook ONCE to https://webhook.<your-domain>/tg/<secret>
```

Then drop the `cloudflared` + `setWebhook` steps from `vm/bootstrap.sh` (the
named tunnel and the fixed webhook persist across restarts).

---

## Updating the code

```bash
sudo -iu belgrade
cd ~/belgrade_flat && git pull
.venv/bin/pip install -r requirements.txt -r vm/requirements.txt
exit
systemctl restart belgrade-webhook
```
