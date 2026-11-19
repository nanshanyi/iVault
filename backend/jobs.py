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
import datetime
import logging
import os
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
FILE_PROG_RE = re.compile(r"\d{1,3}(?:\.\d+)?%\s+[\d.]+\s*[KMGT]i?B\s*/")
# 每切换一个文件就会刷一遍的三类行：3.5 万个文件足以刷出几万行日志。
# 它们只是「活动迹象」，隔几秒留一条就够，不必逐条入库。
NOISE_RE = re.compile(r"^(Status\s|Receiving\s|Sending\s)")
# 报错行必须无条件保留：真实输出里错误常常粘在 Status 行后面
# （`Status Receiving filesERROR: Could not receive from mobilebackup2 (-256)`），
# 如果先按前缀判噪声，最有用的那行就没了。
ERROR_RE = re.compile(r"(?i)\b(error|fatal|failed|failure|abort\w*|cannot|could not|denied|invalid|refused)\b")
# 终端控制序列（清屏、光标上移等）：落库和前端都不该看到这些乱码
ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[a-zA-Z]|\x1b\][^\x07]*\x07")

# 单行上限：进度条很密时不必完整保留，截断即可（日志是给人看的）。
LINE_MAX = 400
# 进度事件节流：两次广播至少间隔这么久，避免每毫秒一条把 SSE 和前端压垮。
PROGRESS_MIN_INTERVAL = 0.4
# 整机进度写进终端日志的最小间隔（界面上的进度环另有实时数据，日志只是留痕）。
PROGRESS_LOG_INTERVAL = 5.0
# 文件级噪声行的最小间隔（它们只是「还在动」的证据，10 秒一条足够）。
NOISE_LOG_INTERVAL = 10.0
# 心跳：精简档不再记录文件级噪声，长时间没有任何输出时补一条，
# 用来区分「慢慢跑」和「彻底卡死」。
HEARTBEAT_INTERVAL = 300

STREAM_LIMIT = 1 << 20

# 活跃任务表：job_id -> Job
_jobs: dict[str, "Job"] = {}


class DeviceBusy(RuntimeError):
    """同一台设备同时只允许一个任务。

    备份与恢复都会动设备端的数据，而备份目录又是同一份可写状态（增量备份
    靠在同一目录里追加），并发跑不只会互相拖慢，还可能把备份目录写坏。
    """

    def __init__(self, job: "Job"):
        self.job = job
        kind = "恢复" if job.kind == "RESTORE" else "备份"
        super().__init__(f"该设备已有进行中的{kind}任务")


