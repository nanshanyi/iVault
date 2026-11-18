"""libimobiledevice 工具链封装：设备发现 / 信息 / 配对。

所有 iOS 通信通过 idevice_* 命令行完成（Apple MobileSync 私有协议的开源实现）。
容器内由 usbmuxd 提供 USB 设备 mux 通道；Wi-Fi 通道（netmuxd/usbmuxd2）属于 v0.2。
"""
import asyncio
import logging
import re
from pathlib import Path

from . import config

log = logging.getLogger("ivault.device")


async def _run(args: list[str], timeout=30) -> tuple[int, str]:
    """执行一条 idevice 命令，返回 (returncode, 合并输出)。"""
    try:
        proc = await asyncio.create_subprocess_exec(
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            # 配对记录由 usbmuxd 的 --config-dir 决定（见 scripts/entrypoint.sh），
            # 不通过环境变量覆盖——libimobiledevice 工具链不识别这类变量。
        )
    except FileNotFoundError:
        # 裸机/本地开发未装工具链时给出可诊断信息，而不是让任务崩掉
        return 127, f"未找到可执行文件 {args[0]}，请确认 libimobiledevice 已安装"
    except Exception as e:  # noqa: BLE001
        return 126, f"启动 {args[0]} 失败：{e}"
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        return proc.returncode, out.decode(errors="replace")
    except asyncio.TimeoutError:
        try:
            proc.kill()
            await proc.wait()
        except Exception:  # noqa: BLE001
            pass
        return -1, f"timeout: {' '.join(args)}"


# ---------- 连接方式（USB / 无线） ----------
# 轮询时写入，命令层据此决定是否给 idevice_* 加 -n（走网络通道）。
# 同一个 UDID 同时插线又开着 Wi-Fi 时以 USB 为准：更快、更稳。
_transports: dict[str, str] = {}

TRANSPORT_USB = "usb"
TRANSPORT_NET = "network"


def set_transports(mapping: dict[str, str]) -> None:
    _transports.clear()
    _transports.update(mapping)


def get_transport(udid: str) -> str:
    return _transports.get(udid, TRANSPORT_USB)


def net_args(udid: str) -> list[str]:
    """网络设备必须显式 -n：否则工具会在 USB 总线上找不到它。"""
    return ["-n"] if get_transport(udid) == TRANSPORT_NET else []


async def scan_devices() -> list[dict]:
    """扫描 USB + 网络设备，返回 [{"udid", "connection"}]。

    -l 列出 USB 直连设备；-n 列出经 mDNS 发现、且已开启 Wi-Fi 同步的设备
    （需要 netmuxd 在跑，见 scripts/entrypoint.sh）。先扫 USB，重复的以 USB 为准。

    网络设备必须再探活一次：mDNS 会留下已关机 / 已离开局域网的失效条目，
    直接当成在线会出现「显示 Wi-Fi 在线但备份立刻 Mux error」的假状态。
    """
    usb: list[str] = []
    code, out = await _run([config.BIN_IDEVICE_ID, "-l"])
    if code == 0:
        usb = [ln.strip() for ln in out.splitlines() if ln.strip()]

    code, out = await _run([config.BIN_IDEVICE_ID, "-n"])
    net_candidates = [ln.strip() for ln in out.splitlines() if ln.strip()] if code == 0 else []
    net_candidates = [u for u in net_candidates if u not in usb]

    alive = await asyncio.gather(*(_net_alive(u) for u in net_candidates)) \
        if net_candidates else []

    result = [{"udid": u, "connection": TRANSPORT_USB} for u in usb]
    result += [{"udid": u, "connection": TRANSPORT_NET}
               for u, ok in zip(net_candidates, alive) if ok]
    return result


async def _net_alive(udid: str, timeout: float = 5.0) -> bool:
    """无线设备探活。

    用 validate 而不是 ideviceinfo：-q 查未知名域时 ideviceinfo 会打 WARNING 却
    依旧返回 0，拿它当探针会把睡着的设备误判成在线；validate 必须真正连上
    lockdownd 才会成功。顺带说明——能通过 validate 也就意味着这台主机已经
    配对过它，无线通道本来就只服务已配对设备。
    """
    code, _ = await _run([config.BIN_IDEVICEPAIR, "-n", "-u", udid, "validate"], timeout=timeout)
    return code == 0


