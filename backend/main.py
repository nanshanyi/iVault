"""iVault 主服务：REST API + SSE 实时推送。

启动流程：
  uvicorn ivault.main:app --host 0.0.0.0 --port 8666
容器 entrypoint 会先拉起 usbmuxd（USB 通道 + 配对证书目录），再启动本服务。
"""
import asyncio
import calendar
import collections
import contextlib
import datetime
import hashlib
import hmac
import logging
import os
import re
import time
import uuid

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import StreamingResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from . import config, device, jobs, notify, store

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

# openapi_url 也挪进 /api：默认挂在根路径 /openapi.json，会绕过鉴权中间件
# 把全部接口结构暴露给未登录访客。只是接口列表、拿不到数据，但一行就能堵上，
# 没有理由留着；挪进来之后 /api/docs 照常可用（登录后）。
@contextlib.asynccontextmanager
async def _lifespan(_app):
    """启动与关停。用 lifespan 而不是 @app.on_event：后者在 FastAPI 里已弃用，
    哪天被移除，启动钩子就静默不跑——设备轮询、调度、清理全都不起，也没有任何报错。

    后台任务要留着引用：asyncio 对 create_task 只持弱引用，没人拿着的任务
    可能在运行中途被垃圾回收掉。关停时逐个取消，免得退出时报「任务被销毁但仍挂起」
    """
    await startup()
    try:
        yield
    finally:
        for t in _bg_tasks:
            t.cancel()
        await asyncio.gather(*_bg_tasks, return_exceptions=True)
        _bg_tasks.clear()


_bg_tasks: list[asyncio.Task] = []

app = FastAPI(title="iVault", version=VERSION, lifespan=_lifespan,
              docs_url="/api/docs", openapi_url="/api/openapi.json")

# 前端每 6 秒刷一轮状态，一次 4 个请求 —— 开着 uvicorn 的 access log 时，
# 一天五万多条轮询记录会把真正的报错彻底淹掉。entrypoint 里已加 --no-access-log，
# 改由这里只记「值得看」的请求：出错、写操作、异常慢的。
_QUIET_GET = ("/api/status", "/api/devices", "/api/backups", "/api/settings", "/api/jobs/active",
              "/api/restore/sources")


@app.middleware("http")
async def _access_log(request: Request, call_next):
    t0 = time.perf_counter()
    response = await call_next(request)
    ms = (time.perf_counter() - t0) * 1000
    path = request.url.path
    if response.status_code >= 400:
        lvl = log.error if response.status_code >= 500 else log.warning
    elif request.method not in ("GET", "HEAD"):
        lvl = log.info                       # 写操作一律记：事后要能回答「谁在什么时候点了什么」
    elif path in _QUIET_GET or not path.startswith("/api"):
        return response                      # 轮询与静态资源：正常时不记
    elif path.endswith("/events"):
        return response                      # SSE 长连接，耗时没有参考意义
    elif ms < 1500:
        return response
    else:
        lvl = log.info                       # 慢查询
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


# 必须和 scripts/usb-watchdog.py 的 INTERNAL_HEADER 一致（看护脚本单独拷进镜像，不能 import 这里）
INTERNAL_HEADER = "X-iVault-Internal"


def _is_internal(request: Request) -> bool:
    """请求是否来自容器内部进程（带着 entrypoint 生成的令牌）。

    以前按来源地址判断（127.0.0.1 就放行），但套了反向代理之后，外网请求到达
    这里时来源同样是 127.0.0.1，等于对外免登录。令牌只存在于容器进程的环境变量里。
    """
    if not config.INTERNAL_TOKEN:
        return False
    got = request.headers.get(INTERNAL_HEADER, "")
    return hmac.compare_digest(got.encode("utf-8", "replace"), config.INTERNAL_TOKEN.encode())


@app.middleware("http")
async def _auth_guard(request: Request, call_next):
    # 写在 _access_log 之后注册 → 位于其外层：401 直接短路返回，
    # 未登录时的 6 秒轮询不会把告警环形缓冲刷满 401 记录。
    if not config.PASSWORD:
        return await call_next(request)
    path = request.url.path
    if path == "/api/login" or not path.startswith("/api"):
        return await call_next(request)     # 登录接口与静态前端（登录页本身）放行
    # usb-watchdog 是容器内的独立进程，重启 usbmuxd 前必须确认没有任务在跑，
    # 而它拿不到登录 Cookie —— 凭内部令牌只放行这一个只读接口。
    if path == "/api/status" and request.method == "GET" and _is_internal(request):
        return await call_next(request)
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
_state = {"online_devices": [], "transports": {}, "boot_at": time.time(),
          "viewer_at": 0.0}     # 最近一次有页面拉设备列表的时间（电量采样据此判断有没有人在看）

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

