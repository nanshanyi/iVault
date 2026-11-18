#!/bin/sh
# 容器入口：拉起 usbmuxd（USB 通道 + 配对证书目录），
# 再把 netmuxd 以 shim 模式叠上去（追加 Wi-Fi 通道），最后启动 Web 服务。
set -e

mkdir -p /data/meta /data/backups /var/lib/lockdown

# 配对记录由 usbmuxd 统一管理；--config-dir 指向 compose 挂载的持久卷。
# 以 root 运行，避免降权后写不进 NAS 目录（宿主目录通常属 root 或特定 uid）。
#
# 日志直接进容器 stdout（docker logs 能看），前缀 [usbmuxd] 以区分来源。
# 之前是重定向到 /var/log/usbmuxd.log —— 容器内该目录没有持久卷，重启即丢，
# 而「USB 为什么没识别到设备」的答案恰恰只在这里。
USBMUXD=/opt/usbmuxd-stock/sbin/usbmuxd
if ! pgrep -x usbmuxd >/dev/null 2>&1; then
    VERBOSE=""
    [ "${IVAULT_USBMUXD_VERBOSE:-0}" = "1" ] && VERBOSE="-v"
    # shellcheck disable=SC2086
    "$USBMUXD" --foreground --user root --config-dir /var/lib/lockdown $VERBOSE 2>&1 \
        | sed -u 's/^/[usbmuxd] /' &
    echo "usbmuxd started · config-dir=/var/lib/lockdown"
fi

# 等待 usbmuxd socket 就绪，避免首次配对时 idevice_id 报 "no device"
for _ in $(seq 1 25); do
    [ -S /var/run/usbmuxd ] && break
    sleep 0.2
done

# ---------- 无线通道 ----------
# netmuxd 在 shim 模式下监听另一个 socket：USB 请求转给上面的 usbmuxd，
# 同时用 mDNS 发现局域网里开着「通过 Wi-Fi 同步」的已配对 iPhone。
# 之后所有 idevice_* 工具都连这个 socket，就能同时看到 USB 与无线设备。
#
# 注意：mDNS 依赖局域网组播，默认 bridge 网络收不到，无线备份需用 host 网络
# 部署（见 docker-compose.wifi.yml）。收不到也不影响 USB 备份。
WIFI_SOCKET=/var/run/usbmuxd-net
if [ "${IVAULT_WIFI:-1}" != "0" ] && command -v netmuxd >/dev/null 2>&1; then
    rm -f "$WIFI_SOCKET"
    RUST_LOG="${IVAULT_WIFI_LOG:-warn}" netmuxd \
        --socket-path "$WIFI_SOCKET" \
        --upstream-usbmuxd /var/run/usbmuxd \
        2>&1 | sed -u 's/^/[netmuxd] /' &
    for _ in $(seq 1 20); do
        [ -S "$WIFI_SOCKET" ] && break
        sleep 0.2
    done
    # 用 pgrep 判断存活：上面是管道，$! 拿到的是 sed 的 pid，不是 netmuxd
    if [ -S "$WIFI_SOCKET" ] && pgrep -x netmuxd >/dev/null 2>&1; then
        export USBMUXD_SOCKET_ADDRESS="$WIFI_SOCKET"
        export IVAULT_WIFI=1
        echo "netmuxd started · socket=$WIFI_SOCKET · 无线通道已启用（日志前缀 [netmuxd]）"
    else
        # 起不来就退回纯 USB，绝不因为无线把主流程搞挂
        echo "⚠ netmuxd 未能启动，回落到 USB-only；原因见上方 [netmuxd] 开头的日志"
        export IVAULT_WIFI=0
    fi
else
    export IVAULT_WIFI=0
    echo "无线通道已关闭（IVAULT_WIFI=0 或镜像内无 netmuxd）"
fi

echo "iVault starting → http://0.0.0.0:${IVAULT_PORT:-8666}"
# --no-access-log：前端每 6 秒 4 个轮询请求，开 access log 一天五万多条，
# 真报错会被淹掉。改用 backend/main.py 里的中间件，只记出错、写操作与慢请求。
exec python3 -m uvicorn backend.main:app \
    --host 0.0.0.0 --port "${IVAULT_PORT:-8666}" \
    --log-level "${IVAULT_LOG:-INFO}" --no-access-log
