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

# 各组件钉死到 40 位 commit SHA，保证任何机器、任何时间构建出的镜像完全一致。
# 取值 = iosbackup 项目 2026-06-19 在 NAS 真机构建通过的 master HEAD（明确支持 iPadOS/iOS 26）。
# 升级组件：改这里的 SHA，重新构建并真机验证一次即可。
ARG LIBPLIST_REF=32428abacb909988e8e960a8845a6430b17b6a60
ARG LIBGLUE_REF=da770a7687f35fbb981db4d7b47b1b032cd5c2c7
ARG LIBTATSU_REF=60a39f36d719344360ec2e87563ed43f61f0530f
ARG LIBUSBMUXD_REF=93eb168bf6b07472d17781328c21df0c60300524
ARG LIBIMD_REF=fa0f79190142bc309307967c058f89c1b36eb6b8

# 依赖顺序固定，不可调换。
# 用 init + fetch --depth 1 <SHA> 而非 clone + checkout：浅克隆只有 HEAD 一个 commit，
# 直接 checkout 其他 SHA 会报 "reference is not a tree"。
RUN set -eux; \
    build() { \
      name="$1"; url="$2"; ref="$3"; shift 3; \
      git init -q "$name"; \
      cd "$name"; \
      git remote add origin "$url"; \
      git fetch -q --depth 1 origin "$ref"; \
      git checkout -q FETCH_HEAD; \
      git submodule update -q --init --recursive >/dev/null 2>&1 || true; \
      ./autogen.sh --prefix=/usr/local "$@"; \
      make -j"$(nproc)"; make install; ldconfig; \
      echo "$name $ref" >> /usr/local/share/build-revisions.txt; \
      cd ..; \
    }; \
    mkdir -p /usr/local/share; \
    build libplist              https://github.com/libimobiledevice/libplist.git "$LIBPLIST_REF" --without-cython; \
    build libimobiledevice-glue https://github.com/libimobiledevice/libimobiledevice-glue.git "$LIBGLUE_REF"; \
    build libtatsu              https://github.com/libimobiledevice/libtatsu.git "$LIBTATSU_REF"; \
    build libusbmuxd            https://github.com/libimobiledevice/libusbmuxd.git "$LIBUSBMUXD_REF"; \
    build libimobiledevice      https://github.com/libimobiledevice/libimobiledevice.git "$LIBIMD_REF" --without-cython

# USB 守护进程单独编译：stock usbmuxd 支持 --config-dir，可把配对证书写进持久卷。
# 同样钉 commit + fetch，避免上游 master 变动导致构建失败。
ARG USBMUXD_STOCK_REV=3ded00c9985a5108cfc7591a309f9a23d57a8cba
RUN set -eux; \
    git init -q usbmuxd-stock; \
    cd usbmuxd-stock; \
    git remote add origin https://github.com/libimobiledevice/usbmuxd.git; \
    git fetch -q --depth 1 origin "$USBMUXD_STOCK_REV"; \
    git checkout -q FETCH_HEAD; \
    ./autogen.sh --prefix=/opt/usbmuxd-stock --without-systemd; \
    make -j"$(nproc)"; make install; \
    echo "usbmuxd-stock $USBMUXD_STOCK_REV" >> /usr/local/share/build-revisions.txt

# ---------- Stage 1.5: netmuxd（无线通道）----------
# usbmuxd 只认 USB；Wi-Fi 备份需要靠 mDNS 发现网络上的设备。
# netmuxd 在 shim 模式下把上面那个 usbmuxd 的 USB 设备一起代理出来，
# 再叠加网络设备，对外仍是一个标准的 usbmuxd socket —— 因此上层工具无感。
# 同样是钉 commit 构建，产物固化进镜像，运行时不联网。
FROM rust:1-bookworm AS netmuxd-build
ARG APT_MIRROR=
RUN if [ -n "$APT_MIRROR" ]; then sed -i "s|deb.debian.org|${APT_MIRROR}|g" /etc/apt/sources.list 2>/dev/null || true; fi
RUN apt-get update && apt-get install -y --no-install-recommends cmake \
 && rm -rf /var/lib/apt/lists/*
ARG NETMUXD_REF=7ce5620e2e2b738f736243a96d9f9a52a8044f90
RUN set -eux; \
    git init -q /src; \
    cd /src; \
    git remote add origin https://github.com/jkcoxson/netmuxd.git; \
    git fetch -q --depth 1 origin "$NETMUXD_REF"; \
    git checkout -q FETCH_HEAD; \
    cargo build --release --bin netmuxd; \
    install -Dm755 target/release/netmuxd /out/netmuxd

# ---------- Stage 2: 精简运行镜像 ----------
FROM debian:trixie-slim
ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1 \
    TZ=Asia/Shanghai
ARG APT_MIRROR=

# 版本元数据：CI 构建时传入 tag 名与 commit，本地构建回落到默认值
ARG IVAULT_VERSION=0.1.0
ARG IVAULT_COMMIT=unknown
ENV IVAULT_VERSION=${IVAULT_VERSION}
LABEL org.opencontainers.image.title="iVault" \
      org.opencontainers.image.version="${IVAULT_VERSION}" \
      org.opencontainers.image.revision="${IVAULT_COMMIT}" \
      org.opencontainers.image.source="https://github.com/nanshanyi/iVault" \
      org.opencontainers.image.description="iPhone / iPad 整机备份中心，任何可运行 Docker 的 NAS 均可部署"

RUN if [ -n "$APT_MIRROR" ]; then sed -i "s|deb.debian.org|${APT_MIRROR}|g" /etc/apt/sources.list.d/debian.sources; fi
RUN apt-get update && apt-get install -y --no-install-recommends \
      libusb-1.0-0 libavahi-client3 libssl3t64 libreadline8t64 libcurl4t64 \
      python3 python3-pip ca-certificates tzdata procps \
 && rm -rf /var/lib/apt/lists/*
# 版本以 backend/requirements.txt 为准（钉死），不在这里另写一份
COPY backend/requirements.txt /tmp/requirements.txt
RUN pip3 install --no-cache-dir --break-system-packages -r /tmp/requirements.txt \
 && rm /tmp/requirements.txt

COPY --from=build /usr/local /usr/local
COPY --from=build /opt/usbmuxd-stock /opt/usbmuxd-stock
COPY --from=netmuxd-build /out/netmuxd /usr/local/bin/netmuxd
RUN echo /usr/local/lib > /etc/ld.so.conf.d/local.conf && ldconfig \
 && mkdir -p /var/lib/lockdown

# iOS 的配对握手用的是 SHA1 签名，OpenSSL 3 默认拒绝。真遇到
# "invalid signature" 时把 OPENSSL_CONF 指向这个文件即可（默认不启用，避免副作用）。
RUN printf '%s\n' \
      '.include /etc/ssl/openssl.cnf' \
      '[openssl_init]' \
      'alg_section = evp_properties' \
      '[evp_properties]' \
      'rh-allow-sha1-signatures = yes' > /etc/ssl/ivault-openssl.cnf

WORKDIR /app
COPY backend /app/backend
COPY frontend /app/frontend
COPY scripts/entrypoint.sh /app/entrypoint.sh
RUN chmod +x /app/entrypoint.sh
# USB 热插拔看护：容器内收不到 uevent，靠它检测设备接入后重启 usbmuxd 重扫
COPY scripts/usb-watchdog.py /app/usb-watchdog.py

EXPOSE 8666
VOLUME ["/data/backups", "/data/meta", "/var/lib/lockdown"]

ENTRYPOINT ["/app/entrypoint.sh"]
