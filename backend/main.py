"""iVault 主服务：REST API + SSE 实时推送。

启动流程：
  uvicorn ivault.main:app --host 0.0.0.0 --port 8666
容器 entrypoint 会先拉起 usbmuxd（USB 通道 + 配对证书目录），再启动本服务。
"""
import asyncio
import collections
import datetime
import logging
import os
import re
import secrets
import time
import uuid

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import StreamingResponse, FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from . import config, device, jobs, store

logging.basicConfig(level=config.LOG_LEVEL,
                    format="%(asctime)s %(name)s %(levelname)s %(message)s")
log = logging.getLogger("ivault.main")


class _RingHandler(logging.Handler):
    """把 WARNING 及以上的日志留在内存里，供「诊断信息」引用。

    容器日志会被重启清空，用户来问问题时往往已经看不到当时那条报错；
    这里保一份最近 200 条，够复现一次故障的上下文。
    """

    def __init__(self, capacity: int = 200):
        super().__init__(level=logging.WARNING)
        self.buf: collections.deque[str] = collections.deque(maxlen=capacity)

    def emit(self, record: logging.LogRecord):
        try:
            self.buf.append(
                datetime.datetime.fromtimestamp(record.created).strftime("%m-%d %H:%M:%S")
                + " " + record.levelname[:4] + " " + record.name.replace("ivault.", "")
                + ": " + record.getMessage()
            )
        except Exception:  # noqa: BLE001
            pass


_ring = _RingHandler()
logging.getLogger().addHandler(_ring)
# uvicorn 自己的 logger 不挂在 root 上，单独挂一份，否则 access / 启动错误收不到
for _name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
    logging.getLogger(_name).addHandler(_ring)

# 版本号由镜像构建参数注入（CI 传 tag 名），本地开发回落到默认值
VERSION = os.environ.get("IVAULT_VERSION", "0.1.0")

app = FastAPI(title="iVault", version=VERSION, docs_url="/api/docs")

# 前端每 6 秒刷一轮状态，一次 4 个请求 —— 开着 uvicorn 的 access log 时，
# 一天五万多条轮询记录会把真正的报错彻底淹掉。entrypoint 里已加 --no-access-log，
# 改由这里只记「值得看」的请求：出错、写操作、异常慢的。
_QUIET_GET = ("/api/status", "/api/devices", "/api/snapshots", "/api/settings", "/api/jobs/active")


@app.middleware("http")
async def _access_log(request: Request, call_next):
    t0 = time.perf_counter()
    response = await call_next(request)
    ms = (time.perf_counter() - t0) * 1000
    path = request.url.path
    if response.status_code >= 400:
        lvl = log.error if response.status_code >= 500 else log.warning
    elif request.method in ("GET", "HEAD") and (
            path in _QUIET_GET or not path.startswith("/api")):
        return response                      # 轮询与静态资源：正常时不记
    elif path.endswith("/events"):
        return response                      # SSE 长连接，耗时没有参考意义
    elif ms < 1500:
        return response
    else:
        lvl = log.info
    lvl("%s %s → %s  %.0fms", request.method, path, response.status_code, ms)
    return response

_pair_sessions: dict[str, device.PairSession] = {}
_state = {"online_devices": [], "transports": {}, "boot_at": time.time()}

# 宿主机系统标识的候选来源（厂商无关）：
#   /host-os-release      compose 可选挂载的宿主机 /etc/os-release
#   /host/etc/os-release  直接挂宿主 /etc 时的位置
#   /etc/ugos-release、/etc/ugos_version  绿联 UGOS 的版本文件
# 刻意不读容器自己的 /etc/os-release：那是镜像发行版（Debian），显示出来是误导。
_OS_FILES = ("/host-os-release", "/host/etc/os-release", "/etc/ugos-release", "/etc/ugos_version")


