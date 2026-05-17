"""Scrape the public preview page of a Telegram channel.

t.me serves `/s/<channel>` as a static HTML page with the most recent ~20
posts — no auth, no MTProto, no Bot API. We parse it with simple regex
rather than pulling in BeautifulSoup; the markup is stable and the field
set we care about is tiny.

This is intentionally a *post* scraper (not a Listing scraper) because
channel posts are free-form text with no structured price/m²/rooms. The
downstream pipeline (src/telegram_channel_pipeline.py) is responsible for
LLM extraction, filtering, dedup, commute, and delivery.
"""
from __future__ import annotations

import html
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Iterable

import httpx

log = logging.getLogger(__name__)

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"
)
PREVIEW_BASE = "https://t.me/s"

# Matches "<div class="tgme_widget_message ..." data-post="channel/N" ...>"
# Body capture runs to the next message's opening div (or end of string) so
# the footer — which carries the <time datetime="…"> we want — is included.
_MESSAGE_BLOCK_RE = re.compile(
    r'<div class="tgme_widget_message[^"]*"[^>]*data-post="([^"/]+)/(\d+)"[^>]*>'
    r'(.*?)'
    r'(?=<div class="tgme_widget_message[^"]*"[^>]*data-post=|\Z)',
    re.DOTALL,
)
_TEXT_RE = re.compile(
    r'<div class="tgme_widget_message_text[^"]*"[^>]*>(.*?)</div>',
    re.DOTALL,
)
_PHOTO_RE = re.compile(
    r"background-image:url\('([^']+)'\)",
)
_TIME_RE = re.compile(
    r'<time[^>]+datetime="([^"]+)"',
)
_BR_RE = re.compile(r"<br\s*/?>", re.IGNORECASE)
_TAG_RE = re.compile(r"<[^>]+>")


@dataclass
class TelegramPost:
    channel: str
    message_id: int
    text: str
    photo_urls: list[str] = field(default_factory=list)
    posted_at: datetime | None = None

    @property
    def permalink(self) -> str:
        return f"https://t.me/{self.channel}/{self.message_id}"


def fetch_recent_posts(
    channel: str,
    *,
    exclude: Iterable[int] = (),
    client: httpx.Client | None = None,
) -> list[TelegramPost]:
    """Return the most recent posts on `t.me/s/<channel>`, oldest first.

    `exclude` is a collection of message_ids we've already processed — they
    won't appear in the output. Posts already marked seen are still cheap to
    skip because we're hitting one HTML page either way.
    """
    own_client = client is None
    client = client or httpx.Client(
        headers={"User-Agent": USER_AGENT, "Accept": "text/html"},
        timeout=20.0,
        follow_redirects=True,
    )
    try:
        url = f"{PREVIEW_BASE}/{channel}"
        r = client.get(url)
        r.raise_for_status()
        posts = _parse(r.text, channel)
    finally:
        if own_client:
            client.close()

    excluded = set(exclude)
    fresh = [p for p in posts if p.message_id not in excluded]
    # Channel HTML lists oldest → newest; we keep that order so downstream
    # delivery follows chronological order naturally.
    log.info("telegram-channel %s: %d posts on page, %d new",
             channel, len(posts), len(fresh))
    return fresh


def _parse(page_html: str, channel: str) -> list[TelegramPost]:
    posts: list[TelegramPost] = []
    for m in _MESSAGE_BLOCK_RE.finditer(page_html):
        block_channel, block_id, body = m.group(1), int(m.group(2)), m.group(3)
        if block_channel != channel:
            # Telegram occasionally injects related-channel cards; skip them.
            continue
        text = _extract_text(body)
        photos = _extract_photos(body)
        posted_at = _extract_time(body)
        if not text and not photos:
            # Pure service messages (joined/left) have neither — ignore.
            continue
        posts.append(TelegramPost(
            channel=channel,
            message_id=block_id,
            text=text,
            photo_urls=photos,
            posted_at=posted_at,
        ))
    return posts


def _extract_text(body: str) -> str:
    m = _TEXT_RE.search(body)
    if not m:
        return ""
    inner = m.group(1)
    inner = _BR_RE.sub("\n", inner)
    inner = _TAG_RE.sub("", inner)
    return html.unescape(inner).strip()


def _extract_photos(body: str) -> list[str]:
    return [u for u in _PHOTO_RE.findall(body) if u.startswith("http")]


def _extract_time(body: str) -> datetime | None:
    m = _TIME_RE.search(body)
    if not m:
        return None
    try:
        return datetime.fromisoformat(m.group(1).replace("Z", "+00:00")).astimezone(timezone.utc)
    except ValueError:
        return None
