<p align="center">
  <!-- ?v= 用于破除 GitHub camo 的图片缓存：相对路径 URL 不变时会一直返回旧图，
       换图后把这个数字加一即可 -->
  <img src="assets/logo.svg?v=2" width="96" height="96" alt="iVault">
</p>

<h1 align="center">iVault</h1>

<p align="center">
  <b>把 iPhone / iPad 整机备份到自己的 NAS</b><br>
  与 Finder、iTunes 同格式 · 一次配对后无线增量
</p>

<p align="center">
  <a href="https://github.com/nanshanyi/iVault/releases"><img src="https://img.shields.io/github/v/release/nanshanyi/iVault?style=flat-square&amp;color=00c47f" alt="release"></a>
  <a href="https://github.com/nanshanyi/iVault/actions/workflows/release.yml"><img src="https://github.com/nanshanyi/iVault/actions/workflows/release.yml/badge.svg" alt="build"></a>
  <a href="LICENSE"><img src="https://img.shields.io/badge/license-MIT-00c47f?style=flat-square" alt="license"></a>
  <img src="https://img.shields.io/github/last-commit/nanshanyi/iVault?style=flat-square" alt="last commit">
</p>

<p align="center">
  <a href="https://github.com/nanshanyi/iVault/pkgs/container/ivault"><img src="https://img.shields.io/badge/image-ghcr.io%2Fnanshanyi%2Fivault-2496ED?style=flat-square&amp;logo=docker&amp;logoColor=white" alt="docker image"></a>
  <img src="https://img.shields.io/badge/platform-amd64-2496ED?style=flat-square&amp;logo=linux&amp;logoColor=white" alt="platform">
  <img src="https://img.shields.io/badge/python-3.10%2B-3776AB?style=flat-square&amp;logo=python&amp;logoColor=white" alt="python">
  <img src="https://img.shields.io/badge/iOS-17%2B-000000?style=flat-square&amp;logo=apple&amp;logoColor=white" alt="iOS">
</p>

---

手机用数据线插一次 NAS 的 USB 口完成配对，之后由 NAS 自己执行**整机备份**——全量与增量，格式与 Finder 完全相同。备份目录可以原样拷到 Mac 上，用 Finder 或 iMazing 直接恢复。

没有云、没有账号、没有订阅。数据只存在你自己的硬盘上。

**跑在任何能运行 Docker 的 NAS 上**：群晖、威联通、绿联、飞牛、TrueNAS、Unraid、树莓派或普通 Linux 主机都可以。唯一的硬要求是容器能拿到 USB 总线（`privileged` + `/dev/bus/usb` 映射）。

> 本项目与 Apple Inc. 无任何关联。

## 快速开始

```bash
git clone https://github.com/nanshanyi/iVault.git && cd iVault
chmod +x install.sh && ./install.sh
```

脚本会问你备份目录、元数据目录、端口和 Web 访问密码（强烈建议设置），然后**直接拉取预构建镜像启动**（`docker compose pull` + `up -d --no-build`），通常几十秒完成，不需要编译。

浏览器打开 `http://<NAS-IP>:8666` → 用数据线把 iPhone 插到 **NAS 机身**的 USB 口（不是电脑）→ 设备管理 → 添加设备 → 手机上点「信任」→ 立即备份。

> 备份进度到 100% 后别急着拔线，后端还要写完备份数据才算落盘，以页面上的任务状态为准。

不想用脚本的话，手写三步：

```bash
cp .env.example .env      # 填 BACKUP_LOCATION / META_LOCATION
mkdir -p /your/volume/ivault/{backups,data/lockdown}
docker compose pull && docker compose up -d --no-build
```

> 路径按你自己的卷结构填：群晖和绿联常见 `/volume1/...`，威联通是 `/share/CACHEDEV1_DATA/...`，飞牛和普通 Linux 常见 `/vol1/...`、`/mnt/disk1/...`。
>
> 部分 NAS 的 Docker 界面里除 `privileged` 外还有单独的「USB 设备直通」开关，也要打开。
>
> 首次使用前请到 GitHub 仓库 **Settings → Packages** 把 `ghcr.io/nanshanyi/ivault` 的可见性改为 Public，否则 NAS 上 pull 前要先 `docker login ghcr.io`。