async def list_devices() -> list[str]:
    """已连接设备的 UDID 列表（含 USB 与无线）。"""
    return [d["udid"] for d in await scan_devices()]


async def device_info(udid: str) -> dict:
    code, out = await _run(
        [config.BIN_IDEVICE_INFO, *net_args(udid), "-u", udid, "-q", "com.apple.mobile.device"],
        timeout=15,
    )
    info: dict[str, str] = {}
    if code == 0:
        for ln in out.splitlines():
            m = re.match(r"^([^:]+):\s*(.+)$", ln.strip())
            if m:
                info[m.group(1).strip()] = m.group(2).strip()
    # 全量查询兜底（个别固件对空域查询返回为空）
    if not info.get("DeviceName"):
        code2, out2 = await _run([config.BIN_IDEVICE_INFO, *net_args(udid), "-u", udid], timeout=15)
        if code2 == 0:
            for ln in out2.splitlines():
                m = re.match(r"^([^:]+):\s*(.+)$", ln.strip())
                if m:
                    info.setdefault(m.group(1).strip(), m.group(2).strip())
    return info


async def is_paired(udid: str) -> bool:
    code, _ = await _run(
        [config.BIN_IDEVICEPAIR, *net_args(udid), "-u", udid, "validate"], timeout=15
    )
    return code == 0


def _is_waiting_trust(text: str) -> bool:
    """判断配对失败是否应当继续重试。

    libimobiledevice 的 idevicepair pair 不会阻塞等用户点信任，未授权时立即返回，
    因此必须循环重试。这里采用「黑名单」策略：只有工具链缺失、权限不足这类
    真正无解的错误才终止，其余（未点信任/误点不信任/弹窗超时/lockdownd 未就绪）
    都继续等待——因为配对本质是需要人工介入的过程，重试成本极低。
    """
    t = text.lower()
    hard = (
        "command not found",
        "no such file or directory",
        "permission denied",
        "operation not permitted",
    )
    return not any(k in t for k in hard)


def _is_denied(text: str) -> bool:
    """是否已被设备端明确拒绝。

    这种状态下重试无效——iOS 会记住拒绝、不再弹窗，必须重新插拔数据线。
    """
    t = text.lower()
    return any(k in t for k in (
        "denied the trust dialog",
        "user did not allow trust",
        "user denied",
    ))


def humanize_pair_error(text: str) -> str:
    """把 libimobiledevice 的英文错误转成可操作的中文提示。"""
    t = text.lower()
    table = (
        ("denied the trust dialog", "手机端拒绝了信任请求。请重新插线，在看到「要信任此电脑吗」时点「信任」并输入锁屏密码。"),
        ("please accept the trust dialog", "手机上还有未确认的信任弹窗。请解锁手机，点「信任」并输入锁屏密码。"),
        ("user did not allow trust", "手机端拒绝了信任。请重新插线，在弹窗中点「信任」。"),
        ("pairing dialog response pending", "信任弹窗还在等待确认，请在手机上点「信任」。"),
        ("could not connect to lockdownd", "无法连接设备服务（lockdownd）。请解锁手机、重新插拔数据线后重试。"),
        ("device is locked", "设备处于锁屏状态。请解锁 iPhone 后重试。"),
        ("invalid hostid", "配对记录无效，请重新插拔数据线再试一次。"),
        ("device not found", "设备已断开。请确认数据线仍连接在 NAS 的 USB 口上。"),
        ("no device found", "设备未连接，撤销信任需要 iPhone 在线。"),
    )
    for key, zh in table:
        if key in t:
            return zh
    return text.strip()


async def pair(udid: str, timeout=60) -> tuple[bool, str, bool]:
    """发起一次配对尝试。

    返回 (是否成功, 输出信息, 是否属于可重试的「等待信任」状态)。
    """
    code, out = await _run(
        [config.BIN_IDEVICEPAIR, *net_args(udid), "-u", udid, "pair"], timeout=timeout
    )
    text = out.strip()
    if code == 0:
        return True, text or "paired", False
    low = text.lower()
    if "already paired" in low:
        return True, "already paired", False
    if code in (126, 127):
        # 工具链缺失 / 无法启动：重试没有意义
        return False, text or f"idevicepair 无法执行（退出码 {code}）", False
    return False, text or f"idevicepair 退出码 {code}", _is_waiting_trust(text)


