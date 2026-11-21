#!/usr/bin/env python3
"""iVault USB 热插拔看护。

为什么必须重启 usbmuxd（查证过，不是猜测）：
    镜像里 usbmuxd 的源码列表只有 client/device/usb/main 等，没有 udev.c，
    热插拔完全依赖 libusb 的 libusb_hotplug_register_callback。libusb 在 Linux
    上通过 udev monitor 订阅的是 udevd 的转发组，而容器里没有 udevd，宿主机
    udevd 也不会跨 netns 转发 —— 所以容器内 usbmuxd 只保留启动瞬间枚举到的
    设备表，之后插拔不可见，也没有 rescan 接口。唯一办法是重启进程让它重扫。
    （切 host 网络同样无效，已实测：uevent 的阻塞点不在 netns。）

为什么用 inotify 而不是轮询：
    /dev/bus/usb 是 bind mount，inotify 对挂载点内的节点增删照样有效，是内核
    级通知：毫秒级响应、不 fork 任何进程、CPU 占用为零。inotify 不可用时自动
    回落到 3 秒轮询，行为与轮询版一致。

部署前提（踩过坑，务必注意）：
    /dev/bus/usb 必须用 volumes 做 bind mount，**不能用 devices:**。
    devices: 只是容器创建时的设备节点快照，拔插后宿主机新建的节点
    （例如 /dev/bus/usb/003/008）在容器内根本不存在 —— 表现就是本看护正确
    触发、usbmuxd 正确重启，却一直报 errno=2 / LIBUSB_ERROR_NO_DEVICE。

安全边界：
    - 只对「创建」事件动作，拔线、插 U 盘都不触发
    - 必须确认「sysfs 里有苹果设备且 /dev 节点就绪 > usbmuxd 列出数」，
      持续 0.8 秒才算真盲区
    - 有备份任务在跑绝不重启
    - 重启做冷却：正常 10 秒，10 分钟内超过 5 次拉到 300 秒
"""

import ctypes
import ctypes.util
import json
import os
import select
import shutil
import subprocess
import sys
import time
import urllib.request

USBMUXD = "/opt/usbmuxd-stock/sbin/usbmuxd"
USBMUXD_SOCKET = "/var/run/usbmuxd"
LOCKDOWN = "/var/lib/lockdown"
WIFI_SOCKET = "/var/run/usbmuxd-net"
PORT = os.environ.get("IVAULT_PORT", "8666")
WIFI_ENABLED = os.environ.get("IVAULT_WIFI", "1") != "0"
VERBOSE = os.environ.get("IVAULT_USBMUXD_VERBOSE", "0") == "1"

USB_ROOT = "/dev/bus/usb"
SYS_USB = "/sys/bus/usb/devices"
POLL_FALLBACK = 3.0      # inotify 不可用时的轮询间隔
EVENT_SETTLE = 0.8       # 事件到达后等设备节点稳定
BLIND_HOLD = 0.8         # 盲区状态需要持续的时长
COOLDOWN = 10.0
COOLDOWN_LONG = 300.0

IN_CREATE = 0x00000100
IN_DELETE = 0x00000200
IN_MOVED_TO = 0x00000080


def log(*args):
    print("[usb-watchdog]", *args, flush=True)


def _devnode(name):
    """返回该 USB 设备对应的 /dev/bus/usb/BBB/DDD 路径，读不到返回 None。"""
    base = os.path.join(SYS_USB, name)
    try:
        with open(os.path.join(base, "busnum")) as f:
            busnum = int(f.read().strip())
        with open(os.path.join(base, "devnum")) as f:
            devnum = int(f.read().strip())
    except (OSError, ValueError):
        return None
    return os.path.join(USB_ROOT, "%03d" % busnum, "%03d" % devnum)


_missing_logged = {}


