"""iVault 全局配置。"""
import os
import sys

# 容器内固定路径（compose 已挂载持久卷）
DATA_DIR = os.environ.get("IVAULT_DATA", "/data/meta")
BACKUP_DIR = os.environ.get("IVAULT_BACKUPS", "/data/backups")
DB_PATH = os.path.join(DATA_DIR, "ivault.db")
# 配对证书目录：libimobiledevice 的配对记录由 usbmuxd 管理，
# entrypoint 用 --config-dir 指向这里；compose 把宿主机 ${META_LOCATION}/lockdown 挂到该路径。
# 注意：不要用环境变量去改 idevice_* 工具的证书路径，它们不认这类变量。
# macOS 上 Homebrew 版把证书放在 /var/db/lockdown，本地调试需要跟着走，
# 否则「取消配对」清理不到任何文件（真机验证时会误以为已经撤干净）。
_DEFAULT_LOCKDOWN = "/var/db/lockdown" if sys.platform == "darwin" else "/var/lib/lockdown"
LOCKDOWN_DIR = os.environ.get("IVAULT_LOCKDOWN", _DEFAULT_LOCKDOWN)

HOST = os.environ.get("IVAULT_HOST", "0.0.0.0")
PORT = int(os.environ.get("IVAULT_PORT", "8666"))


def normalize_log_level(value: str | None, default: str = "INFO") -> str:
    """把用户填的日志级别规范成 logging 认识的大写形式。

    logging.basicConfig(level=...) 只认 "INFO" 这类大写枚举，传 "info" 会直接抛
    ValueError —— 而它在模块导入期执行，等于一个大小写笔误就能让整个服务起不来，
    compose 里又配了 restart，于是变成无限重启。这里宽容处理：大小写、空格、
    WARN 别名都接受，认不出来就退回默认级别，绝不因为一个日志开关把服务搞挂。
    """
    level = (value or "").strip().upper()
    if level == "WARN":
        level = "WARNING"
    if level in ("CRITICAL", "ERROR", "WARNING", "INFO", "DEBUG", "NOTSET"):
        return level
    return default


LOG_LEVEL = normalize_log_level(os.environ.get("IVAULT_LOG"), "INFO")

# 无线（Wi-Fi）备份：由 entrypoint 在 netmuxd 成功启动后置为 1，
# 前端据此显示「无线通道已就绪」而不是让用户猜。
WIFI_ENABLED = os.environ.get("IVAULT_WIFI", "0") in ("1", "true", "yes", "on")

# libimobiledevice 工具链
BIN_IDEVICE_ID = os.environ.get("BIN_IDEVICE_ID", "idevice_id")
BIN_IDEVICE_INFO = os.environ.get("BIN_IDEVICE_INFO", "ideviceinfo")
BIN_IDEVICEPAIR = os.environ.get("BIN_IDEVICEPAIR", "idevicepair")
BIN_IDEVICEBACKUP2 = os.environ.get("BIN_IDEVICEBACKUP2", "idevicebackup2")
BIN_NETMUXD = os.environ.get("BIN_NETMUXD", "/usr/local/bin/netmuxd")

import logging

_log = logging.getLogger("ivault.config")
for _d in (DATA_DIR, BACKUP_DIR, LOCKDOWN_DIR):
    try:
        os.makedirs(_d, exist_ok=True)
    except OSError as e:
        # 容器卷未挂载 / 权限不足时仍允许服务启动，由 API 明确报错
        _log.warning("无法创建目录 %s: %s（请检查 compose 卷挂载）", _d, e)
