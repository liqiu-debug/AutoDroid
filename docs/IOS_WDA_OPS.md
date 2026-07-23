# iOS WDA 运维手册

## 1. 目标

用于保障 iOS 执行与 MJPEG 预览链路可用，覆盖 WDA 健康检查、端口映射、无线直连、实时画面和常见故障排查。

## 2. 依赖与前置

- Python 依赖：`requests`、`tidevice`、`facebook-wda`。
- 设备侧：iOS 设备已信任主机，WebDriverAgent 已可启动。
- 服务侧：`ios_execution` 开关开启。
- 启动策略：默认优先使用 `tidevice xctest` 拉起设备上已安装的 WDA；仅在 macOS 下检测到 runner 缺失或 bundle id 不匹配时，才回退 `xcodebuild` 做首次安装/修复。两种情况会直接优先 `xcodebuild`：设备 iOS ≥ 17（`tidevice xctest` 已不可用），或设备仅以 WiFi 配对可见（无线，tidevice 无法可靠拉起；此时若未配置 Xcode 工程会以 `P3008` 快速失败并提示插线）。
- 等待策略：`tidevice` 启动默认短等待；`xcodebuild` 首次编译/安装默认会自动扩展到更长等待窗口，避免 WDA 实际已启动但 `check` 接口过早返回失败。如需调整，可配置 `ios_wda_xcodebuild_start_retry_attempts`。

## 3. 健康检查入口

- 单设备手动检测：`POST /api/devices/{serial}/wda/check`（无 `/api` 的历史 alias 仍保留）
- 设备状态字段：
  - `IDLE`: 可执行
  - `BUSY`: 执行中
  - `OFFLINE`: 离线
  - `WDA_DOWN`: WDA 不可用

## 3.5 无线模式（WiFi 直连 WDA）

iOS 设备一旦与主机完成 WiFi 配对，即使拔掉数据线也会出现在设备列表（usbmux 会枚举
network 设备）。但默认 WDA 地址走本地 relay（USB 式端口转发），无线状态下 relay 无法
建立、且手机上的 WDA 未启动，因此会显示 `WDA_DOWN`。

无线模式的原理：把该设备的 WDA 地址固定为手机 IP 直连（写入 `ios_wda_url.{serial}`），
之后执行、预检、截图、实时画面（MJPEG）全链路都走 `http://{手机IP}:8100`，无需数据线。

### 3.5.1 一键启用流程（推荐）

1. **插线**，在设备中心点「启动WDA」，等状态变为 `IDLE`（WDA 已在手机上运行）。
   - iOS 17+：`tidevice xctest` 已不可用，系统会优先用 `xcodebuild` 启动 WDA，需配置
     `ios_wda_xcodeproj_path` / `ios_wda_xcworkspace_path`（或依赖 Appium 自带的
     WebDriverAgent 自动发现，详见 §4 与 §5）。
2. 点「启用无线」。系统读取手机 WiFi IP（来自 WDA `/status` 的 `value.ios.ip`），
   校验 `http://{手机IP}:8100` 直连可达后写入配置。
3. **拔线**。此后正常执行/同步/看画面即可；卡片显示 `📶 无线` 徽标。

对应端点：

- `POST /api/devices/{serial}/wireless/enable`
  - 请求体（可选）：`{"ip": "192.168.1.23", "port": 8100}`；不传 `ip` 时自动从 WDA 读取。
  - 成功：`{serial, wireless_enabled: true, device_ip, wda_url, status}`。
  - 失败（不落库）：`400`，`detail` 前缀见下表。
- `POST /api/devices/{serial}/wireless/disable`
  - 删除 `ios_wda_url.{serial}`，回归默认本地 relay；`{serial, wireless_enabled: false, ...}`。