class Job:
    def __init__(self, job_id: str, udid: str, kind: str = "BACKUP", backup_id: int | None = None):
        self.id = job_id
        self.udid = udid
        self.kind = kind                      # BACKUP | RESTORE
        self.backup_id = backup_id
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
        self._last_logged_pct = -1            # 精简档：上一次写进日志的整机百分比
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
        self._last_log_ts = time.time()
        self.log_tail.append(line)
        if len(self.log_tail) > 400:
            self.log_tail.pop(0)
        # 落库版本带时间戳：事后要看「哪一步卡了多久」，没有时间就没法判断
        self.log_stamped.append(datetime.datetime.now().strftime("%H:%M:%S ") + line)
        if len(self.log_stamped) > 2000:
            self.log_stamped.pop(0)
        self.broadcast({"type": "log", "line": line})

    def _verbose(self) -> bool:
        """日志详细度：默认精简（约 100 行/次，与备份时长无关），可切详细。"""
        if self._verbose_cache is None:
            try:
                self._verbose_cache = (store.get_setting("log_verbose") or "") == "true"
            except Exception:  # noqa: BLE001  设置读不到就按精简走
                self._verbose_cache = False
        return self._verbose_cache

    def _emit(self, seg: str):
        """处理子进程输出的一段：整机进度单独识别，其余进日志。

        idevicebackup2 的进度条一秒钟能刷几十次，原样转发会把 SSE 和前端
        压垮，所以进度事件按 PROGRESS_MIN_INTERVAL 节流，日志每两秒留一条。
        """
        line = ANSI_RE.sub("", seg).strip()
        if not line:
            return
        if len(line) > LINE_MAX:
            line = line[:LINE_MAX] + " …"
        now = time.time()

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
            if pct is not None:
                # 只增不减：分段输出混排时数字不会来回跳。
                # 上限压到 99%：idevicebackup2 在收尾（包括中途放弃）时会先打一行
                # 100%，只有真正成功才由 _finish 置 100，避免失败的任务显示成满格。
                self.progress = max(self.progress, min(99, pct))
                self.phase = "正在传输数据"
                if now - self._last_prog_ts >= PROGRESS_MIN_INTERVAL:
                    self._last_prog_ts = now
                    self.broadcast({"type": "progress", "progress": self.progress,
                                    "phase": self.phase})
                # 精简档：只在百分比真的变了才留一行 —— 整机进度只有 100 个刻度，
                # 所以一次备份最多约 100 行，跟跑 1 小时还是 5 小时无关。
                # 详细档：按时间留痕，便于看出「哪一段慢」。
                if self._verbose():
                    if now - self._last_prog_log_ts >= PROGRESS_LOG_INTERVAL:
                        self._last_prog_log_ts = now
                        self.add_log(line)
                elif pct != self._last_logged_pct:
                    self._last_logged_pct = pct
                    self.add_log(line)
                return

        # 文件级噪声（切换文件、单文件进度条）：精简档直接丢弃——3.5 万个文件
        # 就是几万行，它们对事后排查没有价值（真卡住时有心跳兜住）；
        # 详细档稀疏留痕。
        if NOISE_RE.search(line) or FILE_PROG_RE.search(line):
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
            await asyncio.sleep(30)
            if self.finalized:
                return
            idle = time.time() - self._last_log_ts
            if idle >= HEARTBEAT_INTERVAL:
                self.add_log(f"仍在传输中，当前 {self.progress}%"
                             f"（已 {int(idle // 60)} 分钟无新输出）")

    def _log_text(self, header: str = "") -> str:
        body = "\n".join(self.log_stamped)
        return (header + "\n" + body) if header else body

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
        except ProcessLookupError:
            return
        try:
            await asyncio.wait_for(proc.wait(), timeout=grace)
            return
        except Exception:  # 超时或已被回收，都走强杀
            pass
        try:
            proc.kill()
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
        self.add_log(f"$ {' '.join(args)}")
        if self._cancel_requested:
            # 还没起进程就被取消：连备份记录都不该开始写
            return -1
        try:
            self.proc = await asyncio.create_subprocess_exec(
                *args,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                limit=STREAM_LIMIT,
            )
        except FileNotFoundError as e:
            raise RuntimeError(f"找不到 {args[0]}：{e}") from e
        # 取消可能就发生在上面的 await 里：进程刚起来也要立刻收掉，
        # 否则「已取消」的任务会在后台把整轮备份跑完。
        if self._cancel_requested:
            await self._kill_proc()
            return -1
        try:
            assert self.proc.stdout
            buf = ""
            hb = asyncio.create_task(self._heartbeat())
            # 用 read(n) 而不是 async for（后者按 \n 分帧）：进度条只发 \r，
            # 按行读会憋成一条巨型字符串，前端一次插入几 MB HTML 直接卡死，
            # 超长行还会触发 asyncio 的行长上限、把任务拽出循环。
            while True:
                data = await self.proc.stdout.read(8192)
                if not data:
                    break
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
        prior = store.list_backups(self.udid, limit=50)
        backup_type = "INCREMENTAL" if any(
            s["status"] == "COMPLETE" for s in prior
        ) else "FULL"

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
            size = await _dir_size(dev_dir)
            self._close_backup("COMPLETE", size_bytes=size, backup_type=backup_type)
            self._finish("DONE", progress=100, size=size, backup_id=self.backup_id)
        else:
            err = f"idevicebackup2 退出码 {rc}"
            self._close_backup("FAILED", error=err)
            self._finish("FAILED", error=err)

    async def cancel(self):
        if self.finalized or self.status != "RUNNING":
            return
        # 先立旗再动进程：取消有可能早于 create_subprocess_exec（握手前的
        # 建目录、建备份记录都是同步操作，但那一步的 await 仍在）。只改状态的话，
        # 运行的协程看不见旗子，照样会把整轮备份跑完。
        self._cancel_requested = True
        self.status = "CANCELED"
        await self._kill_proc()