async def startup():
    store.init_db()
    # 上次退出时还在跑的任务不会自己收尾：内存里的 Job 已经没了，
    # 数据库里的备份记录还停在 RUNNING。启动时统一对账，避免「永远进行中」。
    try:
        stale = store.fail_interrupted_backups()
        if stale:
            log.warning("上次退出时有 %d 个未收尾的备份记录，已标记为失败", stale)
    except Exception as e:  # noqa: BLE001
        log.warning("对账历史备份记录失败：%s", e)
    _bg_tasks.extend(asyncio.create_task(c) for c in (
        _device_poller(), _battery_sampler(), _history_pruner(), _scheduler_loop()))
    # 启动行带上关键配置：单看容器日志就能知道是哪个版本、开了什么
    sched = (_schedule_text(_schedule_cfg())
             if store.get_setting("schedule_enabled") in ("1", "true", "True") else "关闭")
    usb = "开" if store.get_setting("usb_autobackup") in ("1", "true", "True") else "关"
    log.info("iVault v%s 启动完成 · 访问密码%s · 无线%s · 定时备份 %s · 插线备份 %s · "
             "最低电量 %d%% · 最低剩余 %g GB · 备份目录 %s",
             VERSION, "已开启" if config.PASSWORD else "未开启",
             "已启用" if config.WIFI_ENABLED else "未启用", sched, usb,
             config.MIN_BATTERY, config.MIN_FREE_GB, config.BACKUP_DIR)


# 无线手机锁屏后，netmuxd 会周期性把它从列表里摘掉、mDNS 重新发现后再加回
# （实测锁屏一夜 42 次，大约每 10 分钟一次，离线中位 73 秒）。离线不满这么久就
# 回来的不记上下线日志，否则容器日志一夜就被刷满，导出时只剩这些
OFFLINE_LOG_GRACE = 120
_offline_pending: dict[str, tuple[float, str]] = {}   # udid -> (离线时刻, 离线前的连接方式)，还没记日志
_first_scan = True      # 进程内第一次扫描：那时 old 是空的，a 为 None 不代表「刚离线」


# 插线自动备份：设备从「不是 USB」变成 USB 就算插上了一次。两种「假插线」要排除：
#   · 进程首扫：容器重启时手机本来就插着，不是用户刚插
#   · usbmuxd 被看护重启（另一台设备插线时会发生）：已经插着的设备会短暂掉线又回来
# 后者靠「上一次看到它在 USB 上」的时间判断：不到 USB_REPLUG_GAP 秒就回来的不算
USB_REPLUG_GAP = 20
_usb_seen_at: dict[str, float] = {}   # udid -> 最近一次扫描到它在 USB 上的时间
_usb_plugged: set[str] = set()        # 刚插上、等 _device_poller 处理的设备


def _device_label(udid: str) -> str:
    d = store.get_device(udid)
    return ((d or {}).get("name") or "…" + udid[-6:]) + ("" if d else "（未配对）")


async def _refresh_transports(probe: str | None = None) -> None:
    """刷新在线设备与各自的连接方式（USB / 无线）。

    probe=udid：马上要对这台设备动手（备份、恢复、改加密），无线探活不走缓存，
    避免对一台 60 秒内已经离开的手机发起任务。
    """
    global _first_scan
    devs = await device.scan_devices(probe=probe)
    old = _state["transports"]
    new = {d["udid"]: d["connection"] for d in devs}
    # 离线或换了连接方式时旧电量作废：无线换成 USB 就是插上线开始充电了，
    # 留着旧读数的话，调度器要等它过期（无线 2 分钟）才发现可以备份
    label = {device.TRANSPORT_USB: "USB", device.TRANSPORT_NET: "Wi-Fi"}
    now = time.time()
    # 插线检测放在这里而不是 _device_poller：备份、恢复、改加密的接口也会调这个函数，
    # 插线那一下可能被它们先看到。这里只登记，由 _device_poller 统一处理
    for udid, conn in new.items():
        if conn != device.TRANSPORT_USB:
            continue
        if (old.get(udid) != device.TRANSPORT_USB and not _first_scan
                and now - _usb_seen_at.get(udid, 0) >= USB_REPLUG_GAP):
            _usb_plugged.add(udid)
        _usb_seen_at[udid] = now
    for udid in set(old) | set(new):
        a, b = old.get(udid), new.get(udid)
        if a == b:
            continue
        device.battery_forget(udid)
        # 任务进行中的每次变化都进任务日志，不受下面 OFFLINE_LOG_GRACE 的限制。
        # 首扫跳过：那时 old 是空的，「离线 → Wi-Fi」是假的
        job = jobs.busy_job(udid) if not _first_scan else None
        if job is not None and job.udid == udid:   # 换机恢复的来源设备在不在线无所谓
            why = device.net_offline_reason(udid) if b is None and a == device.TRANSPORT_NET else ""
            job.note_transport(a, b, why)
        # 进程首扫时 old 是空字典，a 为 None 只是「还没有记录」，不是刚离线。
        # 不拦的话每次重启都会为每台在线设备刷一条「离线 → Wi-Fi」
        if a is None and _first_scan:
            continue
        # 只在状态变化时记一条：排查无线「时有时无」最需要这条时间线。
        # 掉线先挂起，满 OFFLINE_LOG_GRACE 还没回来才记（见下方）
        if b is None:
            _offline_pending[udid] = (now, a)
            continue
        if a is None and udid in _offline_pending:
            _, before = _offline_pending.pop(udid)
            if before == b:
                continue                      # 短暂掉线又回来：不记
            a = before                        # 掉线期间换了通道（如 Wi-Fi → USB）：记通道变化
        log.info("设备 %s：%s → %s", _device_label(udid), label.get(a, "离线"), label.get(b))
    _first_scan = False
    for udid, (since, before) in list(_offline_pending.items()):
        if now - since >= OFFLINE_LOG_GRACE:
            _offline_pending.pop(udid)
            log.info("设备 %s：%s → 离线（自 %s 起）", _device_label(udid), label.get(before),
                     datetime.datetime.fromtimestamp(since).strftime("%H:%M:%S"))
    _state["online_devices"] = [d["udid"] for d in devs]
    _state["transports"] = new
    device.set_transports(new)   # 命令层据此决定是否加 -n


