"""iVault 主服务：REST API + SSE 实时推送。

启动流程：
  uvicorn ivault.main:app --host 0.0.0.0 --port 8666
容器 entrypoint 会先拉起 usbmuxd（USB 通道 + 配对证书目录），再启动本服务。
"""
import asyncio
import datetime
import logging
import re
import secrets
import time
import uuid

from fastapi import FastAPI, HTTPException
from fastapi.responses import StreamingResponse, FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from . import config, device, jobs, store

logging.basicConfig(level=config.LOG_LEVEL,
                    format="%(asctime)s %(name)s %(levelname)s %(message)s")
log = logging.getLogger("ivault.main")

app = FastAPI(title="iVault", version="0.1.0", docs_url="/api/docs")

_pair_sessions: dict[str, device.PairSession] = {}
_state = {"online_devices": [], "boot_at": time.time()}


def sse_response(queue_gen):
    return StreamingResponse(
        queue_gen,
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


async def _sse_from_queue(q: asyncio.Queue, heartbeat=15):
    """把 asyncio.Queue 转成 SSE 行流，带心跳保活。"""
    try:
        while True:
            try:
                event = await asyncio.wait_for(q.get(), timeout=heartbeat)
            except asyncio.TimeoutError:
                yield ": keepalive\n\n"
                continue
            if event == {"type": "end"} or event.get("stage") == "__close__":
                break
            payload = {"ts": time.time(), **event}
            line = "data: " + _json_dumps(payload) + "\n\n"
            yield line
    except asyncio.CancelledError:
        pass


def _json_dumps(obj) -> str:
    import json
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"))


# ---------------- 基础 ----------------

@app.on_event("startup")
async def startup():
    store.init_db()
    asyncio.create_task(_device_poller())
    asyncio.create_task(_scheduler_loop())
    log.info("iVault 启动完成，备份目录=%s", config.BACKUP_DIR)


async def _device_poller():
    """每 3 秒刷新 USB 在线设备列表，供前端状态灯使用。"""
    while True:
        try:
            _state["online_devices"] = await device.list_devices()
        except Exception as e:
            log.warning("device poller: %s", e)
        await asyncio.sleep(3)


@app.get("/api/status")
async def status():
    disk_free = shutil_disk_free(config.BACKUP_DIR)
    return {
        "version": "0.1.0",
        "uptime_s": int(time.time() - _state["boot_at"]),
        "online_devices": _state["online_devices"],
        "active_jobs": jobs.active_jobs(),
        "backup_dir": config.BACKUP_DIR,
        "disk_free_bytes": disk_free,
    }


def shutil_disk_free(path: str) -> int:
    try:
        import shutil as _sh
        return _sh.disk_usage(path).free
    except Exception:
        return 0


# ---------------- 设备 ----------------

@app.get("/api/devices")
async def devices():
    online = set(_state["online_devices"])
    out = []
    for d in store.list_devices():
        d["online"] = d["udid"] in online
        snaps = store.list_snapshots(d["udid"], limit=500)
        completed = [s for s in snaps if s["status"] == "COMPLETE"]
        d["snapshot_count"] = len(completed)
        d["total_bytes"] = sum(s["size_bytes"] for s in completed)
        out.append(d)
    return out


@app.delete("/api/devices/{udid}")
async def remove_device(udid: str):
    await device.unpair(udid)
    store.remove_device(udid)
    return {"ok": True}


# ---------------- 配对 ----------------

@app.post("/api/pair/begin")
async def pair_begin():
    sid = uuid.uuid4().hex
    s = device.PairSession(sid)
    _pair_sessions[sid] = s
    s.start()
    return {"session": sid}


@app.get("/api/pair/{sid}/events")
async def pair_events(sid: str):
    s = _pair_sessions.get(sid)
    if not s:
        raise HTTPException(404, "session 不存在或已过期")
    q = s.subscribe()
    s.broadcast({"stage": s.stage, "udid": s.udid})  # 让后加入的订阅者拿到当前阶段

    async def gen():
        try:
            async for chunk in _sse_from_queue(q):
                yield chunk
                # done/error 后自动关闭流
                if '"stage":"done"' in chunk or '"stage":"error"' in chunk:
                    break
        finally:
            s.unsubscribe(q)
            _pair_sessions.pop(sid, None)

    return sse_response(gen())


# ---------------- 备份 ----------------

@app.post("/api/backups")
async def trigger_backup(body: dict):
    udid = body.get("udid")
    if not udid:
        raise HTTPException(400, "缺少 udid")
    if udid not in _state["online_devices"]:
        raise HTTPException(409, "设备当前不在线（需插线或 Wi-Fi 可达）")
    if any(j["udid"] == udid for j in jobs.active_jobs()):
        raise HTTPException(409, "该设备已有进行中的任务")
    job = jobs.start_backup(udid, job_id=uuid.uuid4().hex)
    return {"job_id": job.id, "snapshot_id": job.snap_id}


@app.post("/api/restore")
async def trigger_restore(body: dict):
    udid = body.get("udid")
    if not udid:
        raise HTTPException(400, "缺少 udid")
    job = jobs.start_restore(udid, job_id=uuid.uuid4().hex)
    return {"job_id": job.id}


@app.get("/api/jobs/active")
async def jobs_active():
    return jobs.active_jobs()


@app.get("/api/jobs/{job_id}/events")
async def job_events(job_id: str):
    job = jobs.get_job(job_id)
    if not job:
        raise HTTPException(404, "任务不存在或已结束")
    q = job.subscribe()
    q.put_nowait({"type": "progress", "progress": job.progress, "phase": job.phase})
    for line in job.log_tail[-100:]:
        q.put_nowait({"type": "log", "line": line})

    async def gen():
        try:
            async for chunk in _sse_from_queue(q):
                yield chunk
                if '"type":"end"' in chunk:
                    break
        finally:
            job.unsubscribe(q)

    return sse_response(gen())


@app.post("/api/jobs/{job_id}/cancel")
async def job_cancel(job_id: str):
    job = jobs.get_job(job_id)
    if not job:
        raise HTTPException(404, "任务不存在或已结束")
    await job.cancel()
    return {"ok": True}


@app.get("/api/snapshots")
async def snapshots(udid: str | None = None):
    return store.list_snapshots(udid)


@app.delete("/api/snapshots/{snap_id}")
async def del_snapshot(snap_id: int):
    snap = store.delete_snapshot(snap_id)
    if not snap:
        raise HTTPException(404, "快照不存在")
    return {"ok": True, "note": "增量快照共享基线目录，物理数据未删除，空间由保留策略统一回收"}


# ---------------- 设置 / 定时 ----------------

@app.get("/api/settings")
async def settings():
    return store.all_settings()


@app.put("/api/settings")
async def put_settings(body: dict):
    allowed = {"schedule_enabled", "schedule_time", "retention", "notify_webhook"}
    # 校验并规范化：时间统一成 HH:MM，保留数限定 1–10
    if "schedule_time" in body:
        m = re.fullmatch(r"(\d{1,2}):(\d{2})", str(body["schedule_time"]).strip())
        if not m or int(m.group(1)) > 23 or int(m.group(2)) > 59:
            raise HTTPException(400, "备份时间格式应为 HH:MM（00:00–23:59）")
        body["schedule_time"] = f"{int(m.group(1)):02d}:{int(m.group(2)):02d}"
    if "retention" in body:
        try:
            n = int(body["retention"])
        except (TypeError, ValueError):
            raise HTTPException(400, "保留快照数需为 1–10 的整数")
        if not 1 <= n <= 10:
            raise HTTPException(400, "保留快照数需在 1–10 之间")
        body["retention"] = str(n)
    for k, v in body.items():
        if k in allowed:
            store.set_setting(k, str(v))
    return {"ok": True, "settings": store.all_settings()}


async def _scheduler_loop():
    """每天到达设定时间，对所有在线设备触发备份。"""
    while True:
        try:
            if store.get_setting("schedule_enabled") in ("1", "true", "True"):
                hhmm = store.get_setting("schedule_time") or "03:00"
                now = datetime.datetime.now().strftime("%H:%M")
                today = datetime.date.today().isoformat()
                if now == hhmm and store.get_setting("_last_run") != today:
                    store.set_setting("_last_run", today)
                    for d in store.list_devices():
                        if d["udid"] in _state["online_devices"]:
                            jobs.start_backup(d["udid"], job_id=uuid.uuid4().hex)
                            log.info("定时备份已触发: %s", d["udid"])
                    jobs.cleanup_retention()
                    await _notify("iVault 定时备份已触发")
        except Exception as e:
            log.warning("scheduler: %s", e)
        await asyncio.sleep(20)


async def _notify(text: str):
    url = store.get_setting("notify_webhook")
    if not url:
        return
    try:
        import urllib.request
        import json as _json
        req = urllib.request.Request(
            url, data=_json.dumps({"text": text}).encode(),
            headers={"Content-Type": "application/json"},
        )
        await asyncio.get_event_loop().run_in_executor(
            None, urllib.request.urlopen, req, 5
        )
    except Exception as e:
        log.warning("notify: %s", e)


# ---------------- 前端静态托管 ----------------

import os as _os
_static_dir = _os.path.join(_os.path.dirname(_os.path.dirname(__file__)), "frontend")
if not _os.path.isdir(_static_dir):
    _static_dir = None
if _static_dir:
    app.mount("/", StaticFiles(directory=_static_dir, html=True), name="ui")
else:
    @app.get("/")
    async def no_ui():
        return JSONResponse({"app": "iVault", "hint": "前端未随包发布，请访问 /api/docs"})
