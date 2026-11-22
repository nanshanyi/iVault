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
    -- BACKUP | RESTORE：恢复任务也记在这张表里，任务历史与日志回看共用一套
    kind TEXT NOT NULL DEFAULT 'BACKUP',
    -- 恢复时读的是谁的备份目录（同机恢复等于 udid）；备份任务为空
    source_udid TEXT DEFAULT '',
    started_at INTEGER,
    finished_at INTEGER,
    size_bytes INTEGER DEFAULT 0,
    type TEXT DEFAULT 'FULL',          -- FULL | INCREMENTAL，只对 BACKUP 有意义
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

def upsert_device(udid: str, name="", model="", ios_version="", encrypted: bool | None = None):
    """encrypted=None 表示查不到：新设备按未开启入库，已有设备保留原值，
    不把「不知道」写成「未开启」。"""
    enc = None if encrypted is None else int(bool(encrypted))
    conn().execute(
        """INSERT INTO devices (udid, name, model, ios_version, encrypted, paired_at)
           VALUES (?,?,?,?,COALESCE(?,0),?)
           ON CONFLICT(udid) DO UPDATE SET
             name=excluded.name, model=excluded.model,
             ios_version=excluded.ios_version,
             encrypted=COALESCE(?, devices.encrypted)""",
        (udid, name, model, ios_version, enc, int(time.time()), enc),
    )
    conn().commit()


def touch_last_backup(udid: str):
    conn().execute(
        "UPDATE devices SET last_backup_at=? WHERE udid=?", (int(time.time()), udid)
    )
    conn().commit()


def set_device_encrypted(udid: str, encrypted: bool):
    conn().execute("UPDATE devices SET encrypted=? WHERE udid=?", (int(bool(encrypted)), udid))
    conn().commit()


def latest_complete_backups() -> list[dict]:
    """每台设备（含已取消配对的）最近一次成功备份，按完成时间倒序。

    换机时旧手机往往已经取消配对，但它的备份目录和记录都还在，
    恢复来源不能只从当前设备列表里找。
    """
    rows = conn().execute(
        """SELECT b.udid, b.id, b.started_at, b.finished_at, b.size_bytes
           FROM backups b
           JOIN (SELECT udid, MAX(id) AS mid FROM backups
                 WHERE status='COMPLETE' AND kind='BACKUP' GROUP BY udid) t ON b.id = t.mid
           ORDER BY b.finished_at DESC, b.id DESC"""
    ).fetchall()
    return [dict(r) for r in rows]


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

def create_backup(udid: str, backup_type="FULL", note="", kind="BACKUP",
                  source_udid="") -> int:
    cur = conn().execute(
        "INSERT INTO backups (udid, kind, source_udid, started_at, type, status, note) "
        "VALUES (?,?,?,?,?,'RUNNING',?)",
        (udid, kind, source_udid, int(time.time()), backup_type, note),
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


# 任务记录保留期：更早的自动清掉（含日志）。写死不做配置，README 里写明
HISTORY_DAYS = 180


def prune_history(days: int = HISTORY_DAYS) -> int:
    """删掉 days 天前的任务记录，返回条数。只动数据库，不动磁盘上的备份目录。

    每台设备「最近一次成功的备份」永远留着，不管多老：「有没有成功备份过」靠它判断——
    全量还是增量、「当前可恢复」的来源列表、恢复前的检查都看这条。删掉它的话，
    备份目录还在，界面却说没有可恢复的备份，下一次还会被当成全量。
    已取消配对的旧手机也一样，换机时还要从它恢复。
    正在跑的（RUNNING）不删：收尾时要按 id 回写。
    """
    cutoff = int(time.time()) - days * 86400
    cur = conn().execute(
        """DELETE FROM backups
           WHERE COALESCE(finished_at, started_at, 0) < ?
             AND status != 'RUNNING'
             AND id NOT IN (SELECT MAX(id) FROM backups
                            WHERE kind='BACKUP' AND status='COMPLETE' GROUP BY udid)""",
        (cutoff,),
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


def list_backups(udid: str | None = None, limit=100, backups_only: bool = False) -> list[dict]:
    """列表不返回 log 全文（单条可达几十 KB），只给 has_log 供前端决定是否显示入口。

    恢复任务也记在这张表里（kind='RESTORE'）。凡是用来判断
    「有没有成功备份」的调用方都要传 backups_only=True，否则一次成功的恢复会被
    当成一份可恢复的备份。
    """
    cols = ("id, udid, kind, source_udid, started_at, finished_at, size_bytes, type, status, note, error, "
            "CASE WHEN log IS NULL OR log='' THEN 0 ELSE 1 END AS has_log")
    where, args = [], []
    if udid:
        where.append("udid=?")
        args.append(udid)
    if backups_only:
        where.append("kind='BACKUP'")
    sql = f"SELECT {cols} FROM backups"
    if where:
        sql += " WHERE " + " AND ".join(where)
    rows = conn().execute(sql + " ORDER BY started_at DESC, id DESC LIMIT ?",
                          (*args, limit)).fetchall()
    return [dict(r) for r in rows]


def backup_attempts_since(udid: str, ts: int) -> list[dict]:
    """某台设备在 ts 之后开始或结束的备份记录（不含恢复），定时补跑据此判断今天还欠不欠一次。

    算上「结束于 ts 之后」的：定时时刻前开跑、之后才完成的备份也算今天这一轮。
    """
    rows = conn().execute(
        "SELECT status, started_at, finished_at, error FROM backups "
        "WHERE udid=? AND kind='BACKUP' AND (started_at>=? OR finished_at>=?) ORDER BY id",
        (udid, ts, ts),
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
    # 定时备份默认关：iOS 16.1 起每次备份都要在手机上输锁屏密码，默认开着、
    # 默认凌晨跑，新装第一晚就会挂着等一个没人输的密码。由用户选一个在手机旁边的时间
    "schedule_enabled": "false",
    "schedule_time": "21:00",
    # daily | weekly | monthly
    "schedule_freq": "daily",
    # 每周：ISO 星期（1=周一 … 7=周日），逗号分隔，可多选
    "schedule_weekdays": "6",
    # 每月：1–31，小月没有这一天时在当月最后一天跑
    "schedule_monthday": "1",
    # 插线自动备份：iPhone 插到 NAS 的 USB 口、当天还没备份过就自动开始。默认关
    "usb_autobackup": "false",
    "notify_webhook": "",
    # 通知范围：all = 成功/失败/取消都推；failure = 只在失败或取消时推。
    # 每天一次的成功通知对部分用户是打扰，这个偏好应该由用户选。
    "notify_scope": "all",
    # 日志详细度：false = 精简（整机进度每涨 5% 留一行，约 40 行/次，
    # 与备份时长无关）；true = 详细（按时间留痕并带文件级噪声，排查用）。
    "log_verbose": "false",
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