_USB_DONE = {"COMPLETE": "成功备份过", "RUNNING": "有备份在跑", "CANCELED": "手动取消过备份"}


async def _usb_autobackup():
    """处理刚插上 USB 的设备：开着插线自动备份、今天是备份日、当天还没备份过就开始。

    备份日和定时备份共用「频率」设置（每天 / 每周几 / 每月几号），不看备份时间：
    选了每周六，就只有周六插线才会自动备份，哪个钟点插都行。
    当天（本地零点起）有成功、正在跑、或被手动取消的备份就不再触发：取消说明用户
    这次不想备份。失败的不算，拔插一次就是用户在说「再来一次」。
    不查电量：插着线就在充电。不推等密码提醒：人刚插线，手机就在手边。
    """
    if not _usb_plugged:
        return
    pending = list(_usb_plugged)
    _usb_plugged.clear()                      # 开关关着也要清掉，别等打开后补发一堆旧的
    if store.get_setting("usb_autobackup") not in ("1", "true", "True"):
        return
    today = datetime.date.today()
    cfg = _schedule_cfg()
    is_day = _runs_on(today, cfg)
    midnight = int(datetime.datetime.combine(today, datetime.time()).timestamp())
    for udid in pending:
        # 没配对的（配对向导里刚插上的那台）跳过；配对完它已经插着，也不会再触发
        if not store.get_device(udid):
            continue
        if _state["transports"].get(udid) != device.TRANSPORT_USB or jobs.busy_job(udid):
            continue
        if not is_day:
            log.info("插线自动备份跳过 %s：今天不是备份日（%s）", _device_label(udid), _days_text(cfg))
            continue
        done = [t["status"] for t in store.backup_attempts_since(udid, midnight)
                if t["status"] in _USB_DONE]
        if done:
            log.info("插线自动备份跳过 %s：今天已经%s", _device_label(udid), _USB_DONE[done[-1]])
            continue
        try:
            jobs.start_backup(udid, job_id=uuid.uuid4().hex, trigger="usb")
            log.info("插线自动备份已触发：%s", _device_label(udid))
        except jobs.DeviceBusy:
            pass


async def _history_pruner():
    """任务记录保留 180 天（store.HISTORY_DAYS）：启动时清一次，之后每天一次。

    独立成一个循环而不是放进定时调度器：调度器只在开了定时备份时干活，
    清理和备不备份无关。
    """
    while True:
        try:
            n = store.prune_history()
            if n:
                log.info("已清理 %d 条 %d 天前的任务记录（每台设备最近一次成功的备份保留）",
                         n, store.HISTORY_DAYS)
        except Exception as e:  # noqa: BLE001
            log.warning("清理历史任务记录失败：%s", e)
        await asyncio.sleep(86400)


async def _device_poller():
    """每 3 秒刷新在线设备（USB + 无线），供状态灯与命令通道选择使用；顺带处理插线自动备份。"""
    while True:
        try:
            await _refresh_transports()
            await _usb_autobackup()
        except Exception as e:
            log.warning("device poller: %s", e)
        await asyncio.sleep(3)


# 页面最后一次拉设备列表之后这么久内算「有人在看」（前端 6 秒轮询一次）
VIEWER_ACTIVE_S = 30
_viewer_wake = asyncio.Event()


async def _battery_sampler():
    """只在有页面开着时给卡片采电量：USB 60 秒、无线 2 分钟一次（见 device.BATTERY_MAX_AGE_*）。

    没人看页面时一次都不查。调度器和手动备份要电量时自己按需现查，不靠这里。
    读数过期的设备并发去查，慢的无线设备不会拖住其他设备。
    """
    while True:
        try:
            if time.time() - _state["viewer_at"] < VIEWER_ACTIVE_S:
                # 只采已配对的：插着线但还没配对（或刚取消配对）的手机，ideviceinfo
                # 连不上 lockdown 只会白跑，还可能和配对向导抢同一台设备
                paired = {d["udid"] for d in store.list_devices()}
                stale = [u for u in _state["online_devices"]
                         if u in paired and device.battery_stale(u)]
                if stale:
                    await asyncio.gather(*(device.battery_refresh(u) for u in stale))
        except Exception as e:  # noqa: BLE001
            log.warning("battery sampler: %s", e)
        # 页面刚打开时由 /api/devices 立刻叫醒，不必等满 5 秒
        try:
            await asyncio.wait_for(_viewer_wake.wait(), timeout=5)
        except asyncio.TimeoutError:
            pass
        _viewer_wake.clear()


