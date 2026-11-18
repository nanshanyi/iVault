FROM ubuntu:24.04

ENV DEBIAN_FRONTEND=noninteractive \
    TZ=Asia/Shanghai \
    PYTHONUNBUFFERED=1

# usbmuxd          : USB 设备多路复用（容器内以 privileged 运行）
# libimobiledevice : idevice_id / ideviceinfo / idevicepair / idevicebackup2
#   idevicebackup2 是 Apple MobileSync 整机备份协议的开源实现
RUN apt-get update && apt-get install -y --no-install-recommends \
        python3 python3-pip \
        usbmuxd libimobiledevice-utils libimobiledevice6 \
        ca-certificates tzdata \
    && rm -rf /var/lib/apt/lists/*

RUN pip3 install --no-cache-dir --break-system-packages \
        'fastapi>=0.110' 'uvicorn[standard]>=0.29'

WORKDIR /app
COPY backend /app/backend
COPY frontend /app/frontend
COPY scripts/entrypoint.sh /app/entrypoint.sh
RUN chmod +x /app/entrypoint.sh

EXPOSE 8666
VOLUME ["/data/backups", "/data/meta"]

ENTRYPOINT ["/app/entrypoint.sh"]
