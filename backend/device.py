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


async def list_devices() -> list[str]:
    """USB 已连接的设备 UDID 列表（-l 不含网络设备，-n 才是）。"""
    code, out = await _run([config.BIN_IDEVICE_ID, "-l"])
    if code != 0:
        return []
    return [ln.strip() for ln in out.splitlines() if ln.strip()]


async def device_info(udid: str) -> dict:
    code, out = await _run(
        [config.BIN_IDEVICE_INFO, "-u", udid, "-q", "com.apple.mobile.device"],
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
        code2, out2 = await _run([config.BIN_IDEVICE_INFO, "-u", udid], timeout=15)
        if code2 == 0:
            for ln in out2.splitlines():
                m = re.match(r"^([^:]+):\s*(.+)$", ln.strip())
                if m:
                    info.setdefault(m.group(1).strip(), m.group(2).strip())
    return info


async def is_paired(udid: str) -> bool:
    code, _ = await _run(
        [config.BIN_IDEVICEPAIR, "-u", udid, "validate"], timeout=15
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
        [config.BIN_IDEVICEPAIR, "-u", udid, "pair"], timeout=timeout
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
         它要求设备在线，离线时必然失败——这是协议限制，无法绕过。
      2. 本机侧：lockdown 目录下 <udid>.plist 是 NAS 持有的配对证书，必须无条件
         删除。只要它还在，NAS 就仍能免信任访问手机，且重新添加设备时会被
         validate 判定为「已授权」，用户永远等不到信任弹窗。
    """
    code, out = await _run([config.BIN_IDEVICEPAIR, "-u", udid, "unpair"], timeout=20)
    device_ok = code == 0
    purge = purge_local_pairing(udid)
    purged = purge["ok"]

    if device_ok:
        msg = "已解除配对：iPhone 上的信任记录与 NAS 的配对证书都已撤销，下次连接需要重新点「信任」。"
    else:
        reason = humanize_pair_error(out) or out.strip() or "设备无响应。"
        if purge["scan_error"]:
            head = "本机的配对证书目录读取失败，"
        elif purged:
            head = "已删除 NAS 上的配对证书，但"
        else:
            head = "本机没有该设备的配对证书文件，但"
        msg = (
            head + "设备没有收到撤销指令——" + reason +
            " iPhone 里仍保留对这台 NAS 的信任，下次插线添加设备时可能不再弹窗。"
            "要彻底撤销，请插上数据线后再取消一次，或在手机上依次进入"
            "「设置 > 通用 > 传输或还原 iPhone > 还原 > 还原位置与隐私」。"
        )
        log.info("unpair 未送达设备（%s），本机清理证书 %d 个", reason, purged)
    if purge["scan_error"]:
        msg += "（" + purge["scan_error"] + "；容器内以 root 运行才能清理证书）"
    elif purge["failed"]:
        msg += "（另有 " + str(len(purge["failed"])) + " 个证书文件删除失败：" + purge["failed"][0] + "）"
    return {"trust_revoked": device_ok, "purged": purged, "message": msg}
    return {"trust_revoked": device_ok, "purged": purged, "message": msg}


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
            udids = await list_devices()
            if udids:
                self.udid = udids[0]
                self.broadcast({"stage": "found", "udid": self.udid})
                break
            await asyncio.sleep(1.5)
        else:
            self.stage = "error"
            self.broadcast({
                "stage": "error",
                "message": "5 分钟内未检测到 iOS 设备。请确认：数据线插在 NAS 机身 USB 口、"
                           "手机已解锁、iOS 设备未处于「只充电」模式。",
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