def _parse_os_release(text: str) -> str | None:
    """从 os-release 风格或纯版本号文件里取一个人类可读的系统名。"""
    fields = dict(re.findall(r"^\s*([A-Z_]+)\s*=\s*(.+?)\s*$", text, re.M))
    pretty = (fields.get("PRETTY_NAME") or "").strip().strip('"')
    name = (fields.get("NAME") or "").strip().strip('"')
    ver = (fields.get("VERSION_ID") or "").strip().strip('"')
    if pretty:
        return pretty
    if name and ver:
        return f"{name} {ver}"
    if name:
        return name
    # 纯文本版本文件（例如某些系统的 /etc/xxx_version）：取第一行有效内容
    for line in text.splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" not in line:
            return line
    return None


def detect_os_name() -> str | None:
    """识别运行主机的系统名与版本，识别不出返回 None（界面自动隐藏这段）。

    本项目不绑定任何一家 NAS：群晖、威联通、绿联、飞牛、TrueNAS、Unraid、
    普通 Linux 主机只要 Docker 能跑 `privileged` + USB 映射即可。系统名不是必需信息，
    所以取不到就不显示，而不是猜一个或写死一个。
    """
    env = os.environ.get("IVAULT_OS_NAME", "").strip()
    if env:
        return env
    for path in _OS_FILES:
        try:
            text = open(path, encoding="utf-8", errors="replace").read()
        except OSError:
            continue
        got = _parse_os_release(text)
        if got:
            # 纯版本号文件（如 /etc/ugos_version 只有 "1.21.1.0016"）补上厂商名，避免界面显示一串裸数字
            if re.fullmatch(r"[\d.]+", got) and "ugos" in os.path.basename(path).lower():
                return "UGOS " + got
            return got
    return None


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


async def _refresh_transports() -> None:
    """刷新在线设备与各自的连接方式（USB / 无线）。"""
    devs = await device.scan_devices()
    _state["online_devices"] = [d["udid"] for d in devs]
    _state["transports"] = {d["udid"]: d["connection"] for d in devs}
    device.set_transports(_state["transports"])   # 命令层据此决定是否加 -n


async def _device_poller():
    """每 3 秒刷新在线设备（USB + 无线），供状态灯与命令通道选择使用。"""
    while True:
        try:
            await _refresh_transports()
        except Exception as e:
            log.warning("device poller: %s", e)
        await asyncio.sleep(3)