## 功能

| 能力 | 说明 |
|---|---|
| USB 首次配对 | 信任授权 + 证书持久化，重装容器不用重配 |
| 全量 / 增量整机备份 | Apple MobileSync 格式，目录可直接拷到 Mac 用 Finder 恢复 |
| 实时进度 | SSE 推送进度环、速率与终端日志，断线重连不丢状态 |
| 备份记录 | 任务台账：时间、类型、大小、状态与完整日志，可删除单条、恢复到设备 |
| 定时备份 | 每天自选时刻，服务端调度 |
| Wi-Fi 无线备份 | netmuxd shim 模式，默认部署即可用（host 网络，见下文） |
| 访问密码 | `IVAULT_PASSWORD` 启用登录鉴权，HttpOnly Cookie 会话，SSE 同套保护 |
| Webhook 通知 | 每次任务结束推送，失败带原因。Bark 填 `http://Bark服务器:端口/DeviceKey`；企业微信 / 飞书 / 钉钉群机器人按地址自动匹配格式；响应体业务码非 0 视为失败（测试不假成功） |
| 设备状态 | 卡片显示电量与充电状态，低于阈值且未接电源时拒绝开始备份 |
| 失败早发现 | 备份前检查磁盘剩余空间，低于阈值拒绝开始 |
| 深浅色主题 | 跟随系统 / 手动切换 / 记忆选择 |

## 无线备份

配对一次之后，不插线也能备份。**默认部署（`docker-compose.yml`）就是 host 网络，无线开箱可用。**

**两个前提**，缺一不可：

1. 手机与 NAS 在同一局域网（mDNS 组播可达，跨 VLAN 通常不行）
2. iOS 侧已开启「通过 Wi-Fi 同步」——首次只能在**能识别这台手机的电脑**上用 Finder 或 Apple Devices 勾选，NAS 无法代劳

```bash
docker compose pull
docker compose up -d --no-build
```

默认用 `network_mode: host`：mDNS 靠局域网组播发现设备，bridge 的独立 netns 收不到（实测容器只能收到 Docker 网关 172.27.0.1 的报文，局域网里 iPhone 的广播进不来），所以无线必须 host。代价是 ports 映射失效，Web 端口直接由 `IVAULT_PORT` 决定（默认 8666）。

不想要 host（端口冲突、或在 Docker Desktop 上跑）就用纯 USB 版：

```bash
docker compose -f docker-compose.usb.yml up -d --no-build
```

它用 bridge + 端口映射，并把 `IVAULT_WIFI` 固定为 0——bridge 下 netmuxd 发现不到设备，开着只会让界面显示「无线通道已启用」却没有无线设备。

原理上，`usbmuxd` 只认 USB，所以容器里多跑一个 `netmuxd`（shim 模式）：USB 请求转发给 `usbmuxd`，网络设备由它自己用 mDNS 发现，对外仍是同一个 socket，上层 `idevice_*` 工具完全无感。netmuxd 起不来时入口脚本会自动回落成纯 USB，不影响原有功能。

界面上，设备卡片会标注 `USB` 或 `Wi-Fi`；无线设备在备份前会先探活，避免把 mDNS 残留的失效条目误显示成「在线」。

> 无线只服务**已配对过**的设备。发现不到你的手机，通常是上面第 2 条没做，或者设备正在休眠——点亮屏幕、重连一次 Wi-Fi，3 秒内会刷新。

## 首次配对的两个坑

**手机不弹「信任」窗口？拔掉数据线重新插一次。**

如果在弹窗上点过「不信任」，或者密码框超时没输入，iOS 会记住「这台电脑已被拒绝」，之后 `idevicepair` 一律直接返回 `user denied the trust dialog`，**不会再弹窗**。断电重连才能重置。配对向导能识别这个状态并明确提示拔插线，插回后自动重试。

**不要删 `META_LOCATION/lockdown`。**

配对证书由 `usbmuxd --config-dir /var/lib/lockdown` 写入，compose 已把宿主机目录挂到那里。容器删了重建，只要这个目录还在，就不用重新插线配对。

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

