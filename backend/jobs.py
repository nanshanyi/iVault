"""备份任务引擎：idevicebackup2 子进程管理 + SSE 进度事件。

idevicebackup2 的输出里包含 `Total progress: N`（0-100 整数）逐行刷出，
同时有各 Domain 的传输日志。JobRunner 逐行解析并推送给 SSE 订阅者。

几条贯穿全文件的约定（都是踩过坑之后定下的）：

- 设备级互斥只在 `_claim()` 一处判断：HTTP 端点、定时调度、恢复任务都必须
  先过注册表，否则同一台设备上会并出两个进程同时写同一个备份目录。
- 子进程一旦起来，任何退出路径都要回收（`_pump()` 的 finally + `_fail()`），
  否则任务对象被 done_callback 摘掉了，进程还在后台跑。
- 备份记录只能收尾一次（`_close_backup()`），异常路径与正常路径不会互相覆盖。
"""
import asyncio
import collections
import datetime
import logging
import os
import plistlib
import re
import time

from . import config, device, notify, store

log = logging.getLogger("ivault.jobs")

# idevicebackup2 的进度是 `\r[====   ] 45.2%` 这种「回车覆盖同一行」的写法，
# 整轮只在换阶段时才补一个 \n。按行读取会把几分钟的进度憋成一条巨型字符串
# （前端一次性插入几 MB HTML 会直接卡死浏览器，超长行还会让 readline 抛
# ValueError 把任务拽出循环）。所以这里按 \r 和 \n 一起切分。
#
# 输出里同时有两种百分比，必须区分：
#   Backup     [.....................]   3%                          ← 整机进度
#              [============>        ]  50.9%   13.1 MB / 25.6 MB   ← 单个文件进度
# 取「最后一个百分比」会拿到文件进度，第一个文件传完就把整机进度顶到 100%。
OVERALL_RE = re.compile(r"Backup\s*\[[^\]]*\]\s*(\d{1,3})\s*%")
TOTAL_RE = re.compile(r"Total progress:\s*(\d{1,3})")
# 单文件进度条：`[>       ]   0.0%   832 Bytes / 2.2 MB`
# 单位有 Bytes / Byte / KB / KiB / MB 几种，早期只写了 [KMGT]i?B，
# 漏掉 "Bytes" —— 于是所有小于 1KB 的文件行都漏过过滤（真实输出里绝大多数
# 小文件都是这种），精简档被灌成详细档，3.5 万个文件就是 3.5 万行。
FILE_PROG_RE = re.compile(r"\d{1,3}(?:\.\d+)?%\s+[\d.]+\s*(?:[KMGT]i?)?B(?:ytes?)?\s*/", re.I)
# 只有单文件进度条和百分比、没有字节数的行：`[>         ]   0.0%`。工具在还没开始传文件时
# 重画进度就是这样（真机每次任务都会漏一行进日志），也算文件级噪声。
# 整机进度那行开头有「Backup」，先被 OVERALL_RE 截走，不会落到这里
BARE_BAR_RE = re.compile(r"^\[[^\]]*\]\s*\d{1,3}(?:\.\d+)?%\s*$")
# 每切换一个文件就会刷一遍的三类行：3.5 万个文件足以刷出几万行日志。
# 它们只是「活动迹象」，隔几秒留一条就够，不必逐条入库。
# 用 \b 而不是 \s：工具重画进度时状态为空，会打出一个裸的「Status」「Sending」，
# 用 \s 的话它们不算噪声，会一条条落进日志
NOISE_RE = re.compile(r"^(Status|Receiving|Sending)\b")
# 报错行必须无条件保留：真实输出里错误常常粘在 Status 行后面
# （`Status Receiving filesERROR: Could not receive from mobilebackup2 (-256)`），
# 如果先按前缀判噪声，最有用的那行就没了。
# 但只认大写 ERROR/FATAL：设备端报错一直是这个写法，而小写 error 会命中文件名
# （App 里 `.../js/error.1984885a.js` 极常见），那样每条 Receiving 都会被提成
# 「错误」直接落日志，噪声过滤等于白做。
ERROR_RE = re.compile(r"\b(?:ERROR|FATAL)\b|(?i:\b(?:failed|failure|abort\w*|cannot|could not|refused|denied|invalid)\b)")
# 终端控制序列（清屏、光标上移等）：落库和前端都不该看到这些乱码
ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[a-zA-Z]|\x1b\][^\x07]*\x07")

# 单行上限：进度条很密时不必完整保留，截断即可（日志是给人看的）。
LINE_MAX = 400
# 进度事件节流：两次广播至少间隔这么久，避免每毫秒一条把 SSE 和前端压垮。
PROGRESS_MIN_INTERVAL = 0.4
# 整机进度写进终端日志的最小间隔（界面上的进度环另有实时数据，日志只是留痕）。
PROGRESS_LOG_INTERVAL = 5.0
# 文件级噪声行的最小间隔（它们只是「还在动」的证据，10 秒一条足够）。
# 活动行（"正在接收 …/xxx.js"）的节流：它只走 SSE、不落日志，
# 前端拿它在末行原地刷新。一秒几十帧的单文件进度没必要逐帧推。
LIVE_MIN_INTERVAL = 1.0
# 里程碑步长：整机进度每涨 5% 才落一行日志，一次备份约 20 行。
# 落库的日志要能回答"哪一段慢"，逐百分比留 100 行信息量并没变多。
MILESTONE_STEP = 5
NOISE_LOG_INTERVAL = 10.0
# 工具结束时自己报的接收文件数（取消、失败也会打印），比按输出行数出来的准
RECEIVED_RE = re.compile(r"Received\s+(\d+)\s+files\s+from\s+device", re.I)
# iOS 16.1 起备份开始前手机可能要求输入锁屏密码，工具检测到就打印这一行
PASSCODE_RE = re.compile(r"Waiting for passcode", re.I)
# 阶段文字要短：它显示在 150px 进度环的正中间。完整说明写进日志
PHASE_PASSCODE = "等待输入锁屏密码"
PHASE_PREPARING = "手机准备中"
# 「请输密码」只挂这么久。工具只在开跑时打一次 Waiting for passcode，之后密码输没输
# 它不再说话（接收超时的 Device is not ready yet 是 verbose 2，默认看不见），
# 所以只能按时间收口：过了这个点，界面改说「手机准备中」，别一直谎称在等输入
PASSCODE_HINT_TTL = 90
# 等手机的上限：从开跑算起，这么久还没开始传数据就放弃。iOS 16.1 起每次备份都要在
# 手机上输锁屏密码，没人输时工具会一直等下去（源码里没有超时），任务挂着，
# 这台设备也就一直「忙」。手机准备数据实测要 5 分钟左右，余下的留给人看到提醒再去输。
# 分不清「没输」和「输了但手机还在准备」，所以宁可放宽
WAIT_TIMEOUT_SCHEDULE = 20 * 60
WAIT_TIMEOUT_MANUAL = 30 * 60
# 超时失败的错误信息前缀：调度器据此判断「今天别再自动重来」，改文案时别动这几个字
WAIT_TIMEOUT_PREFIX = "等待手机超时"
# 传输中的上限：开始传数据之后，子进程连续这么久没有任何输出就放弃。看的是原始输出
# （含不进日志的单文件进度帧），大文件传输时工具一直在刷进度，不会被误判；
# 真正卡住（手机走出 Wi-Fi、锁屏断开）时才会一点输出都没有。
# 只管备份：恢复在最后一个文件发完后，手机要自己应用、重建，工具这段时间只在
# verbose 2 下打 Device is not ready yet（默认看不见），收到结果才打 Restore Successful，
# 大机器可能静默半小时以上。误杀一个写到一半的恢复比让它多挂一会儿代价大得多，
# 恢复又是人在手机旁边手动发起的，卡住了能看到、能取消
STALL_TIMEOUT = 30 * 60
STALL_PREFIX = "传输中断"


