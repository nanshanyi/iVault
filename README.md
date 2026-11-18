# iVault · iPhone 备份中心 for UGOS

在绿联 NAS（DXP4800 / UGOS Pro）上运行的 iPhone / iPad **整机备份**应用：
把手机用数据线插到 NAS 的 USB 口完成一次配对，之后由 NAS 自己执行与
Finder / iTunes 同格式的 **Apple MobileSync 全量与增量备份**，通过网页管理。

- 前端：深浅色双主题、总览 / 设备管理 / 备份任务 / 备份库 / 设置
- 后端：FastAPI + SSE 实时推送，调用 `libimobiledevice` 官方工具链
- 部署：单个 Docker 容器（`privileged` + USB 总线映射）

> 本项目与 Apple Inc. 无任何关联。备份格式为 Apple MobileSync，
> 目录下的 `<UDID>/` 可拷贝到 Mac/PC 用 Finder、iTunes 或 iMazing 直接恢复。

---

## 快速开始（绿联 NAS）

**前提**：UGOS 已安装并运行 Docker 套件；有一个空闲 USB-A 口；备份卷有足够空间
（首次全量备份通常是几十到上百 GB，建议放在 SSD 卷上，机械盘会明显慢）。

```bash
git clone <本仓库> iVault && cd iVault
chmod +x install.sh
./install.sh
```

脚本会检查 Docker、询问备份目录（如 `/volume1/ivault/backups`）、生成 `.env`，
然后 `docker compose up -d --build`。首次构建需要下载 Ubuntu 基础镜像与工具链，
约 2–5 分钟。

也可以手动部署：

```bash
cp .env.example .env      # 编辑 BACKUP_LOCATION / META_LOCATION / IVAULT_PORT
mkdir -p /volume1/ivault/backups /volume1/ivault/data
docker compose up -d --build
```

浏览器打开 `http://<NAS-IP>:8666`。

### 首次备份三步

1. **设备管理 → 添加设备**：用数据线把 iPhone 插到 **NAS 机身上的 USB 口**
   （不是电脑），手机解锁
2. 手机弹出「要信任此电脑吗？」→ 点**信任**并输入锁屏密码；网页配对向导会自动完成
3. **立即备份**：进度、实时日志由 SSE 推送；**进度到 100% 后仍需等待后端写快照完成**
   （以页面提示/任务状态为准），再拔线

> ⚠️ 配对证书保存在 `META_LOCATION/lockdown`，务必持久化该目录。
> 容器删除后只要该目录还在，就不需要重新插线配对。

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
| 多用户权限（对接 UGOS 账户） | ⏳ 后续 |

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
  entrypoint.sh  启动 usbmuxd + uvicorn
```

### 主要 API

```
GET    /api/status                 服务状态 / 在线设备 / 磁盘剩余
GET    /api/devices                已配对设备（含在线状态、快照统计）
DELETE /api/devices/{udid}         取消配对
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
4. **iOS 大版本升级**可能要求更新 `libimobiledevice`；镜像基于 Ubuntu 24.04 的
   发行版包，若遇新 iOS 不支持，可改为从上游源码构建（参考项目里有完整 Dockerfile）。
5. UGOS 的 Docker 需允许 `privileged` 容器与 `/dev/bus/usb` 映射；
   若你的系统策略禁止，可改装宿主 `usbmuxd` 并把 socket 挂进容器。

---

## 参考与致谢

设计阶段参考了以下开源实现（仅参考思路，未复制代码）：

- [razeencheng/iosbackup](https://github.com/razeencheng/iosbackup) — Go 实现，官方镜像，定时/Wi-Fi/通知
- [JerryGHT04/ios-backup-station](https://github.com/JerryGHT04/ios-backup-station) — Python 实现，飞牛 fnOS 社区
- [libimobiledevice](https://libimobiledevice.org/) — iOS 协议开源实现