@app.get("/api/status")
async def status():
    disk_free = shutil_disk_free(config.BACKUP_DIR)
    return {
        "version": VERSION,
        "os_name": detect_os_name(),
        "auth": bool(config.PASSWORD),
        "session_days": config.SESSION_DAYS,
        "uptime_s": int(time.time() - _state["boot_at"]),
        "online_devices": _state["online_devices"],
        "transports": _state["transports"],
        "wifi_enabled": config.WIFI_ENABLED,
        "active_jobs": jobs.active_jobs(),
        "backup_dir": config.BACKUP_DIR,
        "data_dir": config.DATA_DIR,
        "disk_free_bytes": disk_free,
        "activity": _activity(),
    }


ACTIVITY_DAYS = 30


def _activity() -> dict:
    """总览「最近 30 天」：每台已配对设备每天的结果，外加最近一次成功、最近一次结果。

    days 里只放有记录的日子，前端按日期补空格子；日期是容器本地日期（YYYY-MM-DD）。
    最近一次成功不限 30 天：两个月没备份的设备，要能显示「62 天前」而不是「无」
    """
    today = datetime.date.today()
    start = today - datetime.timedelta(days=ACTIVITY_DAYS - 1)
    since = int(datetime.datetime.combine(start, datetime.time()).timestamp())
    by_dev: dict[str, dict] = {}
    for r in store.daily_outcomes(since):
        by_dev.setdefault(r["udid"], {})[r["day"]] = {"ok": r["ok"], "failed": r["failed"]}
    out = []
    for d in store.list_devices():
        udid = d["udid"]
        recent = store.list_backups(udid, limit=20, backups_only=True)
        last = next((b for b in recent if b["status"] in ("COMPLETE", "FAILED")), None)
        ok = next((b for b in recent if b["status"] == "COMPLETE"), None)
        out.append({
            "udid": udid,
            "days": by_dev.get(udid, {}),
            "last_ok_at": (ok["finished_at"] or ok["started_at"]) if ok else d.get("last_backup_at"),
            "last_status": last["status"] if last else None,
            "last_error": (last.get("error") or "") if last and last["status"] == "FAILED" else "",
        })
    return {"from": start.isoformat(), "to": today.isoformat(), "devices": out}


def shutil_disk_free(path: str) -> int:
    try:
        import shutil as _sh
        return _sh.disk_usage(path).free
    except Exception:
        return 0


# ---------------- 设备 ----------------

@app.get("/api/devices")
async def devices():
    now = time.time()
    if now - _state["viewer_at"] >= VIEWER_ACTIVE_S:
        _viewer_wake.set()                       # 停了一阵后第一次有人看：马上开始采样
    _state["viewer_at"] = now
    out = []
    for d in store.list_devices():
        conn = _state["transports"].get(d["udid"])
        d["online"] = conn is not None
        d["connection"] = conn or "offline"      # usb / network / offline
        snaps = store.list_backups(d["udid"], limit=500, backups_only=True)
        completed = [s for s in snaps if s["status"] == "COMPLETE"]
        d["encrypted"] = bool(d.get("encrypted"))
        d["backup_count"] = len(completed)
        # 占用只能取最近一次成功备份的统计：单目录覆盖模式下每条记录的
        # size_bytes 都是整个备份目录的大小（增量也写同一目录），
        # 按条数求和会把同一份数据成倍虚高（60GB 手机备份 5 次显示 300GB）。
        d["total_bytes"] = completed[0]["size_bytes"] if completed else 0
        # 电量只读缓存，不在请求里拉进程：一台无线设备超时会把整个列表拖住 6 秒。
        # 读数由 _battery_sampler 在后台补，带 at（采样时间），前端据此标注「N 分钟前」
        d["battery"] = device.battery_cached(d["udid"]) if d["online"] else None
        # 在线但还没采过：首屏时采样器刚被叫醒，前端显示「读取中…」而不是「未取到」
        d["battery_pending"] = d["online"] and not device.battery_sampled(d["udid"])
        out.append(d)
    return out


@app.delete("/api/devices/{udid}")
async def remove_device(udid: str):
    # 先撤销信任再删记录：unpair 需要在设备表还查得到的时候执行更有意义，
    # 但它本身只需要 UDID，所以顺序不影响；这里保留先撤销的语义。
    r = await device.unpair(udid)
    store.remove_device(udid)
    device.battery_forget(udid)
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
    # 补发当前阶段状态：必须是完整的 last_event（含 message/remaining 等字段），
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

async def _battery_block_reason(udid: str, max_age: float | None = 0) -> str | None:
    """电量过低且未接电源时返回拒绝原因，否则 None。

    max_age=0（手动备份）总是现查；调度器传 None，按连接方式复用未过期的读数
    （USB 60 秒、无线 2 分钟），被电量拦住的无线设备因此最多 2 分钟查一次。

    取不到电量时一律放行：这条检查是「避免备份跑到一半没电」，不是安全边界，
    不能因为查询失败（设备忙、字段缺失）就把正常备份挡在门外。
    接着电源（USB 线或有线充电）时也不拦——插着线本来就在充电。
    """
    if config.MIN_BATTERY <= 0:
        return None
    b = device.battery_cached(udid)
    if device.battery_stale(udid, max_age):
        b = await device.battery_refresh(udid)
    if not b or b["level"] >= config.MIN_BATTERY or b["charging"] or b["external"]:
        return None
    return (f"电量仅 {b['level']}%，低于 {config.MIN_BATTERY}% 且未在充电。"
            "请先充电，或插上数据线备份（插线会边充边备）")