@app.get("/api/status")
async def status():
    disk_free = shutil_disk_free(config.BACKUP_DIR)
    return {
        "version": VERSION,
        "os_name": detect_os_name(),
        "uptime_s": int(time.time() - _state["boot_at"]),
        "online_devices": _state["online_devices"],
        "transports": _state["transports"],
        "wifi_enabled": config.WIFI_ENABLED,
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
    out = []
    for d in store.list_devices():
        conn = _state["transports"].get(d["udid"])
        d["online"] = conn is not None
        d["connection"] = conn or "offline"      # usb / network / offline
        snaps = store.list_snapshots(d["udid"], limit=500)
        completed = [s for s in snaps if s["status"] == "COMPLETE"]
        d["snapshot_count"] = len(completed)
        d["total_bytes"] = sum(s["size_bytes"] for s in completed)
        out.append(d)
    return out


@app.delete("/api/devices/{udid}")
async def remove_device(udid: str):
    # 先撤销信任再删记录：unpair 需要在设备表还查得到的时候执行更有意义，
    # 但它本身只需要 UDID，所以顺序不影响；这里保留先撤销的语义。
    r = await device.unpair(udid)
    store.remove_device(udid)
    return {
        "ok": True,
        "trust_revoked": r.get("trust_revoked", False),
        "purged": r.get("purged", 0),
        "message": r.get("message", "已取消配对"),
    }


# ---------------- 配对 ----------------

@app.post("/api/pair/begin")
async def pair_begin():
    # 配对是设备级独占操作（lockdownd 同一时刻只能进行一次配对握手），
    # 多个会话并发会互相打断，导致谁都配不上。这里保证同一时间只有一个会话。
    for old in list(_pair_sessions.values()):
        old.cancel()
    _pair_sessions.clear()
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
    # 补发当前阶段快照：必须是完整的 last_event（含 message/remaining 等字段），
    # 只推给新订阅者，避免给已有订阅者重复广播。
    snap = s.last_event or {"stage": s.stage, "udid": s.udid}
    try:
        q.put_nowait(snap)
    except Exception:  # noqa: BLE001
        pass

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
    # 触发前再刷一次：既校验在线状态，也确保连接方式（USB / 无线）是最新的，
    # 否则可能用错误通道去连设备
    try:
        await _refresh_transports()
    except Exception as e:  # noqa: BLE001
        log.warning("触发前刷新设备失败：%s", e)
    if udid not in _state["online_devices"]:
        raise HTTPException(409, "设备当前不在线（需插线，或确保 Wi-Fi 可达且已开启同步）")
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


@app.get("/api/snapshots/{snap_id}/log")
async def snapshot_log(snap_id: int):
    """回看某次备份的日志。

    以前这些日志只活在 Job 对象里，任务一结束就没了；现在落进 snapshots.log，
    可以事后回答「昨晚那次为什么失败」。
    """
    s = store.get_snapshot(snap_id)
    if not s:
        raise HTTPException(404, "快照不存在")
    return {
        "id": snap_id,
        "udid": s["udid"],
        "status": s["status"],
        "error": s.get("error") or "",
        "started_at": s["started_at"],
        "finished_at": s["finished_at"],
        "log": s.get("log") or "",
    }


@app.get("/api/diagnostics")
async def diagnostics():
    """一键收集排查所需的全部信息（给用户复制粘贴用）。"""
    st = await status()
    lines: list[str] = []
    add = lines.append
    add("=== iVault 诊断信息 ===")
    add("生成时间   : " + datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S %Z").strip())
    add("版本       : " + str(st["version"]))
    add("系统       : " + (st["os_name"] or "（未识别）"))
    add("运行时长   : " + fmt_uptime(st["uptime_s"]))
    add("无线通道   : " + ("已启用" if st["wifi_enabled"] else "未启用（仅 USB）"))
    add("备份目录   : " + st["backup_dir"])
    add("剩余空间   : " + human_size(st["disk_free_bytes"]))
    add("")
    add("--- 设备 ---")
    devs = await devices()
    for d in devs:
        add(f"{d.get('name') or '?'} · {d.get('model') or '?'} · iOS {d.get('ios_version') or '?'}"
            f" · {d['connection']} · ····{d['udid'][-6:]}")
    if not devs:
        add("（无）")
    add("")
    add("--- 最近快照 ---")
    for s in store.list_snapshots(limit=8):
        flag = ("  " + (s.get("error") or "")[:80]) if s.get("error") else ""
        add(f"#{s['id']} {s['status']} {datetime.datetime.fromtimestamp(s['started_at']).strftime('%m-%d %H:%M')}"
            f" {human_size(s['size_bytes'])}{flag}")
    add("")
    add("--- 最近告警/错误（内存中最近 200 条） ---")
    errs = list(_ring.buf)
    add("\n".join(errs) if errs else "（无）")
    return {"text": "\n".join(lines), "generated_at": int(time.time())}


def human_size(n: int) -> str:
    f = float(n or 0)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if f < 1024 or unit == "TB":
            return f"{f:.1f}{unit}" if unit != "B" else f"{int(f)}B"
        f /= 1024
    return f"{f:.1f}TB"


def fmt_uptime(sec: int) -> str:
    dd, rem = divmod(int(sec or 0), 86400)
    hh, rem = divmod(rem, 3600)
    mm = rem // 60
    return (f"{dd}天" if dd else "") + (f"{hh}小时" if hh or dd else "") + f"{mm}分"


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