def apple_usb_count():
    """总线上「/dev 节点已就绪」的苹果设备数量。

    为什么要检查节点：插入瞬间内核先挂出 sysfs 条目，udev 稍后才 mknod 出
    /dev/bus/usb/BBB/DDD。此时若重启 usbmuxd，它枚举到该设备却打不开节点，
    只会报 ENOENT(errno=2) → LIBUSB_ERROR_NO_DEVICE，白重启一次还要多等一个
    冷却周期。所以只统计节点真实存在的设备，天然避开这个竞态。
    """
    n = 0
    try:
        names = os.listdir(SYS_USB)
    except OSError:
        return 0
    for name in names:
        base = os.path.join(SYS_USB, name)
        try:
            with open(os.path.join(base, "idVendor")) as f:
                if f.read().strip() != "05ac":
                    continue
            with open(os.path.join(base, "product")) as f:
                product = f.read().strip()
        except OSError:
            continue
        if not product.startswith(("iPhone", "iPad", "iPod")):
            continue
        node = _devnode(name)
        if node and os.path.exists(node):
            n += 1
        else:
            now = time.time()
            if now - _missing_logged.get(name, 0) > 15:
                _missing_logged[name] = time.time()
                log("苹果设备 %s 已出现在 sysfs，但 /dev 节点还未就绪，暂不重启" % name)
    return n


def client_env(sock):
    """构造指向指定 socket 的客户端环境变量。

    libusbmuxd 的 connect_usbmuxd_socket() 只认 "UNIX:<路径>"（大写，strncmp
    比较）或 "<host>:<port>"。裸路径会静默回落到 /var/run/usbmuxd，且不报错
    —— 早期版本的自检就是栽在这里：以为在查 netmuxd，实际一直查的是原生
    usbmuxd，结果永远是空，看起来像「netmuxd 没上报设备」。
    """
    return dict(os.environ, USBMUXD_SOCKET_ADDRESS="UNIX:" + sock)


def listed_count():
    """usbmuxd 实际列出的设备数。

    显式指定 usbmuxd 的 socket：本进程可能继承 entrypoint 设置的
    USBMUXD_SOCKET_ADDRESS（指向 netmuxd），那样统计会把 Wi-Fi 设备也算进来，
    「总线数 > 列出数」的判断就失真了。这里要的始终是 USB 通道的裸视图。
    """
    env = client_env(USBMUXD_SOCKET)
    try:
        out = subprocess.run(["idevice_id", "-l"], stdout=subprocess.PIPE,
                             stderr=subprocess.DEVNULL, timeout=4, env=env).stdout
    except (OSError, subprocess.TimeoutExpired):
        return 0
    return sum(1 for line in out.decode("utf-8", "replace").split() if line.strip())


def jobs_running():
    """True=有备份任务, False=空闲, None=状态查不到（保守按忙处理）。"""
    try:
        with urllib.request.urlopen("http://127.0.0.1:%s/api/status" % PORT, timeout=2) as r:
            data = json.load(r)
    except Exception:
        return None
    return bool(data.get("active_jobs"))


def pgrep(name):
    return subprocess.run(["pgrep", "-x", name],
                          stdout=subprocess.DEVNULL,
                          stderr=subprocess.DEVNULL).returncode == 0


def start_usbmuxd():
    args = [USBMUXD, "--foreground", "--user", "root", "--config-dir", LOCKDOWN]
    if VERBOSE:
        args.append("-v")
    # 用 sh -c 把 stderr 也接进 stdout 并加前缀，与 entrypoint 的输出风格一致
    cmd = "exec " + " ".join(args) + " 2>&1 | sed -u 's/^/[usbmuxd] /'"
    subprocess.Popen(["/bin/sh", "-c", cmd])
    log("usbmuxd 已启动")


def start_netmuxd():
    if not shutil.which("netmuxd"):
        return
    try:
        os.unlink(WIFI_SOCKET)
    except OSError:
        pass
    env = dict(os.environ, RUST_LOG=os.environ.get("IVAULT_WIFI_LOG", "warn"))
    # netmuxd 自己也读 USBMUXD_SOCKET_ADDRESS 当上游地址（src/add_device.rs）。
    # 本进程继承的是 "UNIX:<netmuxd 自己的 socket>"，不剔除会让它把上游指向
    # 自己，形成自环 —— 表现为 netmuxd 活着、USB 列表也拿不到。上游一律由
    # --upstream-usbmuxd 显式指定。
    env.pop("USBMUXD_SOCKET_ADDRESS", None)
    cmd = ("exec netmuxd --socket-path %s --upstream-usbmuxd %s "
           "2>&1 | sed -u 's/^/[netmuxd] /'" % (WIFI_SOCKET, USBMUXD_SOCKET))
    subprocess.Popen(["/bin/sh", "-c", cmd], env=env)
    log("netmuxd 已拉起")


