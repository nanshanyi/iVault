"""iVault 全局配置。"""
import os

# 容器内固定路径（compose 已挂载持久卷）
DATA_DIR = os.environ.get("IVAULT_DATA", "/data/meta")
BACKUP_DIR = os.environ.get("IVAULT_BACKUPS", "/data/backups")
DB_PATH = os.path.join(DATA_DIR, "ivault.db")
LOCKDOWN_DIR = os.environ.get("IVAULT_LOCKDOWN", "/data/meta/lockdown")

HOST = os.environ.get("IVAULT_HOST", "0.0.0.0")
PORT = int(os.environ.get("IVAULT_PORT", "8666"))
LOG_LEVEL = os.environ.get("IVAULT_LOG", "INFO")

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
