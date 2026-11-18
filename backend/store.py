"""SQLite 元数据存储（设备 / 快照 / 设置）。"""
import json
import logging
import sqlite3
import threading
import time

from . import config

log = logging.getLogger("ivault.store")

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
    note TEXT DEFAULT '',
    error TEXT DEFAULT '',
    log TEXT DEFAULT ''
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
    _migrate()
    conn().commit()


def _migrate():
    """给旧库补上后加的列（SQLite 没有 ADD COLUMN IF NOT EXISTS）。

    老库里的快照没有日志，界面会显示「无日志」，不影响使用。
    """
    have = {r["name"] for r in conn().execute("PRAGMA table_info(snapshots)")}
    for col, decl in (("error", "TEXT DEFAULT ''"), ("log", "TEXT DEFAULT ''")):
        if col not in have:
            conn().execute(f"ALTER TABLE snapshots ADD COLUMN {col} {decl}")
            log.info("数据库迁移：snapshots 增加 %s 列", col)


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


def finish_snapshot(snap_id: int, status="COMPLETE", size_bytes=0, snap_type=None,
                    error: str = "", log_text: str | None = None):
    """收尾一次快照。

    error / log_text 用于事后排查：日志只保留末尾 MAX_LOG_CHARS 字符，
    足够看清失败前后的上下文，又不会把库撑大（一次备份可刷出上千行）。
    """
    if log_text is not None:
        if len(log_text) > MAX_LOG_CHARS:
            log_text = "…（前面 " + str(len(log_text) - MAX_LOG_CHARS) + " 字符已省略）\n" \
                       + log_text[-MAX_LOG_CHARS:]
    sets = ["status=?", "size_bytes=?", "finished_at=?", "error=?"]
    vals: list = [status, size_bytes, int(time.time()), (error or "")[:500]]
    if snap_type:
        sets.append("type=?")
        vals.append(snap_type)
    if log_text is not None:
        sets.append("log=?")
        vals.append(log_text)
    vals.append(snap_id)
    conn().execute("UPDATE snapshots SET " + ", ".join(sets) + " WHERE id=?", vals)
    conn().commit()


# 单条快照日志的落库上限（字符）
MAX_LOG_CHARS = 24000


def save_snapshot_log(snap_id: int, log_text: str, error: str = ""):
    """任务结束时把日志尾部与失败原因写进快照记录。

    进程退出后 Job 对象就从内存里移除，日志若不落库就彻底没了——
    用户过几个小时再问「那次为什么失败」，只能看到一条 FAILED。
    """
    if len(log_text) > MAX_LOG_CHARS:
        log_text = "…（前面 " + str(len(log_text) - MAX_LOG_CHARS) + " 字符已省略）\n" \
                   + log_text[-MAX_LOG_CHARS:]
    if error:
        conn().execute("UPDATE snapshots SET log=?, error=? WHERE id=?",
                       (log_text, error[:500], snap_id))
    else:
        conn().execute("UPDATE snapshots SET log=? WHERE id=?", (log_text, snap_id))
    conn().commit()


def get_snapshot(snap_id: int) -> dict | None:
    row = conn().execute("SELECT * FROM snapshots WHERE id=?", (snap_id,)).fetchone()
    return dict(row) if row else None


def list_snapshots(udid: str | None = None, limit=100) -> list[dict]:
    """列表不返回 log 全文（单条可达几十 KB），只给 has_log 供前端决定是否显示入口。"""
    cols = ("id, udid, started_at, finished_at, size_bytes, type, status, note, error, "
            "CASE WHEN log IS NULL OR log='' THEN 0 ELSE 1 END AS has_log")
    if udid:
        rows = conn().execute(
            f"SELECT {cols} FROM snapshots WHERE udid=? ORDER BY started_at DESC LIMIT ?",
            (udid, limit),
        ).fetchall()
    else:
        rows = conn().execute(
            f"SELECT {cols} FROM snapshots ORDER BY started_at DESC LIMIT ?", (limit,)
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
