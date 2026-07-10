from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from io import BytesIO

import imagehash
import pytest
from PIL import Image

from src import dedup
from src.models import Listing


def _listing(**over) -> Listing:
    base = dict(
        id="x", source="4zida", url="https://x", price_eur=900, m2=60, rooms=2.0,
        floor=3, total_floors=5, last_floor=False, elevator=True,
        furnished="yes", heating_type="district", pets_allowed=True,
        title="Dvosoban stan, Vračar, 60m²", description="d",
        address=None, place_names=["Vračar"], image_url="https://x/img.jpg",
        is_agency=False, created_at=datetime.now(timezone.utc),
    )
    base.update(over)
    return Listing(**base)


def _gradient_image_bytes(seed: int = 0, size: int = 64) -> bytes:
    """A simple deterministic image for phash testing."""
    img = Image.new("RGB", (size, size))
    px = img.load()
    for y in range(size):
        for x in range(size):
            px[x, y] = ((x + seed) % 256, (y + seed) % 256, ((x + y) // 2) % 256)
    buf = BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def test_phash_same_image_zero_hamming():
    h1 = dedup._phash_from_bytes(_gradient_image_bytes(0))
    h2 = dedup._phash_from_bytes(_gradient_image_bytes(0))
    assert h1 == h2
    assert dedup.hamming(h1, h2) == 0


def test_phash_different_images_high_hamming():
    h1 = dedup._phash_from_bytes(_gradient_image_bytes(0))
    h2 = dedup._phash_from_bytes(_gradient_image_bytes(50))
    assert dedup.hamming(h1, h2) > dedup.PHASH_MAX_HAMMING


def test_is_skipped_duplicate_matches_same_photo():
    h = dedup._phash_from_bytes(_gradient_image_bytes(0))
    hidden = _listing(id="old", source="4zida", title="whatever", price_eur=1)
    hidden.image_phash = h
    # Re-list: new id/portal/title/price, but same photo.
    relist = _listing(id="new", source="halooglasi", title="totally different", price_eur=2)
    relist.image_phash = h
    assert dedup.is_skipped_duplicate(relist, [hidden]) is True


def test_is_skipped_duplicate_matches_same_title_and_price():
    # No usable pHash (coords not persisted for hidden flats either), but the
    # title+price fallback still catches a same-agency re-list.
    hidden = _listing(id="old", title="Dvosoban stan, Vračar, 60m²", price_eur=900)
    relist = _listing(id="new", title="Dvosoban stan, Vračar, 60m²", price_eur=910)
    assert dedup.is_skipped_duplicate(relist, [hidden]) is True


def test_is_skipped_duplicate_no_false_positive():
    hidden = _listing(id="old", title="Dvosoban stan, Vračar, 60m²", price_eur=900)
    other = _listing(id="new", title="Garsonjera u Zemunu, Pinki", price_eur=500,
                     lat=44.85, lng=20.40, m2=30)
    assert dedup.is_skipped_duplicate(other, [hidden]) is False


def test_is_skipped_duplicate_empty_set():
    assert dedup.is_skipped_duplicate(_listing(), []) is False


def test_cluster_groups_same_phash():
    """Same photo across portals + a third listing with a clearly different photo, title, AND price."""
    h_same = dedup._phash_from_bytes(_gradient_image_bytes(0))
    h_other = dedup._phash_from_bytes(_gradient_image_bytes(99))
    a = _listing(id="a", source="4zida")
    a.image_phash = h_same
    b = _listing(id="b", source="halooglasi")
    b.image_phash = h_same
    c = _listing(
        id="c", source="cityexpert",
        title="Garsonjera u Zemunu, Pinki, daleko",
        price_eur=560,                       # > 5% from 900 → fails price-similar fallback
        lat=44.85, lng=20.40, m2=30,         # different coord bucket too
    )
    c.image_phash = h_other
    clusters = dedup.cluster_duplicates([a, b, c])
    sizes = sorted(len(cl) for cl in clusters)
    assert sizes == [1, 2]


def test_pick_canonical_prefers_4zida_over_halooglasi():
    a = _listing(id="a", source="halooglasi")
    b = _listing(id="b", source="4zida")
    canonical = dedup.pick_canonical([a, b])
    assert canonical.source == "4zida"


def test_coord_bucket_cluster_without_phash():
    a = _listing(id="a", lat=44.81, lng=20.45, price_eur=900, m2=60)
    b = _listing(id="b", lat=44.81, lng=20.45, price_eur=910, m2=60)  # same bucket
    clusters = dedup.cluster_duplicates([a, b])
    assert len(clusters) == 1


def test_title_trigram_cluster_when_no_other_match():
    a = _listing(id="a", title="Dvosoban stan, Vračar 60m2 lux", price_eur=900)
    b = _listing(id="b", title="Dvosoban stan Vracar 60m2", price_eur=920)  # similar title, similar price
    clusters = dedup.cluster_duplicates([a, b])
    assert len(clusters) == 1


@pytest.fixture
def conn():
    db = sqlite3.connect(":memory:")
    db.execute(
        """CREATE TABLE listings (
            fingerprint_key TEXT PRIMARY KEY,
            notified_at TEXT,
            notified_price REAL,
            notified_stage TEXT
        )"""
    )
    return db


def _notified(conn, l, *, price, stage="match", at=None):
    conn.execute(
        "INSERT INTO listings (fingerprint_key, notified_at, notified_price, notified_stage) "
        "VALUES (?, ?, ?, ?)",
        (l.fingerprint_key, (at or datetime.now(timezone.utc)).isoformat(), price, stage),
    )


def test_notify_reason_new_for_unseen_listing(conn):
    assert dedup.notify_reason(_listing(), conn) == "new"


def test_notify_reason_none_when_nothing_changed(conn):
    l = _listing(price_eur=900)
    _notified(conn, l, price=900.0)
    assert dedup.notify_reason(l, conn) is None


def test_notify_reason_price_drop_on_significant_drop(conn):
    l = _listing(price_eur=800)   # was 1000, 20% drop
    _notified(conn, l, price=1000.0)
    assert dedup.notify_reason(l, conn) == "price_drop"


def test_notify_reason_price_change_on_small_move(conn):
    # A 3% drop is below the 📉 badge threshold but still a change worth a card.
    l = _listing(price_eur=970)
    _notified(conn, l, price=1000.0)
    assert dedup.notify_reason(l, conn) == "price_change"
    # Price increases count as a change too.
    conn.execute("UPDATE listings SET notified_price=950.0 WHERE fingerprint_key=?",
                 (l.fingerprint_key,))
    assert dedup.notify_reason(l, conn) == "price_change"


def test_notify_reason_upgraded_from_near_miss(conn):
    l = _listing(price_eur=900)
    _notified(conn, l, price=900.0, stage="near_miss")
    assert dedup.notify_reason(l, conn, stage="match") == "upgraded"
    # …but staying a near-miss with the same price is not news.
    assert dedup.notify_reason(l, conn, stage="near_miss") is None


def test_notify_reason_relisted_after_two_weeks(conn):
    l = _listing(price_eur=900)
    _notified(conn, l, price=900.0,
              at=datetime.now(timezone.utc) - timedelta(days=15))
    assert dedup.notify_reason(l, conn) == "relisted"


def test_mark_notified_records_stage_and_resets_clock(conn):
    l = _listing(price_eur=900)
    _notified(conn, l, price=800.0, stage="near_miss",
              at=datetime.now(timezone.utc) - timedelta(days=30))
    dedup.mark_notified(l, conn, stage="match")
    # Freshly stamped at the current price and stage → nothing left to re-notify.
    assert dedup.notify_reason(l, conn, stage="match") is None