| 错误码 | 含义 | 处理 |
| --- | --- | --- |
| `P3003_WIRELESS_IOS_ONLY` | 非 iOS 设备 | 无线模式仅适用于 iOS |
| `P3004_WIRELESS_WDA_NOT_READY` | WDA 未运行 | 先插线「启动WDA」再启用 |
| `P3005_WIRELESS_IP_UNAVAILABLE` | 取不到手机 IP | 手机未连 WiFi / 开了 VPN；可在请求体手填 `ip` |
| `P3006_WIRELESS_DIRECT_UNREACHABLE` | 直连不可达 | 确认电脑与手机同一局域网、路由器未开 AP 隔离 |
| `P3007_WIRELESS_INVALID_PARAM` | IP/端口非法 | 检查请求体 |
| `P3008_WIRELESS_WDA_START_UNAVAILABLE` | 无线下无法启动 WDA | 仅 WiFi 配对时无法用 tidevice 启动 WDA，请插线重试或配置 Xcode 工程 |

### 3.5.2 手动配置（等价物 / 无前端时）

```
POST /api/settings/
[{"key": "ios_wda_url.<UDID>", "value": "http://<手机IP>:8100"}]
```

回滚 = 关闭无线 = 删除该 setting。

### 3.5.3 约束与排障

- **同一局域网**：电脑与手机需在同一子网，路由器不能开 AP/客户端隔离。
- **WDA 会被系统回收**：锁屏、内存压力、长时间后台都可能让 WDA 退出；无线状态下**无法
  远程重启 WDA**，需插线重新「启动WDA」并重新「启用无线」。建议保持屏幕常亮、关闭自动锁屏。
- **手机 IP 变化需重新启用**：DHCP 续租/换网会改 IP，导致直连失效。建议在路由器为设备
  绑定固定 IP。
- **VPN 干扰取 IP**：手机开 VPN 时 `value.ios.ip` 可能是隧道地址，导致直连失败，可手动
  指定局域网 IP。
- **同步与在线判定**：无线设备即使不在 usbmux 扫描结果，只要直连 WDA 健康即保持在线；
  直连不可达则标记 `OFFLINE`（不会误报 `WDA_DOWN`）。`GET /devices/?refresh_ios_wda=true`
  会对已启用无线的 OFFLINE 设备重试直连、健康则自动恢复 `IDLE`。
- **安全**：WDA 端口无鉴权，启用无线等于把 WDA 暴露在局域网内，请仅在可信网络使用。

## 4. WDA URL 与端口策略

WDA 地址解析优先级：

1. `ios_wda_url.{device_serial}`
2. `ios_wda_url_map[device_serial]`
3. `ios_wda_url`
4. 自动本地 relay（默认 `http://127.0.0.1:{8200-8299}`）

多设备并发时，系统为每台设备分配独立 relay 端口，避免冲突。

## 5. 常见问题与处理

### 5.1 `P1005_WDA_UNAVAILABLE`

现象：

- 预检失败，提示 WDA health check failed。
- 设备状态转为 `WDA_DOWN`。

处理：

1. 在设备中心执行“检测WDA”。
2. 检查设备是否在线、已信任主机。
3. 检查 WDA URL 配置是否可达。
4. 重启后端服务，触发 relay 重建。

### 5.2 端口冲突/占用

现象：

- 本地 relay 建立失败或设备连接异常。

处理：

1. 确认 8200-8299 端口段是否被占用。
2. 清理异常进程后重启服务。
3. 必要时更改端口策略并重启。

### 5.3 执行前全部被拦截

现象：

- 场景运行返回 `S1001_SCENARIO_PRECHECK_FAILED`。

处理：

1. 先看 `blocked_prechecks` 中首个设备原因。
2. 若是 `WDA` 问题，按 5.1 修复。
3. 若是动作/选择器问题，按执行规范补齐 iOS 覆盖。

## 6. MJPEG 实时画面

基于 WDA 内置 MJPEG server（设备端默认端口 `9100`）提供 iOS 近实时画面流。前端把它作为只读预览层；点击、框选、元素审查和步骤录制仍使用静态截图与 WDA 页面层级，不能从 MJPEG 帧直接生成定位器。

### 6.1 端口与 relay 策略

