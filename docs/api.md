# HTTP API

[← 返回 README](../README.md)

启用 `IVAULT_PASSWORD` 后，除 `/api/login` 和带内部令牌的 `GET /api/status`（容器内 USB 看护进程用，令牌每次启动随机生成）外，所有接口都需要登录 Cookie。运行中的服务在 `/api/docs` 提供 Swagger 文档，以那里为准。

## 接口列表

```
POST   /api/login                  {password} 登录（启用 IVAULT_PASSWORD 时）
POST   /api/logout                 退出登录
GET    /api/status                 服务状态 / 在线设备 / 磁盘剩余
GET    /api/devices                已配对设备（含在线状态、备份统计、电量缓存读数）
DELETE /api/devices/{udid}         取消配对
POST   /api/devices/{udid}/encryption  {enable, password} 开启/关闭备份加密
POST   /api/pair/begin             开始配对向导
GET    /api/pair/{sid}/events      SSE：waiting → found → pairing → done / error
POST   /api/backups                {udid} 触发备份
GET    /api/jobs/{id}/events       SSE：progress / live（末行原地刷新）/ log / done / failed / canceled
GET    /api/jobs/active            进行中的任务
POST   /api/jobs/{id}/cancel       取消任务
GET    /api/backups?udid=          任务记录列表：备份与恢复（kind / source_udid 区分，含 has_log / error）
GET    /api/backups/{id}/log       某次任务的完整日志（事后排查）
DELETE /api/backups/{id}           删除任务记录（进行中的返回 409）
GET    /api/restore/sources        可恢复的备份来源（含已取消配对的设备）
POST   /api/restore                {udid, source_udid?, password?} 恢复；source_udid 不同于 udid 即换机
GET/PUT /api/settings              定时（schedule_enabled / schedule_time / schedule_freq /
                                   schedule_weekdays / schedule_monthday）、插线备份（usb_autobackup）、
                                   Webhook、通知范围、日志详细度
POST   /api/settings/test-webhook  {url?} 发送测试通知
GET    /api/diagnostics            版本 / 系统 / 设备 / 最近告警，纯文本一键复制
GET    /api/docs                   Swagger 文档
```

## 取消配对是两侧的事

设备侧靠 `idevicepair unpair` 通知 iPhone 删掉信任记录，要求设备在线；本机侧则无条件删除 `lockdown/<udid>.plist`。设备离线时只有本机侧生效，iPhone 上的信任仍在保留，接口会在 `message` 里如实说明，前端据此提示「先插上线再取消一次」。证书必须删的原因有两层：留着它 NAS 仍可免信任访问手机，而且它是 `validate` 判定「已授权」的依据，不删就永远等不到信任弹窗。
