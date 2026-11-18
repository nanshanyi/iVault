"""备份任务引擎：idevicebackup2 子进程管理 + SSE 进度事件。

idevicebackup2 的输出里包含 `Total progress: N`（0-100 整数）逐行刷出，
同时有各 Domain 的传输日志。JobRunner 逐行解析并推送给 SSE 订阅者。
"""
import asyncio
import datetime
import logging
import os
import re
import shutil
import subprocess

from . import config, store

log = logging.getLogger("ivault.jobs")

PROGRESS_RE = re.compile(r"Total progress:\s*(\d+)")
DONE_RE = re.compile(r"(Backup (finished|complete)|Finished)")

# 活跃任务表：job_id -> Job
_jobs: dict[str, "Job"] = {}


class Job:
    def __init__(self, job_id: str, udid: str, kind: str = "BACKUP", snap_id: int | None = None):
        self.id = job_id
        self.udid = udid
        self.kind = kind                      # BACKUP | RESTORE
        self.snap_id = snap_id
        self.subscribers: list[asyncio.Queue] = []
        self.status = "RUNNING"               # RUNNING | DONE | FAILED | CANCELED
        self.progress = 0
        self.phase = "准备中"
        self.log_tail: list[str] = []
        self.proc: asyncio.subprocess.Process | None = None
        self.task: asyncio.Task | None = None

    # ----- 事件广播 -----

    def broadcast(self, event: dict):
        for q in list(self.subscribers):
            try:
                q.put_nowait(event)
            except Exception:
                pass

    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=2000)
        self.subscribers.append(q)
        return q

    def unsubscribe(self, q: asyncio.Queue):
        if q in self.subscribers:
            self.subscribers.remove(q)

    def add_log(self, line: str):
        self.log_tail.append(line)
        if len(self.log_tail) > 400:
            self.log_tail.pop(0)
        self.broadcast({"type": "log", "line": line})

    # ----- 执行 -----

    async def run_backup(self):
        """执行一次备份。增量由 idevicebackup2 自动判断（目标目录已有快照则增量）。"""
        dev = store.get_device(self.udid)
        snap_dir = os.path.join(config.BACKUP_DIR, self.udid)
        os.makedirs(snap_dir, exist_ok=True)
        prior = store.list_snapshots(self.udid, limit=1)
        snap_type = "INCREMENTAL" if prior and any(
            s["status"] == "COMPLETE" for s in prior
        ) else "FULL"

        self.snap_id = store.create_snapshot(self.udid, snap_type=snap_type)
        store.touch_last_backup(self.udid)
        args = [config.BIN_IDEVICEBACKUP2, "-u", self.udid, "backup", config.BACKUP_DIR]
        self.add_log(f"$ {' '.join(args)}")
        try:
            self.proc = await asyncio.create_subprocess_exec(
                *args,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )
        except FileNotFoundError as e:
            self._finish("FAILED", error=f"找不到 {config.BIN_IDEVICEBACKUP2}: {e}")
            return

        assert self.proc.stdout
        async for raw in self.proc.stdout:
            line = raw.decode(errors="replace").rstrip()
            if not line.strip():
                continue
            self.add_log(line)
            m = PROGRESS_RE.search(line)
            if m:
                self.progress = min(100, int(m.group(1)))
                self.phase = "正在传输数据"
                self.broadcast({"type": "progress", "progress": self.progress,
                                "phase": self.phase})
        rc = await self.proc.wait()

        if self.status == "CANCELED":
            store.finish_snapshot(self.snap_id, status="CANCELED")
            self.broadcast({"type": "canceled"})
            return
        if rc == 0:
            size = await _dir_size(snap_dir)
            store.finish_snapshot(self.snap_id, status="COMPLETE",
                                  size_bytes=size, snap_type=snap_type)
            self._finish("DONE", progress=100, size=size, snapshot=self.snap_id)
        else:
            store.finish_snapshot(self.snap_id, status="FAILED")
            self._finish("FAILED", error=f"idevicebackup2 退出码 {rc}")

    def _finish(self, status: str, **extra):
        self.status = status
        self.broadcast({"type": status.lower(), **extra})
        for q in list(self.subscribers):
            try:
                q.put_nowait({"type": "end"})  # 终止哨兵
            except Exception:
                pass

    async def cancel(self):
        if self.status != "RUNNING":
            return
        self.status = "CANCELED"
        if self.proc and self.proc.returncode is None:
            try:
                self.proc.terminate()
                await asyncio.wait_for(self.proc.wait(), timeout=5)
            except Exception:
                self.proc.kill()


