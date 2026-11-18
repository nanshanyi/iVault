# iVault · iPhone / iPad 整机备份中心（Docker）

把手机用数据线插到 NAS 的 USB 口完成一次配对，之后由 NAS 自己执行与
Finder / iTunes 同格式的 **Apple MobileSync 全量与增量备份**，通过网页管理。

**跑在任何能运行 Docker 的 NAS 上**——群晖、威联通、绿联、飞牛、TrueNAS、Unraid、
树莓派或普通 Linux 主机都行，不绑定某一家系统。前提只有两条：容器能拿到
USB 总线（`privileged` + `/dev/bus/usb` 映射），以及有一个空闲 USB-A 口。

> 当前真机验证环境是绿联 DXP4800（UGOS 1.21）。其他平台的差异主要在 Docker 界面
> 的叫法与卷路径写法，`docker-compose.yml` 不需要改结构。

- 前端：深浅色双主题、总览 / 设备管理 / 备份任务 / 备份库 / 设置
- 后端：FastAPI + SSE 实时推送，调用 `libimobiledevice` 官方工具链
- 部署：单个 Docker 容器（`privileged` + USB 总线映射）；CI 提供 amd64 / arm64 预构建镜像，
  目标机器上 `docker compose pull` 即用，无需自己编译工具链

> 本项目与 Apple Inc. 无任何关联。备份格式为 Apple MobileSync，
> 目录下的 `<UDID>/` 可拷贝到 Mac/PC 用 Finder、iTunes 或 iMazing 直接恢复。

---

## 依赖（三层，全部由 Dockerfile 自动安装）

| 层 | 依赖 | 安装方式 |
|---|---|---|
| 前端 | **零第三方库**：原生 HTML / CSS / JS，无 npm、无框架、无 CDN | 随镜像静态托管 |
| Python 后端 | `fastapi`、`uvicorn[standard]`（仅此 2 个 pip 包）；其余 `sqlite3` / `asyncio` / `subprocess` 都是标准库 | `pip install -r backend/requirements.txt` |
| 系统工具链 | `libimobiledevice`（`idevice_id` / `ideviceinfo` / `idevicepair` / `idevicebackup2`）+ `usbmuxd` | **从上游源码编译**，见下 |

**iOS 备份协议不是本项目实现的**：整机备份能力来自 `libimobiledevice` 命令行工具链，
后端只用 `subprocess` 调用它们并解析输出。本项目自研的是 Web 管理层、任务调度与 UI。
所以「零依赖」只适用于前端，整个项目的地基恰恰是第三方开源组件。

### 为什么从源码编译

Ubuntu 24.04 / Debian 发行版源里的 `libimobiledevice-utils` 是上游 **2020-06-15**
发布的 **1.3.0**，缺少 iOS 17+ 支持与 `idevicebackup2` 的后续修复。因此 `Dockerfile`
按固定依赖顺序从上游构建：

```
libplist → libimobiledevice-glue → libtatsu → libusbmuxd → libimobiledevice
```

并单独编译 `usbmuxd`（为使用 `--config-dir` 指定证书目录）。

**所有组件都钉到 40 位 commit SHA**，构建结果可复现：上游 `master` 怎么变都不会影响
已有的镜像。升级组件只需改 `Dockerfile` 顶部的 `ARG *_REF`，重新构建并真机验证一次。

编译只发生在 `docker build` 阶段，**产物固化进镜像层**，容器运行时零安装、零联网。
本地首次构建约 **10–20 分钟**；国内网络慢可设 `APT_MIRROR=mirrors.ustc.edu.cn`。
但正常部署**不需要你编译**——CI 已经构建好镜像，直接 pull 即可（见下）。

### 本地开发

```bash
python3 -m venv .venv && .venv/bin/pip install -r backend/requirements.txt
# 需要真连 iPhone 时（macOS）：brew install libimobiledevice usbmuxd
```

只做 Web / API 调试不需要系统工具链；`idevice_*` 不存在时设备相关接口会明确报错。

### 部署后体检

```bash
docker exec ivault cat /usr/local/share/build-revisions.txt  # 各组件编译时的 commit
docker exec ivault idevice_id -l                            # 插线后应列出 UDID
docker compose logs -f ivault | grep usbmuxd                # 看守护进程是否正常
```

---

## 快速开始（任意 Docker NAS / Linux 主机）

**前提**：已装 Docker（群晖 Container Manager、威联通 Container Station、
绿联/飞牛的 Docker 应用、或 docker-ce 都行）；有一个空闲 USB-A 口；备份卷有足够空间
（首次全量备份通常是几十到上百 GB，建议放在 SSD 卷上，机械盘会明显慢）。

```bash
git clone https://github.com/nanshanyi/iVault.git && cd iVault
chmod +x install.sh
./install.sh
```

脚本会检查 Docker、按你的实际卷路径询问备份目录、生成 `.env`，
然后**拉取预构建镜像并启动**（`docker compose pull` + `up -d --no-build`），通常几十秒完成。
只有拉取失败时才回落到本地编译。

也可以手动部署：