def _lockdown_files(udid: str) -> tuple[list[Path], str | None]:
    """本机 lockdown 目录下属于该设备的配对记录文件。

    返回 (文件列表, 扫描失败原因)。macOS 的 /var/db/lockdown 属主是 _usbmuxd
    且组/其他位没有 r，普通用户 iterdir 会抛 EACCES——必须区分「没有证书」和
    「读不到目录」，否则会把没清理成功误报成清理成功。
    """
    d = Path(config.LOCKDOWN_DIR)
    if not d.is_dir():
        return [], None
    try:
        entries = list(d.iterdir())
    except OSError as e:  # noqa: BLE001
        return [], f"无法读取 {d}（{e.strerror or e}）"
    keys = {udid.lower(), udid.replace("-", "").lower()}
    return [
        p for p in entries
        if p.is_file() and p.name.split(".")[0].lower() in keys
    ], None


def purge_local_pairing(udid: str) -> dict:
    """删除 NAS 侧该设备的配对证书。

    返回 {"ok": 成功数, "failed": [失败说明], "scan_error": 目录不可读的原因}。
    不删 SystemConfiguration.plist（那是本机的主机身份，删了会让所有设备失效）。
    """
    files, scan_error = _lockdown_files(udid)
    ok, failed = 0, []
    for p in files:
        try:
            p.unlink()
            ok += 1
            log.info("已删除配对证书 %s", p)
        except OSError as e:  # noqa: BLE001
            failed.append(f"{p.name}: {e.strerror or e}")
            log.warning("删除配对证书失败 %s: %s", p, e)
    return {"ok": ok, "failed": failed, "scan_error": scan_error}


async def unpair(udid: str) -> dict:
    """解除与 iPhone 的配对关系，返回 {"trust_revoked", "purged", "message"}。

    这件事有两侧，必须分别处理：
      1. 设备侧：idevicepair unpair 通过 lockdownd 让 iPhone 删掉对本机的信任。
         它要求设备在线且已解锁，离线/锁屏时无法生效。
      2. 本机侧：lockdown 目录下 <udid>.plist 是 NAS 持有的配对证书，只要它还在，
         NAS 就仍能免信任访问手机，且重新添加设备时会被 validate 判定为「已授权」。

    关键教训：**不能相信 idevicepair unpair 的退出码**。实测它会在什么都没删掉的
    情况下打印 "SUCCESS: Unpaired with device"，退出码 0（例如手机锁屏、或证书文件
    所在目录对当前用户不可写）。唯一可信的判据是再问一次设备：validate 还能不能过。
    """
    online = udid in await list_devices()
    claimed, out = False, ""
    purged, scan_error, failed = 0, None, []
    still_paired, tool_broken = False, False

    # 设备在线时最多试两轮：第一轮遇到锁屏/瞬时状态失败，第二轮通常能过
    for attempt in range(2 if online else 1):
        code, out = await _run([config.BIN_IDEVICEPAIR, *net_args(udid), "-u", udid, "unpair"], timeout=20)
        claimed = claimed or code == 0
        tool_broken = tool_broken or code in (126, 127)   # 工具链缺失，重试无意义
        purge = purge_local_pairing(udid)                 # 每轮都再试一次删证书
        purged, scan_error, failed = purge["ok"], purge["scan_error"], purge["failed"]
        still_paired = await is_paired(udid) if online and not tool_broken else False
        if not still_paired:
            break
        if attempt == 0:
            await asyncio.sleep(0.8)

    revoked = online and claimed and not still_paired and not tool_broken

    if revoked:
        msg = "已解除配对：iPhone 上的信任记录与 NAS 的配对证书都已撤销，下次连接需要重新点「信任」。"
    elif online and still_paired and not tool_broken:
        # unpair 声称成功、但设备仍认可本机 → 撤销并未真正发生
        msg = (
            "撤销未生效：iPhone 仍保持着对这台 NAS 的信任（unpair 报告成功，但 validate 依然通过）。"
            "常见原因是手机处于锁屏，或 NAS 无权删除本机证书。请解锁 iPhone 后重试；"
            "若仍无效，请在手机上依次进入「设置 > 通用 > 传输或还原 iPhone > 还原 > 还原位置与隐私」，"
            "清除后重新连接会正常弹出信任框。"
        )
        log.warning("unpair 假成功：设备 %s 仍 validate 通过（purged=%d）", udid, purged)
    else:
        reason = humanize_pair_error(out) or out.strip() or "设备无响应。"
        if scan_error:
            head = "本机的配对证书目录读取失败，"
        elif purged:
            head = "已删除 NAS 上的配对证书，但"
        else:
            head = "本机没有该设备的配对证书文件，但"
        msg = (
            head + "设备没有收到撤销指令——" + reason +
            " iPhone 里仍保留对这台 NAS 的信任，下次插线添加设备时可能不再弹窗。"
            "要彻底撤销，请插上数据线、解锁手机后再取消一次，或在手机上依次进入"
            "「设置 > 通用 > 传输或还原 iPhone > 还原 > 还原位置与隐私」。"
        )
        log.info("unpair 未送达设备（%s），本机清理证书 %d 个", reason, purged)

    if scan_error:                        # 撤销已生效时证书残留也要说清楚
        msg += "（" + scan_error + "；容器内以 root 运行才能清理证书）"
    elif failed:
        msg += "（另有 " + str(len(failed)) + " 个证书文件删除失败：" + failed[0] + "）"
    return {"trust_revoked": revoked, "purged": purged, "online": online, "message": msg}