```
backend/
  main.py     REST API、SSE 流、设备轮询、定时调度
  device.py   idevice_id / ideviceinfo / idevicepair 封装 + 配对向导状态机
  jobs.py     idevicebackup2 子进程管理、进度解析、取消
  store.py    SQLite（devices / backups / settings）
  config.py   路径与工具链配置
frontend/
  index.html  单页界面（内联 CSS，零依赖）
  app.js      渲染、REST、SSE、配对向导、主题切换
scripts/
  entrypoint.sh  启动 usbmuxd + netmuxd（无线）+ uvicorn
```

## 配置

| 变量 | 默认 | 说明 |
|---|---|---|
| `BACKUP_LOCATION` | *必填* | 备份库在宿主机上的绝对路径 |
| `META_LOCATION` | `./data` | SQLite 与配对证书目录，**必须持久化** |
| `IVAULT_PORT` | `8666` | Web 端口 |
| `IVAULT_TAG` | `latest` | 镜像版本，可固定为 `v0.4.0` |
| `IVAULT_PASSWORD` | 空 | **强烈建议设置**：Web 访问密码，启用后所有接口需登录（见下方「安全」） |
| `IVAULT_SESSION_DAYS` | `30` | 登录会话有效期（天），1–365，非法值自动回落为 30 |
| `IVAULT_MIN_FREE_GB` | `2` | 备份前磁盘剩余空间下限（GB），低于则拒绝开始备份；`0` 关闭检查 |
| `IVAULT_MIN_BATTERY` | `20` | 备份前最低电量（%），低于此值且未接电源时拒绝开始；插线边充边备不受影响，`0` 关闭检查 |
| `IVAULT_WIFI` | `1` | `0` 关闭无线通道，只走 USB |
| `IVAULT_USB_WATCHDOG` | `1` | `0` 关闭 USB 热插拔看护（关闭后容器运行中插线不识别，需重启容器） |
| `IVAULT_OS_NAME` | 空 | 界面左下角显示的系统名，如 `DSM 7.2`；留空则自动探测，探测不到就隐藏 |
| `IVAULT_LOG` | `INFO` | 日志级别 |
| `IVAULT_BUILD` | — | 设为 `1` 时 `install.sh` 走本地编译而非拉镜像 |

界面左下角默认只显示服务状态与版本号，不猜系统名。想让它是「DSM 7.2」或「UGOS 1.21」，填 `IVAULT_OS_NAME` 最稳；也可以取消 `docker-compose.yml` 里 `/etc/os-release:/host-os-release:ro` 那行的注释。容器内的 `/etc/os-release` 是镜像自带的 Debian 信息，后端刻意不读——否则界面上会写着「Debian 13」而你的设备其实是群晖。

## 安全

iVault 能**写设备数据**（恢复会覆盖整机数据），Web 默认监听在局域网可达的 `0.0.0.0:8666`，而容器又是 `privileged` + host 网络。**强烈建议设置访问密码**：

```bash
# .env 里加一行（或在 install.sh 交互时直接填）
IVAULT_PASSWORD=你的密码
docker compose up -d     # 重启生效
```

- 启用后所有 `/api/*` 接口（含 SSE 实时推送）都需要登录，未登录访问返回 401，页面自动弹出登录浮层
- 登录态以 HttpOnly Cookie（`SameSite=Lax`）下发，默认 30 天有效（`IVAULT_SESSION_DAYS`，1–365）
- **退出登录只清除浏览器里的 Cookie**：令牌在有效期内本身仍然有效，被复制走的 Cookie 可继续使用。要让所有会话立即失效，修改 `IVAULT_PASSWORD` 并重启容器即可
- 密码支持任意字符（含中文）；登录接口带来源级限速：同一 IP 10 分钟内失败 8 次后暂时拒绝
- iVault 的 Web 走**明文 HTTP**。内网直接访问没有问题；需要从外网访问时，请使用 Tailscale / WireGuard 这类隧道组网，或 NAS 自带的反向代理套 HTTPS——**不要把 8666 端口直接映射到公网**
- 未设置该变量时服务无鉴权（旧行为），仅适合完全可信的内网环境