def restart_netmuxd():
    subprocess.run(["pkill", "-x", "netmuxd"], stdout=subprocess.DEVNULL,
                   stderr=subprocess.DEVNULL)
    for _ in range(20):
        if not pgrep("netmuxd"):
            break
        time.sleep(0.1)
    start_netmuxd()


def restart_daemon():
    """优雅退出优先：usbmuxd -x 会让已有实例自己退出，socket 清理更干净。"""
    log("USB 总线上有苹果设备但 usbmuxd 没列出，重启守护进程重新扫描")
    subprocess.run([USBMUXD, "-x"], stdout=subprocess.DEVNULL,
                   stderr=subprocess.DEVNULL, timeout=5)
    for _ in range(20):
        if not pgrep("usbmuxd"):
            break
        time.sleep(0.15)
    else:
        subprocess.run(["pkill", "-x", "usbmuxd"], stdout=subprocess.DEVNULL,
                       stderr=subprocess.DEVNULL)
        time.sleep(0.5)

    start_usbmuxd()

    for _ in range(20):
        if os.path.exists(USBMUXD_SOCKET):
            break
        time.sleep(0.15)

    if WIFI_ENABLED:
        time.sleep(0.5)
        # 必须无条件重启 netmuxd：它的 upstream 是指向 /var/run/usbmuxd 的长连接，
        # usbmuxd 一重启这条连接就失效了；而应用侧的 USBMUXD_SOCKET_ADDRESS 指向
        # 的正是 netmuxd 的 socket —— netmuxd 是 Web 服务唯一的设备通道。
        # 只判断「进程还活着」就跳过，会留下一个活着但不转发设备的 netmuxd，
        # 表现为「USB 能认、Wi-Fi 在广播却永远离线」。
        log("重启 netmuxd 以重建 upstream 连接")
        restart_netmuxd()


def wifi_health_check():
    """netmuxd 掉了就拉起。它承载 Wi-Fi 通道，进程没了无线设备必然全掉。"""
    if not WIFI_ENABLED or pgrep("netmuxd"):
        return
    log("netmuxd 已不在运行，重新拉起（Wi-Fi 通道）")
    restart_netmuxd()


class Inotify:
    """最小 inotify 封装：只订阅 /dev/bus/usb 下的节点创建/删除。"""

    def __init__(self):
        libc_name = ctypes.util.find_library("c") or "libc.so.6"
        self.libc = ctypes.CDLL(libc_name, use_errno=True)
        self.fd = self.libc.inotify_init()
        if self.fd < 0:
            raise OSError(ctypes.get_errno(), "inotify_init failed")
        self.wds = {}

    def watch_tree(self):
        """给根目录和每条总线目录加 watch（重复 add 是幂等的，不会泄漏 wd）。"""
        paths = [USB_ROOT]
        try:
            for name in os.listdir(USB_ROOT):
                full = os.path.join(USB_ROOT, name)
                if os.path.isdir(full):
                    paths.append(full)
        except OSError:
            pass
        for p in paths:
            wd = self.libc.inotify_add_watch(self.fd, p.encode(),
                                             IN_CREATE | IN_DELETE | IN_MOVED_TO)
            if wd >= 0:
                self.wds[wd] = p

    def wait(self, timeout):
        r, _, _ = select.select([self.fd], [], [], timeout)
        if not r:
            return []
        try:
            buf = os.read(self.fd, 65536)
        except OSError:
            return []
        events = []
        i = 0
        while i + 16 <= len(buf):
            wd = int.from_bytes(buf[i:i + 4], "little")
            mask = int.from_bytes(buf[i + 4:i + 8], "little")
            length = int.from_bytes(buf[i + 12:i + 16], "little")
            name = buf[i + 16:i + 16 + length].rstrip(b"\0").decode("utf-8", "replace")
            if mask & (IN_CREATE | IN_MOVED_TO) and name:
                events.append((self.wds.get(wd, USB_ROOT), name))
            i += 16 + length
        return events


