# iOS WDA 运维手册

## 1. 目标

用于保障 iOS 执行链路可用，覆盖 WDA 健康检查、端口映射、常见故障排查。

## 2. 依赖与前置

- Python 依赖：`requests`、`tidevice`、`facebook-wda`。
- 设备侧：iOS 设备已信任主机，WebDriverAgent 已可启动。
- 服务侧：`ios_execution` 开关开启。

## 3. 健康检查入口

- 单设备手动检测：`POST /devices/{serial}/wda/check`
- 设备状态字段：
  - `IDLE`: 可执行
  - `BUSY`: 执行中
  - `OFFLINE`: 离线
  - `WDA_DOWN`: WDA 不可用

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

## 6. 发布建议

1. 先灰度开启 `ios_execution`，观察失败率。
2. 监控 `WDA_DOWN` 比例与平均恢复时间。
3. 回滚时可仅关闭 `ios_execution`，Android 不受影响。
