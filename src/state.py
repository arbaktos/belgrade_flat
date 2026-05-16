import logging
import os
import sqlite3
import subprocess
from pathlib import Path

from src.models import Listing

log = logging.getLogger(__name__)

LOCAL_DB = Path("db.sqlite")
SCHEMA_VERSION = 5


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
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS commute_cache (
            bucket_key   TEXT PRIMARY KEY,         -- "lat,lng" 3-decimal bucket OR "addr:<hash>"
            walk_min     INTEGER,                   -- null = "Google said no route"
            transit_min  INTEGER,
            fetched_at   TEXT NOT NULL DEFAULT (datetime('now'))
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
            last_seen_at    TEXT NOT NULL DEFAULT (datetime('now'))
        )
        """
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_listings_source ON listings(source)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_listings_created_at ON listings(created_at)")
    conn.execute(
        "INSERT INTO meta (key, value) VALUES ('schema_version', ?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (str(SCHEMA_VERSION),),
    )
    conn.commit()
    return conn


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