恢复功能自带两道前置检查：设备必须在线、必须已有成功备份；若手机没关「查找我的 iPhone」，失败原因里会直接给出解决办法。

## HTTP API

<details>
<summary>展开接口列表</summary>

```
POST   /api/login                  {password} 登录（启用 IVAULT_PASSWORD 时）
POST   /api/logout                 退出登录
GET    /api/status                 服务状态 / 在线设备 / 磁盘剩余
GET    /api/devices                已配对设备（含在线状态、备份统计）
DELETE /api/devices/{udid}         取消配对
POST   /api/pair/begin             开始配对向导
GET    /api/pair/{sid}/events      SSE：waiting → found → pairing → done / error
POST   /api/backups                {udid} 触发备份
GET    /api/jobs/{id}/events       SSE：progress / log / done / failed
POST   /api/jobs/{id}/cancel       取消任务
GET    /api/backups                备份记录列表（含 has_log / error）
GET    /api/backups/{id}/log       某次任务的完整日志（事后排查）
DELETE /api/backups/{id}           删除备份记录
POST   /api/restore                {udid} 恢复最近一份成功备份
GET/PUT /api/settings              定时、Webhook
GET    /api/diagnostics            版本 / 系统 / 设备 / 最近告警，纯文本一键复制
GET    /api/docs                   Swagger 文档
```

**取消配对是两侧的事**：设备侧靠 `idevicepair unpair` 通知 iPhone 删掉信任记录，要求设备在线；本机侧则无条件删除 `lockdown/<udid>.plist`。设备离线时只有本机侧生效，iPhone 上的信任仍在保留，接口会在 `message` 里如实说明，前端据此提示「先插上线再取消一次」。证书必须删的原因有两层：留着它 NAS 仍可免信任访问手机，而且它是 `validate` 判定「已授权」的依据，不删就永远等不到信任弹窗。

</details>

## 出问题怎么查

三处日志，覆盖三种场景：

| 场景 | 看哪里 | 保留多久 |
|---|---|---|
| 某次备份/恢复为什么失败 | 界面「备份任务 → 历史」状态旁的**日志**按钮 | 随备份记录永久保存 |
| 配对 / 无线通道为什么不工作 | `docker compose logs ivault`，找 `[usbmuxd]` `[netmuxd]` 前缀 | 容器日志，10MB×3 自动轮转 |
| 说不清哪里不对 | 设置 → 关于 → **诊断信息 → 复制** | 内存里最近 200 条告警 |

细节：

- **任务日志落库**。`idevicebackup2` 的输出（含时间戳与完整命令行）在任务结束时写进 `backups.log`，只留末尾 24KB。以前这些只活在内存里，任务一结束就没了，事后只能看到一条 FAILED。
- **容器日志一处看全**。`usbmuxd` 和 `netmuxd` 不再写 `/var/log/*.log`（容器里没有持久卷，重启即丢），而是带前缀直接进 stdout。想看 usbmuxd 的详细过程：`IVAULT_USBMUXD_VERBOSE=1`。
- **轮询不再刷屏**。前端每 6 秒刷新会打 4 个请求，一天五万多条；`uvicorn` 的 access log 已关掉，改由中间件只记**出错、写操作、超过 1.5 秒的请求**。
- **诊断信息**是纯文本，含版本、系统、运行时长、设备与连接方式、最近备份记录与失败原因、最近 200 条告警。留一份在手上，过几天回来排查也够用。

### 插上 iPhone 却一直显示离线

容器内的 `usbmuxd` 收不到 USB 热插拔事件（`libusb` 的 hotplug 订阅的是 `udevd` 的转发组，容器里没有 `udevd`，宿主也不会跨 netns 转发），设备表在容器启动瞬间就冻结了。镜像里的 `scripts/usb-watchdog.py` 负责兜底：检测到设备接入就重启 `usbmuxd` 重扫，所以**插线后要等 1~2 秒**页面才会变在线。

按现象分三种情况：