- 设备端 MJPEG 端口默认 `9100`（WDA `mjpegServerPort`）。
- WDA URL 解析为本机（含自动 relay）时，系统通过 `tidevice relay` 将设备端
  MJPEG 端口映射到本地 `9300-9399` 端口段，与 WDA HTTP relay（8200-8299）相互独立。
- WDA URL 指向远端主机时，直接连接 `{远端主机}:{MJPEG端口}`，不建立本地 relay。
- 每台设备只维持一条上游连接，多客户端共享广播；客户端全部断开或上游断开后，
  自动关闭上游连接并回收对应 relay。

### 6.2 配置项（SystemSetting）

解析优先级对齐 `ios_wda_url` 风格（scoped -> map -> global -> 默认值）：

| 配置 key | 说明 | 默认值 |
| --- | --- | --- |
| `ios_mjpeg_port.{serial}` / `ios_mjpeg_port_map`(JSON) / `ios_mjpeg_port` | 设备端 MJPEG 端口 | `9100` |
| `ios_mjpeg_framerate.{serial}` / `ios_mjpeg_framerate` | 推流帧率（1-60） | `15` |
| `ios_mjpeg_quality.{serial}` / `ios_mjpeg_quality` | 截图质量（1-100） | `50` |

帧率/质量在新建流时通过 WDA `/appium/settings`
（`mjpegServerFramerate` / `mjpegServerScreenshotQuality`）尽力设置，
失败仅记录日志、不阻断推流（此时使用 WDA 端默认参数）。

### 6.3 端点契约

- `WS /ws/ios-mjpeg/{serial}`：每条二进制消息为一帧完整 JPEG（前端 blob -> img 渲染）。
  - 关闭码 `4005`：WDA/MJPEG 上游不可用，reason 含 `P1005_WDA_UNAVAILABLE`；
  - 关闭码 `4000`：内部错误；
  - 关闭码 `1000`：上游正常结束（如 WDA 停止）。
- `GET /api/stream/ios-mjpeg/{serial}`：`multipart/x-mixed-replace` 透传，
  可直接用于 `<img src>`（兼容路径 `/stream/ios-mjpeg/{serial}` 等）。
  - `503`：WDA/MJPEG 上游不可用，detail 含 `P1005_WDA_UNAVAILABLE`；
  - `500`：内部错误。

两类端点共享同一条上游连接，可混用；鉴权姿态与 `/ws/scrcpy/{serial}` 一致（无鉴权）。

### 6.4 故障排查

1. 建流返回 `P1005_WDA_UNAVAILABLE`：
   - 先按 5.1 确认 WDA 本身健康（`POST /devices/{serial}/wda/check`）；
   - 确认设备上 WDA 版本带 MJPEG server（Appium WDA 均内置，端口 9100）；
   - 若设备端 MJPEG 端口非默认值，配置 `ios_mjpeg_port.{serial}`；
   - 检查本地 `9300-9399` 端口段是否被占用。
2. 连接后无画面 / 画面停止：
   - 上游持续 15 秒无数据会被判定为 WDA 挂死并主动断开客户端，重连即可；
   - WDA 崩溃后重启 WDA，再重新发起连接（relay 会自动重建）。
3. 带宽/CPU 过高：调低 `ios_mjpeg_framerate` 与 `ios_mjpeg_quality`。

## 7. 发布建议

1. 先灰度开启 `ios_execution`，观察失败率。
2. 监控 `WDA_DOWN` 比例与平均恢复时间。
3. 回滚时可仅关闭 `ios_execution`，Android 不受影响。

## 8. 与智能巡检的边界

模型化智能巡检和基于巡检路径的兼容性回放目前只支持 Android，不会占用 iOS WDA 或 MJPEG relay。iOS 继续通过标准用例/场景执行、静态层级录制和报告链路完成自动化；不要为 iOS 设备开启或伪造巡检 Profile。

跨端标准动作见 [执行规范](EXECUTION_SPEC.md)，巡检与回放的 Android 专项边界见 [巡检、回放与证据资产指南](INSPECTION_REPLAY_ASSETS.md)。
