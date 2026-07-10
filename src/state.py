from __future__ import annotations

import dataclasses
import hashlib
import json
import logging
import os
import sqlite3
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

from src.models import Extraction, Listing

log = logging.getLogger(__name__)

LOCAL_DB = Path("db.sqlite")
SCHEMA_VERSION = 15


def _rclone_env() -> dict[str, str]:
    return {
        **os.environ,
        "RCLONE_CONFIG_R2_TYPE": "s3",
        "RCLONE_CONFIG_R2_PROVIDER": "Cloudflare",
        "RCLONE_CONFIG_R2_ACCESS_KEY_ID": os.environ["R2_ACCESS_KEY_ID"],
        "RCLONE_CONFIG_R2_SECRET_ACCESS_KEY": os.environ["R2_SECRET_ACCESS_KEY"],
        "RCLONE_CONFIG_R2_ENDPOINT": f"https://{os.environ['R2_ACCOUNT_ID']}.r2.cloudflarestorage.com",
        "RCLONE_CONFIG_R2_REGION": "auto",
    }


def _remote_path(key: str) -> str:
    return f"r2:{os.environ['R2_BUCKET']}/{key}"


def pull(key: str = "state/db.sqlite") -> bool:
    """Fetch SQLite from R2. Returns True if pulled, False if remote missing."""
    if LOCAL_DB.exists():
        LOCAL_DB.unlink()
    result = subprocess.run(
        ["rclone", "copyto", _remote_path(key), str(LOCAL_DB)],
        env=_rclone_env(),
        capture_output=True,
        text=True,
    )
    if result.returncode == 0 and LOCAL_DB.exists():
        return True
    # Treat "not found" as first run; surface other failures.
    stderr = result.stderr.lower()
    if "not found" in stderr or "directory not found" in stderr or "object not found" in stderr:
        return False
    if result.returncode != 0:
        raise RuntimeError(f"rclone pull failed: {result.stderr}")
    return False