```bash
cp .env.example .env      # 编辑 BACKUP_LOCATION / META_LOCATION / IVAULT_PORT
mkdir -p /your/volume/ivault/backups /your/volume/ivault/data/lockdown
docker compose pull && docker compose up -d --no-build
```

> 路径写法各平台不同：群晖/绿联常是 `/volume1/...`，威联通是 `/share/CACHEDEV1_DATA/...`，
> 飞牛与普通 Linux 常见 `/vol1/...`、`/mnt/disk1/...`。填宿主机上的绝对路径即可。

> **USB 透传**：容器需要 `privileged: true` 与 `/dev/bus/usb` 映射（compose 已配好）。
> 部分 NAS 的 Docker 界面另有「USB 设备直通 / 设备访问」开关，也要一并打开。
> 若系统策略不允许 privileged，可改装宿主机 `usbmuxd` 并把 socket 挂进容器。

> **镜像从哪来**：推 tag 时 CI 自动构建 `linux/amd64` + `linux/arm64` 双架构镜像，
> 推送到 `ghcr.io/nanshanyi/ivault`。
>
> - 首次发布后请到 GitHub 仓库 **Settings → Packages** 把该包可见性改为 **Public**，
>   否则 NAS 上 pull 之前要先 `docker login ghcr.io`
> - 固定版本：`.env` 里设 `IVAULT_TAG=v0.2.0`（默认 `latest`）
> - 本地编译（改过代码 / 完全离线）：`IVAULT_BUILD=1 ./install.sh`，或
>   `docker compose up -d --build`

浏览器打开 `http://<NAS-IP>:8666`。

### 可选：让界面显示你的系统版本

左下角那行「服务运行中 · 系统名」是可选的：识别不到就只显示服务状态与版本号，不会瞎猜。

```bash
# 方式一（最稳）：直接指定，install.sh 会问，也可以写进 .env
IVAULT_OS_NAME="DSM 7.2"        # 或 "UGOS 1.21"、'QTS 5.1'、'TrueNAS SCALE 24.04'

# 方式二：取消 docker-compose.yml 里这行的注释，让容器读宿主机 os-release
#   - /etc/os-release:/host-os-release:ro
```

> 容器内的 `/etc/os-release` 是镜像自带的 Debian 信息，后端刻意不读它，避免界面上
> 写着「Debian 13」而你的设备其实是群晖。默认不挂载也是这个原因：少数系统没有这个
> 文件时，Docker 会在宿主机上建一个同名空目录。

### 首次备份三步

1. **设备管理 → 添加设备**：用数据线把 iPhone 插到 **NAS 机身上的 USB 口**
   （不是电脑），手机解锁
2. 手机弹出「要信任此电脑吗？」→ 点**信任**并输入锁屏密码；网页配对向导会自动完成
3. **立即备份**：进度、实时日志由 SSE 推送；**进度到 100% 后仍需等待后端写快照完成**
   （以页面提示/任务状态为准），再拔线

> ⚠️ **手机没有弹窗？拔掉数据线重新插一次。**
> 在弹窗上点过「不信任」，或密码框超时未输入，iOS 会记住「这台电脑已被拒绝」，
> 之后 `idevicepair` 一律直接返回 `user denied the trust dialog`，**不会再弹窗**。
> 断电重连会重置这个状态。配对向导能识别这种情况并明确提示拔插线，插回后会自动重试。

> ⚠️ 配对证书由 `usbmuxd --config-dir /var/lib/lockdown` 写入，compose 已把宿主机
> `META_LOCATION/lockdown` 挂到该路径。务必持久化此目录：容器删除重建后只要它还在，
> 就不需要重新插线配对。

---

## 功能

| 能力 | 状态 |
|---|---|
| USB 首次配对（信任 + 证书持久化） | ✅ |
| 全量 / 增量整机备份（MobileSync 格式） | ✅ |
| 网页实时进度环、速率、终端日志流（SSE） | ✅ |
| 快照列表、删除记录、恢复（restore） | ✅ |
| 定时备份（每天自选时刻，时间选择器） | ✅ |
| 保留策略（1–10 份滑杆，每设备保留最近 N 份记录） | ✅ |
| Webhook 通知 | ✅ |
| 深浅色主题（跟随系统 / 手动切换 / 记忆） | ✅ |
| Wi-Fi 无线备份（netmuxd） | ⏳ v0.2，见「已知限制」 |
| 备份内容浏览（相册/信息/联系人统计） | ⏳ v0.3 |
| 备份加密（密码在 iPhone 侧设置） | ⏳ 后续 |
| 多用户权限（对接 NAS 账户体系） | ⏳ 后续 |

---

## 架构

```
iPhone ──USB──► /dev/bus/usb ──► usbmuxd ──► libimobiledevice (idevicepair / idevicebackup2)
                                                  │
浏览器 ◄──REST / SSE──► FastAPI (backend) ─────────┤
                          │                        │
                     SQLite 元数据            /data/backups/<UDID>/
                     + lockdown 证书           Apple MobileSync 备份库
```