async def _dir_size(path: str) -> int:
    """du -sb 的纯 Python 实现（备份目录可能很大，在线程池里算）。"""
    def _walk() -> int:
        total = 0
        for root, _dirs, files in os.walk(path):
            for f in files:
                try:
                    total += os.lstat(os.path.join(root, f)).st_size
                except OSError:
                    pass
        return total
    return await asyncio.get_event_loop().run_in_executor(None, _walk)


# ---------- 对外 API ----------

def get_job(job_id: str) -> Job | None:
    return _jobs.get(job_id)


def active_jobs() -> list[dict]:
    return [
        {"id": j.id, "udid": j.udid, "kind": j.kind, "status": j.status,
         "progress": j.progress, "phase": j.phase}
        for j in _jobs.values() if j.status == "RUNNING"
    ]


def start_backup(udid: str, job_id: str) -> Job:
    job = Job(job_id, udid, kind="BACKUP")
    _jobs[job_id] = job
    job.task = asyncio.create_task(job.run_backup())
    job.task.add_done_callback(lambda _t: _jobs.pop(job.id, None) or None)
    return job


def start_restore(udid: str, job_id: str, source_udid: str | None = None):
    """恢复最近一份 COMPLETE 快照到当前设备。

    注意：增量快照依赖基线目录，idevicebackup2 restore 直接指向备份根目录
    即可由其内部链接解析完整数据。
    """
    job = Job(job_id, udid, kind="RESTORE")
    _jobs[job_id] = job
    job.task = asyncio.create_task(_run_restore(job))
    job.task.add_done_callback(lambda _t: _jobs.pop(job.id, None) or None)
    return job


async def _run_restore(job: "Job"):
    args = [config.BIN_IDEVICEBACKUP2, "-u", job.udid, "restore",
            "--system", "--settings", config.BACKUP_DIR]
    job.add_log(f"$ {' '.join(args)}")
    try:
        job.proc = await asyncio.create_subprocess_exec(
            *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
        )
    except FileNotFoundError as e:
        job._finish("FAILED", error=str(e))
        return
    assert job.proc.stdout
    async for raw in job.proc.stdout:
        line = raw.decode(errors="replace").rstrip()
        if line.strip():
            job.add_log(line)
            m = PROGRESS_RE.search(line)
            if m:
                job.progress = min(100, int(m.group(1)))
                job.broadcast({"type": "progress", "progress": job.progress})
    rc = await job.proc.wait()
    if job.status == "CANCELED":
        job.broadcast({"type": "canceled"})
    elif rc == 0:
        job._finish("DONE", progress=100)
    else:
        job._finish("FAILED", error=f"restore 退出码 {rc}")


def cleanup_retention():
    """按保留策略删除最旧快照（仅清理库记录 + 磁盘上对应的基线目录不存在时跳过）。

    注意：增量快照与全量共享基线目录（idevicebackup2 用同一 UDID 目录持续更新），
    磁盘内容为「最新状态」，因此这里只回收超过保留数的记录，不删物理目录。
    """
    keep = int(store.get_setting("retention") or 20)
    removed = []
    for dev in store.list_devices():
        snaps = store.list_snapshots(dev["udid"], limit=10000)
        completed = [s for s in snaps if s["status"] == "COMPLETE"]
        for old in completed[keep:]:
            store.delete_snapshot(old["id"])
            removed.append(old["id"])
    return removed
