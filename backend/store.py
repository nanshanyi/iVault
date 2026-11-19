"""SQLite 元数据存储（设备 / 备份记录 / 设置）。"""
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
CREATE TABLE IF NOT EXISTS backups (
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


# ---------- backups ----------

def create_backup(udid: str, backup_type="FULL", note="") -> int:
    cur = conn().execute(
        "INSERT INTO backups (udid, started_at, type, status, note) VALUES (?,?,?,'RUNNING',?)",
        (udid, int(time.time()), backup_type, note),
    )
    conn().commit()
    return cur.lastrowid


def finish_backup(backup_id: int, status="COMPLETE", size_bytes=0, backup_type=None,
                    error: str = "", log_text: str | None = None):
    """收尾一次备份记录。

    error / log_text 用于事后排查：日志只保留末尾 MAX_LOG_CHARS 字符，
    足够看清失败前后的上下文，又不会把库撑大（一次备份可刷出上千行）。
    """
    if log_text is not None:
        if len(log_text) > MAX_LOG_CHARS:
            log_text = "…（前面 " + str(len(log_text) - MAX_LOG_CHARS) + " 字符已省略）\n" \
                       + log_text[-MAX_LOG_CHARS:]
    sets = ["status=?", "size_bytes=?", "finished_at=?", "error=?"]
    vals: list = [status, size_bytes, int(time.time()), (error or "")[:500]]
    if backup_type:
        sets.append("type=?")
        vals.append(backup_type)
    if log_text is not None:
        sets.append("log=?")
        vals.append(log_text)
    vals.append(backup_id)
    conn().execute("UPDATE backups SET " + ", ".join(sets) + " WHERE id=?", vals)
    conn().commit()


# 单条备份记录日志的落库上限（字符）
MAX_LOG_CHARS = 24000


def fail_interrupted_backups(reason: str = "服务重启，任务被中断") -> int:
    """把上次进程留下的 RUNNING 记录标成 FAILED，返回条数。

    任务状态只活在内存里：容器一重启，Job 对象没了，但备份记录还停在
    RUNNING —— 界面上会永远显示「进行中」，也不会算进成功统计。
    启动时对账一次，让这些记录有个明确的结局。
    """
    cur = conn().execute(
        "UPDATE backups SET status='FAILED', finished_at=?, error=? WHERE status='RUNNING'",
        (int(time.time()), reason[:500]),
    )
    conn().commit()
    return cur.rowcount


def save_backup_log(backup_id: int, log_text: str, error: str = ""):
    """任务结束时把日志尾部与失败原因写进备份记录。

    进程退出后 Job 对象就从内存里移除，日志若不落库就彻底没了——
    用户过几个小时再问「那次为什么失败」，只能看到一条 FAILED。
    """
    if len(log_text) > MAX_LOG_CHARS:
        log_text = "…（前面 " + str(len(log_text) - MAX_LOG_CHARS) + " 字符已省略）\n" \
                   + log_text[-MAX_LOG_CHARS:]
    if error:
        conn().execute("UPDATE backups SET log=?, error=? WHERE id=?",
                       (log_text, error[:500], backup_id))
    else:
        conn().execute("UPDATE backups SET log=? WHERE id=?", (log_text, backup_id))
    conn().commit()


def get_backup(backup_id: int) -> dict | None:
    row = conn().execute("SELECT * FROM backups WHERE id=?", (backup_id,)).fetchone()
    return dict(row) if row else None


def list_backups(udid: str | None = None, limit=100) -> list[dict]:
    """列表不返回 log 全文（单条可达几十 KB），只给 has_log 供前端决定是否显示入口。"""
    cols = ("id, udid, started_at, finished_at, size_bytes, type, status, note, error, "
            "CASE WHEN log IS NULL OR log='' THEN 0 ELSE 1 END AS has_log")
    if udid:
        rows = conn().execute(
            f"SELECT {cols} FROM backups WHERE udid=? ORDER BY started_at DESC, id DESC LIMIT ?",
            (udid, limit),
        ).fetchall()
    else:
        rows = conn().execute(
            f"SELECT {cols} FROM backups ORDER BY started_at DESC, id DESC LIMIT ?", (limit,)
        ).fetchall()
    return [dict(r) for r in rows]


def delete_backup(backup_id: int) -> dict | None:
    row = conn().execute("SELECT * FROM backups WHERE id=?", (backup_id,)).fetchone()
    if not row:
        return None
    conn().execute("DELETE FROM backups WHERE id=?", (backup_id,))
    conn().commit()
    return dict(row)


# ---------- settings ----------

DEFAULTS = {
    "schedule_enabled": "true",
    "schedule_time": "03:00",
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
    return {"devices": list_devices(), "backups": list_backups()}