def _fmt_size(n: int) -> str:
    """字节数转人读单位（日志里一位小数足够）。"""
    if n < 1024:
        return f"{int(n)} B"
    for unit in ("KB", "MB", "GB"):
        n /= 1024
        if n < 1024:
            return f"{n:.1f} {unit}"
    return f"{n / 1024:.1f} TB"
# 心跳：精简档不再记录文件级噪声，长时间没有任何输出时补一条，
# 用来区分「慢慢跑」和「彻底卡死」。
HEARTBEAT_INTERVAL = 300
# 开始收发文件之前（等密码、手机准备）一般有几分钟没有任何输出，心跳要更勤，
# 不然日志里只有一大段空白，看不出是在等手机还是卡死了
HEARTBEAT_WAITING = 60
HEARTBEAT_CHECK = 15                  # 心跳每隔多久检查一次

STREAM_LIMIT = 1 << 20

# 活跃任务表：job_id -> Job
_jobs: dict[str, "Job"] = {}


class DeviceBusy(RuntimeError):
    """同一台设备同时只允许一个任务。

    备份与恢复都会动设备端的数据，而备份目录又是同一份可写状态（增量备份
    靠在同一目录里追加），并发跑不只会互相拖慢，还可能把备份目录写坏。
    换机恢复时，来源设备的备份目录正在被读，它也算「忙」。
    """

    def __init__(self, job: "Job | None", udid: str = "", message: str = ""):
        self.job = job
        if not message and job is not None:
            if job.kind == "RESTORE" and udid and udid == job.source_udid and udid != job.udid:
                message = "这台设备的备份正在被用来恢复另一台设备，请等恢复结束"
            else:
                kind = "恢复" if job.kind == "RESTORE" else "备份"
                message = f"该设备已有进行中的{kind}任务"
        super().__init__(message or "该设备正忙，请稍后再试")


