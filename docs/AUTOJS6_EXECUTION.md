# AutoJs6 设备端执行指南（用例导出 → 手机离线执行 → 结果回传）

> 除本文的自建脚本包链路外，平台还提供原生 Android 客户端 **[AutoDroid-Android](https://github.com/liqiu-debug/AutoDroid-Android)**（内置同一套 AutoJs6 引擎与 runtime），免安装脚本、免 Agent 配置，接口契约见 [App 接口契约](APP_RUNNER_API.md)。本文保留给「官方 AutoJs6 + Agent 脚本」与自定义脚本包的场景。

## 1. 能力与适用场景

把平台用例/场景导出为 **AutoJs6 脚本包**，在装了 AutoJs6 的 Android 手机上**脱离 PC/USB 独立执行**，执行结果自动回传平台：报告中心可见、大盘统计、飞书卡片通知。

适用：

- 无人值守回归：手机放工位/设备墙，定时或随时接任务执行，不需要电脑在场；
- 多机并行：每台手机各自跑自己的任务，互不干扰；
- 现场/外场验证：不方便接电脑的场景。

不适用（继续走 PC 链路）：

- 交互式录制/调试、实时投屏（scrcpy）——设备端执行没有画面流，调试请用 PC 链路；
- Fastbot、模型化巡检、兼容性回放（PC 驱动架构专项）；
- iOS（AutoJs6 仅 Android）。

## 2. 架构与数据路径

```
[AutoDroid 平台]
  TestCaseStep / Scenario
    │ ①导出器 backend/autojs_export/（zip 脚本包：main.js + runtime.js + cases/*.js + assets/*.png）
    ▼
  设备任务 DeviceRunTask（PENDING）→ 包存 uploads/autojs6_packages/
    │ ②设备领取（API Token 鉴权）GET /api/device-runs/pending
    ▼
[Android 手机 · AutoJs6]
  scripts/autojs6/autodroid_agent.js（常驻）
   注册/心跳 → 领任务 → 下载解压 → require(main.js) 执行
    │ ③结果回传 POST /api/device-runs/report（含步骤级状态/错误码/失败截图）
    ▼
平台 ④落库 TestExecution/TestResult → 生成 HTML 报告 → 飞书卡片 → 报告中心/大盘
```

复用平台既有设施：API Token（`adk_`）、执行记录/报告模型、`report_generator`、`notification_service`（飞书）、设备表（设备中心可见，`autojs6:` 前缀 + `connection_type=autojs6`）。

## 3. 平台侧使用

### 3.1 导出脚本

- 用例编辑页 → 顶部「导出脚本」：预检通过后可 **下载脚本包（zip）** 或 **单文件 .js**（仅适合无图像步骤的简单用例）；
- 用例列表 → 勾选多个用例 → 「批量导出」；
- 场景编辑页 → 「导出脚本」：包含全部子用例与串联入口。

导出前自动预检（`GET /api/autojs/cases/{id}/precheck`）：

| 预检项 | 说明 |
|---|---|
| `P1003_SELECTOR_MISSING` | click/wait 步骤没有可用定位器 |
| `EXPORT_UNSUPPORTED_LOCATOR` | xpath 定位（AutoJs6 不支持，建议改用 text/id/description） |
| `P1004_APP_MAPPING_MISSING` | start_app/stop_app 的 app_key 未映射且不是包名 |
| `EXPORT_IMAGE_MISSING` | 图像步骤的模板图缺失 |
| `EXPORT_UNRESOLVED_VARIABLE`（警告） | 存在未解析的 `{{KEY}}`（运行时若由前序用例导出则可忽略） |

### 3.2 下发到设备

「导出脚本」弹窗 → 「下发到设备」创建任务（`POST /api/autojs/tasks`），设备端 Agent 在线时自动领取执行。任务列表：`GET /api/autojs/tasks`；取消：`POST /api/autojs/tasks/{task_id}/cancel`。

执行完成后：报告中心出现 `device_serial=autojs6:…`、`batch_name=AutoJs6 设备端执行` 的执行记录；飞书群收到卡片（通过率/失败摘要/报告链接）。

### 3.3 动作映射（与 PC 链路的保真约定）

13 个动作全部支持（`backend/autojs_export/runtime.js` 解释执行）：

- `click`/`wait_until_exists`：定位候选链逐个尝试；text 类先精确匹配 2 秒再 `textContains` 降级（与 PC 一致）；
- `input`：元素 `setText`（无障碍）→ 聚焦 `automator.input` 回退；无定位器时向当前焦点输入；
- `assert_text`：页面级 `textContains || descContains`（contains/not_contains）；
- `assert_image`/`click_image`：OpenCV 多尺度模板匹配（0.8–1.2）+ SSIM 二次校验；click_image 带"模板图 OCR 文本提示"与"点击后页面无变化重试"（镜像 PC 行为）；
- `extract_by_ocr`：设备端 OCR（默认 MLKit，可选 Paddle/Rapid）；`regex/boundary/preset` 提取规则与 PC 完全一致；`output_var` 写入运行时变量，场景中后续用例的 `{{KEY}}` 运行时渲染；
- `start_app/stop_app`：`app_key` 在导出时解析为包名；stop_app 优先 `app.kill`（需 root/Shizuku），失败时自动回退为**无障碍路径**（打开应用详情页点「强行停止」，无需 root）；
- `swipe/back/home/sleep`：与 PC 语义一致（swipe 按 uiautomator2 swipe_ext scale=0.8 换算）。

> ⚠️ **不要用 PC 链路同时驱动同一台设备（实测冲突）**：平台的 PC 链路（设备中心截图/层级 dump、用例编辑页、执行）基于 uiautomator2 的 UiAutomation 连接，而 Android 的 UiAutomation 会**压制（suppress）无障碍服务**——只要平台在轮询这台手机（哪怕只是打开了用例编辑页），AutoJs6 的无障碍就会被系统下线，所有 UI 自动化步骤都会报「无障碍服务已启用但未运行」。设备端执行时请**拔掉 USB / 不要在平台上操作该设备**。

运行时语义：步骤级 `PASS/SKIP/WARNING/FAIL`、错误码 `E2xxx`、`retry_count` 重试（1s 退避 + attempts）、`error_strategy`（ABORT/CONTINUE/IGNORE）、失败自动截图（JPEG base64 随上报）。

## 4. 设备侧准备（一次性 provisioning）

每台设备做一次，之后可永久脱线：

1. 安装 AutoJs6 APK（官方发布版即可，无需改包）；
2. 打开 AutoJs6 → 开启**无障碍服务**（Android 13+ 需先「允许受限设置」）；
3. 系统设置：电池优化白名单 / 自启动允许（MIUI/HyperOS、EMUI、ColorOS 必做，否则长任务被杀）；
4. （可选，推荐）`adb shell appops set org.autojs.autojs6 PROJECT_MEDIA allow` —— 允许 MediaProjection 免弹窗（重启仍有效；图像/OCR 步骤优先走无障碍截图，此命令是兜底）；
5. 将 `scripts/autojs6/autodroid_agent.js` 与 `config.json`（参考 `config.example.json`，填平台地址 + API Token + 设备名）放到手机 AutoJs6 的同一目录；
6. 运行 `autodroid_agent.js` 验证：日志出现注册成功与轮询；建议加入 AutoJs6「定时任务」并开启开机自启（或在打包应用中内置本脚本）。
   - 电脑侧可用 `scripts/autojs6/agent_watchdog.sh` 看门狗（手机 USB 连接期间自动拉起意外退出的 Agent）：`nohup bash scripts/autojs6/agent_watchdog.sh <设备序列号> &`

> **OPPO / ColorOS 等国产 ROM 特别注意（真机实测）**：
> - **不要用 `adb shell am force-stop org.autojs.autojs6` 停应用**——系统会同时取消其无障碍服务注册，需要重新开启；
> - 无障碍服务"已开启但不生效"（脚本报「无障碍服务已启用但未运行」）时：在**系统设置 → 无障碍 → 已下载的应用 → AutoJs6** 里把开关**关掉再重新打开**，然后**重启一次设备**（ColorOS 的进程管理会拦截无障碍服务进程绑定，重启后恢复正常）；
> - 重启后首次需解锁屏幕一次，adb 才会重新识别设备（USB 模式可能被重置为"仅充电"，需要在手机上重新选择"传输文件"）；
> - 建议同时执行：`adb shell dumpsys deviceidle whitelist +org.autojs.autojs6`（电池白名单）；ColorOS 上开启无障碍后应用会自动获得自启动权限，无需额外设置。

> API Token 在平台「设置 → API Token」创建（`adk_` 开头），绑定创建者账号，可随时禁用。

## 5. 脚本包格式

```
project.json    # AutoJs6 项目描述（可作为「项目」导入）
README.txt      # 使用说明
manifest.json   # 元信息：kind(case/scenario/batch)、用例清单、变量、预检结果、生成器版本
main.js         # 入口（自动生成）
runtime.js      # 设备端运行时（随包分发，勿手改）
cases/*.js      # 各用例 JSON 步骤清单（由 main.js 加载）
assets/*.png    # 图像步骤模板图（来自平台 static/images/element_*.png）
```

**手动导入使用**：将 zip **完整解压**（保留目录结构）到 AutoJs6 脚本目录，或作为「项目」导入；然后**运行 main.js**（项目模式下运行项目即可）。不要单独运行 `cases/*.js` 或 `runtime.js`——它们是模块，由 main.js 通过 require 加载；单独运行没有 `module` 定义会报 `ReferenceError: "module" 未定义`。无平台地址/Token 时结果写入同目录 `report.json`。单文件模式已内嵌 runtime 与模板图（base64），直接运行即可。

## 6. 排障

| 现象 | 排查 |
|---|---|
| 脚本报 `createContext 是 object 而非函数`（或变量赋值"不生效"） | **不要用 `R` 作为脚本变量名**：AutoJs6 引擎把 Android 资源类 `R` 暴露为全局对象，顶层脚本中 `var R = ...` 会被静默忽略。生成的脚本已改用 `AUTODROID_RUNTIME`；自写脚本请避开 `R`、`global` 等引擎全局名 |
| 下发任务后手机 AutoJs6 里"没有这个任务" | 任务是**设备端 Agent 主动领取**的，不会推送到手机：需先按 §4 部署并运行 `autodroid_agent.js`（config.json 里 server 必须是手机可访问的局域网/公网地址，不能是 127.0.0.1） |
| 单独运行 `cases/*.js` 或 `runtime.js` 报 `"module" 未定义` | 这两个是模块文件，由 main.js 加载执行；请运行 main.js（新版本包已做防御，直接运行也不会崩溃，但不会执行任何步骤） |
| 运行 main.js 报 `manifest.json: ENOENT` | zip 解压不完整或目录结构被拍平；请完整解压（保留 `cases/`、`assets/` 子目录）后从包根目录运行 main.js |
| 步骤失败 `E2001 元素未找到` | 确认无障碍服务已开启、页面已加载；必要时重录用例刷新定位器 |
| 脚本报「无障碍服务已启用但未运行」 | ① 先确认**没有**用 PC 链路（平台的设备中心/用例编辑页）同时在驱动这台手机——uiautomator2 会压制无障碍服务，拔掉 USB 或停止在平台上操作该设备即可恢复；② ColorOS/OPPO 等机型：系统设置 → 无障碍 → 已下载的应用 → AutoJs6 开关关掉再打开，必要时重启设备（详见 §4 提示） |
| 同一任务被执行两次 | 检查手机上是否有**两个 Agent 实例**（重复启动了 `autodroid_agent.js`）；新版 Agent 自带单实例保护，服务端任务领取也已改为原子领取（并发只会有一个赢家）。在 AutoJs6 的「运行中的脚本」里停掉多余实例即可 |
| 设备端无任何日志可查 | Agent 会把日志写到同目录 `agent.log`（含执行期日志），`adb shell cat /storage/emulated/0/脚本/autodroid/agent.log` |
| 图像/OCR 步骤报截图失败 | Android 11+ 走无障碍截图（无需弹窗）；低版本需授予投屏权限（每次弹窗）或执行第 4 步 appops 命令 |
| OCR 识别结果与 PC 不一致 | 设备端默认 MLKit 引擎与 PC 侧 PaddleOCR 存在差异；对提取结果敏感的场景建议用 `regex/boundary` 规则约束（两端规则一致），或后续在 AutoJs6 fork 中嵌入 PP-OCRv5 模型对齐 |
| `stop_app` 失败 | `app.kill` 需要 root 或 Shizuku；无 root 设备建议改用其他方式终止应用 |
| Agent 日志「轮询失败」连续多次 | 检查平台地址/Token/网络；服务端需包含 `/api/device-runs/*` 路由（本功能版本） |
| 任务一直是 PENDING | 设备端 Agent 未运行/离线；`GET /api/autojs/tasks` 查看状态；CLAIMED 超 15 分钟自动回收重试 |
| 上报后报告中心没有记录 | 检查上报返回的 execution_id；重复上报同一 task_id 会覆盖旧执行记录（幂等） |
| 场景前序用例变量在后续用例为空 | 检查 `extract_by_ocr` 的 `output_var` 是否设置、`{{KEY}}` 拼写是否一致 |

## 7. 关键代码入口

- 导出器：`backend/autojs_export/`（generator / precheck / packager / runtime.js）
- 平台 API：`backend/api/autojs.py`（导出 + 设备注册/领取/下载/上报 + 任务管理）
- 设备端 Agent：`scripts/autojs6/autodroid_agent.js`
- 任务表：`backend/models.py::DeviceRunTask`
- 测试：`backend/tests/test_autojs_export.py`