def wait_for_socket(timeout=30.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if os.path.exists(USBMUXD_SOCKET):
            return True
        time.sleep(0.25)
    return False


def self_diagnose():
    """一次性自检（IVAULT_USB_DIAG=1 时启用），复现应用侧看到的世界。

    应用是通过 USBMUXD_SOCKET_ADDRESS=<netmuxd socket> 间接看到设备表的，
    这里用同样的环境变量跑一遍并打进日志，排查「USB 能认但无线离线」时有用。
    """
    if os.environ.get("IVAULT_USB_DIAG") != "1":
        return
    try:
        udids = sorted(n[:-6] for n in os.listdir(LOCKDOWN) if n.endswith(".plist"))
    except OSError:
        udids = []
    env = client_env(WIFI_SOCKET)
    log("自检：socket=%r 配对记录=%r" % (env["USBMUXD_SOCKET_ADDRESS"], udids))
    for args in (["idevice_id", "-l"], ["idevice_id", "-n"]):
        try:
            r = subprocess.run(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                               timeout=8, env=env)
            log("自检 %s → rc=%d out=%r err=%r" % (
                " ".join(args), r.returncode,
                r.stdout.decode("utf-8", "replace").strip(),
                r.stderr.decode("utf-8", "replace").strip()[:300]))
        except Exception as e:
            log("自检 %s → 异常 %r" % (" ".join(args), e))
    for udid in udids:
        if udid == "SystemConfiguration":
            continue
        try:
            r = subprocess.run(["idevicepair", "-n", "-u", udid, "validate"],
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                               timeout=10, env=env)
            log("自检 validate %s → rc=%d out=%r err=%r" % (
                udid, r.returncode,
                r.stdout.decode("utf-8", "replace").strip(),
                r.stderr.decode("utf-8", "replace").strip()[:300]))
        except Exception as e:
            log("自检 validate %s → 异常 %r" % (udid, e))


def main():
    if not wait_for_socket():
        log("等待 %s 超时，仍然启动看护" % USBMUXD_SOCKET)

    ino = None
    try:
        ino = Inotify()
        ino.watch_tree()
        log("看护已启动：inotify 事件驱动（不可用时回落 3s 轮询）")
    except Exception as e:
        # 符号缺失会抛 AttributeError，不止 OSError，这里统一兜住
        log("inotify 不可用（%r），回落 3s 轮询" % (e,))

    blind_since = None
    last_restart = 0.0
    restarts = []
    # 「查不到任务状态」每分钟只记一次：盲区持续时循环是 3 秒一轮，
    # 原样打印会把容器日志刷满，反而盖住真正有用的行
    unknown_logged = 0.0
    # 启动宽限：entrypoint 在拉起 netmuxd 之后才启动本看护，这之前不做健康检查
    started_at = time.time()
    last_wifi_check = started_at
    diag_done = False

    while True:
        now = time.time()
        if now - started_at > 20 and now - last_wifi_check > 10:
            last_wifi_check = now
            wifi_health_check()

        if not diag_done and now - started_at > 25:
            diag_done = True
            self_diagnose()

        got_create = False
        if ino is not None:
            try:
                events = ino.wait(POLL_FALLBACK)
                if events:
                    got_create = True
                    ino.watch_tree()          # 可能是新总线目录出现
            except Exception:
                ino = None
                log("inotify 读取失败，之后按 3s 轮询")
        else:
            time.sleep(POLL_FALLBACK)

        if got_create:
            time.sleep(EVENT_SETTLE)          # 等设备节点和 sysfs 稳定

        bus = apple_usb_count()
        listed = listed_count()
        now = time.time()

        if bus <= listed:
            blind_since = None
            continue

        if blind_since is None:
            blind_since = now

        # 事件路径必须在这里补足 BLIND_HOLD 后立刻确认：否则要等下一轮 3 秒
        # 超时才复查，事件驱动的提速就白做了（实测延迟会回到 3.8 秒）。
        if got_create and now - blind_since < BLIND_HOLD:
            time.sleep(BLIND_HOLD - (now - blind_since))
            bus = apple_usb_count()
            listed = listed_count()
            now = time.time()
            if bus <= listed:
                blind_since = None
                continue

        if now - blind_since < BLIND_HOLD:
            continue

        recent = [t for t in restarts if now - t < 600]
        cooldown = COOLDOWN_LONG if len(recent) >= 5 else COOLDOWN
        if now - last_restart < cooldown:
            continue

        busy = jobs_running()
        if busy is None:
            if now - unknown_logged > 60:
                unknown_logged = now
                log("无法确认任务状态，暂缓重启（下一轮再判断）")
            continue
        if busy:
            log("检测到未识别的设备，但有备份任务在跑，暂缓重启")
            continue

        restart_daemon()
        last_restart = time.time()
        restarts.append(last_restart)
        blind_since = None


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(0)
    except Exception as e:
        log("看护异常退出：%r" % e)
        sys.exit(1)
