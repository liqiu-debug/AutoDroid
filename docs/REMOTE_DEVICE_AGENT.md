# 远程设备接入指南（设备接入助手 Agent）

## 1. 能力与适用场景

平台部署在服务器 A，使用者在自己的电脑 B 上通过浏览器访问平台。本方案让**插在 B 机 USB 口上的 Android 设备**接入 A 上的平台，全功能可用（同步、截图、Scrcpy 投屏交互、用例执行、模型化巡检、Fastbot、兼容性回放）。

- **网络要求只有一条：B 能访问 A 的服务端口**（能打开平台网页即满足）。A 不需要能主动访问 B，跨网段、有防火墙均可。
- 数据链路走 **USB + 网络反向隧道**，不依赖手机 WiFi，比《[Android 无线 adb 指南](ANDROID_WIRELESS_ADB.md)》的 WiFi 直连模式更稳定，适合长任务。
- 仅支持 Android。iOS 请继续使用 WDA 无线模式（`IOS_WDA_OPS.md` §3.5）。

数据路径：

```
平台 adb server ⇄ 127.0.0.1:<隧道端口>(28100-28199，平台进程内中继)
  ⇄ WebSocket /ws/device-agent（B → A 反向连接，复用平台服务端口）
  ⇄ B 机 Agent 脚本 ⇄ B 机 adb forward ⇄ USB ⇄ 手机 adbd（tcpip 模式）
```

设备在平台侧的 serial 形态为 `127.0.0.1:<隧道端口>`。端口按（接入点, USB 序列号）持久固定，因此设备记录、执行历史、报告归属跨重启稳定。

## 2. B 机准备