| 现象 | 原因 | 处理 |
|---|---|---|
| 插线后 1~2 秒自己变在线 | 正常，看护在重启 `usbmuxd` 重扫 | 无需处理 |
| 日志有 `[usb-watchdog] ... 重新扫描`，紧接着 `libusb couldn't open USB device ... errno=2` | compose 里用 `devices:` 挂的 USB 总线，那是**容器创建时的节点备份记录**，插拔后新节点在容器内不存在 | 改成 `volumes: - /dev/bus/usb:/dev/bus/usb` |
| 拔线后手机仍在广播 mDNS，但页面显示离线 | 无线通道探活失败 | 见下一节「无线（Wi-Fi）设备一直不出现」 |

看护由 `IVAULT_USB_WATCHDOG` 控制，默认开启；设成 `0` 会回到旧行为（容器运行中插线不识别，需要重启容器）。

### 无线（Wi-Fi）设备一直不出现

先分清两件事：手机**有没有广播**，和**本机客户端连的是谁**。

```sh
# 1) 手机侧：应在同一局域网内看到 _apple-mobdev2._tcp 记录（含你的 iPhone）
dns-sd -B _apple-mobdev2._tcp local.        # macOS；Linux 用 avahi-browse -rt _apple-mobdev2._tcp
nc -z <手机IP> 62078                        # lockdownd 端口可达

# 2) 本机侧：设备列表里应能看到网络设备
idevice_id -n
```

查看 `[netmuxd]` 日志里的 `Adding network device <UDID>`：有这一行说明发现成功，问题在客户端侧。

**最常见的坑：`USBMUXD_SOCKET_ADDRESS` 少了 `UNIX:` 前缀。** `libusbmuxd` 的 `connect_usbmuxd_socket()` 只认两种写法——`UNIX:<路径>`（大写，`strncmp` 比较）和 `<host>:<port>`。裸路径 `/var/run/usbmuxd-net` 两个分支都不匹配，会**静默回落**到 `/var/run/usbmuxd`：于是所有 `idevice_*` 拿到的是原生 `usbmuxd` 的设备表，USB 一切正常、Wi-Fi 设备永远为空，而且**退出码恒为 0、不报任何错**。`entrypoint.sh` 里已按 `UNIX:$WIFI_SOCKET` 导出，并带一条防回归探针（指向不存在的 socket 若仍返回 0 就告警）。

三条自查，按顺序排掉：

| 检查 | 期望 | 不符合说明 |
|---|---|---|
| `echo $USBMUXD_SOCKET_ADDRESS` | `UNIX:/var/run/usbmuxd-net` | 缺前缀 → 客户端连错 socket |
| `idevice_id -l`（unset 该变量） | 插线时能列出 USB 设备 | 工具链或 usbmuxd 有问题 |
| `idevice_id -n` | 无线在线时列出 UDID | 需先看 `[netmuxd]` 是否 `Adding network device` |

设备发现成功后仍需通过探活：`idevicepair -n -u <UDID> validate` 返回 0 才算在线。失败说明手机锁屏休眠、已离开局域网，或配对证书被清掉。

## 从源码构建

正常部署**不需要编译**——CI 在推 tag 时构建 `linux/amd64` 镜像并推到 GHCR，NAS 上 pull 即可。只有改过代码或完全离线时才需要自己构建。

需要 `linux/arm64`（树莓派、Apple Silicon 上的 Docker）时，到 Actions 页面手动运行 `Release image`，`platforms` 填 `linux/amd64,linux/arm64`——arm64 在 GitHub 的 runner 上要走 QEMU 模拟，耗时翻倍，所以没有放进默认流程。

<details>
<summary>展开：依赖构成与编译细节</summary>

**三层依赖**

| 层 | 依赖 | 安装方式 |
|---|---|---|
| 前端 | 原生 HTML / CSS / JS，无 npm、无框架、无 CDN | 随镜像静态托管 |
| 后端 | `fastapi`、`uvicorn[standard]`（仅此 2 个 pip 包），其余全用标准库 | `pip install -r backend/requirements.txt` |
| 系统工具链 | `libimobiledevice`（`idevice_id` / `ideviceinfo` / `idevicepair` / `idevicebackup2`）+ `usbmuxd` | 从上游源码编译 |

**为什么不用发行版包**

Debian / Ubuntu 源里的 `libimobiledevice-utils` 还停在上游 2020-06-15 的 1.3.0，缺 iOS 17+ 支持与 `idevicebackup2` 的后续修复。所以 `Dockerfile` 按固定顺序从上游构建：