def _disk_free_bytes(path: str) -> int:
    """备份盘剩余字节数；拿不到时返回 -1（跳过检查，不因为 stat 失败拦备份）。"""
    try:
        import shutil
        return shutil.disk_usage(path).free
    except OSError:
        return -1


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
    """某台设备当前占用者；没有则 None。"""
    return next((j for j in _jobs.values() if j.udid == udid), None)


def active_jobs() -> list[dict]:
    return [
        {"id": j.id, "udid": j.udid, "kind": j.kind, "status": j.status,
         "progress": j.progress, "phase": j.phase, "started_at": j.started_at}
        for j in _jobs.values() if j.status == "RUNNING"
    ]


def _claim(udid: str) -> None:
    """设备级互斥的唯一判定点。

    检查与登记之间不能有 await，否则两个并发请求会双双通过检查；
    调用方（HTTP 端点、调度器）只需接住 DeviceBusy。
    """
    busy = busy_job(udid)
    if busy is not None:
        raise DeviceBusy(busy)


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


def start_backup(udid: str, job_id: str) -> Job:
    _claim(udid)
    job = Job(job_id, udid, kind="BACKUP")
    _register(job)
    job.task = asyncio.create_task(job.run_backup())
    job.task.add_done_callback(lambda t: _on_done(job, t))
    return job


def start_restore(udid: str, job_id: str, source_udid: str | None = None):
    """恢复最近一次成功完成的备份到当前设备。

    注意：增量备份依赖基线目录，idevicebackup2 restore 直接指向备份根目录
    即可由其内部链接解析完整数据。
    """
    _claim(udid)
    job = Job(job_id, udid, kind="RESTORE")
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
    rc = await job._pump([
        config.BIN_IDEVICEBACKUP2, *device.net_args(job.udid), "-u", job.udid,
        "restore", "--system", "--settings", config.BACKUP_DIR,
    ])
    if job._cancel_requested or job.status == "CANCELED":
        job._finish("CANCELED")
    elif rc == 0:
        job._finish("DONE", progress=100)
    else:
        err = f"restore 退出码 {rc}"
        # 「查找我的 iPhone」是恢复最常见的失败原因，但 idevicebackup2.c 里
        # 并没有 "Find My" 字样，能否命中取决于设备返回的错误描述，属尽力识别。
        # 设备端错误统一带 MBErrorDomain 前缀，但该域覆盖所有备份域错误，
        # 只能作为软提示；具体哪个错误码对应未关「查找」，需在真机上
        # 开着「查找」实测一次恢复，从日志确认后再收紧判断。
        tail = "\n".join(job.log_tail[-40:])
        if re.search(r"find\s*my|查找", tail, re.I):
            err += ("：检测到「查找我的 iPhone」相关报错。"
                    "请先在手机上关闭「查找」（设置 → Apple 账户 → 查找），恢复完成后再打开，然后重试")
        elif re.search(r"MBErrorDomain", tail):
            err += ("：设备端返回了备份域错误（MBErrorDomain）。"
                    "常见原因是未关闭「查找我的 iPhone」，请先关闭再重试；"
                    "若仍失败，可查看本条记录日志中的完整设备报错")
        job._finish("FAILED", error=err)