# ---------- 配对向导会话 ----------

class PairSession:
    """一个配对向导实例：轮询 USB 总线 -> 检测设备 -> 等待信任 -> 配对完成。

    订阅者通过 subscribe() 拿到 asyncio.Queue，事件为 dict：
      {"stage": "waiting" | "found" | "pairing" | "done" | "error", ...}
    """

    def __init__(self, sid: str):
        self.sid = sid
        self.subscribers: list[asyncio.Queue] = []
        self.stage = "waiting"
        self.udid: str | None = None
        self.info: dict = {}
        self.task: asyncio.Task | None = None
        self.last_event: dict | None = None

    def broadcast(self, event: dict):
        event.setdefault("stage", self.stage)
        self.last_event = event          # 供后加入的订阅者补发完整快照
        log.info("pair[%s] %s", self.sid[:8], event)
        for q in list(self.subscribers):
            try:
                q.put_nowait(event)
            except Exception:
                pass

    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=500)
        self.subscribers.append(q)
        return q

    def unsubscribe(self, q: asyncio.Queue):
        if q in self.subscribers:
            self.subscribers.remove(q)

    async def _finish_done(self, already: bool):
        """读取设备信息入库并宣告完成。"""
        self.info = await device_info(self.udid)
        from . import store
        store.upsert_device(
            self.udid,
            name=self.info.get("DeviceName", self.udid[-8:]),
            model=self.info.get("ProductType", ""),
            ios_version=self.info.get("ProductVersion", ""),
        )
        self.stage = "done"
        self.broadcast({
            "stage": "done",
            "udid": self.udid,
            "name": self.info.get("DeviceName", ""),
            "model": self.info.get("ProductType", ""),
            "ios_version": self.info.get("ProductVersion", ""),
            "already_paired": already,
        })

    async def run(self):
        loop = asyncio.get_event_loop()
        # 阶段 1：等待设备插线（最多 5 分钟）
        deadline = loop.time() + 300
        while loop.time() < deadline:
            # 只挑 USB 设备：iPhone 的无线配对不被 idevicepair 支持
            # （官方 NOTE：network pairing 仅 Apple TV），无线只能备份已配对设备。
            usb = [d["udid"] for d in await scan_devices() if d["connection"] == TRANSPORT_USB]
            if usb:
                self.udid = usb[0]
                self.broadcast({"stage": "found", "udid": self.udid})
                break
            await asyncio.sleep(1.5)
        else:
            self.stage = "error"
            self.broadcast({
                "stage": "error",
                "message": "5 分钟内未检测到 USB 连接的 iOS 设备。请确认：数据线插在 NAS 机身 USB 口、"
                           "手机已解锁、iOS 设备未处于「只充电」模式。"
                           "（首次配对必须用数据线：iPhone 不支持无线配对，无线只用于已配对设备的备份。）",
            })
            return

        # 阶段 2：已配对的设备直接完成——不需要也没有信任弹窗可点
        if await is_paired(self.udid):
            self.stage = "pairing"
            self.broadcast({"stage": "pairing", "udid": self.udid,
                            "hint": "该设备已授权过，正在读取设备信息…"})
            await self._finish_done(already=True)
            return

        # 阶段 3：等待用户在手机上点「信任」
        # idevicepair pair 不会阻塞等待，未授权时立即返回，因此循环重试。
        #
        # 关键细节：一旦用户在弹窗上点了「不信任」或超时未输密码，iOS 会记住
        # 「该主机已被拒绝」，之后重试会被直接拒绝且不再弹窗。此时唯一有效的
        # 办法是重新插拔数据线（设备重连会重置拒绝状态）。所以这里同时监听
        # 设备是否掉线，检测到重新插入就立即重试并重置计数。
        deadline = loop.time() + 300
        attempt = 0
        last_msg = ""
        denied_hits = 0
        plugged = True
        self.stage = "pairing"
        self.broadcast({"stage": "pairing", "udid": self.udid, "attempt": 0,
                        "waiting_trust": True,
                        "hint": "请在 iPhone 上点击「信任此电脑」"})
        while loop.time() < deadline:
            # 先确认设备还在线上：拔线是用户看到「不弹窗」后最自然的动作
            udids = await list_devices()
            if self.udid not in udids:
                if plugged:
                    plugged = False
                    self.broadcast({
                        "stage": "pairing", "udid": self.udid,
                        "waiting_trust": True, "unplugged": True,
                        "remaining": int(deadline - loop.time()),
                        "hint": "设备已断开，等待重新插入",
                        "message": "检测到数据线已拔出。请把 iPhone 重新插回 NAS 的 USB 口，"
                                   "保持屏幕解锁，插上后会自动继续配对。",
                    })
                await asyncio.sleep(1.5)
                continue
            if not plugged:
                plugged = True
                attempt = 0
                denied_hits = 0
                self.broadcast({
                    "stage": "pairing", "udid": self.udid,
                    "waiting_trust": True, "replugged": True,
                    "remaining": int(deadline - loop.time()),
                    "hint": "已重新连接，正在请求信任弹窗…",
                    "message": "设备已重新连接，已重新请求配对。请留意手机上的「要信任此电脑吗」弹窗。",
                })

            ok, msg, retryable = await pair(self.udid, timeout=30)
            if ok:
                await self._finish_done(already=False)
                return
            last_msg = msg
            if not retryable:
                self.stage = "error"
                self.broadcast({"stage": "error", "udid": self.udid,
                                "message": humanize_pair_error(msg), "retryable": False})
                return
            attempt += 1
            denied = _is_denied(msg)
            if denied:
                denied_hits += 1
            self.broadcast({
                "stage": "pairing", "udid": self.udid, "attempt": attempt,
                "waiting_trust": True,
                "denied": denied,
                "remaining": int(deadline - loop.time()),
                "hint": ("手机端此前拒绝了信任，请拔掉数据线重新插入" if denied
                         else "仍在等待 iPhone 上的「信任」确认"),
                "message": (
                    "手机上没有出现弹窗？这是因为上一次点过「不信任」或密码框超时，"
                    "iOS 记住了这次拒绝，之后不会再弹。请拔掉数据线，重新插一次，"
                    "看到「要信任此电脑吗」时点「信任」并输入锁屏密码。"
                    if denied else ""),
            })
            await asyncio.sleep(2.5)

        self.stage = "error"
        self.broadcast({
            "stage": "error", "udid": self.udid, "retryable": True,
            "message": "5 分钟内未完成配对。请解锁 iPhone、保持数据线连接；"
                       "若手机没有弹窗，请拔掉数据线重新插入后重试。"
                       + (f"（最后错误：{humanize_pair_error(last_msg)}）" if last_msg else ""),
        })

    def start(self):
        self.task = asyncio.create_task(self.run())

    def cancel(self):
        """终止轮询任务。配对是设备级的独占操作，同一时间只应有一个会话。"""
        if self.task and not self.task.done():
            self.task.cancel()
