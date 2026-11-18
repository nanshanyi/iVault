"""SQLite 元数据存储（设备 / 快照 / 设置）。"""
import json
import sqlite3
import threading
import time

from . import config

_local = threading.local()

SCHEMA = """
CREATE TABLE IF NOT EXISTS devices (
    udid TEXT PRIMARY KEY,
    name TEXT,
    model TEXT,
    ios_version TEXT,
    encrypted INTEGER DEFAULT 0,
    paired_at INTEGER,
    last_backup_at INTEGER
);
CREATE TABLE IF NOT EXISTS snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    udid TEXT NOT NULL,
    started_at INTEGER,
    finished_at INTEGER,
    size_bytes INTEGER DEFAULT 0,
    type TEXT DEFAULT 'FULL',
    status TEXT DEFAULT 'RUNNING',
    note TEXT DEFAULT ''
);
CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT
);
"""


def conn() -> sqlite3.Connection:
    """每个线程复用一个连接，sqlite3 对象不可跨线程共享。"""
    c = getattr(_local, "conn", None)
    if c is None:
        c = sqlite3.connect(config.DB_PATH, timeout=10)
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA journal_mode=WAL")
        _local.conn = c
    return c


def init_db():
    conn().executescript(SCHEMA)
    conn().commit()


# ---------- devices ----------

def upsert_device(udid: str, name="", model="", ios_version="", encrypted=False):
    conn().execute(
        """INSERT INTO devices (udid, name, model, ios_version, encrypted, paired_at)
           VALUES (?,?,?,?,?,?)
           ON CONFLICT(udid) DO UPDATE SET
             name=excluded.name, model=excluded.model,
             ios_version=excluded.ios_version, encrypted=excluded.encrypted""",
        (udid, name, model, ios_version, int(bool(encrypted)), int(time.time())),
    )
    conn().commit()


def touch_last_backup(udid: str):
    conn().execute(
        "UPDATE devices SET last_backup_at=? WHERE udid=?", (int(time.time()), udid)
    )
    conn().commit()


def remove_device(udid: str):
    conn().execute("DELETE FROM devices WHERE udid=?", (udid,))
    conn().commit()


def list_devices() -> list[dict]:
    rows = conn().execute(
        "SELECT * FROM devices ORDER BY paired_at DESC"
    ).fetchall()
    return [dict(r) for r in rows]


def get_device(udid: str) -> dict | None:
    row = conn().execute("SELECT * FROM devices WHERE udid=?", (udid,)).fetchone()
    return dict(row) if row else None


# ---------- snapshots ----------

def create_snapshot(udid: str, snap_type="FULL", note="") -> int:
    cur = conn().execute(
        "INSERT INTO snapshots (udid, started_at, type, status, note) VALUES (?,?,?,'RUNNING',?)",
        (udid, int(time.time()), snap_type, note),
    )
    conn().commit()
    return cur.lastrowid


def finish_snapshot(snap_id: int, status="COMPLETE", size_bytes=0, snap_type=None):
    if snap_type:
        conn().execute(
            "UPDATE snapshots SET status=?, size_bytes=?, finished_at=?, type=? WHERE id=?",
            (status, size_bytes, int(time.time()), snap_type, snap_id),
        )
    else:
        conn().execute(
            "UPDATE snapshots SET status=?, size_bytes=?, finished_at=? WHERE id=?",
            (status, size_bytes, int(time.time()), snap_id),
        )
    conn().commit()


def list_snapshots(udid: str | None = None, limit=100) -> list[dict]:
    if udid:
        rows = conn().execute(
            "SELECT * FROM snapshots WHERE udid=? ORDER BY started_at DESC LIMIT ?",
            (udid, limit),
        ).fetchall()
    else:
        rows = conn().execute(
            "SELECT * FROM snapshots ORDER BY started_at DESC LIMIT ?", (limit,)
        ).fetchall()
    return [dict(r) for r in rows]


def delete_snapshot(snap_id: int) -> dict | None:
    row = conn().execute("SELECT * FROM snapshots WHERE id=?", (snap_id,)).fetchone()
    if not row:
        return None
    conn().execute("DELETE FROM snapshots WHERE id=?", (snap_id,))
    conn().commit()
    return dict(row)


# ---------- settings ----------

DEFAULTS = {
    "schedule_enabled": "true",
    "schedule_time": "03:00",
    "retention": "5",
    "notify_webhook": "",
}


def get_setting(key: str) -> str:
    row = conn().execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    if row:
        return row["value"]
    return DEFAULTS.get(key, "")


def set_setting(key: str, value: str):
    conn().execute(
        "INSERT INTO settings (key,value) VALUES (?,?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, value),
    )
    conn().commit()


def all_settings() -> dict:
    out = dict(DEFAULTS)
    for r in conn().execute("SELECT key,value FROM settings").fetchall():
        out[r["key"]] = r["value"]
    return out


def dump_meta() -> dict:
    return {"devices": list_devices(), "snapshots": list_snapshots()}