@app.post("/api/backups")
async def trigger_backup(body: dict):
    udid = body.get("udid")
    if not udid:
        raise HTTPException(400, "缺少 udid")
    # 先看忙不忙：下面现场探活要将近 1 秒，任务进行中再点一次没必要等它
    busy = jobs.busy_job(udid)
    if busy is not None:
        raise HTTPException(409, str(jobs.DeviceBusy(busy, udid)))
    # 触发前再刷一次：既校验在线状态，也确保连接方式（USB / 无线）是最新的，
    # 否则可能用错误通道去连设备
    try:
        await _refresh_transports(probe=udid)
    except Exception as e:  # noqa: BLE001
        log.warning("触发前刷新设备失败：%s", e)
    if udid not in _state["online_devices"]:
        raise HTTPException(409, "设备当前不在线（需插线，或确保 Wi-Fi 可达且已开启同步）")
    reason = await _battery_block_reason(udid)
    if reason:
        raise HTTPException(409, reason)
    # 设备互斥由 jobs 层统一裁决：HTTP 与定时调度器走的是同一条路，
    # 检查与登记在同一个事件循环 tick 内完成，不会出现两个请求都通过的情况。
    try:
        job = jobs.start_backup(udid, job_id=uuid.uuid4().hex)
    except jobs.DeviceBusy as e:
        raise HTTPException(409, str(e)) from e
    return {"job_id": job.id, "backup_id": job.backup_id}


@app.get("/api/restore/sources")
async def restore_sources():
    """可以拿来恢复的备份：每台设备（含已取消配对的旧手机）最新一份成功备份。

    换机时旧手机常常已经取消配对，但它的备份还在磁盘上，必须能选到。
    只列磁盘上真的有完整备份目录的（Manifest.plist + Info.plist 都在）。
    """
    paired = {d["udid"]: d for d in store.list_devices()}
    out = []
    for b in store.latest_complete_backups():
        meta = jobs.backup_meta(b["udid"])
        if not meta:
            continue
        d = paired.get(b["udid"])
        out.append({
            "udid": b["udid"],
            "name": (d or {}).get("name") or meta.get("device_name") or "…" + b["udid"][-6:],
            "paired": d is not None,
            "encrypted": meta.get("encrypted", False),
            "ios_version": meta.get("ios_version", ""),
            "product_type": meta.get("product_type", ""),
            "finished_at": b["finished_at"] or b["started_at"],
            "size_bytes": b["size_bytes"],
        })
    return out


@app.post("/api/restore")
async def trigger_restore(body: dict):
    """把 source_udid 的备份恢复到 udid；不给 source_udid 即恢复到自己（同机恢复）。"""
    udid = body.get("udid")
    if not udid:
        raise HTTPException(400, "缺少 udid")
    source = str(body.get("source_udid") or udid)
    password = str(body.get("password") or "")
    # 前置检查放在启动任务之前：恢复是少数会覆盖设备数据的操作，
    # 让失败发生在请求阶段、带着明确原因返回，而不是起一个注定失败的任务。
    if not store.get_device(udid):
        raise HTTPException(409, "目标设备还没有配对：新手机请先在「设备管理」里添加设备")
    if not any(s["status"] == "COMPLETE"
               for s in store.list_backups(source, limit=500, backups_only=True)):
        raise HTTPException(409, "还没有可恢复的成功备份，请先完成一次备份")
    meta = jobs.backup_meta(source)
    if not meta:
        raise HTTPException(409, "磁盘上找不到这份备份的完整目录（缺少 Manifest.plist 或 Info.plist）")
    if meta.get("encrypted") and not password:
        raise HTTPException(400, "这份备份已加密，需要输入备份密码")
    try:
        await _refresh_transports(probe=udid)
    except Exception as e:  # noqa: BLE001
        log.warning("恢复前刷新设备失败：%s", e)
    if udid not in _state["online_devices"]:
        raise HTTPException(409, "设备当前不在线：恢复需要把手机插到 NAS 的 USB 口，"
                                 "或确保无线可达且手机已解锁")
    # iOS 不允许把新系统的备份恢复到旧系统上，这一步必然失败，提前拦下并说清楚。
    # 目标版本现查一次：库里的 ios_version 是配对时记的，手机升级后会过期。
    src_ver = meta.get("ios_version", "")
    info = await device.device_info(udid)
    dst_ver = info.get("ProductVersion") or (store.get_device(udid) or {}).get("ios_version") or ""
    if src_ver and dst_ver and jobs.version_tuple(src_ver) > jobs.version_tuple(dst_ver):
        raise HTTPException(409, f"这份备份来自 iOS {src_ver}，目标手机是 iOS {dst_ver}。"
                                 f"请先把目标手机升级到 iOS {src_ver} 或更高版本再恢复")
    # 恢复同时占用目标与来源设备：来源的备份目录在被读，不能同时被备份改写
    try:
        job = jobs.start_restore(udid, job_id=uuid.uuid4().hex,
                                 source_udid=source, password=password or None)
    except jobs.DeviceBusy as e:
        raise HTTPException(409, str(e)) from e
    return {"job_id": job.id}


