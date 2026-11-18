#!/bin/sh
# 容器入口：拉起 usbmuxd（USB 通道 + 配对证书目录），然后启动 iVault Web 服务。
set -e

mkdir -p /data/meta /data/backups /var/lib/lockdown

# 配对记录由 usbmuxd 统一管理；--config-dir 指向 compose 挂载的持久卷。
# 以 root 运行，避免降权后写不进 NAS 目录（宿主目录通常属 root 或特定 uid）。
USBMUXD=/opt/usbmuxd-stock/sbin/usbmuxd
if ! pgrep -x usbmuxd >/dev/null 2>&1; then
    "$USBMUXD" --foreground --user root --config-dir /var/lib/lockdown -v \
        >/var/log/usbmuxd.log 2>&1 &
    echo "usbmuxd started (pid $!) · config-dir=/var/lib/lockdown"
fi

# 等待 usbmuxd socket 就绪，避免首次配对时 idevice_id 报 "no device"
for _ in $(seq 1 25); do
    [ -S /var/run/usbmuxd ] && break
    sleep 0.2
done

echo "iVault starting → http://0.0.0.0:8666"
exec python3 -m uvicorn backend.main:app \
    --host 0.0.0.0 --port 8666 --log-level "${IVAULT_LOG:-INFO}"