def push(key: str = "state/db.sqlite") -> None:
    if not LOCAL_DB.exists():
        raise FileNotFoundError(f"Cannot push {LOCAL_DB}: file missing")
    result = subprocess.run(
        ["rclone", "copyto", str(LOCAL_DB), _remote_path(key)],
        env=_rclone_env(),
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(f"rclone push failed: {result.stderr}")


def ensure_schema() -> sqlite3.Connection:
    conn = sqlite3.connect(LOCAL_DB)
    conn.execute(
        "CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)"
    )

    # Schema v3 makes `elevator` nullable so card-only sources (halooglasi)
    # can persist listings with elevator data still pending detail-page fetch.
    # SQLite can't ALTER a column's NOT NULL constraint, so drop the v2 table
    # if present; M6 dedup isn't wired yet, so losing in-flight rows is fine.
    prev = conn.execute(
        "SELECT value FROM meta WHERE key='schema_version'"
    ).fetchone()
    if prev is not None and int(prev[0]) < 3:
        log.info("state: migrating listings table from v%s → v3", prev[0])
        conn.execute("DROP TABLE IF EXISTS listings")

    # v4 added commute_cache. v5 drops + recreates it once to flush poisoned
    # (None, None) entries written during the brief REQUEST_DENIED period.
    if prev is not None and int(prev[0]) < 5:
        log.info("state: dropping commute_cache to flush poisoned entries from REQUEST_DENIED era")
        conn.execute("DROP TABLE IF EXISTS commute_cache")

    # v6 adds dedup columns to listings. SQLite supports ADD COLUMN cheaply.
    if prev is not None and int(prev[0]) < 6:
        log.info("state: migrating listings table to v6 (dedup columns)")
        for col_sql in (
            "ALTER TABLE listings ADD COLUMN image_phash TEXT",
            "ALTER TABLE listings ADD COLUMN notified_at TEXT",
            "ALTER TABLE listings ADD COLUMN notified_price REAL",
        ):
            try:
                conn.execute(col_sql)
            except sqlite3.OperationalError:
                pass    # column already exists from a previous partial migration

    # v7 adds transit_transfers to commute_cache. Drop the table so we re-query
    # Google with FEWER_TRANSFERS preference and a new field mask.
    if prev is not None and int(prev[0]) < 7:
        log.info("state: dropping commute_cache to recompute with FEWER_TRANSFERS preference")
        conn.execute("DROP TABLE IF EXISTS commute_cache")

    # v13 drops transit entirely: commute is now walking-only, per named
    # destination. bucket_key gains a "@<destination>" suffix so each
    # (location, destination) pair caches separately. Drop the old table so the
    # transit columns disappear and walk re-queries repopulate per destination.
    if prev is not None and int(prev[0]) < 13:
        log.info("state: dropping commute_cache for walking-only per-destination schema (v13)")
        conn.execute("DROP TABLE IF EXISTS commute_cache")

    # v8 adds the skipped table (user-clicked 'hide this listing forever').
    # v9 adds geocode_cache for Nominatim results (winter smog enrichment,
    # retired 2026-07 — table kept so existing DBs and the idempotent
    # migration stay valid).
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS geocode_cache (
            cache_key   TEXT PRIMARY KEY,
            lat         REAL NOT NULL,
            lng         REAL NOT NULL,
            fetched_at  TEXT NOT NULL DEFAULT (datetime('now'))
        )
        """
    )

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS commute_cache (
            bucket_key  TEXT PRIMARY KEY,    -- "<lat,lng bucket OR addr:hash>@<destination>"
            walk_min    INTEGER,             -- null = "Google said no walking route"
            fetched_at  TEXT NOT NULL DEFAULT (datetime('now'))
        )
        """
    )

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS listings (
            fingerprint_key TEXT PRIMARY KEY,
            source          TEXT NOT NULL,
            id              TEXT NOT NULL,
            url             TEXT NOT NULL,
            price_eur       REAL NOT NULL,
            m2              REAL NOT NULL,
            rooms           REAL NOT NULL,
            floor           INTEGER,
            total_floors    INTEGER,
            last_floor      INTEGER NOT NULL,
            elevator        INTEGER,
            furnished       TEXT,
            heating_type    TEXT,
            pets_allowed    INTEGER,
            title           TEXT NOT NULL,
            description     TEXT NOT NULL,
            address         TEXT,
            place_names     TEXT NOT NULL,
            image_url       TEXT,
            is_agency       INTEGER NOT NULL,
            created_at      TEXT NOT NULL,
            first_seen_at   TEXT NOT NULL DEFAULT (datetime('now')),
            last_seen_at    TEXT NOT NULL DEFAULT (datetime('now')),
            image_phash     TEXT,
            notified_at     TEXT,
            notified_price  REAL,
            notified_stage  TEXT
        )
        """
    )
    # v15 adds notified_stage ("match" | "near_miss") so the re-notify policy
    # can detect a near-miss upgrading to a perfect match — that transition is
    # worth a fresh card even when the price didn't move.
    try:
        conn.execute("ALTER TABLE listings ADD COLUMN notified_stage TEXT")
    except sqlite3.OperationalError:
        pass    # column already exists (fresh CREATE above, or re-run)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_listings_source ON listings(source)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_listings_created_at ON listings(created_at)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_listings_image_phash ON listings(image_phash) WHERE image_phash IS NOT NULL")
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS skipped (
            fingerprint_key TEXT PRIMARY KEY,
            skipped_at      TEXT NOT NULL DEFAULT (datetime('now'))
        )
        """
    )
    # v10: dedicated table for the Telegram-channel source. message_id is the
    # post number from the t.me URL; storing the bare int is enough to gate
    # re-processing across runs without persisting full post content.
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS seen_telegram_posts (
            message_id INTEGER PRIMARY KEY,
            seen_at    TEXT NOT NULL DEFAULT (datetime('now'))
        )
        """
    )
    # v11: user-clicked ⭐ Favorite. Parallel to `skipped`; presence here means
    # the card has been forwarded to the favorites destination (or queued if
    # the env var is unset).
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS favorites (
            fingerprint_key TEXT PRIMARY KEY,
            favorited_at    TEXT NOT NULL DEFAULT (datetime('now'))
        )
        """
    )
    # v12: cache LLM extraction results keyed by fingerprint_key so each
    # listing is sent to the LLM exactly once across runs. Keeps us under the
    # Gemini free-tier rate/day caps and cuts cost on any provider. Forward-only
    # CREATE — a cache miss just re-extracts, so no data migration is needed.
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS extraction_cache (
            fingerprint_key TEXT PRIMARY KEY,
            payload         TEXT NOT NULL,
            extracted_at    TEXT NOT NULL DEFAULT (datetime('now')),
            desc_hash       TEXT
        )
        """
    )
    # v14 adds desc_hash so a cache hit requires the description the LLM saw to
    # match the current one — detail-page enrichment can grow a listing's text
    # after it was first extracted (e.g. 4zida's 100-char preview → full desc),
    # and a stale "pets unknown" from the preview must not stick. NULL (legacy
    # rows) loads as a miss, so pre-v14 extractions refresh once.
    try:
        conn.execute("ALTER TABLE extraction_cache ADD COLUMN desc_hash TEXT")
    except sqlite3.OperationalError:
        pass    # column already exists (fresh CREATE above, or re-run)
    conn.execute(
        "INSERT INTO meta (key, value) VALUES ('schema_version', ?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (str(SCHEMA_VERSION),),
    )
    conn.commit()
    return conn


def desc_hash(description: str | None) -> str:
    """Stable hash of the text the LLM extracts from. Cache rows are valid only
    while this matches, so descriptions that grow (detail-page enrichment)
    trigger a re-extraction instead of serving stale 'unknown' fields."""
    return hashlib.sha1((description or "").strip().encode("utf-8")).hexdigest()


def load_extractions(
    conn: sqlite3.Connection,
    keys: Iterable[str],
    *,
    desc_hashes: dict[str, str] | None = None,
) -> dict[str, Extraction]:
    """Return {fingerprint_key: Extraction} for any of `keys` already cached.

    Missing or corrupt rows are simply absent from the result, so the caller
    re-extracts them — the cache is an optimization, never a correctness gate.
    When `desc_hashes` is given, a row whose stored desc_hash doesn't match
    (including legacy NULL) is also a miss: the listing's text changed since
    extraction, so the cached fields may describe a truncated version of it.
    """
    keys = list(keys)
    out: dict[str, Extraction] = {}
    chunk = 500  # stay well under SQLite's 999-variable limit
    for i in range(0, len(keys), chunk):
        batch = keys[i:i + chunk]
        placeholders = ",".join("?" * len(batch))
        rows = conn.execute(
            f"SELECT fingerprint_key, payload, desc_hash FROM extraction_cache "
            f"WHERE fingerprint_key IN ({placeholders})",
            batch,
        )
        for fk, payload, stored_hash in rows:
            if desc_hashes is not None and stored_hash != desc_hashes.get(fk):
                continue
            try:
                out[fk] = _extraction_from_payload(payload)
            except Exception as e:  # noqa: BLE001 — corrupt row → treat as miss
                log.warning("extraction_cache: dropping corrupt row %s: %s", fk, e)
    return out


def save_extractions(conn: sqlite3.Connection, listings: list[Listing]) -> int:
    """Persist the extraction of each listing that has one. Idempotent upsert."""
    rows = [
        (
            l.fingerprint_key,
            json.dumps(dataclasses.asdict(l.extraction)),
            desc_hash(l.description),
        )
        for l in listings
        if l.extraction is not None
    ]
    if not rows:
        return 0
    conn.executemany(
        "INSERT INTO extraction_cache (fingerprint_key, payload, desc_hash) "
        "VALUES (?, ?, ?) "
        "ON CONFLICT(fingerprint_key) DO UPDATE SET "
        "payload=excluded.payload, desc_hash=excluded.desc_hash, "
        "extracted_at=datetime('now')",
        rows,
    )
    conn.commit()
    return len(rows)


def _extraction_from_payload(payload: str) -> Extraction:
    """Rebuild an Extraction from cached JSON, ignoring fields it no longer has
    (so a schema that adds/removes a field stays backward-compatible)."""
    data = json.loads(payload)
    known = {f.name for f in dataclasses.fields(Extraction)}
    return Extraction(**{k: v for k, v in data.items() if k in known})


def upsert_listings(conn: sqlite3.Connection, listings: list[Listing]) -> int:
    rows = [l.to_row() for l in listings]
    if not rows:
        return 0
    cols = [
        "fingerprint_key", "source", "id", "url", "price_eur", "m2", "rooms",
        "floor", "total_floors", "last_floor", "elevator", "furnished",
        "heating_type", "pets_allowed", "title", "description", "address",
        "place_names", "image_url", "is_agency", "created_at",
    ]
    placeholders = ",".join(["?"] * len(cols))
    set_clause = ",".join(f"{c}=excluded.{c}" for c in cols if c != "fingerprint_key")
    sql = (
        f"INSERT INTO listings ({','.join(cols)}) VALUES ({placeholders}) "
        f"ON CONFLICT(fingerprint_key) DO UPDATE SET {set_clause}, "
        f"last_seen_at=datetime('now')"
    )
    conn.executemany(sql, [tuple(r[c] for c in cols) for r in rows])
    conn.commit()
    return len(rows)


def stats(conn: sqlite3.Connection) -> dict[str, int]:
    size_bytes = LOCAL_DB.stat().st_size if LOCAL_DB.exists() else 0
    n_listings = conn.execute("SELECT COUNT(*) FROM listings").fetchone()[0]
    return {"size_bytes": size_bytes, "listings_tracked": n_listings}


def skipped_listings(conn: sqlite3.Connection) -> list[Listing]:
    """Reconstruct the flats the user 🙈 hid, for duplicate detection.

    The `skipped` table holds only fingerprint_keys; we join back to `listings`
    to recover the data the dedup cascade needs (image_phash, title, price, m²)
    so a RE-LISTED hidden flat — same flat, new id — can be suppressed even
    though its fingerprint_key differs. Only carries the fields `_same_flat`
    inspects; the rest get neutral placeholders. Skipped flats with no listings
    row (never persisted) are simply absent.
    """
    rows = conn.execute(
        "SELECT l.source, l.id, l.price_eur, l.m2, l.rooms, l.title, l.image_phash "
        "FROM skipped s JOIN listings l ON l.fingerprint_key = s.fingerprint_key"
    ).fetchall()
    epoch = datetime(1970, 1, 1, tzinfo=timezone.utc)
    out: list[Listing] = []
    for source, id_, price, m2, rooms, title, phash in rows:
        l = Listing(
            id=id_, source=source, url="", price_eur=price, m2=m2, rooms=rooms,
            floor=None, total_floors=None, last_floor=False, elevator=None,
            furnished=None, heating_type=None, pets_allowed=None, title=title or "",
            description="", address=None, place_names=[], image_url=None,
            is_agency=False, created_at=epoch,
        )
        l.image_phash = phash
        out.append(l)
    return out


def seen_telegram_message_ids(conn: sqlite3.Connection) -> set[int]:
    """All channel-post ids we've already processed (any run, any outcome)."""
    return {row[0] for row in conn.execute("SELECT message_id FROM seen_telegram_posts")}


def mark_telegram_message_seen(conn: sqlite3.Connection, message_id: int) -> None:
    conn.execute(
        "INSERT OR IGNORE INTO seen_telegram_posts (message_id) VALUES (?)",
        (message_id,),
    )
    conn.commit()


def notified_keys(conn: sqlite3.Connection) -> set[str]:
    """Snapshot of fingerprint_keys that have been notified before.

    Used by instant-push mode to surface only listings that haven't reached
    the user yet. Capture this BEFORE the pipeline runs so listings marked
    notified during the current run are still considered new for delivery.
    """
    return {row[0] for row in conn.execute(
        "SELECT fingerprint_key FROM listings WHERE notified_at IS NOT NULL"
    )}
