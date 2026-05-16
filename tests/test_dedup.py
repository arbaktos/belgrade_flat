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
            notified_price REAL
        )"""
    )
    return db


def test_should_notify_new_listing(conn):
    l = _listing()
    ok, reason = dedup.should_notify(l, conn)
    assert ok and reason == "new"


def test_should_notify_already_notified_suppressed(conn):
    l = _listing()
    conn.execute("INSERT INTO listings (fingerprint_key, notified_at, notified_price) VALUES (?, ?, ?)",
                 (l.fingerprint_key, datetime.now(timezone.utc).isoformat(), 900.0))
    ok, reason = dedup.should_notify(l, conn)
    assert not ok and reason == "already_notified"


def test_should_notify_price_drop_reopens(conn):
    l = _listing(price_eur=800)   # was 1000 last time, 20% drop
    conn.execute("INSERT INTO listings (fingerprint_key, notified_at, notified_price) VALUES (?, ?, ?)",
                 (l.fingerprint_key, datetime.now(timezone.utc).isoformat(), 1000.0))
    ok, reason = dedup.should_notify(l, conn)
    assert ok and reason == "price_drop"


def test_should_notify_reappears_after_silence(conn):
    l = _listing()
    old = (datetime.now(timezone.utc) - timedelta(days=20)).isoformat()
    conn.execute("INSERT INTO listings (fingerprint_key, notified_at, notified_price) VALUES (?, ?, ?)",
                 (l.fingerprint_key, old, 900.0))
    ok, reason = dedup.should_notify(l, conn)
    assert ok and reason == "reappeared"
