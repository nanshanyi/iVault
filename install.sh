#!/usr/bin/env bash
# iVault 部署助手：在 NAS / Linux 主机上检查环境、生成 .env 并启动容器。
#
# 默认使用 CI 预构建镜像（docker compose pull），通常几十秒完成，NAS 不需要访问 GitHub 源码。
# 需要本地编译（改过代码 / 完全离线）时：IVAULT_BUILD=1 ./install.sh
set -euo pipefail
cd "$(dirname "$0")"

echo "==> iVault 部署检查"

if ! command -v docker >/dev/null 2>&1; then
    echo "✗ 未找到 docker，请先在 NAS 上安装 Docker 套件"; exit 1
fi
if ! docker compose version >/dev/null 2>&1; then
    echo "✗ 未找到 docker compose v2，请升级 Docker"; exit 1
fi
echo "✓ Docker / Compose 可用"

DEFAULT_BACKUP="/volume1/ivault/backups"
read -r -p "备份目录（NAS 绝对路径，留空使用 ${DEFAULT_BACKUP}）: " BACKUP_LOCATION
BACKUP_LOCATION="${BACKUP_LOCATION:-$DEFAULT_BACKUP}"
read -r -p "元数据目录（存放数据库与配对证书，默认 ${BACKUP_LOCATION%/backups}/data）: " META_LOCATION
META_LOCATION="${META_LOCATION:-$(dirname "$BACKUP_LOCATION")/data}"
read -r -p "Web 端口（默认 8666）: " IVAULT_PORT
IVAULT_PORT="${IVAULT_PORT:-8666}"

mkdir -p "$BACKUP_LOCATION" "$META_LOCATION" "$META_LOCATION/lockdown" || {
    echo "✗ 无法创建目录，请确认路径有效且当前用户有写权限"; exit 1
}

cat > .env <<EOF
BACKUP_LOCATION=${BACKUP_LOCATION}
META_LOCATION=${META_LOCATION}
IVAULT_PORT=${IVAULT_PORT}
# latest = 跟随最新发布；也可固定成某个版本，例如 v0.2.0
IVAULT_TAG=latest
EOF
echo "✓ 已写入 .env"

echo ""
if [ "${IVAULT_BUILD:-0}" = "1" ]; then
    echo "==> 本地构建镜像（IVAULT_BUILD=1）"
    echo "    首次约 10–20 分钟，需能从 GitHub 拉取上游源码"
    echo "    国内网络慢可加：APT_MIRROR=mirrors.ustc.edu.cn ./install.sh"
    docker compose up -d --build
else
    echo "==> 拉取预构建镜像（通常几十秒）"
    if docker compose pull ivault; then
        docker compose up -d --no-build
    else
        echo ""
        echo "⚠ 拉取失败：镜像可能尚未发布，或当前网络无法访问 ghcr.io"
        echo "  回落到本地构建，首次约 10–20 分钟，需能从 GitHub 拉取上游源码"
        echo "  国内网络慢可加：APT_MIRROR=mirrors.ustc.edu.cn ./install.sh"
        echo "  若镜像是私有的，先执行：docker login ghcr.io"
        docker compose up -d --build
    fi
fi

IP=$(hostname -I 2>/dev/null | awk '{print $1}' || echo "<NAS-IP>")
echo ""
echo "✓ iVault 已启动：http://${IP}:${IVAULT_PORT}"
echo "  下一步：用数据线把 iPhone 插到 NAS 的 USB 口，打开网页 → 设备管理 → 添加设备"
echo "  验证工具链：docker exec ivault idevice_id -l（插线后应列出 UDID）"
echo "  查看日志：docker compose logs -f ivault"
