"""iVault 主服务：REST API + SSE 实时推送。

启动流程：
  uvicorn ivault.main:app --host 0.0.0.0 --port 8666
容器 entrypoint 会先拉起 usbmuxd（USB 通道 + 配对证书目录），再启动本服务。
"""
import asyncio
import collections
import datetime
import hashlib
import hmac
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

# config.normalize_log_level 已保证合法；这里再兜一层：basicConfig 在导入期执行，
# 一旦抛异常就是「容器起来又立刻退出」的崩溃循环，不值得为一个日志开关冒险。
try:
    logging.basicConfig(level=config.LOG_LEVEL,
                        format="%(asctime)s %(name)s %(levelname)s %(message)s")
except ValueError:
    logging.basicConfig(level=logging.INFO,
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

# 版本号由镜像构建参数注入（CI 传 tag 名，形如 v0.4.0；本地开发回落到默认值）。
# 前端模板自带 "v" 前缀，这里必须只保留裸版本号，否则界面会显示成 "vv0.4.0"。
_raw_ver = os.environ.get("IVAULT_VERSION", "0.4.0").strip()
VERSION = _raw_ver[1:] if _raw_ver[:1] in ("v", "V") else _raw_ver

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


# ---------------- 登录鉴权 ----------------
# 服务以 privileged + host 网络运行，能写设备数据（恢复会覆盖整机），
# 无鉴权时局域网里任何人都能调 /api/restore 或解除配对。
# 通过 IVAULT_PASSWORD 启用；未设置时完全跳过，保持旧行为。
# 实现刻意保持零依赖：HMAC 签名的时效 Cookie，签名密钥由口令派生——
# 改密码立即让全部已有会话失效，这是想要的语义。
COOKIE_NAME = "ivault_session"

# 登录失败限速：按来源 IP 记最近 10 分钟的失败次数，防局域网内穷举
_login_fails: dict[str, list[float]] = {}


def _signing_key() -> bytes:
    return hashlib.sha256(("ivault-session-v1:" + config.PASSWORD).encode()).digest()


def _make_token() -> str:
    exp = str(int(time.time()) + config.SESSION_DAYS * 86400)
    sig = hmac.new(_signing_key(), exp.encode(), hashlib.sha256).hexdigest()
    return f"{exp}.{sig}"


def _check_token(token: str) -> bool:
    try:
        exp_s, sig = token.split(".", 1)
        exp = int(exp_s)
    except (ValueError, AttributeError):
        return False
    if exp < time.time():
        return False
    good = hmac.new(_signing_key(), exp_s.encode(), hashlib.sha256).hexdigest()
    return hmac.compare_digest(sig, good)


@app.middleware("http")
async def _auth_guard(request: Request, call_next):
    # 写在 _access_log 之后注册 → 位于其外层：401 直接短路返回，
    # 未登录时的 6 秒轮询不会把告警环形缓冲刷满 401 记录。
    if not config.PASSWORD:
        return await call_next(request)
    path = request.url.path
    if path == "/api/login" or not path.startswith("/api"):
        return await call_next(request)     # 登录接口与静态前端（登录页本身）放行
    if _check_token(request.cookies.get(COOKIE_NAME, "")):
        return await call_next(request)
    return JSONResponse({"detail": "需要登录"}, status_code=401)


@app.post("/api/login")
async def login(request: Request, body: dict):
    ip = request.client.host if request.client else "?"
    now = time.time()
    # 顺手清掉过期条目：失败过的 IP 若不再尝试，记录不能永远留在内存里
    for k, v in list(_login_fails.items()):
        if not v or now - v[-1] >= 600:
            _login_fails.pop(k, None)
    fails = [t for t in _login_fails.get(ip, []) if now - t < 600]
    if len(fails) >= 8:
        raise HTTPException(429, "尝试次数过多，请 10 分钟后再试")
    if not config.PASSWORD:
        raise HTTPException(400, "未启用登录：设置环境变量 IVAULT_PASSWORD 后重启容器")
    pw = str(body.get("password") or "")
    # 必须编码成 bytes 再比较：compare_digest 对含非 ASCII 的 str 会抛
    # TypeError（接口 500），密码含中文时将永远登不进去，且这类请求
    # 还绕过了失败计数。utf-8 两侧同编码后比较恒安全。
    try:
        ok = hmac.compare_digest(pw.encode("utf-8"), config.PASSWORD.encode("utf-8"))
    except (TypeError, UnicodeEncodeError):
        ok = False
    if not ok:
        fails.append(now)
        _login_fails[ip] = fails
        log.warning("登录失败（来源 %s，10 分钟内第 %d 次）", ip, len(fails))
        raise HTTPException(401, "密码不正确")
    _login_fails.pop(ip, None)
    resp = JSONResponse({"ok": True})
    resp.set_cookie(COOKIE_NAME, _make_token(), max_age=config.SESSION_DAYS * 86400,
                    httponly=True, samesite="lax")
    return resp


@app.post("/api/logout")
async def logout():
    resp = JSONResponse({"ok": True})
    resp.set_cookie(COOKIE_NAME, "", max_age=0, httponly=True, samesite="lax")
    return resp


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
    # 上次退出时还在跑的任务不会自己收尾：内存里的 Job 已经没了，
    # 数据库里的快照还停在 RUNNING。启动时统一对账，避免「永远进行中」。
    try:
        stale = store.fail_interrupted_snapshots()
        if stale:
            log.warning("上次退出时有 %d 个未收尾的快照，已标记为失败", stale)
    except Exception as e:  # noqa: BLE001
        log.warning("对账历史快照失败：%s", e)
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
        "auth": bool(config.PASSWORD),
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
    # 设备互斥由 jobs 层统一裁决：HTTP 与定时调度器走的是同一条路，
    # 检查与登记在同一个事件循环 tick 内完成，不会出现两个请求都通过的情况。
    try:
        job = jobs.start_backup(udid, job_id=uuid.uuid4().hex)
    except jobs.DeviceBusy as e:
        raise HTTPException(409, str(e)) from e
    return {"job_id": job.id, "snapshot_id": job.snap_id}


@app.post("/api/restore")
async def trigger_restore(body: dict):
    udid = body.get("udid")
    if not udid:
        raise HTTPException(400, "缺少 udid")
    # 前置检查放在启动任务之前：恢复是少数会覆盖设备数据的操作，
    # 让失败发生在请求阶段、带着明确原因返回，而不是起一个注定失败的任务。
    if not any(s["status"] == "COMPLETE"
               for s in store.list_snapshots(udid, limit=500)):
        raise HTTPException(409, "还没有可恢复的成功备份，请先完成一次备份")
    try:
        await _refresh_transports()
    except Exception as e:  # noqa: BLE001
        log.warning("恢复前刷新设备失败：%s", e)
    if udid not in _state["online_devices"]:
        raise HTTPException(409, "设备当前不在线：恢复需要把手机插到 NAS 的 USB 口，"
                                 "或确保无线可达且手机已解锁")
    # 恢复同样是设备级独占操作：备份正在写同一个目录时恢复会读到半成品
    try:
        job = jobs.start_restore(udid, job_id=uuid.uuid4().hex)
    except jobs.DeviceBusy as e:
        raise HTTPException(409, str(e)) from e
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
        raise HTTPException(404, "记录不存在")
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
    add("--- 最近备份记录 ---")
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
        raise HTTPException(404, "记录不存在")
    return {"ok": True,
            "note": "磁盘上只保留最新一次可恢复状态；删除记录仅移除展示条目，不回收磁盘空间"}


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
                        if d["udid"] not in _state["online_devices"]:
                            continue
                        # 手动备份 / 恢复正在跑时，定时任务让路：
                        # 以前这里直接调 start_backup，绕过端点的占用检查，
                        # 会在同一台设备上并出第二个进程写同一个目录。
                        try:
                            jobs.start_backup(d["udid"], job_id=uuid.uuid4().hex)
                            log.info("定时备份已触发: %s", d["udid"])
                        except jobs.DeviceBusy:
                            log.info("定时备份跳过 %s：已有进行中的任务", d["udid"])
                    jobs.cleanup_retention()
                    # 这里不再发 webhook：任务结果（成功/失败/取消）由 notify
                    # 在每个任务收尾时推送，触发通知只会制造噪音。
        except Exception as e:
            log.warning("scheduler: %s", e)
        await asyncio.sleep(20)


# ---------------- 前端静态托管 ----------------

import os as _os


class NoCacheStatic(StaticFiles):
    """静态资源一律要求回源校验。

    Starlette 默认只发 ETag/Last-Modified，浏览器会按启发式规则强缓存
    index.html / app.js，于是升级镜像后页面还跑着旧 JS，接口一改就报
    「服务连接中断」，普通刷新（F5）也不生效，非得手动清缓存。
    no-cache 不是不缓存：每次都带 If-None-Match 回源，没变就 304，
    带宽开销几乎为零，但升级后第一次访问必定拿到新文件。
    """

    async def get_response(self, path, scope):
        resp = await super().get_response(path, scope)
        resp.headers["Cache-Control"] = "no-cache, must-revalidate"
        return resp


_static_dir = _os.path.join(_os.path.dirname(_os.path.dirname(__file__)), "frontend")
if not _os.path.isdir(_static_dir):
    _static_dir = None
if _static_dir:
    app.mount("/", NoCacheStatic(directory=_static_dir, html=True), name="ui")
else:
    @app.get("/")
    async def no_ui():
        return JSONResponse({"app": "iVault", "hint": "前端未随包发布，请访问 /api/docs"})
