import os
import shutil
import sqlite3
import subprocess
from pathlib import Path

LOCAL_DB = Path("db.sqlite")
SCHEMA_VERSION = 1


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
    conn.execute(
        "INSERT OR IGNORE INTO meta (key, value) VALUES ('schema_version', ?)",
        (str(SCHEMA_VERSION),),
    )
    conn.commit()
    return conn


def stats(conn: sqlite3.Connection) -> dict[str, int]:
    size_bytes = LOCAL_DB.stat().st_size if LOCAL_DB.exists() else 0
    return {"size_bytes": size_bytes}