@app.post("/api/devices/{udid}/encryption")
async def set_encryption(udid: str, body: dict):
    """开启或关闭备份加密。

    加密是 iPhone 上的开关：开启后之后的每一份备份都由设备加密，备份时不需要密码；
    恢复和关闭时才需要。密码不保存在 NAS 上，只在这一次请求里经环境变量交给工具。
    """
    enable = bool(body.get("enable"))
    password = str(body.get("password") or "")
    if not password:
        raise HTTPException(400, "请输入备份密码")
    if len(password) > 128:
        raise HTTPException(400, "密码太长")
    if not store.get_device(udid):
        raise HTTPException(404, "设备不存在")
    try:
        await _refresh_transports(probe=udid)
    except Exception as e:  # noqa: BLE001
        log.warning("修改加密前刷新设备失败：%s", e)
    if udid not in _state["online_devices"]:
        raise HTTPException(409, "设备当前不在线：请插上数据线并解锁手机")
    try:
        with jobs.device_lock(udid):
            ok, msg = await device.set_encryption(udid, enable, password)
            state = await device.will_encrypt(udid)
    except jobs.DeviceBusy as e:
        raise HTTPException(409, str(e)) from e
    if state is not None:
        store.set_device_encrypted(udid, state)
    if not ok:
        log.warning("修改备份加密失败 %s：%s", udid, msg)
        raise HTTPException(409, msg)
    log.info("备份加密已%s：%s", "开启" if enable else "关闭",
             (store.get_device(udid) or {}).get("name") or udid)
    return {"ok": True, "encrypted": state if state is not None else enable, "message": msg}


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
    # 底部两行常驻（进度 / 当前文件）是前端原地刷新的：重连时补一次快照，
    # 否则要等到下一次进度或换文件才出现，刷新页面看起来像丢了状态
    for slot, text in job.live_snapshot().items():
        q.put_nowait({"type": "live", "slot": slot, "text": text})

    async def gen():
        try:
            async for chunk in _sse_from_queue(q):
                yield chunk
                if '"type":"end"' in chunk:
                    break
        finally:
            job.unsubscribe(q)

    return sse_response(gen())


def _client_label(request: Request) -> str:
    """给日志看的请求来源，回答「谁点的」。只用于日志展示，不参与任何鉴权。

    不自己读 X-Forwarded-For：它的第一段是客户端可以随便填的。uvicorn 默认信任
    来自 127.0.0.1 的代理头（--forwarded-allow-ips 默认值），并且取的是代理自己追加的
    那一段，所以本机反向代理后面 client.host 已经是真实客户端，直连时就是对端地址。
    剩下回环地址（代理没加头、或容器内自己请求）没有意义，只写「来自网页」。
    """
    host = request.client.host if request.client else ""
    if not host or host in ("127.0.0.1", "::1", "::ffff:127.0.0.1"):
        return "来自网页"
    return f"来自网页 · {host}"


@app.post("/api/jobs/{job_id}/cancel")
async def job_cancel(job_id: str, request: Request):
    job = jobs.get_job(job_id)
    if not job:
        raise HTTPException(404, "任务不存在或已结束")
    await job.cancel(source=_client_label(request))
    return {"ok": True}


