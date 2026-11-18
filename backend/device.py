"""libimobiledevice 工具链封装：设备发现 / 信息 / 配对。

所有 iOS 通信通过 idevice_* 命令行完成（Apple MobileSync 私有协议的开源实现）。
容器内由 usbmuxd（USB）与 netmuxd（Wi-Fi）提供设备 mux 通道。
"""
import asyncio
import logging
import os
import re

from . import config

log = logging.getLogger("ivault.device")


async def _run(args: list[str], timeout=30) -> tuple[int, str]:
    """执行一条 idevice 命令，返回 (returncode, 合并输出)。"""
    try:
        proc = await asyncio.create_subprocess_exec(
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            env={**os.environ, "IDEVICEPAIR_LOCKDOWN_DIR": config.LOCKDOWN_DIR},
        )
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        return proc.returncode, out.decode(errors="replace")
    except asyncio.TimeoutError:
        proc and proc.kill()
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


async def pair(udid: str, timeout=120) -> tuple[bool, str]:
    """发起配对。设备上弹出「信任此电脑」后命令返回。"""
    code, out = await _run(
        [config.BIN_IDEVICEPAIR, "-u", udid, "pair"], timeout=timeout
    )
    if code == 0:
        return True, out.strip() or "paired"
    if "already paired" in out.lower():
        return True, "already paired"
    return False, out.strip()


async def unpair(udid: str) -> bool:
    code, _ = await _run([config.BIN_IDEVICEPAIR, "-u", udid, "unpair"], timeout=15)
    return code == 0


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

    def broadcast(self, event: dict):
        event.setdefault("stage", self.stage)
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

    async def run(self):
        # 阶段 1：等待设备插线（最多 5 分钟）
        deadline = asyncio.get_event_loop().time() + 300
        while asyncio.get_event_loop().time() < deadline:
            udids = await list_devices()
            if udids:
                self.udid = udids[0]
                self.broadcast({"stage": "found", "udid": self.udid})
                break
            await asyncio.sleep(1.5)
        else:
            self.stage = "error"
            self.broadcast({"stage": "error", "message": "5 分钟内未检测到 USB 设备"})
            return

        self.stage = "pairing"
        self.broadcast({"stage": "pairing", "udid": self.udid,
                        "hint": "请在 iPhone 上点击「信任此电脑」"})
        # 阶段 2：pair 命令会阻塞直到用户点信任（或超时）
        ok, msg = await pair(self.udid, timeout=150)
        if not ok:
            self.stage = "error"
            self.broadcast({"stage": "error", "udid": self.udid, "message": msg})
            return
        # 阶段 3：读取设备信息入库
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
        })

    def start(self):
        self.task = asyncio.create_task(self.run())
