# iVault 运行镜像（多阶段构建）
#
# 为什么不用发行版包：Ubuntu 24.04 / Debian 源里的 libimobiledevice-utils 是
# 上游 2020-06-15 发布的 1.3.0，缺少 iOS 17+ 支持与 idevicebackup2 的后续修复。
# 因此从上游源码构建整套工具链；构建顺序参考已验证的 ios-backup-station Dockerfile。
# 国内网络慢时可用 APT_MIRROR=mirrors.ustc.edu.cn 换 apt 源（见 docker-compose.yml）。

# ---------- Stage 1: 编译 libimobiledevice 全栈 ----------
FROM debian:trixie-slim AS build
ENV DEBIAN_FRONTEND=noninteractive
ARG APT_MIRROR=
RUN if [ -n "$APT_MIRROR" ]; then sed -i "s|deb.debian.org|${APT_MIRROR}|g" /etc/apt/sources.list.d/debian.sources; fi
RUN apt-get update && apt-get install -y --no-install-recommends \
      build-essential git ca-certificates autoconf automake libtool pkg-config \
      libusb-1.0-0-dev libavahi-client-dev libssl-dev libreadline-dev libcurl4-openssl-dev \
 && rm -rf /var/lib/apt/lists/*

ENV PKG_CONFIG_PATH=/usr/local/lib/pkgconfig \
    LD_LIBRARY_PATH=/usr/local/lib \
    CPPFLAGS=-I/usr/local/include \
    LDFLAGS=-L/usr/local/lib
WORKDIR /src

# 依赖顺序固定，不可调换；--depth 1 足够（无需 libgeneral / usbmuxd2 的 commit 计数）
RUN set -eux; \
    build() { \
      name="$1"; url="$2"; shift 2; \
      git clone --depth 1 --recursive "$url" "$name"; \
      cd "$name"; \
      ./autogen.sh --prefix=/usr/local "$@"; \
      make -j"$(nproc)"; make install; ldconfig; \
      echo "$name $(git rev-parse --short HEAD)" >> /usr/local/share/build-revisions.txt; \
      cd ..; \
    }; \
    mkdir -p /usr/local/share; \
    build libplist              https://github.com/libimobiledevice/libplist.git --without-cython; \
    build libimobiledevice-glue https://github.com/libimobiledevice/libimobiledevice-glue.git; \
    build libtatsu              https://github.com/libimobiledevice/libtatsu.git; \
    build libusbmuxd            https://github.com/libimobiledevice/libusbmuxd.git; \
    build libimobiledevice      https://github.com/libimobiledevice/libimobiledevice.git --without-cython

# USB 守护进程单独编译：stock usbmuxd 支持 --config-dir，可把配对证书写进持久卷。
# 固定到已验证的 commit，避免上游 master 变动导致构建失败。
ARG USBMUXD_STOCK_REV=3ded00c9985a5108cfc7591a309f9a23d57a8cba
RUN git clone --depth 1 https://github.com/libimobiledevice/usbmuxd.git usbmuxd-stock \
 && cd usbmuxd-stock && git checkout "$USBMUXD_STOCK_REV" \
 && ./autogen.sh --prefix=/opt/usbmuxd-stock --without-systemd \
 && make -j"$(nproc)" && make install \
 && echo "usbmuxd-stock $(git rev-parse --short HEAD)" >> /usr/local/share/build-revisions.txt

# ---------- Stage 2: 精简运行镜像 ----------
FROM debian:trixie-slim
ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1 \
    TZ=Asia/Shanghai
ARG APT_MIRROR=
RUN if [ -n "$APT_MIRROR" ]; then sed -i "s|deb.debian.org|${APT_MIRROR}|g" /etc/apt/sources.list.d/debian.sources; fi
RUN apt-get update && apt-get install -y --no-install-recommends \
      libusb-1.0-0 libavahi-client3 libssl3t64 libreadline8t64 libcurl4t64 \
      python3 python3-pip ca-certificates tzdata procps \
 && rm -rf /var/lib/apt/lists/*
RUN pip3 install --no-cache-dir --break-system-packages \
      'fastapi>=0.110' 'uvicorn[standard]>=0.29'

COPY --from=build /usr/local /usr/local
COPY --from=build /opt/usbmuxd-stock /opt/usbmuxd-stock
RUN echo /usr/local/lib > /etc/ld.so.conf.d/local.conf && ldconfig \
 && mkdir -p /var/lib/lockdown

WORKDIR /app
COPY backend /app/backend
COPY frontend /app/frontend
COPY scripts/entrypoint.sh /app/entrypoint.sh
RUN chmod +x /app/entrypoint.sh

EXPOSE 8666
VOLUME ["/data/backups", "/data/meta", "/var/lib/lockdown"]

ENTRYPOINT ["/app/entrypoint.sh"]
