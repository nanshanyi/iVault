"""Webhook 通知：每次备份 / 恢复任务结束时把结果推给用户配置的地址。

以前只在定时任务「触发」时发一条，但用户最需要知道的其实是
「昨晚这次备份到底成没成」。现在统一改为在任务收尾时发送：
成功、失败、取消都会推，失败带具体原因，取消也会告知。

payload 同时带 `text` 字段（人类可读摘要），Bark / 企业微信机器人等
只认 text 的接收端无需做任何改造。
"""
import asyncio
import json
import logging
import time
import urllib.request

from . import store

log = logging.getLogger("ivault.notify")


def _device_label(udid: str) -> str:
    d = store.get_device(udid)
    if not d:
        return "…" + udid[-6:]
    return d.get("name") or d.get("model") or ("…" + udid[-6:])


def _post(url: str, payload: dict, timeout: float = 5):
    req = urllib.request.Request(
        url,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json; charset=utf-8"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        log.info("webhook 已送达（HTTP %s）", getattr(resp, "status", "?"))


def task_finished(kind: str, status: str, udid: str, *,
                  backup_id: int | None = None, error: str = "",
                  size_bytes: int | None = None,
                  duration_s: float | None = None) -> None:
    """任务收尾时调用。同步入口：在事件循环里时丢线程池执行，绝不阻塞收尾路径。"""
    url = (store.get_setting("notify_webhook") or "").strip()
    if not url:
        return

    name = _device_label(udid)
    kind_cn = "备份" if kind == "BACKUP" else "恢复"
    detail_parts: list[str] = []
    if status == "DONE":
        outcome = "完成"
        # size_bytes 来自整个备份目录的统计：idevicebackup2 增量持续更新同一目录，
        # 这是「备份总大小」而不是本次新增的数据量，措辞必须如实。
        if size_bytes:
            if size_bytes >= 1024 ** 3:
                detail_parts.append(f"备份总大小 {size_bytes / 1024 ** 3:.1f} GB")
            else:
                detail_parts.append(f"备份总大小 {size_bytes / 1024 ** 2:.0f} MB")
        if duration_s:
            # 不足 1 分钟按秒显示，避免短任务出现「耗时 0 分钟」
            if duration_s < 60:
                detail_parts.append(f"耗时 {int(duration_s)} 秒")
            else:
                detail_parts.append(f"耗时 {duration_s / 60:.0f} 分钟")
    elif status == "CANCELED":
        outcome = "已取消"
    else:
        outcome = "失败"
        detail_parts.append(error or "未知原因")

    title = f"iVault：{name} {kind_cn}{outcome}"
    body = "，".join(detail_parts)
    # text = title + 详情，供只认单个文本字段的接收端（企业微信机器人等）
    text = f"{title}：{body}" if body else title

    payload = {
        "event": f"{kind.lower()}.{status.lower()}",
        "text": text,
        # Bark 只认 title/body：直接把 Webhook 地址填成
        # http://<Bark服务器>:<端口>/<DeviceKey> 即可推送
        "title": title,
        "body": body,
        "group": "iVault",
        "kind": kind,
        "status": status,
        "udid": udid,
        "device": name,
        "backup_id": backup_id,
        "error": error,
        "size_bytes": size_bytes,
        "duration_s": round(duration_s, 1) if duration_s else None,
        "ts": int(time.time()),
    }

    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None
    if loop is None:
        try:
            _post(url, payload)
        except Exception as e:  # noqa: BLE001
            log.warning("webhook 发送失败：%s", e)
        return
    # 任务收尾永远在事件循环里：走线程池，webhook 挂了也不能拖住任务收尾
    loop.run_in_executor(None, lambda: _safe_post(url, payload))


def _safe_post(url: str, payload: dict):
    try:
        _post(url, payload)
    except Exception as e:  # noqa: BLE001
        log.warning("webhook 发送失败：%s", e)