| 依赖 | Windows | macOS |
|---|---|---|
| Python 3.8+ | [python.org 下载](https://www.python.org/downloads/)，安装时勾选 "Add python.exe to PATH" | 系统自带 python3 即可 |
| adb（platform-tools） | [下载解压](https://developer.android.com/tools/releases/platform-tools)后将目录加入 PATH，或运行 Agent 时用 `--adb` 指定 | 同左，或 `brew install android-platform-tools` |
| 手机 | 开启「开发者选项 → USB 调试」，用可传数据的数据线连接 | 同左 |

Agent 脚本为**单文件、纯 Python 标准库**，无需 `pip install` 任何依赖。

## 3. 启动步骤

1. **生成 API Token**：平台「设置 → API Token」创建一个 Token（`adk_` 开头，明文仅创建时显示一次）。Token 是机器凭证，长期有效，请妥善保管。
2. **下载 Agent**：设备中心 →「接入远程设备」→ 下载 `device_agent.py`（也可直接拷贝仓库 `scripts/device_agent.py`）。
3. **运行**：

   ```bash
   python device_agent.py --server http://<平台地址>:8000 --token adk_xxx --name 工位B
   ```

   参数说明：
   - `--server`：平台地址（http/https 均可，https 下自动走 wss）
   - `--token`：API Token
   - `--name`：接入点名称（默认取主机名）。**名称是接入点身份**，换名称会分配新的隧道端口、在平台侧形成新设备记录，请保持稳定。
   - `--adb`：adb 路径（PATH 中找不到时指定）
   - 也可以使用仓库内 `scripts/device_agent.bat`（Windows）/ `scripts/device_agent.sh`（macOS）：编辑文件顶部的 SERVER/TOKEN 后双击/执行。

4. **手机授权（首次）**：会弹出**两次**「允许 USB 调试吗？」——一次是 B 机 adb 的密钥，一次是平台服务器经隧道发来的密钥。都勾选「一律允许」并确认。
5. 平台设备中心点「一键同步物理设备」，出现带 `🔌 远程USB · 工位B` 徽标、serial 为 `127.0.0.1:281xx` 的设备即接入成功；卡片上会显示真实 USB 序列号。

## 4. 运行语义与故障恢复

| 场景 | 行为 |
|---|---|
| 设备拔出 | Agent 上报移除 → 平台断开隧道 adb 连接 → 设备转 OFFLINE |
| 设备插回 | Agent 自动重建隧道并上报 → 平台自动 `adb connect` → 同步后恢复 |
| 手机重启 | tcpip 模式失效，Agent 周期探测（约 10s）发现后自动重跑 `tcpip + forward` 自愈 |
| Agent 进程退出/断网 | 平台把接入点标记 OFFLINE（约 45s 内判定）并断开其设备；Agent 恢复后指数退避自动重连（1s→15s），设备端口不变、身份延续 |
| 平台重启 | Agent 自动重连并重新注册，无需人工干预 |
| B 机 adb server 被杀 | Agent 下一轮探测发现 forward 丢失，自动重建 |
| API Token 被禁用/删除 | Agent 收到鉴权失败提示后退出，需换有效 Token 重启 |

长任务（巡检/兼容性）执行中隧道断开的语义与无线 adb 断连一致：任务按现有链路进入错误终态、租约释放；请勿在任务运行中主动停止 Agent。

## 5. 安全边界

- **WebSocket 鉴权**：Agent 连接需有效 API Token；Token 与平台账号绑定，可在 Token 管理页随时禁用。
- **平台侧无新增对外暴露面**：隧道中继端口只绑定 `127.0.0.1`，仅供本机 adb server 使用。
- **设备侧授权**：经隧道接入仍走 adb 标准 RSA 授权，未在手机上确认过的密钥无法操控设备。
- **`tcpip 5555` 暴露面**：开启后手机 adbd 会在其所有网络接口监听 5555（与无线 adb 指南相同的注意事项）。本方案不需要手机联网，**纯 USB 场景建议手机关闭 WLAN**，暴露面即为零；Agent 退出不会自动关闭 tcpip 模式，重启手机或执行 `adb usb` 可关闭。
- 对外部署平台时建议启用 HTTPS 并用 `AUTODROID_CORS_ORIGINS` 收紧来源（Agent 对自签证书不做强校验，等价于浏览器手动信任）。

## 6. 性能与运维建议

- **投屏清晰度三档**：高清 1920px/8Mbps/60fps（USB 直插默认）、标准 1280px/2Mbps/30fps（无线 adb 默认）、流畅 800px/1Mbps/20fps/GOP2（**Agent 隧道设备默认**）。播放器工具栏可按设备切换档位，选择记忆在浏览器本地并在平台重启后自动重新下发；也可调 REST `POST /api/stream/devices/{serial}/stream-profile`（`{"profile": "hd|standard|smooth|auto"}`）。环境变量 `AUTODROID_SCRCPY_*`（高清档）与 `AUTODROID_SCRCPY_REMOTE_*`（标准档）仍可覆盖默认档参数；运行时档位优先于环境变量。
- **带宽预算**：投屏码率必须低于 B→A 可用带宽，否则隧道各段 TCP 队列积压、投屏延迟会持续累积。流畅档含开销约 1.2Mbps，建议 B→A ≥ 3Mbps；标准档约 2.5Mbps，建议 ≥ 6Mbps。注意：adb 对每台设备只有一条 transport 连接，码率吃满带宽会队头阻塞该设备的所有 adb 命令（同步、截图、控制）。`adb install` 大 APK 受 B→A 上行带宽限制。
- **截图链路**：设备中心快照弹窗对 Android 默认走实时投屏（不再整图截图过隧道），静态截图由服务端压为最长边 1280 的 JPEG 预览；用例编辑页对远程设备默认进入投屏模式（仅轮询层级 XML），静态模式截图为原分辨率 JPEG（作为图像模板裁剪素材，不缩放）。
- **延迟**：交互延迟增加约一个 B→A 往返（同城内网通常 <10ms，无感；跨公网/VPN 取决于线路 RTT）；隧道两端已启用 TCP_NODELAY，无 Nagle 合并延迟。
- **断连恢复时序**：Agent 断网后平台最迟约 45s 判定失联；Agent 恢复后 1s 起步重连；平台 adb keeper 每 10s 兜底巡检并修复 `adb connect`。整体恢复通常在 1 分钟内。
- **休眠**：长任务期间 B 机必须保持不休眠、不断网；一台电脑只运行一个 Agent 实例。
- **多设备**：一个 Agent 可同时接入多台 USB 设备；多个工位各自运行 Agent，互不影响。全平台隧道端口共 100 个（28100-28199），删除废弃接入点可释放端口。
- **反向代理**：若平台前置了 Nginx 等反代，需放行 WebSocket upgrade（`/ws/` 路径）并将读超时调大（建议 ≥300s）。直连 uvicorn 无需配置。

## 7. 排障

| 现象 | 排查 |
|---|---|
| Agent 打印「鉴权失败」 | Token 是否有效/被禁用；是否完整复制（adk_ 开头 52 字符） |
| Agent 注册成功但平台设备列表没有 | 点「一键同步物理设备」；看设备状态是否 unauthorized（手机上确认平台密钥授权弹窗） |
| 设备反复 OFFLINE | B 机是否休眠断网；数据线/USB 口是否松动；查看 Agent 控制台日志 |
| `设备 xxx 未授权` | 手机上确认 B 机的 USB 调试授权弹窗 |
| 平台日志 `隧道端口 281xx 监听失败` | A 机端口被占用，排查占用进程或删除接入点重新分配 |
| 投屏卡顿/延迟大 | 隧道设备默认流畅档（1Mbps）；在播放器工具栏确认档位未被调高，仍卡顿说明 B→A 带宽不足 3Mbps，需改善线路 |

隧道设备与直插/无线设备一样出现在 `adb devices` 中，平台侧可用 `adb -s 127.0.0.1:281xx shell` 直接诊断。