```
libplist → libimobiledevice-glue → libtatsu → libusbmuxd → libimobiledevice
```

并单独编译 `usbmuxd`（为了用 `--config-dir` 指定证书目录）。

**所有组件都钉到 40 位 commit SHA**，上游 `master` 怎么变都不影响已有镜像。升级只需改 `Dockerfile` 顶部的 `ARG *_REF`，重新构建并真机验证一次。

编译只发生在 `docker build` 阶段，产物固化进镜像层，运行时零安装、零联网。本地首次构建约 10–20 分钟；国内网络慢可设 `APT_MIRROR=mirrors.ustc.edu.cn`。

**部署后体检**

```bash
docker exec ivault cat /usr/local/share/build-revisions.txt  # 各组件编译时的 commit
docker exec ivault idevice_id -l                            # 插线后应列出 UDID
docker compose logs -f ivault | grep usbmuxd                # 守护进程是否正常
```

</details>

## 本地开发

```bash
python3 -m venv .venv && .venv/bin/pip install -r backend/requirements.txt
.venv/bin/python -m uvicorn backend.main:app --host 127.0.0.1 --port 8666
```

只做 Web / API 调试不需要系统工具链，`idevice_*` 不存在时设备相关接口会明确报错。要在本机真连 iPhone，macOS 上 `brew install libimobiledevice usbmuxd` 即可。

## 已知限制

1. **备份是单目录覆盖模式，没有快照机制**：`idevicebackup2` 始终更新同一个 `<UDID>/` 目录，新备份覆写旧状态，磁盘上只有「最新一次可恢复状态」，不能回滚到历史时间点。备份库里的历史记录只是任务台账，删记录不会释放空间。中途失败的增量备份可能让这份唯一状态处于不一致，需重新跑一次成功备份修复。
2. **未加密备份不含钥匙串密码、Wi-Fi 设置、健康数据、通话记录**。需要这些请在 iPhone 侧开启加密备份并牢记密码——密码不会写到 NAS 上。
3. **无线备份的两个硬边界**：首次配对必须插线（iPhone 不支持无线配对，这是 `idevicepair` 的限制）；iOS 侧的「通过 Wi-Fi 同步」只能由电脑端打开一次。
4. **平台差异只在 Docker 外壳**：需要 `privileged` 与 `/dev/bus/usb`——且必须用 `volumes:` 做 bind mount，写 `devices:` 会让热插拔失效（见「出问题怎么查」）。若系统策略禁止 privileged，可改装宿主机 `usbmuxd` 并把 socket 挂进容器。
5. **验证状态**：配对链路已在真机验证（iPhone Xs / iOS 18.7.12，绿联 DXP4800 / UGOS 1.21），含「未弹窗需拔插线」的排障路径；容器 USB 透传与热插拔看护已在 DXP4800 + iPhone 15 / iOS 27.3 实测（拔线重插后约 1 秒自动恢复在线）；无线通道的命令层已在 macOS 实测（`-n` 发现、探活、失效条目过滤），容器内 mDNS 发现（`[netmuxd] Adding network device`）也已实测。**`idevicebackup2` 落盘尚未实机联调**，其他 NAS 平台也未实机测试。

## 致谢

设计与构建参考了这些开源实现（借鉴思路与构建顺序，未复制应用代码）：

- [razeencheng/iosbackup](https://github.com/razeencheng/iosbackup) — Go 实现，定时 / Wi-Fi / 通知
- [JerryGHT04/ios-backup-station](https://github.com/JerryGHT04/ios-backup-station) — 飞牛 fnOS 社区项目；本项目的源码编译顺序与 `usbmuxd --config-dir` 用法参考其 Stage 1
- [libimobiledevice](https://libimobiledevice.org/) — iOS 协议开源实现，本项目的备份能力来源

## 许可证

[MIT](LICENSE) © nanshanyi

个人自用项目，代码公开只是因为可能对别人也有用。按需维护，不承诺响应时间，也不提供技术支持。正式使用前请自行评估风险——尤其是恢复功能，会覆盖设备上的现有数据。
