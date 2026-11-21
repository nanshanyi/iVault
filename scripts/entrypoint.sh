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
# 注意：mDNS 依赖局域网组播，bridge 网络收不到，所以默认部署用 host 网络
# （docker-compose.yml）；纯 USB 的 bridge 版本见 docker-compose.usb.yml。
# 收不到组播也不影响 USB 备份。
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
        # 必须带 "UNIX:" 前缀，且是大写。libusbmuxd 的 connect_usbmuxd_socket()
        # 只认两种写法：strncmp 比较的 "UNIX:<路径>"，或 "<host>:<port>"。
        # 裸路径两个分支都不匹配，会**静默回落**到 /var/run/usbmuxd —— 客户端
        # 于是拿到原生 usbmuxd 的设备表：USB 正常，Wi-Fi 设备永远为空，且不报
        # 任何错。排查时极难发现，因为工具退出码一直是 0。
        export USBMUXD_SOCKET_ADDRESS="UNIX:$WIFI_SOCKET"
        export IVAULT_WIFI=1
        # 防回归探针：把地址指向一个不存在的 socket，客户端必须失败。若这里仍
        # 返回 0，说明变量根本没被采纳（静默回落到了默认 socket）——那无线设备
        # 接下来必然全不可见，且所有 idevice_* 都不会报错，事后极难定位。
        if USBMUXD_SOCKET_ADDRESS="UNIX:/nonexistent-ivault-probe" \
                idevice_id -l >/dev/null 2>&1; then
            echo "⚠ USBMUXD_SOCKET_ADDRESS 未被采纳（已静默回落到 /var/run/usbmuxd），无线设备将不可见"
        fi
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

# 内部令牌：看护进程读 /api/status 时用它代替登录 Cookie。每次启动重新生成，
# 只在本容器的进程环境里，不落盘、不打印。不能按来源地址放行：
# 套反向代理后，外网请求到达 uvicorn 时来源同样是 127.0.0.1。
IVAULT_INTERNAL_TOKEN=$(python3 -c 'import secrets; print(secrets.token_hex(32))')
export IVAULT_INTERNAL_TOKEN

# ---------- USB 热插拔看护 ----------
# 容器里的 usbmuxd 收不到 USB 热插拔事件：libusb 的 hotplug 订阅的是 udevd 的
# 转发组，而容器内没有 udevd、宿主 udevd 也不跨 netns 转发。结果就是设备表在
# 启动瞬间冻结 —— 容器运行中插 iPhone 永远认不到，只有重启进程才能重新枚举。
# 看护监听 /dev/bus/usb 的节点创建事件，确认「总线上有苹果设备但 usbmuxd 没列出」
# 后重启 usbmuxd 重扫，代价是插入后约 1 秒才在页面出现。详见 scripts/usb-watchdog.py。
# 必须用后台启动：它会阻塞在事件循环里，且异常退出不影响主服务。
if [ "${IVAULT_USB_WATCHDOG:-1}" != "0" ] && [ -f /app/usb-watchdog.py ]; then
    python3 /app/usb-watchdog.py &
    echo "usb-watchdog started · 插拔 iPhone 自动重扫（IVAULT_USB_WATCHDOG=0 可关闭）"
fi

# 日志级别统一成小写给 uvicorn：它的 --log-level 只接受小写枚举
# (critical/error/warning/info/debug/trace)，传大写 "INFO" 会被 click 拒绝并启动失败。
# Python 侧不用迁就：config.normalize_log_level 会把同一个值规范成大写给 logging。
IVAULT_LOG=$(printf '%s' "${IVAULT_LOG:-INFO}" | tr '[:upper:]' '[:lower:]')
case "$IVAULT_LOG" in
    critical|error|warning|warn|info|debug|notset) ;;
    *) echo "⚠ IVAULT_LOG='${IVAULT_LOG}' 不是合法级别，已回落到 info"; IVAULT_LOG=info ;;
esac
# WARN -> warning；NOTSET 不是 uvicorn 的合法级别，回落到 info
[ "$IVAULT_LOG" = "warn" ] && IVAULT_LOG=warning
[ "$IVAULT_LOG" = "notset" ] && IVAULT_LOG=info
export IVAULT_LOG

# --no-access-log：前端每 6 秒 5 个轮询请求，开 access log 一天七万多条，
# 真报错会被淹掉。改用 backend/main.py 里的中间件，只记出错、写操作与慢请求。
exec python3 -m uvicorn backend.main:app \
    --host 0.0.0.0 --port "${IVAULT_PORT:-8666}" \
    --log-level "$IVAULT_LOG" --no-access-log