class Job:
    def __init__(self, job_id: str, udid: str, kind: str = "BACKUP", backup_id: int | None = None,
                 source_udid: str | None = None, env: dict | None = None, trigger: str = "manual"):
        self.id = job_id
        self.trigger = trigger                # manual | schedule：定时触发的要推等密码提醒
        self.wait_timeout = WAIT_TIMEOUT_SCHEDULE if trigger == "schedule" else WAIT_TIMEOUT_MANUAL
        self._timeout_err = ""                # 等手机超时时的失败原因（收尾时用它代替退出码）
        self._kill_task: asyncio.Task | None = None
        self.udid = udid                      # 目标设备（备份：被备份的；恢复：被写入的）
        self.kind = kind                      # BACKUP | RESTORE
        self.backup_id = backup_id
        # 恢复时读谁的备份目录；同机恢复与备份时等于 udid
        self.source_udid = source_udid or udid
        # 只用于传备份密码，绝不写进日志
        self._env = env
        self.subscribers: list[asyncio.Queue] = []
        self.status = "RUNNING"               # RUNNING | DONE | FAILED | CANCELED
        self.progress = 0
        self.phase = "准备中"
        self.log_tail: list[str] = []
        self.log_stamped: list[str] = []      # 带时间戳，落库用（前端自己有时间列）
        self.proc: asyncio.subprocess.Process | None = None
        self.task: asyncio.Task | None = None
        self.started_at = time.time()         # 绝对时间：前端重连后据此还原计时
        self.rc: int | None = None
        self.finalized = False                # 事件只收尾一次
        self._backup_closed = False             # 备份记录只收尾一次
        self._cancel_requested = False        # 取消可能早于子进程创建
        self._last_prog_ts = 0.0              # 上一次广播进度的时间（节流用）
        self._last_prog_log_ts = 0.0          # 上一次把整机进度写进日志的时间
        self._last_noise_ts = 0.0             # 上一次把文件级噪声写进日志的时间
        self._last_log_ts = time.time()       # 上一次写日志的时间（心跳判据）
        self._last_out_ts = time.time()       # 上一次读到子进程任何输出的时间（卡住判据）
        self._recent: collections.deque = collections.deque(maxlen=8)  # (时间, 行)，去重用
        self._last_logged_pct = -1            # 精简档：上一次写进日志的整机百分比
        self._last_milestone = 0              # 上一次落日志的里程碑百分比
        # 终端底部两行常驻刷新（见 _live）：progress 是整机进度，file 是当前收发的文件。
        # 以前只有一个 live 行，进度帧和文件帧互相顶掉，看起来一直在闪
        self._live_text: dict[str, str] = {}
        self._live_ts: dict[str, float] = {}
        self._files_seen = 0                  # 本次任务收发过的文件数（按文件名变化计数）
        self._last_file = ""                  # 上一个收发的文件名（同一文件分块传输会重复打印）
        self._last_activity = ""              # 最后一个收发的文件（失败/取消时写进日志）
        self._reported_files: int | None = None   # 工具自己报的 Received N files
        self._transfer_started = False        # 开始收发文件了没有（之前都算「等手机」）
        self._passcode_seen = False           # 工具打过 Waiting for passcode（不等于用户输了）
        self._waiting_since = time.time()     # 等手机从什么时候开始（心跳文案用）
        self._kill_note = ""                  # 进程是怎么结束的：SIGTERM / 强制结束
        self.backup_type = ""                 # FULL / INCREMENTAL，由 _run_backup 决定
        self._verbose_cache: bool | None = None   # 日志详细度（设置项，懒加载）

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
        # 同一行 10 秒内再出现就丢掉。工具收到 SIGTERM 后每次重画进度都会再打一遍
        # 「Status  Abort requested, hold on...」，中间还夹着别的行（真机一次取消落了 4 条），
        # 只和上一行比去不干净；隔得久的重复（比如同一个报错过几分钟又来）照常保留
        now = time.time()
        if any(prev == line and now - ts < 10 for ts, prev in self._recent):
            return
        self._recent.append((now, line))
        self._last_log_ts = now
        self.log_tail.append(line)
        if len(self.log_tail) > 400:
            self.log_tail.pop(0)
        # 落库版本带时间戳：事后要看「哪一步卡了多久」，没有时间就没法判断
        self.log_stamped.append(datetime.datetime.now().strftime("%H:%M:%S ") + line)
        if len(self.log_stamped) > 2000:
            self.log_stamped.pop(0)
        self.broadcast({"type": "log", "line": line})

    def _verbose(self) -> bool:
        """日志详细度：默认精简（约 40 行/次，与备份时长无关），可切详细。"""
        if self._verbose_cache is None:
            try:
                self._verbose_cache = (store.get_setting("log_verbose") or "") == "true"
            except Exception:  # noqa: BLE001  设置读不到就按精简走
                self._verbose_cache = False
        return self._verbose_cache

    def tag(self) -> str:
        """记录编号，和任务历史里显示的一致：BKP-0002 / RST-0003。"""
        if not self.backup_id:
            return "job-" + self.id[:8]
        return ("RST-" if self.kind == "RESTORE" else "BKP-") + f"{self.backup_id:04d}"

    def _mark_transfer_started(self):
        """第一次真正开始收发数据：切阶段，并在日志里留下时间点。

        这一行回答「输完锁屏密码后手机准备了多久」。以前只能靠一个碰巧落库的裸
        Status 行去猜，现在裸 Status 按噪声丢掉了，必须显式记。
        """
        if self._transfer_started:
            return
        self._transfer_started = True
        self.phase = "正在传输数据"
        waited = int(time.time() - self.started_at)
        self.add_log(f"开始传输数据（距开始 {waited // 60} 分 {waited % 60} 秒）")
        self.broadcast({"type": "progress", "progress": self.progress, "phase": self.phase})

    def _verb(self) -> str:
        return "正在恢复" if self.kind == "RESTORE" else "正在备份"

    def _header_text(self) -> str:
        """任务头：谁、什么类型、加不加密、写到哪。开跑时一行，事后一眼能对上是哪次。"""
        dst = store.get_device(self.udid) or {}
        dst_name = dst.get("name") or "…" + self.udid[-6:]
        if self.kind == "RESTORE":
            src = store.get_device(self.source_udid) or {}
            src_name = src.get("name") or "…" + self.source_udid[-6:]
            enc = "加密备份" if self._env else "未加密备份"
            return f"恢复 · 「{src_name}」→「{dst_name}」 · {enc}"
        # 和备份记录的 type 用同一个依据（有没有成功过的备份），任务头、记录、
        # 任务历史三处说法一致。idevicebackup2 自己打印的 Incremental/Full backup mode
        # 看的是目录里的文件，失败过的半截备份也算，所以它说的可能和这里不同
        kind = ("增量备份" if self.backup_type == "INCREMENTAL"
                else "全量备份（还没有成功的备份）")
        enc = "已加密" if dst.get("encrypted") else "未加密"
        trig = {"schedule": " · 定时触发", "usb": " · 插线触发"}.get(self.trigger, "")
        return f"{dst_name} · {kind} · {enc}{trig} · → {config.BACKUP_DIR}"

    def _env_text(self) -> str:
        """开跑时的环境：走什么通道、电量、备份盘剩余。失败时最先要排除的就是这几样。"""
        conn = "Wi-Fi" if device.get_transport(self.udid) == device.TRANSPORT_NET else "USB"
        b = device.battery_cached(self.udid)
        if b:
            chg = "充电中" if b["charging"] else "接着电源" if b["external"] else "未充电"
            # 读数可能是缓存的（定时备份复用未过期读数，无线最长 10 分钟），
            # 不写时间的话事后会把它当成开跑那一刻的电量
            age = int((time.time() - b["at"]) // 60)
            batt = f"电量 {b['level']}%（{chg}" + (f"，读数 {age} 分钟前" if age >= 1 else "") + "）"
        else:
            batt = "电量未知"
        free = _disk_free_bytes(config.BACKUP_DIR)
        disk = f"备份盘剩余 {_fmt_size(free)}" if free >= 0 else "备份盘剩余未知"
        return f"{conn} · {batt} · {disk}"

    def _elapsed_text(self) -> str:
        sec = max(0, int(time.time() - (self.started_at or time.time())))
        if sec < 60:
            return f"{sec} 秒"
        if sec < 3600:
            return f"{sec // 60} 分"
        return f"{sec // 3600} 小时 {sec % 3600 // 60} 分"

    def _live(self, text: str, slot: str = "file"):
        """常驻行：只走 SSE，不进日志数组。

        前端把 slot 相同的行原地替换，所以底部是固定的两行：
          progress —— 正在备份 6% · 已用 12 分     （进度变了才动）
          file     —— 正在接收 …/xxx.js            （换文件才动）
        两行各刷各的，不会再互相顶掉。idevicebackup2 本来就是用 \\r 原地刷新，
        这里只是把那个语义还原回去，而不是把每一帧都追加成一条日志。
        """
        if not text or self._live_text.get(slot) == text:
            return
        now = time.time()
        if now - self._live_ts.get(slot, 0.0) < LIVE_MIN_INTERVAL:
            return
        self._live_ts[slot] = now
        self._live_text[slot] = text
        self.broadcast({"type": "live", "slot": slot, "text": text})

    def live_snapshot(self) -> dict[str, str]:
        """重连时补下发当前两行：刷新页面后底部不该空着等下一次事件。"""
        return dict(self._live_text)

    @staticmethod
    def _activity(line: str) -> str | None:
        """把一条文件级噪声行压成活动行文案；压不出有用信息的返回 None。

        `Receiving .../AppOhoVip/7.0.50/js/error.js` → `正在接收 …/7.0.50/js/error.js`
        只留路径尾部：完整路径动辄两三百字符，且开头全是重复的容器目录。
        """
        m = re.match(r"^(Receiving|Sending)\s+(\S.*)$", line)
        if not m:
            return None
        tail = m.group(2).strip()
        if len(tail) > 64:
            tail = "…" + tail[-63:]
        return ("正在接收 " if m.group(1) == "Receiving" else "正在发送 ") + tail

    def _emit(self, seg: str):
        """处理子进程输出的一段：整机进度单独识别，其余进日志。

        idevicebackup2 的进度条一秒钟能刷几十次，原样转发会把 SSE 和前端
        压垮。所以这里分三档：
          · 报错       —— 无条件落日志，不节流不判噪声
          · 整机进度   —— 走 SSE 事件 + 每 MILESTONE_STEP 个百分比落一行
          · 文件级噪声 —— 只更新「活动行」，不落日志（详细档才稀疏留痕）
        """
        line = ANSI_RE.sub("", seg).strip()
        if not line:
            return
        if len(line) > LINE_MAX:
            line = line[:LINE_MAX] + " …"
        now = time.time()

        m = RECEIVED_RE.search(line)
        if m:
            self._reported_files = int(m.group(1))

        # 手机在等锁屏密码：界面上要明确说出来，否则几分钟的 0% 看起来像卡死
        if PASSCODE_RE.search(line) and not self._transfer_started:
            self.phase = PHASE_PASSCODE
            self._passcode_seen = True
            self._waiting_since = now
            self.broadcast({"type": "progress", "progress": self.progress, "phase": self.phase})
            self.add_log(line)
            self.add_log("手机要求输入锁屏密码：请在 iPhone 上输入。输完密码手机还要生成清单、"
                         "比对变更，这期间没有任何输出，进度停在 0% 是正常的（实测要等 5 分钟左右）")
            # 定时任务没人盯着页面：推一条提醒，不然密码框只会在锁着的手机上默默等着。
            # 手动备份不推，人就在页面前
            if self.trigger == "schedule":
                try:
                    notify.passcode_needed(self.udid, self.tag(), self.wait_timeout // 60)
                except Exception as e:  # noqa: BLE001
                    log.warning("任务 %s 等密码提醒发送失败：%s", self.tag(), e)
            return

        # 报错/告警优先：不节流、不判噪声，原样留下
        if ERROR_RE.search(line):
            self.add_log(line)
            return

        # 整机进度：只认 `Backup [....] 3%` 这种标记，没有就退到 "Total progress:"
        m = OVERALL_RE.search(line)
        if not m:
            m = TOTAL_RE.search(line)
        if m:
            try:
                pct = int(m.group(1))
            except ValueError:
                pct = None
            # 100% 一律不认：idevicebackup2 退出前无条件调用 progress_finish()，
            # 把整机进度置成 100 再打一遍——取消、失败也一样。认了它，日志里就会
            # 冒出一行「正在备份 99%」，排查时以为差一点就完成了。
            # 真正成功时由任务收尾置 100。
            if pct is not None and pct >= 100:
                return
            if pct is not None:
                # 只增不减：分段输出混排时数字不会来回跳
                self.progress = max(self.progress, pct)
                # 阶段只由 _mark_transfer_started 切到「正在传输数据」。以前这里按
                # 「不是等密码」就切，工具重画的 0% 帧会把「手机准备中」改掉，
                # 数据其实还没开始传
                if pct > 0:
                    self._mark_transfer_started()
                if now - self._last_prog_ts >= PROGRESS_MIN_INTERVAL:
                    self._last_prog_ts = now
                    self.broadcast({"type": "progress", "progress": self.progress,
                                    "phase": self.phase})
                # 进度行：和文件行分两行常驻，不再互相顶掉
                self._live(f"{self._verb()} {self.progress}% · 已用 {self._elapsed_text()}",
                           "progress")
                # 里程碑：每 5% 落一行，约 20 行。带上已传文件数和耗时，
                # 事后能看出「哪一段慢 / 传了多少」——只有百分比的话时间信息就丢了。
                if self.progress - self._last_milestone >= MILESTONE_STEP:
                    self._last_milestone = self.progress
                    self.add_log(f"{self._verb()} {self.progress}%"
                                 f" · 已传 {self._files_seen} 个文件"
                                 f" · 已用 {self._elapsed_text()}")
                # 详细档：额外按时间留痕（排查"哪一秒卡住"用）
                if self._verbose() and now - self._last_prog_log_ts >= PROGRESS_LOG_INTERVAL:
                    self._last_prog_log_ts = now
                    self.add_log(line)
                return

        # 文件级噪声（切换文件、单文件进度条）：不落日志，
        # 只把「正在收发的文件」喂给活动行 —— 卡住时最后一条就是卡住的那个文件。
        if NOISE_RE.search(line) or FILE_PROG_RE.search(line) or BARE_BAR_RE.match(line):
            act = self._activity(line)
            if act:
                # 文件计数按「文件名变了」算：工具每收一块数据就把四行进度整个重画一遍，
                # 大文件分很多块，同一个 Receiving 行会出现很多次（实测多算近一倍）
                name = line.split(None, 1)[1].strip()
                if name != self._last_file:
                    self._last_file = name
                    self._files_seen += 1
                self._last_activity = act
                # 只认主方向：备份是从手机收（Receiving），恢复是往手机发（Sending）。
                # 备份开始前工具可能先给手机发一两个小文件，不能把那个当成开始传数据，
                # 否则「等待输入锁屏密码」的提示会提前消失
                main_dir = "Sending" if self.kind == "RESTORE" else "Receiving"
                if line.startswith(main_dir):
                    self._mark_transfer_started()
                self._live(act)
            if self._verbose() and now - self._last_noise_ts >= NOISE_LOG_INTERVAL:
                self._last_noise_ts = now
                self.add_log(line)
            return

        self.add_log(line)

    async def _heartbeat(self):
        """长时间没有任何输出时补一条心跳，避免日志看起来像卡死。

        精简档不再记录文件级噪声，如果设备传输很慢（大文件），中间可能几分钟
        没有进度变化——这条心跳让人知道「还在跑，只是没动静」。
        """
        while not self.finalized:
            await asyncio.sleep(HEARTBEAT_CHECK)
            if self.finalized:
                return
            now = time.time()
            idle = now - self._last_log_ts
            # 进度不变时底部那行「已用」也得跟着走：设备卡在 1% 好几分钟的时候，
            # 界面上的时间还在跳，才看得出没死。心跳 15 秒一跳，够用且不吵
            self._live(f"{self._verb()} {self.progress}% · 已用 {self._elapsed_text()}",
                       "progress")
            if not self._transfer_started:
                if now - self.started_at >= self.wait_timeout and not self._timeout_err:
                    self._give_up_waiting()
                    return
                # 还没开始收发：等密码或手机准备，一分钟报一次，带总等待时长
                waited = max(1, int((now - self.started_at) // 60))
                switched = False
                # 密码提示挂满 TTL 就改口。工具只在开跑时打一次 Waiting for passcode，
                # 用户输完它也不会再说一句，界面一直写「等待输入锁屏密码」就是在骗人
                if (self.phase == PHASE_PASSCODE
                        and now - self._waiting_since >= PASSCODE_HINT_TTL):
                    self.phase = PHASE_PREPARING
                    switched = True
                    self.broadcast({"type": "progress", "progress": self.progress,
                                    "phase": self.phase})
                    self.add_log(f"已等 {waited} 分钟仍是 0%：密码输完后手机要生成清单、比对变更，"
                                 "这段时间没有任何输出是正常的；若手机屏幕还停在输密码那一步，"
                                 "请再输一次（手机锁屏后可能需要重新解锁）")
                if not switched and idle >= HEARTBEAT_WAITING:
                    if self._passcode_seen:
                        self.add_log(f"仍在等待手机开始传输，已等 {waited} 分钟"
                                     "（手机在准备数据，进度停在 0% 是正常的）")
                    else:
                        self.add_log(f"仍在等待手机，已等 {waited} 分钟")
            elif (self.kind == "BACKUP" and now - self._last_out_ts >= STALL_TIMEOUT
                  and not self._timeout_err):
                self._give_up_stalled(now - self._last_out_ts)
                return
            elif idle >= HEARTBEAT_INTERVAL:
                # 日志久没新行不等于工具没输出：文件行只刷底部常驻行、不进日志，
                # 整机进度 5% 才落一行，大备份/大恢复一个 5% 可能要很久。
                # 所以文案按「工具最近有没有输出」分：还在出就是在传，带上当前文件
                out_idle = now - self._last_out_ts
                if out_idle < 60:
                    cur = f" · {self._last_activity}" if self._last_activity else ""
                    self.add_log(f"仍在传输中，当前 {self.progress}%{cur}")
                elif self.kind == "RESTORE":
                    # 恢复的长静默一般在文件发完之后：手机在自己应用数据。
                    # 分不清是不是中途断线，所以两种都说
                    self.add_log(f"已 {int(out_idle // 60)} 分钟没有输出，当前 {self.progress}%："
                                 "多半是文件已发完、手机正在应用数据（正常）；"
                                 "如果手机屏幕上没有恢复进度，可能是连接断了")
                else:
                    self.add_log(f"仍在传输中，当前 {self.progress}%"
                                 f"（已 {int(out_idle // 60)} 分钟没有输出）")

    def _give_up_waiting(self):
        """等手机超时：记原因，结束进程。任务按失败收尾（不是取消：没人点取消）。

        结束进程放到独立任务里：心跳任务会在 _pump 收尾时被取消，在这里 await
        的话强杀可能做到一半就断了。实测等密码阶段工具不理 SIGTERM，要等 5 秒后强杀。
        """
        minutes = self.wait_timeout // 60
        reason = "，可能没有在 iPhone 上输入锁屏密码" if self._passcode_seen else ""
        tail = {"schedule": "。这一次定时备份不再自动重试，可以随时手动备份",
                "usb": "。可以拔掉数据线半分钟后重新插上再来一次，或手动备份"}.get(self.trigger, "")
        self._timeout_err = f"{WAIT_TIMEOUT_PREFIX}：{minutes} 分钟内没有开始传输数据{reason}{tail}"
        self.add_log(self._timeout_err)
        log.warning("任务 %s %s", self.tag(), self._timeout_err)
        self._kill_task = self._spawn_kill()

    def _give_up_stalled(self, idle: float):
        """传输中卡住：记原因，结束进程，按失败收尾。

        和等手机超时不同，定时备份遇到这种失败照常按规则重试：手机回到 Wi-Fi 或
        插上线就可能跑通，不像没人输密码那样再试也没用
        """
        self._timeout_err = (f"{STALL_PREFIX}：{int(idle // 60)} 分钟没有任何进展（停在 {self.progress}%），"
                             "可能手机离开了 Wi-Fi、锁屏后断开，或数据线松了")
        self.add_log(self._timeout_err)
        log.warning("任务 %s %s", self.tag(), self._timeout_err)
        self._kill_task = self._spawn_kill()

    def _spawn_kill(self) -> asyncio.Task:
        """在后台结束子进程，不等它。

        强杀要 SIGTERM 后等 5 秒，调用方（心跳、取消接口）不该跟着等。done_callback
        负责把异常取出来记日志，不然 _kill_proc 万一抛错会变成 Task exception was
        never retrieved。_pump 的 finally 还会再调一次 _kill_proc 兜底，并且会等进程真正退出
        """
        task = asyncio.create_task(self._kill_proc())

        def _done(t: asyncio.Task):
            if t.cancelled():
                return
            e = t.exception()
            if e is not None:
                log.warning("任务 %s 结束子进程失败：%r", self.tag(), e)
        task.add_done_callback(_done)
        return task

    def _log_text(self, header: str = "") -> str:
        body = "\n".join(self.log_stamped)
        return (header + "\n" + body) if header else body

    def _files_text(self) -> str:
        """小结里的文件数：工具报了就用工具的，没报（恢复、异常中断）才用计数。"""
        if self._reported_files is not None:
            return f"设备报告收到 {self._reported_files} 个文件"
        return f"本次传 {self._files_seen} 个文件"

    def _persist_log(self, error: str = ""):
        """把日志落到备份记录里；失败也不能影响任务收尾。"""
        if not self.backup_id:
            return
        header = (f"# {self.kind} {self.udid}  {self.status}  "
                  f"{datetime.datetime.now().isoformat(timespec='seconds')}")
        try:
            store.save_backup_log(self.backup_id, self._log_text(header), error)
        except Exception as e:  # noqa: BLE001
            log.warning("备份记录 #%s 日志落库失败：%s", self.backup_id, e)

    def _close_backup(self, status: str, **extra):
        """备份记录收尾，且只写一次。

        正常结束、异常中断、用户取消三条路径都会走到这里；先到先算，
        后到的不会把已经写好的 COMPLETE 又盖成 FAILED。
        """
        if not self.backup_id or self._backup_closed:
            return
        self._backup_closed = True
        try:
            store.finish_backup(self.backup_id, status=status, **extra)
        except Exception as e:  # noqa: BLE001
            log.warning("备份记录 #%s 收尾失败：%s", self.backup_id, e)

    # ----- 收尾 -----

    def _finish(self, status: str, **extra):
        if self.finalized:
            return
        self.finalized = True
        self.status = status
        # 跑完一轮电量必然变了，旧读数作废；有人看页面或调度器需要时再查
        device.battery_forget(self.udid)
        if status != "DONE":
            # 最后一个收发的文件：跑的过程中文件行不进日志（那是几万行），
            # 但卡住或失败时它恰恰是最有用的线索。放在小结之前，免得看起来像取消后还在收
            if self._last_activity:
                self.add_log("最后一个文件：" + self._last_activity)
            # 失败/取消也留一句小结：没跑完的备份最需要知道「动了多少、花了多久」
            label = "已取消" if status == "CANCELED" else "失败"
            self.add_log(f"{label} · {self._files_text()} · 已用 {self._elapsed_text()}")
        err = extra.get("error", "")
        log.info("任务结束 %s · %s · 用时 %s%s", self.tag(), status, self._elapsed_text(),
                 f" · {err}" if err else "")
        if status == "CANCELED":
            self._persist_log("用户取消")
        else:
            self._persist_log(extra.get("error", ""))
        self.broadcast({"type": status.lower(), **extra})
        for q in list(self.subscribers):
            try:
                q.put_nowait({"type": "end"})  # 终止哨兵
            except Exception:
                pass
        # Webhook 在这里发而不是定时调度器里：用户要的是「这次任务的结果」，
        # 失败与取消也必须送到，而不是只有触发时的一条已开跑通知。
        try:
            notify.task_finished(
                self.kind, status, self.udid,
                backup_id=self.backup_id,
                error=extra.get("error", ""),
                size_bytes=extra.get("size"),
                duration_s=time.time() - self.started_at,
            )
        except Exception as e:  # noqa: BLE001
            log.warning("任务 %s webhook 通知失败：%s", self.id, e)

    async def _kill_proc(self, grace: float = 5.0):
        """确保子进程退出。可重入：进程已退出或压根没起来时直接返回。"""
        proc = self.proc
        if not proc or proc.returncode is not None:
            return
        try:
            proc.terminate()
            # 只有第一次写才有意义：cancel() 与 _pump() 的 finally 会并发进来，
            # 后进来的那次不能把「SIGTERM 后强制结束」改回「已发送 SIGTERM」
            self._kill_note = self._kill_note or "已发送 SIGTERM"
        except ProcessLookupError:
            return
        try:
            await asyncio.wait_for(proc.wait(), timeout=grace)
            return
        except Exception:  # 超时或已被回收，都走强杀
            pass
        try:
            proc.kill()
            self._kill_note = f"SIGTERM 后 {grace:g} 秒未退出，已强制结束"
        except ProcessLookupError:
            return
        try:
            await asyncio.wait_for(proc.wait(), timeout=5)
        except Exception:  # noqa: BLE001
            log.warning("任务 %s 的子进程 pid=%s 未能回收", self.id, proc.pid)

    async def _fail(self, e: BaseException):
        """异常路径的统一收尾：回收进程、备份记录标失败、通知订阅者。"""
        msg = f"任务异常中断：{e!r}" if not str(e) else f"任务异常中断：{e}"
        log.error("任务 %s 失败：%r", self.id, e, exc_info=True)
        self.add_log(msg)
        await self._kill_proc()
        self._close_backup("FAILED", error=msg)
        self._finish("FAILED", error=msg)

    # ----- 执行 -----

    async def _pump(self, args: list[str]) -> int:
        """起子进程并把 stdout 逐行转发，返回退出码（未启动即取消时返回 -1）。

        无论循环是正常结束还是抛异常，finally 都会保证进程不留在后台。
        """
        header, env = self._header_text(), self._env_text()
        self.add_log(header)
        self.add_log(env)
        self.add_log(f"$ {' '.join(args)}")
        log.info("任务开始 %s · %s · %s", self.tag(), header, env)
        if self._cancel_requested:
            # 还没起进程就被取消：连备份记录都不该开始写
            return -1
        try:
            self.proc = await asyncio.create_subprocess_exec(
                *args,
                # 缺密码时 idevicebackup2 会尝试从终端读，容器里没有 TTY 会一直挂着
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                limit=STREAM_LIMIT,
                env={**os.environ, **self._env} if self._env else None,
            )
        except FileNotFoundError as e:
            raise RuntimeError(f"找不到 {args[0]}：{e}") from e
        # 取消可能就发生在上面的 await 里：进程刚起来也要立刻收掉，
        # 否则「已取消」的任务会在后台把整轮备份跑完。
        if self._cancel_requested:
            await self._kill_proc()
            return -1
        # 心跳必须在 try 之前起：放在里面的话，前面一行抛异常时
        # finally 里的 hb.cancel() 会变成 NameError，把原始异常盖掉
        hb = asyncio.create_task(self._heartbeat())
        try:
            assert self.proc.stdout
            buf = ""
            # 用 read(n) 而不是 async for（后者按 \n 分帧）：进度条只发 \r，
            # 按行读会憋成一条巨型字符串，前端一次插入几 MB HTML 直接卡死，
            # 超长行还会触发 asyncio 的行长上限、把任务拽出循环。
            while True:
                data = await self.proc.stdout.read(8192)
                if not data:
                    break
                self._last_out_ts = time.time()
                buf += data.decode(errors="replace")
                parts = re.split(r"[\r\n]", buf)
                buf = parts.pop()          # 末尾可能是半截输出，留到下一轮
                for seg in parts:
                    self._emit(seg)
                if len(buf) > LINE_MAX * 4:   # 长时间不换行：及时吐出去，别无限膨胀
                    self._emit(buf)
                    buf = ""
            if buf.strip():
                self._emit(buf)
        finally:
            hb.cancel()
            if self.proc and self.proc.returncode is None:
                await self._kill_proc()
        rc = await self.proc.wait()
        self.rc = rc
        # 退出码 + 怎么结束的：区分「正常退出」「响应 SIGTERM 退出」「被强制结束」
        how = f"被信号 {-rc} 结束" if rc < 0 else ""
        note = "，".join(x for x in (self._kill_note, how) if x)
        self.add_log(f"{os.path.basename(args[0])} 退出码 {rc}" + (f"（{note}）" if note else ""))
        return rc

    async def run_backup(self):
        """执行一次备份。增量由 idevicebackup2 自动判断（目标目录已有备份数据则增量）。"""
        try:
            await self._run_backup()
        except asyncio.CancelledError:
            await self._kill_proc()
            raise
        except Exception as e:  # noqa: BLE001
            await self._fail(e)
        finally:
            # 在协程退出前就注销，而不是等 done_callback：
            # 否则任务已经结束、下一个请求却在同一 tick 里被误判成「设备忙」。
            _jobs.pop(self.id, None)

    async def _run_backup(self):
        store.get_device(self.udid)           # 设备记录不存在时尽早暴露，别等进程报错
        dev_dir = os.path.join(config.BACKUP_DIR, self.udid)
        os.makedirs(dev_dir, exist_ok=True)
        # 增量与否的判断依据是「磁盘上是否已有成功完成的备份基线」，
        # 不是最近一条记录——上一轮失败后基线目录仍在，这轮实际跑的还是增量。
        # 只看最近 1 条会把「失败后的正常增量」误标成 FULL，界面因此说谎。
        prior = store.list_backups(self.udid, limit=50, backups_only=True)
        backup_type = "INCREMENTAL" if any(
            s["status"] == "COMPLETE" for s in prior
        ) else "FULL"

        self.backup_type = backup_type
        self.backup_id = store.create_backup(self.udid, backup_type=backup_type)
        if self._cancel_requested:
            self._close_backup("CANCELED", error="启动前已取消")
            self._finish("CANCELED")
            return

        # 备份盘写满的后果不只是这次失败：唯一那份可恢复状态会停在中间态，
        # 且没有上一个版本可退回。低于安全下限直接拒绝，让失败发生在最开始。
        free = _disk_free_bytes(config.BACKUP_DIR)
        if config.MIN_FREE_BYTES > 0 and free >= 0 and free < config.MIN_FREE_BYTES:
            err = (f"备份盘剩余空间不足：仅剩 {free / 1024 ** 3:.1f} GB，"
                   f"低于安全下限 {config.MIN_FREE_GB:g} GB。"
                   "请清理备份盘，或在 .env 里调低 IVAULT_MIN_FREE_GB")
            self.add_log(err)
            self._close_backup("FAILED", error=err)
            self._finish("FAILED", error=err)
            return

        rc = await self._pump([
            config.BIN_IDEVICEBACKUP2, *device.net_args(self.udid), "-u", self.udid,
            "backup", config.BACKUP_DIR,
        ])

        if self._cancel_requested or self.status == "CANCELED":
            self._close_backup("CANCELED")
            self._finish("CANCELED")
            return
        if rc == 0:
            # last_backup_at 只在真正成功时更新：失败的任务不能让设备卡片
            # 显示「今天已备份」——那是健康状态里最容易被误读的字段。
            store.touch_last_backup(self.udid)
            # 加密状态以刚写完的这份备份为准：用户可能在 Finder 里改过开关
            meta = backup_meta(self.udid)
            if meta.get("encrypted") is not None:
                store.set_device_encrypted(self.udid, meta["encrypted"])
            size = await _dir_size(dev_dir)
            self.add_log(f"完成 · {self._files_text()}"
                         f" · 目录 {_fmt_size(size)} · 用时 {self._elapsed_text()}")
            self._close_backup("COMPLETE", size_bytes=size, backup_type=backup_type)
            self._finish("DONE", progress=100, size=size, backup_id=self.backup_id)
        else:
            err = self._timeout_err or f"idevicebackup2 退出码 {rc}"
            self._close_backup("FAILED", error=err)
            self._finish("FAILED", error=err)

    async def cancel(self, source: str = ""):
        if self.finalized or self.status != "RUNNING":
            return
        # 记下是谁取消的：日志里只有工具自己的 Abort requested 的话，
        # 分不清是用户点了取消、服务在重启，还是进程被外部杀掉
        self.add_log("收到取消请求" + (f"（{source}）" if source else ""))
        log.info("任务 %s 收到取消请求%s", self.tag(), f"（{source}）" if source else "")
        # 先立旗再动进程：取消有可能早于 create_subprocess_exec（握手前的
        # 建目录、建备份记录都是同步操作，但那一步的 await 仍在）。只改状态的话，
        # 运行的协程看不见旗子，照样会把整轮备份跑完。
        self._cancel_requested = True
        self.status = "CANCELED"
        # 不在这里等进程退出：等密码阶段工具不理 SIGTERM，要等 5 秒后强杀，
        # 取消按钮也就转 5 秒才有反应（真机 5020ms）。任务的结束由 SSE 推给页面
        self._kill_task = self._spawn_kill()


def _disk_free_bytes(path: str) -> int:
    """备份盘剩余字节数；拿不到时返回 -1（跳过检查，不因为 stat 失败拦备份）。"""
    try:
        import shutil
        return shutil.disk_usage(path).free
    except OSError:
        return -1


_meta_cache: dict[str, tuple[tuple, dict]] = {}


def backup_meta(udid: str) -> dict:
    """读磁盘上某台设备的备份目录：是否加密、来自哪个 iOS 版本、设备名。

    以文件为准而不是数据库：加密是写在 Manifest.plist 里的事实，恢复时
    idevicebackup2 也是看它决定要不要密码。Info.plist 可能有几 MB（含 App 图标），
    按 mtime 缓存，页面轮询不会每次都重新解析。目录或文件不存在时返回 {}。
    """
    base = os.path.join(config.BACKUP_DIR, udid)
    manifest, info = os.path.join(base, "Manifest.plist"), os.path.join(base, "Info.plist")
    try:
        key = (os.path.getmtime(manifest), os.path.getmtime(info))
    except OSError:
        return {}
    hit = _meta_cache.get(udid)
    if hit and hit[0] == key:
        return hit[1]
    meta: dict = {}
    try:
        with open(manifest, "rb") as f:
            meta["encrypted"] = bool(plistlib.load(f).get("IsEncrypted", False))
        with open(info, "rb") as f:
            p = plistlib.load(f)
        meta["ios_version"] = str(p.get("Product Version") or "")
        meta["device_name"] = str(p.get("Device Name") or p.get("Display Name") or "")
        meta["product_type"] = str(p.get("Product Type") or "")
    except Exception as e:  # noqa: BLE001  损坏的 plist 不能把列表接口拖垮
        log.warning("读取备份元数据失败 %s：%s", base, e)
        return {}
    _meta_cache[udid] = (key, meta)
    return meta


def version_tuple(v: str) -> tuple[int, ...]:
    """'17.6.1' → (17, 6, 1)；解析不了的段按 0 处理。"""
    out = []
    for part in (v or "").split("."):
        m = re.match(r"\d+", part)
        out.append(int(m.group()) if m else 0)
    return tuple(out)


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


def busy_job(udid: str) -> Job | None:
    """某台设备当前占用者；没有则 None。

    恢复任务同时占用目标与来源：来源的备份目录正在被读，这时再对来源设备
    跑一次备份，会让目标设备恢复到一份写了一半的数据。
    """
    return next((j for j in _jobs.values() if udid in (j.udid, j.source_udid)), None)


# 不登记为 Job 的短时独占操作（开关备份加密）：它们不需要进度和日志，
# 但同样不能和备份/恢复并发，否则加密状态会在一次备份中途改变。
_locked: set[str] = set()


class device_lock:
    """短时独占一台设备；与 Job 共用同一套忙闲判定。用法：`with device_lock(udid): ...`"""

    def __init__(self, udid: str):
        self.udid = udid

    def __enter__(self):
        _claim(self.udid)
        _locked.add(self.udid)
        return self

    def __exit__(self, *exc):
        _locked.discard(self.udid)
        return False


def active_jobs() -> list[dict]:
    return [
        {"id": j.id, "udid": j.udid, "source_udid": j.source_udid, "kind": j.kind,
         "status": j.status, "progress": j.progress, "phase": j.phase, "started_at": j.started_at}
        for j in _jobs.values() if j.status == "RUNNING"
    ]


def _claim(udid: str) -> None:
    """设备级互斥的唯一判定点。

    检查与登记之间不能有 await，否则两个并发请求会双双通过检查；
    调用方（HTTP 端点、调度器）只需接住 DeviceBusy。
    """
    if udid in _locked:
        raise DeviceBusy(None, udid, "该设备正在修改备份加密设置，请稍后再试")
    busy = busy_job(udid)
    if busy is not None:
        raise DeviceBusy(busy, udid)


def _register(job: Job) -> Job:
    _jobs[job.id] = job
    return job


def _on_done(job: Job, task: asyncio.Task):
    _jobs.pop(job.id, None)
    if task.cancelled():
        log.info("任务 %s 已取消", job.id)
        return
    exc = task.exception()
    if exc:
        # 正常都会在 run_backup 的 except 里兜住；能走到这儿说明兜底本身也炸了，
        # 至少把异常取出来，别让它以 "Task exception was never retrieved" 的形式丢掉。
        log.error("任务 %s 异常结束：%r", job.id, exc)


def start_backup(udid: str, job_id: str, trigger: str = "manual") -> Job:
    _claim(udid)
    job = Job(job_id, udid, kind="BACKUP", trigger=trigger)
    _register(job)
    job.task = asyncio.create_task(job.run_backup())
    job.task.add_done_callback(lambda t: _on_done(job, t))
    return job


def start_restore(udid: str, job_id: str, source_udid: str | None = None,
                  password: str | None = None):
    """把 source_udid 的备份恢复到 udid（不给 source_udid 即同机恢复）。

    换机就是 source ≠ udid：idevicebackup2 的 -s 指定读哪台设备的备份目录。
    备份密码只经环境变量交给子进程，不进命令行、不进日志、不落库。
    """
    source = source_udid or udid
    _claim(udid)
    if source != udid:
        _claim(source)
    job = Job(job_id, udid, kind="RESTORE", source_udid=source,
              env={"BACKUP_PASSWORD": password} if password else None)
    _register(job)
    job.task = asyncio.create_task(_run_restore(job))
    job.task.add_done_callback(lambda t: _on_done(job, t))
    return job


async def _run_restore(job: "Job"):
    try:
        await _run_restore_inner(job)
    except asyncio.CancelledError:
        await job._kill_proc()
        raise
    except Exception as e:  # noqa: BLE001
        await job._fail(e)
    finally:
        _jobs.pop(job.id, None)


async def _run_restore_inner(job: "Job"):
    # 恢复也落一条记录（kind=RESTORE）：任务历史里要看得到它，
    # 失败日志也要能事后回看。以前 backup_id 为空，_persist_log 直接跳过，
    # 恢复失败的输出随 Job 对象一起丢掉。
    job.backup_id = store.create_backup(job.udid, backup_type="", kind="RESTORE",
                                       source_udid=job.source_udid)
    if job._cancel_requested:
        job._close_backup("CANCELED", error="启动前已取消")
        job._finish("CANCELED")
        return
    src = ["-s", job.source_udid] if job.source_udid != job.udid else []
    if src:
        job.add_log(f"换机恢复：读取 ····{job.source_udid[-6:]} 的备份，写入 ····{job.udid[-6:]}")
    if job._env:
        job.add_log("备份已加密，密码已通过环境变量提供（不记录）")
    # --remove：删掉设备上备份里没有的项目，恢复后手机回到备份时的状态（与 Finder 一致）。
    # 不加的话备份之后新增的文件可能留在手机上，结果接近「合并」，和界面上
    # 「手机会回到备份时的状态」的说明对不上。尚未在真机上验证。
    rc = await job._pump([
        config.BIN_IDEVICEBACKUP2, *device.net_args(job.udid), "-u", job.udid, *src,
        "restore", "--system", "--settings", "--remove", config.BACKUP_DIR,
    ])
    if job._cancel_requested or job.status == "CANCELED":
        job._close_backup("CANCELED")
        job._finish("CANCELED")
    elif rc == 0:
        job.add_log(f"完成 · {job._files_text()} · 用时 {job._elapsed_text()}")
        job._close_backup("COMPLETE")
        job._finish("DONE", progress=100)
    else:
        err = job._timeout_err or _restore_error(rc, "\n".join(job.log_tail[-40:]),
                                                 password_provided=bool(job._env))
        job._close_backup("FAILED", error=err)
        job._finish("FAILED", error=err)


def _restore_error(rc: int, tail: str, password_provided: bool) -> str:
    """把恢复失败的退出码和输出尾部翻译成一句能照着做的话。

    「查找」与密码错误两类提示都是尽力识别：设备端返回的具体错误码还没在
    真机上确认过（idevicebackup2.c 里没有 "Find My" 字样，设备错误统一带
    MBErrorDomain 前缀但该域覆盖所有备份错误）。真机实测拿到日志后再收紧。
    """
    if re.search(r"password is required", tail, re.I):
        return f"restore 退出码 {rc}：这份备份已加密，需要输入备份密码"
    hints = []
    if re.search(r"find\s*my|查找", tail, re.I):
        hints.append("检测到「查找我的 iPhone」相关报错，请先在手机上关闭「查找」"
                     "（设置 → Apple 账户 → 查找），恢复完成后再打开")
    elif re.search(r"MBErrorDomain", tail):
        hints.append("设备端返回了备份域错误（MBErrorDomain），常见原因是未关闭「查找我的 iPhone」")
    if password_provided:
        hints.append("这份备份已加密，密码不正确也会导致恢复失败，请核对")
    if not hints:
        return f"restore 退出码 {rc}，可查看本条任务日志中的完整设备报错"
    return f"restore 退出码 {rc}：" + "；".join(hints)
