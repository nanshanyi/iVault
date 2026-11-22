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


def _robot_payload(url: str, payload: dict) -> dict:
    """按地址识别群机器人格式。

    企业微信 / 飞书 / 钉钉的群机器人各有私有 JSON 结构，通用 payload 会被
    拒收；Bark 与自定义接收端则吃完整 payload。URL 是最可靠的判别依据，
    这样本项目不需要为每家机器人做设置项。
    """
    text = payload.get("text", "")
    if "qyapi.weixin.qq.com" in url:
        return {"msgtype": "text", "text": {"content": text}}
    if "open.feishu.cn" in url:
        return {"msg_type": "text", "content": {"text": text}}
    if "oapi.dingtalk.com" in url:
        return {"msgtype": "text", "text": {"content": text}}
    return payload


def _post(url: str, payload: dict, timeout: float = 5, label: str = ""):
    data = _robot_payload(url, payload)
    req = urllib.request.Request(
        url,
        data=json.dumps(data, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json; charset=utf-8"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read()
        log.info("webhook 已送达（HTTP %s）%s", getattr(resp, "status", "?"), label)
    # 群机器人常见「HTTP 200 + 响应体业务码失败」——只看状态码会把拒收
    # 当成功，测试按钮就会假成功。errcode 是企业微信 / 钉钉（0=成功）；
    # code 是飞书（0=成功）与 Bark（200=成功）。
    try:
        r = json.loads(raw.decode("utf-8", errors="replace"))
    except Exception:
        return
    if not isinstance(r, dict):
        return
    errcode = r.get("errcode")
    if errcode is not None and errcode != 0:
        raise RuntimeError(f"接收端返回业务错误 errcode={errcode} {r.get('errmsg') or ''}".strip())
    code = r.get("code")
    if code is not None and code not in (0, 200):
        raise RuntimeError(f"接收端返回业务错误 code={code} {r.get('msg') or ''}".strip())


def task_finished(kind: str, status: str, udid: str, *,
                  backup_id: int | None = None, error: str = "",
                  size_bytes: int | None = None,
                  duration_s: float | None = None) -> None:
    """任务收尾时调用。同步入口：在事件循环里时丢线程池执行，绝不阻塞收尾路径。"""
    url = (store.get_setting("notify_webhook") or "").strip()
    if not url:
        return
    # 通知范围：用户可选「仅失败与取消」——每天一次的成功推送对部分人是打扰。
    # 测试通知（send_test）不受此开关影响，任何时候都发得出去。
    scope = (store.get_setting("notify_scope") or "all").strip()
    if scope == "failure" and status == "DONE":
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
        # Bark 对空 body 的容忍度随服务端版本而异：取消也带一句正文最稳
        detail_parts.append("任务已被手动取消")
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
    # 日志里带上记录编号与事件，和任务日志、任务历史对得上
    tag = (("RST-" if kind == "RESTORE" else "BKP-") + f"{backup_id:04d}") if backup_id else ""
    label = f"· {tag} {payload['event']}".replace("  ", " ")
    if loop is None:
        _safe_post(url, payload, label)
        return
    # 任务收尾永远在事件循环里：走线程池，webhook 挂了也不能拖住任务收尾
    loop.run_in_executor(None, lambda: _safe_post(url, payload, label))


def passcode_needed(udid: str, tag: str, minutes: int) -> None:
    """定时备份等到了锁屏密码：提醒用户去手机上输入。

    不受「通知范围」限制：它不是结果通知，是这次备份能不能跑成的前提，
    选了「仅失败与取消」的人也需要它。
    """
    url = (store.get_setting("notify_webhook") or "").strip()
    if not url:
        return
    name = _device_label(udid)
    title = f"iVault：请在「{name}」上输入锁屏密码"
    body = f"定时备份已开始，{minutes} 分钟内没输入会自动放弃"
    payload = {
        "event": "backup.passcode", "text": f"{title}：{body}", "title": title, "body": body,
        "group": "iVault", "kind": "BACKUP", "udid": udid, "device": name, "ts": int(time.time()),
    }
    label = f"· {tag} backup.passcode"
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        _safe_post(url, payload, label)
        return
    loop.run_in_executor(None, lambda: _safe_post(url, payload, label))


def send_test(url: str):
    """发送一条测试通知。失败直接抛异常，由调用方决定如何呈现给用户。"""
    _post(url, label="· 测试通知", payload={
        "event": "test",
        "text": "iVault 测试通知：Webhook 配置成功，之后每次备份 / 恢复的结果都会推送到这里",
        "title": "iVault 测试通知",
        "body": "Webhook 配置成功，之后每次备份 / 恢复的结果都会推送到这里",
        "group": "iVault",
        "ts": int(time.time()),
    })


def _safe_post(url: str, payload: dict, label: str = ""):
    try:
        _post(url, payload, label=label)
    except Exception as e:  # noqa: BLE001
        log.warning("webhook 发送失败%s：%s", label, e)
