import os
import httpx

BASE_URL = "https://api.telegram.org"


def send_message(text: str, *, disable_notification: bool = False) -> None:
    token = os.environ["TELEGRAM_BOT_TOKEN"]
    chat_id = os.environ["TELEGRAM_CHAT_ID"]
    response = httpx.post(
        f"{BASE_URL}/bot{token}/sendMessage",
        json={
            "chat_id": chat_id,
            "text": text,
            "disable_notification": disable_notification,
        },
        timeout=15,
    )
    response.raise_for_status()
