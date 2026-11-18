#!/bin/sh
# 容器入口：拉起 usbmuxd（USB 设备通道），然后启动 iVault Web 服务。
set -e

mkdir -p /data/meta /data/meta/lockdown /data/backups

# usbmuxd 需要访问 /dev/bus/usb；compose 已开启 privileged + 设备映射。
# -f 前台运行（由本脚本置于后台），-v 输出日志便于排查。
if ! pgrep -x usbmuxd >/dev/null 2>&1; then
    usbmuxd -f -v >/var/log/usbmuxd.log 2>&1 &
    echo "usbmuxd started (pid $!)"
fi

# 等待 usbmuxd socket 就绪，避免首次配对时 idevice_id 报 "no device"
for _ in $(seq 1 20); do
    [ -S /var/run/usbmuxd ] && break
    sleep 0.2
done

echo "iVault starting → http://0.0.0.0:8666"
exec python3 -m uvicorn backend.main:app \
    --host 0.0.0.0 --port 8666 --log-level "${IVAULT_LOG:-INFO}"