@app.get("/api/backups/{backup_id}/log")
async def backup_log(backup_id: int):
    """回看某次备份的日志。

    以前这些日志只活在 Job 对象里，任务一结束就没了；现在落进 backups.log，
    可以事后回答「昨晚那次为什么失败」。
    """
    s = store.get_backup(backup_id)
    if not s:
        raise HTTPException(404, "记录不存在")
    return {
        "id": backup_id,
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
    for s in store.list_backups(limit=8):
        flag = ("  " + (s.get("error") or "")[:80]) if s.get("error") else ""
        add(f"#{s['id']} {s['kind']} {s['status']} {datetime.datetime.fromtimestamp(s['started_at']).strftime('%m-%d %H:%M')}"
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


@app.get("/api/backups")
async def backups(udid: str | None = None):
    return store.list_backups(udid)


@app.delete("/api/backups/{backup_id}")
async def del_backup(backup_id: int):
    cur = store.get_backup(backup_id)
    if not cur:
        raise HTTPException(404, "记录不存在")
    # 进行中的记录不能删：任务收尾时会按 id 回写状态和日志，记录没了这些就静默丢失，
    # 任务历史里也看不到这次任务。前端已隐藏按钮，这里防直接调接口。
    if cur["status"] == "RUNNING":
        raise HTTPException(409, "任务还在进行中，结束后才能删除这条记录")
    rec = store.delete_backup(backup_id)
    if not rec:
        raise HTTPException(404, "记录不存在")
    return {"ok": True,
            "note": "磁盘上只保留最新一次可恢复状态；删除记录仅移除展示条目，不回收磁盘空间"}


# ---------------- 设置 / 定时 ----------------

@app.get("/api/settings")
async def settings():
    return store.all_settings()


@app.put("/api/settings")
async def put_settings(body: dict):
    allowed = {"schedule_enabled", "schedule_time", "schedule_freq", "schedule_weekdays",
               "schedule_monthday", "usb_autobackup", "notify_webhook", "notify_scope", "log_verbose"}
    # 校验并规范化：时间统一成 HH:MM。
    # 没有「保留份数」之类的设置：备份是单目录覆盖模式，磁盘上始终只有
    # 最新一次可恢复状态，历史记录只记账不回收，也不需要配置。
    if "schedule_time" in body:
        m = re.fullmatch(r"(\d{1,2}):(\d{2})", str(body["schedule_time"]).strip())
        if not m or int(m.group(1)) > 23 or int(m.group(2)) > 59:
            raise HTTPException(400, "备份时间格式应为 HH:MM（00:00–23:59）")
        body["schedule_time"] = f"{int(m.group(1)):02d}:{int(m.group(2)):02d}"
    if "schedule_freq" in body and str(body["schedule_freq"]) not in ("daily", "weekly", "monthly"):
        raise HTTPException(400, "频率只支持 daily（每天）、weekly（每周）、monthly（每月）")
    if "schedule_weekdays" in body:
        try:
            days = sorted({int(x) for x in str(body["schedule_weekdays"]).split(",") if x.strip()})
        except ValueError:
            days = []
        if not days or not all(1 <= d <= 7 for d in days):
            raise HTTPException(400, "每周至少选一天（1=周一 … 7=周日）")
        body["schedule_weekdays"] = ",".join(map(str, days))
    if "schedule_monthday" in body:
        try:
            mday = int(str(body["schedule_monthday"]).strip())
        except ValueError:
            mday = 0
        if not 1 <= mday <= 31:
            raise HTTPException(400, "每月的日期应为 1–31")
        body["schedule_monthday"] = str(mday)
    if "notify_scope" in body and str(body["notify_scope"]) not in ("all", "failure"):
        raise HTTPException(400, "通知范围只支持 all（全部结果）或 failure（仅失败与取消）")
    if "usb_autobackup" in body and str(body["usb_autobackup"]) not in ("true", "false"):
        raise HTTPException(400, "插线自动备份只能是 true（开）或 false（关）")
    if "log_verbose" in body and str(body["log_verbose"]) not in ("true", "false"):
        raise HTTPException(400, "日志详细度只能是 true（详细）或 false（精简）")
    for k, v in body.items():
        if k in allowed:
            store.set_setting(k, str(v))
    return {"ok": True, "settings": store.all_settings()}


@app.post("/api/settings/test-webhook")
async def test_webhook(body: dict | None = None):
    """发一条测试通知。

    请求体可带 {"url": "..."}：前端直接传输入框当前值，先测通再保存；
    不带则测已保存的地址。成功与否都如实返回。
    """
    url = ((body or {}).get("url") or store.get_setting("notify_webhook") or "").strip()
    if not url:
        raise HTTPException(400, "请先填写 Webhook 地址")
    try:
        await asyncio.get_running_loop().run_in_executor(None, notify.send_test, url)
    except Exception as e:  # noqa: BLE001
        log.warning("webhook 测试发送失败：%s", e)
        return {"ok": False, "error": str(e) or repr(e)}
    return {"ok": True}


# 定时备份失败后的补跑：当天最多试这么多次，两次之间至少隔这么久。
# 不设上限的话，磁盘满这类必然失败的原因会每 20 秒起一个任务、发一条失败通知。
SCHED_MAX_ATTEMPTS = 3
SCHED_RETRY_GAP = 1800
# 补跑窗口：设定时刻之后这么久内，设备上线就发起。以前是到午夜，但每次备份都要
# 在手机上输密码，23 点多才发起的那次多半没人看见，只会挂满等待上限再失败
SCHED_WINDOW = 3 * 3600


def _schedule_cfg() -> dict:
    """读出定时设置。值都在 PUT /api/settings 校验过，这里只做兜底解析。"""
    try:
        hh, mm = (store.get_setting("schedule_time") or "21:00").split(":")
        at = datetime.time(int(hh), int(mm))
    except ValueError:
        at = datetime.time(21, 0)
    days = {int(x) for x in (store.get_setting("schedule_weekdays") or "").split(",")
            if x.strip().isdigit() and 1 <= int(x) <= 7}
    try:
        mday = max(1, min(31, int(store.get_setting("schedule_monthday") or 1)))
    except ValueError:
        mday = 1
    return {"time": at, "freq": store.get_setting("schedule_freq") or "daily",
            "weekdays": days, "monthday": mday}


def _days_text(cfg: dict) -> str:
    """备份日描述：每天 / 每周六、日 / 每月 1 日。"""
    if cfg["freq"] == "weekly":
        return "每周" + "、".join("一二三四五六日"[d - 1] for d in sorted(cfg["weekdays"]))
    if cfg["freq"] == "monthly":
        return f"每月 {cfg['monthday']} 日"
    return "每天"


def _schedule_text(cfg: dict) -> str:
    """给日志看的计划描述：每天 21:00 / 每周六、日 21:00 / 每月 1 日 21:00。"""
    return f"{_days_text(cfg)} {cfg['time'].strftime('%H:%M')}"


def _runs_on(day: datetime.date, cfg: dict) -> bool:
    """这一天是不是计划日。每月 31 号这类设置在小月落到当月最后一天。"""
    if cfg["freq"] == "weekly":
        return day.isoweekday() in cfg["weekdays"]
    if cfg["freq"] == "monthly":
        return day.day == min(cfg["monthday"], calendar.monthrange(day.year, day.month)[1])
    return True


def _last_occurrence(now: datetime.datetime, cfg: dict) -> datetime.datetime | None:
    """不晚于 now 的最近一次计划时刻；只看今天和昨天（窗口 3 小时，最多跨过一次午夜）。"""
    for back in (0, 1):
        day = now.date() - datetime.timedelta(days=back)
        at = datetime.datetime.combine(day, cfg["time"])
        if at <= now and _runs_on(day, cfg):
            return at
    return None


def _sched_due(udid: str, since: int, now: float) -> bool:
    """这一轮定时备份对这台设备是否还欠着。

    看的是本轮计划时刻之后的备份记录（手动备份也算）：
      · 已有成功的          → 这一轮已完成
      · 有被取消的          → 用户主动叫停，不再自动重来
      · 有等手机超时的      → 多半没人输锁屏密码，再试只会再弹一次窗，不重来
      · 失败满 3 次          → 放弃这一轮，避免反复失败刷通知
      · 上次失败不到 30 分钟 → 等一等再补
    """
    tries = store.backup_attempts_since(udid, since)
    if any(t["status"] in ("COMPLETE", "CANCELED", "RUNNING") for t in tries):
        return False
    failed = [t for t in tries if t["status"] == "FAILED"]
    if any((t.get("error") or "").startswith(jobs.WAIT_TIMEOUT_PREFIX) for t in failed):
        return False
    if len(failed) >= SCHED_MAX_ATTEMPTS:
        return False
    if failed:
        last = failed[-1]["finished_at"] or failed[-1]["started_at"] or 0
        if now - last < SCHED_RETRY_GAP:
            return False
    return True


async def _scheduler_loop():
    """到了计划时刻（每天 / 每周几 / 每月几号），给这一轮还没成功备份过的在线设备跑一次。

    以前只在「当前分钟 == 设定时刻」那一下触发：到点时手机恰好不在线就整天不备份。
    现在计划时刻之后 SCHED_WINDOW 内都是补跑窗口，设备什么时候上线就什么时候补，
    失败按 _sched_due 的规则有限次重试。发起后手机会要求输锁屏密码，等待上限
    见 jobs.WAIT_TIMEOUT_SCHEDULE。
    """
    battery_logged: set[str] = set()      # 电量不足的跳过日志：每台设备每轮只记一次
    logged_round = 0
    while True:
        try:
            if store.get_setting("schedule_enabled") in ("1", "true", "True"):
                occ = _last_occurrence(datetime.datetime.now(), _schedule_cfg())
                since = int(occ.timestamp()) if occ else 0
                now = time.time()
                if since != logged_round:
                    logged_round, battery_logged = since, set()
                if occ and now - since < SCHED_WINDOW:
                    for d in store.list_devices():
                        udid = d["udid"]
                        # 手动备份 / 恢复 / 改加密正在进行时让路，下一轮再看
                        if udid not in _state["online_devices"] or jobs.busy_job(udid):
                            continue
                        if not _sched_due(udid, since, now):
                            continue
                        # 电量过低且没接电源：这一轮跳过（不写失败记录），充上电后自然补跑。
                        # 定时任务没人盯着，跑到一半没电最容易留下半截备份。
                        # max_age=None：复用未过期读数，被拦住的无线设备最多 2 分钟查一次；
                        # 插上线时连接方式变化会作废读数，下一轮（20 秒内）就能放行。
                        reason = await _battery_block_reason(udid, max_age=None)
                        if reason:
                            if udid not in battery_logged:
                                battery_logged.add(udid)
                                log.warning("定时备份暂缓 %s：%s", udid, reason)
                            continue
                        # 开跑前对这一台现探：online_devices 里的无线状态最多滞后 60 秒，
                        # 手机刚离开局域网就会被当成在线，白白耗掉一次失败重试。
                        # 必须放在电量检查之后：被电量拦住的设备每 20 秒都会走到上面，
                        # 放前面就等于每 20 秒强制探活一次，缓存形同虚设。
                        try:
                            await _refresh_transports(probe=udid)
                        except Exception as e:  # noqa: BLE001
                            log.warning("定时备份前刷新设备失败：%s", e)
                        if udid not in _state["online_devices"]:
                            continue
                        # 设备互斥仍由 jobs._claim 裁决，这里只接住 DeviceBusy
                        try:
                            jobs.start_backup(udid, job_id=uuid.uuid4().hex, trigger="schedule")
                            log.info("定时备份已触发: %s", udid)
                        except jobs.DeviceBusy:
                            pass
                    # 这里不发 webhook：任务结果（成功/失败/取消）由 notify
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
