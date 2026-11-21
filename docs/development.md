# 开发与构建

[← 返回 README](../README.md)

## 工作原理

```
iPhone ──USB──► /dev/bus/usb ──► usbmuxd ──┐
iPhone ──Wi-Fi──► mDNS ──► netmuxd ───────┴──► 统一 usbmuxd socket
                                                  │
浏览器 ◄──REST / SSE──► FastAPI (backend) ─────────┤
                          │                        │
                     SQLite 元数据            /data/backups/<UDID>/
                     + lockdown 证书           Apple MobileSync 备份库
```

备份协议不是本项目实现的。整机备份能力来自 [`libimobiledevice`](https://libimobiledevice.org/) 的命令行工具链，后端用 `subprocess` 调用并解析输出。自研部分是 Web 管理层、任务调度、配对向导和界面。

原理上，`usbmuxd` 只认 USB，所以容器里多跑一个 `netmuxd`（shim 模式）：USB 请求转发给 `usbmuxd`，网络设备由它自己用 mDNS 发现，对外仍是同一个 socket，上层 `idevice_*` 工具完全无感。netmuxd 起不来时入口脚本会自动回落成纯 USB，不影响原有功能。

无线必须用 host 网络：mDNS 靠局域网组播发现设备，bridge 的独立 netns 收不到（实测容器只能收到 Docker 网关 172.27.0.1 的报文，局域网里 iPhone 的广播进不来）。

## 目录结构

```
backend/
  main.py     REST API、SSE 流、设备轮询、电量采样、定时调度
  device.py   idevice_id / ideviceinfo / idevicepair 封装 + 配对向导状态机
  jobs.py     idevicebackup2 子进程管理、进度解析、取消
  store.py    SQLite（devices / backups / settings）
  config.py   路径与工具链配置
frontend/
  index.html  单页界面（内联 CSS，零依赖）
  app.js      渲染、REST、SSE、配对向导、主题切换
scripts/
  entrypoint.sh    启动 usbmuxd + netmuxd（无线）+ uvicorn
  usb-watchdog.py  USB 热插拔看护：插线后重启 usbmuxd 重扫，netmuxd 掉了就拉起
```

## 从源码构建

正常部署**不需要编译**——CI 在推 tag 时构建 `linux/amd64` 镜像并推到 GHCR，NAS 上 pull 即可。只有改过代码或完全离线时才需要自己构建。

需要 `linux/arm64`（树莓派、Apple Silicon 上的 Docker）时，到 Actions 页面手动运行 `Release image`，`platforms` 填 `linux/amd64,linux/arm64`——arm64 在 GitHub 的 runner 上要走 QEMU 模拟，耗时翻倍，所以没有放进默认流程。

### 三层依赖

| 层 | 依赖 | 安装方式 |
|---|---|---|
| 前端 | 原生 HTML / CSS / JS，无 npm、无框架、无 CDN | 随镜像静态托管 |
| 后端 | `fastapi`、`uvicorn[standard]`（仅此 2 个 pip 包），其余全用标准库 | `pip install -r backend/requirements.txt` |
| 系统工具链 | `libimobiledevice`（`idevice_id` / `ideviceinfo` / `idevicepair` / `idevicebackup2`）+ `usbmuxd` | 从上游源码编译 |

### 为什么不用发行版包

Debian / Ubuntu 源里的 `libimobiledevice-utils` 还停在上游 2020-06-15 的 1.3.0，缺 iOS 17+ 支持与 `idevicebackup2` 的后续修复。所以 `Dockerfile` 按固定顺序从上游构建：

```
libplist → libimobiledevice-glue → libtatsu → libusbmuxd → libimobiledevice
```

并单独编译 `usbmuxd`（为了用 `--config-dir` 指定证书目录）。

**所有组件都钉到 40 位 commit SHA**，上游 `master` 怎么变都不影响已有镜像。升级只需改 `Dockerfile` 顶部的 `ARG *_REF`，重新构建并真机验证一次。

编译只发生在 `docker build` 阶段，产物固化进镜像层，运行时零安装、零联网。本地首次构建约 10–20 分钟；国内网络慢可设 `APT_MIRROR=mirrors.ustc.edu.cn`。

### 部署后体检

```bash
docker exec ivault cat /usr/local/share/build-revisions.txt  # 各组件编译时的 commit
docker exec ivault idevice_id -l                            # 插线后应列出 UDID
docker compose logs -f ivault | grep usbmuxd                # 守护进程是否正常
```

## 本地开发

```bash
python3 -m venv .venv && .venv/bin/pip install -r backend/requirements.txt
.venv/bin/python -m uvicorn backend.main:app --host 127.0.0.1 --port 8666
```

只做 Web / API 调试不需要系统工具链，`idevice_*` 不存在时设备相关接口会明确报错。要在本机真连 iPhone，macOS 上 `brew install libimobiledevice usbmuxd` 即可。