```
backend/
  main.py     REST API、SSE 流、设备轮询、定时调度
  device.py   idevice_id / ideviceinfo / idevicepair 封装 + 配对向导会话
  jobs.py     idevicebackup2 子进程管理、进度解析、取消、保留策略
  store.py    SQLite（devices / snapshots / settings）
  config.py   路径与工具链配置
frontend/
  index.html  单页界面（内联 CSS，零依赖）
  app.js      渲染、REST、SSE、配对向导、主题切换
scripts/
  entrypoint.sh  启动 usbmuxd（--config-dir 持久证书）+ uvicorn
```

### 主要 API

```
GET    /api/status                 服务状态 / 在线设备 / 磁盘剩余
GET    /api/devices                已配对设备（含在线状态、快照统计）
DELETE /api/devices/{udid}         取消配对（撤销信任 + 删除本地证书，见下方说明）
POST   /api/pair/begin             开始配对向导
GET    /api/pair/{sid}/events      SSE：waiting → found → pairing → done/error
POST   /api/backups                {udid} 触发备份
GET    /api/jobs/{id}/events       SSE：progress / log / done / failed
POST   /api/jobs/{id}/cancel       取消任务
GET    /api/snapshots              快照列表
DELETE /api/snapshots/{id}         删除快照记录
POST   /api/restore                {udid} 恢复最近一份成功备份
GET/PUT /api/settings              定时、保留数、Webhook
GET    /api/docs                   Swagger 文档
```

> **取消配对的语义（两侧）**：设备侧靠 `idevicepair unpair` 通知 iPhone 删掉对本机的
> 信任记录，这要求设备在线；本机侧则**无条件**删除 `lockdown/<udid>.plist` 配对证书。
> 设备离线时只有本机侧生效，iPhone 上的信任仍会保留，接口会在 `message` 里如实说明，
> 前端据此提示「先插上数据线再取消一次」。证书必须删除的原因有两层：留着它 NAS 仍可
> 免信任访问手机；而且它是 `validate` 判定「已授权」的依据，不删就永远等不到信任弹窗。

---

## 已知限制（诚实清单）

1. **v0.1 走 USB 通道**：容器内运行的是标准 `usbmuxd`，仅支持 USB 直连设备。
   Wi-Fi 备份需要 `netmuxd`/`usbmuxd2` 做网络设备发现，且需先在 iPhone 上开启
   「通过 Wi-Fi 同步」（通常要借 Mac/PC 的 iTunes 打开一次开关）；这属于 v0.2 范围，
   两个参考项目里都有可复用的实现。
2. **增量快照共享基线目录**：`idevicebackup2` 用同一 `<UDID>/` 目录持续更新，
   磁盘上始终是「最新状态」。备份库中的历史记录是时间点标记，删除记录不会立即
   释放空间；若要真正保留多个独立版本，需要每次备份后对目录做硬链接快照（v0.3）。
3. **未加密备份**不含钥匙串密码、Wi-Fi 设置、健康数据、通话记录等。
   如果需要这些，请在 iPhone 侧开启加密备份并牢记密码（密码不落 NAS）。
4. **工具链为上游源码构建，已钉 commit SHA**：各库固定在 `Dockerfile` 顶部的
   `ARG *_REF`（40 位 SHA），上游 `master` 变动不会影响构建结果，也不会突然构建失败。
   若遇到新 iOS 不兼容，把这些 SHA 换成更新的版本后重新构建、真机验证一次即可。
5. **平台差异只在 Docker 外壳**：需要 `privileged` 容器与 `/dev/bus/usb` 映射。
   群晖/威联通/绿联/飞牛的图形界面里开关位置不同，个别系统还要额外打开「USB 直通」；
   若系统策略禁止 privileged，可改装宿主机 `usbmuxd` 并把 socket 挂进容器。
6. **验证状态**：
   - ✅ **配对链路已在真机验证**（iPhone Xs / iOS 18.7.12，绿联 DXP4800 / UGOS 1.21）：
     设备发现 → 信任授权 → 证书落地 → 入库，含「未弹窗需拔插线」的排障路径
   - ⬜ 镜像构建、容器内 USB 透传、`idevicebackup2` 备份落盘仍未实测
   - ⬜ 其他 NAS 平台（群晖 / 威联通 / 飞牛 / TrueNAS）未实机测试，理论上只需 Docker 侧配置
   - 首次部署仍属 M0 验证，需把 iPhone 用数据线插到 NAS 上配合测试

---

## 参考与致谢

设计与构建参考了以下开源实现（借鉴思路与构建顺序，未复制其应用代码）：

- [razeencheng/iosbackup](https://github.com/razeencheng/iosbackup) — Go 实现，官方镜像，定时/Wi-Fi/通知
- [JerryGHT04/ios-backup-station](https://github.com/JerryGHT04/ios-backup-station) — Python 实现，飞牛 fnOS 社区；本项目 `Dockerfile` 的源码编译顺序与 `usbmuxd --config-dir` 用法参考其 Stage 1
- [libimobiledevice](https://libimobiledevice.org/) — iOS 协议开源实现，本项目备份能力的来源

