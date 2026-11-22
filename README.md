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
  <img src="https://img.shields.io/badge/iOS-17%2B-000000?style=flat-square&amp;logo=apple&amp;logoColor=white" alt="iOS">
</p>

---

手机用数据线插一次 NAS 的 USB 口完成配对，之后由 NAS 自己执行**整机备份**——全量与增量，格式与 Finder 完全相同。备份目录可以原样拷到 Mac 上，用 Finder 或 iMazing 直接恢复。

没有云、没有账号、没有订阅。数据只存在你自己的硬盘上。

**跑在任何能运行 Docker 的 NAS 上**：群晖、威联通、绿联、飞牛、TrueNAS、Unraid、树莓派或普通 Linux 主机都可以。唯一的硬要求是容器能拿到 USB 总线（`privileged` + `/dev/bus/usb` 映射）。

<!-- 截图占位：把界面截图放到 assets/screenshot.png 后取消下面这行的注释
<p align="center"><img src="assets/screenshot.png" width="860" alt="iVault 界面"></p>
-->

> [!WARNING]
> iVault 还在早期开发阶段，请先读完这几条再用：
> - **备份落盘还没在真机上完整联调**，恢复（含换机恢复）和加密开关也没在真机上验证过。恢复会覆盖整机数据，首次使用请用备用机测试。
> - **磁盘上只有最新一份备份**。每次备份覆盖同一个目录，不能回到历史时间点。重要数据请另外再留一份（3-2-1 原则）。
> - **默认不设密码**。服务以 `privileged` 运行，能触发恢复，请务必设置 `IVAULT_PASSWORD`（见[安全](#安全)）。
> - **定时备份做不到无人值守**。iOS 16.1 起每次备份都要在手机上输一次锁屏密码，到点后 iVault 会推送提醒，20 分钟内没人输就放弃。

本项目与 Apple Inc. 无任何关联。

## 功能

| 能力 | 说明 |
|---|---|
| USB 首次配对 | 信任授权 + 证书持久化，重装容器不用重配 |
| 全量 / 增量整机备份 | Apple MobileSync 格式，目录可直接拷到 Mac 用 Finder 恢复 |
| 实时进度 | SSE 推送进度环、已用时间与终端日志，断线重连不丢状态 |
| 备份任务 | 「当前可恢复」列出每台设备最新一份可恢复备份；「任务历史」记录每次备份与恢复（含失败与取消）和完整日志 |
| 换机恢复 | 把一台设备的备份恢复到另一台已配对的设备；旧手机取消配对后，它的备份仍可选作来源 |
| 备份加密 | 在设备卡片上开启/关闭 iPhone 的备份加密；恢复加密备份时输入密码，密码不保存在 NAS 上 |
| 定时备份 | 每天 / 每周几 / 每月几号自选时刻；到点后会推送提醒，**每次都要在 iPhone 上输一次锁屏密码**（iOS 系统要求） |
| 插线自动备份 | 开启后，在备份日（和定时备份共用频率）把 iPhone 插到 NAS 的 USB 口、当天还没备份过就自动开始（默认关；同样要输锁屏密码） |
| Wi-Fi 无线备份 | netmuxd shim 模式，默认部署即可用（host 网络，见下文） |
| 访问密码 | `IVAULT_PASSWORD` 启用登录鉴权，HttpOnly Cookie 会话，SSE 同套保护 |
| Webhook 通知 | 每次任务结束推送结果，失败带原因；支持 Bark 与企业微信 / 飞书 / 钉钉群机器人 |
| 设备状态 | 卡片显示连接方式、电量与充电状态；电量过低且未接电源时拒绝开始备份 |
| 失败早发现 | 备份前检查磁盘剩余空间，低于阈值拒绝开始 |
| 深浅色主题 | 跟随系统 / 手动切换 / 记忆选择 |

## 快速开始

NAS 上最省事的路径是 **Compose**：不用 `git`、不用编译，拉 CI 预构建好的镜像起容器即可。三步。

### 1. 建目录、取两个文件

```bash
# 路径换成你自己的卷结构：群晖 / 绿联 /volume1/...、威联通 /share/CACHEDEV1_DATA/...、
# 飞牛与普通 Linux 常见 /vol1/...、/mnt/disk1/...
mkdir -p /volume1/docker/ivault && cd /volume1/docker/ivault

curl -fsSLO https://raw.githubusercontent.com/nanshanyi/iVault/main/docker-compose.yml
curl -fsSL -o .env https://raw.githubusercontent.com/nanshanyi/iVault/main/.env.example

# 备份库与元数据目录提前建好：路径不存在时 Docker 会自动以 root 建一个空目录，
# 路径写错也不报错，备份会悄悄写到别处
mkdir -p /volume1/ivault/backups /volume1/ivault/data/lockdown
```

没有 `curl`（部分 NAS 系统比较精简）：用浏览器打开上面两个链接，把内容分别另存为 `docker-compose.yml` 和 `.env`，放进同一个目录。

### 2. 改 `.env`

| 变量 | 填什么 |
|---|---|
| `BACKUP_LOCATION` | 备份库所在的宿主机目录，如 `/volume1/ivault/backups` |
| `META_LOCATION` | SQLite 与配对证书目录，如 `/volume1/ivault/data` |
| `IVAULT_PASSWORD` | 你的 Web 访问密码，**强烈建议设置**（留空 = 局域网内任何人都能打开页面、触发备份或恢复） |

两个路径都写**绝对路径**，并且和上一步 `mkdir` 创建的一致。`META_LOCATION` 删掉就等于丢了配对证书，下次要重新插线配对。

### 3. 起容器

```bash
docker compose pull          # 拉预构建镜像，通常几十秒
docker compose up -d --no-build
```

打开 `http://<NAS-IP>:8666`（端口由 `IVAULT_PORT` 决定，默认 8666）→ 用数据线把 iPhone 插到 **NAS 机身**的 USB 口（不是电脑）→ 设备管理 → 添加设备 → 手机上点「信任」→ 立即备份。

> 进度到 100% 后别急着拔线，后端还要把备份写完才算落盘，以页面上的任务状态为准。

### 在 NAS 的 Docker 图形界面里部署

群晖 Container Manager 的「项目」、威联通 Container Station 的「应用程序」、绿联的「Docker → 项目」都能直接粘贴 compose，不必进 SSH：

1. 新建项目，把 `docker-compose.yml` 的内容整段粘进去
2. 把 `${BACKUP_LOCATION}`、`${META_LOCATION}` 换成实际路径（界面里没有 `.env` 入口时直接改这两行，效果一样）
3. 删掉 `build:` 那几行（`context` / `args`）——只有本地编译才需要，粘贴部署用不到，留着会在镜像拉取失败时触发构建并报「找不到 Dockerfile」
4. 确认 `privileged: true` 和 `- /dev/bus/usb:/dev/bus/usb` 都在

> 部分 NAS 的 Docker 界面里除 `privileged` 外还有单独的「USB 设备直通」开关，也要打开。

### 可选：交互式脚本

仓库里另有一个 `install.sh`，把上面改 `.env` 的几步做成了问答（备份目录、元数据目录、端口、密码），然后同样 `pull` + `up -d --no-build`。适合有 SSH 且装了 `git`、`bash` 的机器：

```bash
git clone https://github.com/nanshanyi/iVault.git && cd iVault
chmod +x install.sh && ./install.sh
```

## 更新

```bash
docker compose pull
docker compose up -d --no-build
```

更新只换镜像：`.env` 留在 compose 目录里，界面上的设置、任务记录和配对证书都在 `META_LOCATION` 里，都不会动，也不用重新配对。

- 有备份或恢复正在跑时别更新：容器重启会中断任务，这条记录会被标成失败
- 想停在某个版本，把 `.env` 里的 `IVAULT_TAG` 改成具体版本号（如 `v0.7.0`）

## 无线备份

配对一次之后，不插线也能备份。**默认部署（`docker-compose.yml`）就是 host 网络，无线开箱可用。**

**两个前提**，缺一不可：

1. 手机与 NAS 在同一局域网（mDNS 组播可达，跨 VLAN 通常不行）
2. iOS 侧已开启「通过 Wi-Fi 同步」——首次只能在**能识别这台手机的电脑**上用 Finder 或 Apple Devices 勾选，NAS 无法代劳

默认用 `network_mode: host`：mDNS 靠局域网组播发现设备，bridge 的独立 netns 收不到，所以无线必须 host。代价是 ports 映射失效，Web 端口直接由 `IVAULT_PORT` 决定（默认 8666）。

不想要 host（端口冲突、或在 Docker Desktop 上跑）就用纯 USB 版：

```bash
docker compose -f docker-compose.usb.yml up -d --no-build
```

它用 bridge + 端口映射，并把 `IVAULT_WIFI` 固定为 0——bridge 下 netmuxd 发现不到设备，开着只会让界面显示「无线通道已启用」却没有无线设备。

设备卡片会标注 `USB` 或 `Wi-Fi`。手机离开或回到局域网后，卡片状态最多滞后 1～2 分钟；点「立即备份」时会现场确认，不受影响（原因见[排查文档](docs/troubleshooting.md#在线状态为什么会滞后)）。

> 无线只服务**已配对过**的设备。发现不到你的手机，通常是上面第 2 条没做，或者设备正在休眠——点亮屏幕、重连一次 Wi-Fi，最多等 2 分钟，或者直接点「立即备份」。

## 首次配对的两个坑

**手机不弹「信任」窗口？拔掉数据线重新插一次。**

如果在弹窗上点过「不信任」，或者密码框超时没输入，iOS 会记住「这台电脑已被拒绝」，之后 `idevicepair` 一律直接返回 `user denied the trust dialog`，**不会再弹窗**。断电重连才能重置。配对向导能识别这个状态并明确提示拔插线，插回后自动重试。

**不要删 `META_LOCATION/lockdown`。**

配对证书由 `usbmuxd --config-dir /var/lib/lockdown` 写入，compose 已把宿主机目录挂到那里。容器删了重建，只要这个目录还在，就不用重新插线配对。

## 配置

| 变量 | 默认 | 说明 |
|---|---|---|
| `BACKUP_LOCATION` | *必填* | 备份库在宿主机上的绝对路径 |
| `META_LOCATION` | `./data` | SQLite 与配对证书目录，**必须持久化** |
| `IVAULT_PORT` | `8666` | Web 端口 |
| `IVAULT_TAG` | `latest` | 镜像版本，可固定为某个发布，例如 `v0.7.0`（避免 `latest` 被上游覆盖后被动升级） |
| `IVAULT_PASSWORD` | 空 | **强烈建议设置**：Web 访问密码，启用后所有接口需登录（见下方「安全」） |
| `IVAULT_SESSION_DAYS` | `30` | 登录会话有效期（天），1–365，非法值自动回落为 30 |
| `IVAULT_MIN_FREE_GB` | `2` | 备份前磁盘剩余空间下限（GB），低于则拒绝开始备份；`0` 关闭检查 |
| `IVAULT_MIN_BATTERY` | `20` | 备份前最低电量（%），低于此值且未接电源时拒绝开始；插线边充边备不受影响，`0` 关闭检查 |
| `IVAULT_WIFI` | `1` | `0` 关闭无线通道，只走 USB |
| `IVAULT_USB_WATCHDOG` | `1` | `0` 关闭 USB 热插拔看护（关闭后容器运行中插线不识别，需重启容器） |
| `IVAULT_OS_NAME` | 空 | 界面左下角显示的系统名，如 `DSM 7.2`；留空则自动探测，探测不到就隐藏 |
| `IVAULT_LOG` | `INFO` | 日志级别 |
| `IVAULT_USBMUXD_VERBOSE` | `0` | `1` 时 usbmuxd 输出详细日志（排查 USB 识别问题用） |
| `IVAULT_WIFI_LOG` | `warn` | netmuxd 的日志级别（`RUST_LOG` 语法，如 `info`、`debug`） |
| `IVAULT_USB_DIAG` | `0` | `1` 时 USB 看护启动后做一次自检，把它看到的设备与 socket 状态打进日志 |
| `IVAULT_BUILD` | — | 仅 `install.sh` 读取：设为 `1` 时本地编译；手动部署等价于 `docker compose up -d --build` |

界面左下角默认只显示服务状态与版本号，不猜系统名。想让它是「DSM 7.2」或「UGOS 1.21」，填 `IVAULT_OS_NAME` 最稳；也可以取消 `docker-compose.yml` 里 `/etc/os-release:/host-os-release:ro` 那行的注释。容器内的 `/etc/os-release` 是镜像自带的 Debian 信息，后端刻意不读——否则界面上会写着「Debian 13」而你的设备其实是群晖。

## 安全

iVault 能**写设备数据**（恢复会覆盖整机数据），Web 默认监听在局域网可达的 `0.0.0.0:8666`，而容器又是 `privileged` + host 网络。**强烈建议设置访问密码**：

```bash
# .env 里加一行（或在 install.sh 交互时直接填）
IVAULT_PASSWORD=你的密码
docker compose up -d --no-build     # 重建容器生效
```

- 启用后所有 `/api/*` 接口（含 SSE 实时推送）都需要登录，未登录访问返回 401，页面自动弹出登录浮层
  唯一例外是容器内的 `usb-watchdog` 读取 `GET /api/status`：它重启 usbmuxd 前要确认没有任务在跑，但没有登录态，
  所以凭容器启动时随机生成的内部令牌访问。放行不看来源地址，套反向代理也不会对外免登录
- 登录态以 HttpOnly Cookie（`SameSite=Lax`）下发，默认 30 天有效（`IVAULT_SESSION_DAYS`，1–365）
- **退出登录只清除浏览器里的 Cookie**：令牌在有效期内本身仍然有效，被复制走的 Cookie 可继续使用。要让所有会话立即失效，修改 `IVAULT_PASSWORD` 并重启容器即可
- 密码支持任意字符（含中文）；登录接口带来源级限速：同一 IP 10 分钟内失败 8 次后暂时拒绝
- iVault 的 Web 走**明文 HTTP**。内网直接访问没有问题；需要从外网访问时，请使用 Tailscale / WireGuard 这类隧道组网，或 NAS 自带的反向代理套 HTTPS——**不要把 8666 端口直接映射到公网**
- 未设置该变量时服务无鉴权，仅适合完全可信的内网环境

恢复功能的前置检查：目标设备必须已配对且在线；来源必须有成功备份；加密备份必须提供密码；备份的 iOS 版本不能高于目标手机。若手机没关「查找我的 iPhone」，失败原因里会给出解决办法。

备份密码只在当次请求里经环境变量 `BACKUP_PASSWORD` 交给 `idevicebackup2`，不进命令行、不写日志、不落库。

## 常见问题

| 问题 | 先做什么 |
|---|---|
| 某次备份或恢复失败了 | 「备份任务 → 任务历史」里点那条记录的**日志** |
| 进度一直停在 0% | 去 iPhone 上输入锁屏密码，输入后还要等几分钟，见[这里](docs/troubleshooting.md#进度一直停在-0) |
| 插上 iPhone 一直显示离线 | 插线后等 1～2 秒；还不行看[这里](docs/troubleshooting.md#插上-iphone-却一直显示离线) |
| 手机锁屏后卡片偶尔闪「离线」 | 正常：无线通道被发现/摘除的抖动，见[这里](docs/troubleshooting.md#在线状态为什么会滞后) |
| 无线设备不出现 | 先确认[无线备份](#无线备份)的两个前提，再看[这里](docs/troubleshooting.md#无线wi-fi设备一直不出现) |
| 定时备份没跑 | 多半是没在手机上输锁屏密码，看[这里](docs/troubleshooting.md#定时备份为什么没有跑) |
| 说不清哪里不对 | 设置 → 关于 → **诊断信息 → 复制**，提 Issue 时附上 |

更多文档：

- [出问题怎么查](docs/troubleshooting.md)：日志在哪、USB / 无线排障、定时备份与电量的规则
- [HTTP API](docs/api.md)：接口列表
- [开发与构建](docs/development.md)：工作原理、目录结构、从源码构建、本地开发

## 已知限制

1. **备份是单目录覆盖模式，没有快照机制**：`idevicebackup2` 始终更新同一个 `<UDID>/` 目录，新备份覆写旧状态，磁盘上只有「最新一次可恢复状态」，不能回滚到历史时间点。「备份任务」页的任务历史记录每一次任务（含失败与取消），删记录不会释放空间。中途失败的增量备份可能让这份唯一状态处于不一致，需重新跑一次成功备份修复。
2. **未加密备份不含钥匙串密码、Wi-Fi 设置、健康数据、通话记录**。需要这些请在设备卡片上开启备份加密并牢记密码——密码不会写到 NAS 上，忘记密码就无法恢复加密备份，只能在手机上「还原所有设置」清除它。换机前尤其建议开启。
3. **无线备份的两个硬边界**：首次配对必须插线（iPhone 不支持无线配对，这是 `idevicepair` 的限制）；iOS 侧的「通过 Wi-Fi 同步」只能由电脑端打开一次。
4. **平台差异只在 Docker 外壳**：需要 `privileged` 与 `/dev/bus/usb`——且必须用 `volumes:` 做 bind mount，写 `devices:` 会让热插拔失效（见[排查文档](docs/troubleshooting.md#插上-iphone-却一直显示离线)）。若系统策略禁止 privileged，可改装宿主机 `usbmuxd` 并把 socket 挂进容器。
5. **验证状态**：配对链路已在真机验证（iPhone Xs / iOS 18.7.12，绿联 DXP4800 / UGOS 1.21），含「未弹窗需拔插线」的排障路径；容器 USB 透传与热插拔看护已在 DXP4800 + iPhone 15 / iOS 27.3 实测（拔线重插后约 1 秒自动恢复在线）；无线通道的命令层已在 macOS 实测（`-n` 发现、探活、失效条目过滤），容器内 mDNS 发现（`[netmuxd] Adding network device`）也已实测。没验证的部分见顶部提示；其他 NAS 平台也未实机测试。

## 致谢

设计与构建参考了这些开源实现（借鉴思路与构建顺序，未复制应用代码）：

- [razeencheng/iosbackup](https://github.com/razeencheng/iosbackup) — Go 实现，定时 / Wi-Fi / 通知
- [JerryGHT04/ios-backup-station](https://github.com/JerryGHT04/ios-backup-station) — 飞牛 fnOS 社区项目；本项目的源码编译顺序与 `usbmuxd --config-dir` 用法参考其 Stage 1
- [libimobiledevice](https://libimobiledevice.org/) — iOS 协议开源实现，本项目的备份能力来源

## 许可证

[MIT](LICENSE) © nanshanyi

个人自用项目，代码公开只是因为可能对别人也有用。按需维护，不承诺响应时间，也不提供技术支持。正式使用前请自行评估风险——尤其是恢复功能，会覆盖设备上的现有数据。
