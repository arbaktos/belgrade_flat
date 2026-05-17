from __future__ import annotations

from datetime import timezone

from src.sources import telegram_channel as tc


# Trimmed shape of a real t.me/s/<channel> response. The selectors we depend
# on: data-post, tgme_widget_message_text, background-image:url('...'),
# tgme_widget_message_footer sentinel, <time datetime="...">.
SAMPLE_HTML = """
<html><body>
<div class="tgme_widget_message_wrap">
<div class="tgme_widget_message" data-post="beograd_stan/101">
  <a class="tgme_widget_message_photo_wrap" href="x" style="background-image:url('https://cdn.example.com/img/101a.jpg')"></a>
  <div class="tgme_widget_message_text js-message_text" dir="auto">
    Vra&#269;ar, 65m2, ljubimci dozvoljeni, 800&euro;.<br/>
    Kontakt 0641234567.
  </div>
  <div class="tgme_widget_message_footer compact js-message_footer">
    <div class="tgme_widget_message_info">
      <a class="tgme_widget_message_date" href="x">
        <time datetime="2026-05-17T09:30:00+00:00">9:30</time>
      </a>
    </div>
  </div>
</div>

<div class="tgme_widget_message service_message" data-post="beograd_stan/102">
  <div class="tgme_widget_message_footer compact js-message_footer"></div>
</div>

<div class="tgme_widget_message" data-post="beograd_stan/103">
  <div class="tgme_widget_message_text js-message_text" dir="auto">
    No photo here, just text.
  </div>
  <div class="tgme_widget_message_footer compact js-message_footer">
    <time datetime="2026-05-17T10:00:00+00:00">10:00</time>
  </div>
</div>

<div class="tgme_widget_message" data-post="other_channel/999">
  <div class="tgme_widget_message_text">Wrong channel — should be ignored.</div>
  <div class="tgme_widget_message_footer compact js-message_footer"></div>
</div>
</div></body></html>
"""


def test_parse_extracts_message_ids_text_photos_and_time():
    posts = tc._parse(SAMPLE_HTML, channel="beograd_stan")
    # Service message (102) lacks text+photos so it's dropped.
    # Other-channel (999) is filtered by the channel check.
    ids = [p.message_id for p in posts]
    assert ids == [101, 103]

    p101 = posts[0]
    assert p101.channel == "beograd_stan"
    assert "Vračar" in p101.text and "ljubimci dozvoljeni" in p101.text
    assert p101.photo_urls == ["https://cdn.example.com/img/101a.jpg"]
    assert p101.posted_at is not None
    assert p101.posted_at.tzinfo == timezone.utc
    assert p101.posted_at.hour == 9

    p103 = posts[1]
    assert p103.photo_urls == []  # text-only post is still kept
    assert "just text" in p103.text


def test_permalink_property():
    p = tc.TelegramPost(channel="beograd_stan", message_id=42, text="x")
    assert p.permalink == "https://t.me/beograd_stan/42"


def test_fetch_recent_posts_excludes_seen_ids(monkeypatch):
    # Replace httpx round-trip with the canned page.
    class FakeResp:
        text = SAMPLE_HTML
        def raise_for_status(self): ...
    class FakeClient:
        def __init__(self, *a, **k): ...
        def __enter__(self): return self
        def __exit__(self, *a): ...
        def get(self, url): return FakeResp()
        def close(self): ...
    monkeypatch.setattr(tc.httpx, "Client", FakeClient)

    posts = tc.fetch_recent_posts("beograd_stan", exclude={101})
    assert [p.message_id for p in posts] == [103]


def test_br_tags_become_newlines():
    body = (
        '<div class="tgme_widget_message" data-post="beograd_stan/7">'
        '<div class="tgme_widget_message_text">line a<br/>line b</div>'
        '<div class="tgme_widget_message_footer">x</div>'
    )
    parsed = tc._parse(body, "beograd_stan")
    assert "line a\nline b" in parsed[0].text
