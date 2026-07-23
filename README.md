# AutoDroid

AutoDroid 是一个面向 Android/iOS 的低代码 UI 自动化测试平台，覆盖设备管理、可视化录制、用例与场景编排、跨端执行、定时调度、模型化智能巡检、稳定性探索、兼容性回放、报告分析和通知闭环。

当前产品边界是：**Android 支持 Scrcpy 实时交互或静态截图录制，iOS 支持 WDA MJPEG 近实时预览，并以静态截图和页面层级完成元素审查、交互录制；Android/iOS 均支持标准用例执行**。模型化智能巡检、Fastbot、冷热启动和兼容性测试目前是 Android 专项能力。同一套标准步骤可以按平台分发，并通过预检提前发现动作、定位器、变量、应用映射和 WDA 状态问题。

## 核心能力

| 业务域 | 主要能力 |
|---|---|
| 设备中心 | Android/iOS 设备发现与同步、设备状态、截图、解锁、重启、iOS WDA 检查与启动 |
| 安装包管理 | APK/Ad Hoc IPA 上传与版本管理，向已接入的 Android/iOS 设备一键安装 |
| 可视化录制 | Android 实时投屏/静态截图录制；iOS 静态截图、元素审查、交互录制与单步调试 |
| 用例管理 | 目录与标签、复制、变量、标准步骤模型、平台覆盖、执行前预检、运行中止 |
| 场景编排 | 多用例串联、变量上下文传递、多设备并发、设备级预检过滤 |
| 跨端执行 | Android 使用 uiautomator2，iOS 使用 facebook-wda；支持统一动作与平台定位覆盖 |
| 调度中心 | 单次、每天、每周、循环任务；支持 UI 场景、Fastbot 和模型化巡检任务 |
| 智能稳定性 | Fastbot 探索、Crash/ANR、CPU/内存、framestats 卡顿检测、回放与 Trace 分析 |
| 冷热启动专项 | ADB 启动计时、uiautomator2 首页就绪检查、慢启动 Perfetto 取证 |
| 模型化智能巡检 | Android 双业务线自主探索、页面身份与页面族建模、安全边界、覆盖调度、实时拓扑和稳定回放路径 |
| 兼容性测试 | Android APK 版本/机型/巡检快照对比，以及基于巡检冻结路径的当前安装版本回放 |
| 报告与大盘 | 用例、Fastbot、兼容性与巡检报告；内容寻址证据资产、失败截图、趋势与设备状态概览 |
| AI 辅助 | 自然语言生成测试步骤、Fastbot 日志根因分析、OpenAI 兼容接口 |
| 系统管理 | JWT 登录、管理员用户管理、公开注册开关、资源删除权限、飞书通知 |
| 移动端访问 | 移动端登录、运行概览、设备状态、用例/场景执行和报告查看 |

## 技术架构

```mermaid
flowchart TD
    UI["Vue 3 Web 前端"] -->|"HTTP / WebSocket"| API["FastAPI 后端"]
    API --> AUTH["认证与权限"]
    API --> CASE["用例 / 场景 / 调度"]
    API --> REPORT["报告 / 大盘 / 通知"]
    API --> STREAM["Scrcpy H.264 / WDA MJPEG"]
    API --> INSPECT["智能巡检 / 兼容性回放"]
    CASE --> MODEL["标准步骤模型 + Legacy 兼容"]
    MODEL --> ANDROID["Android Driver\nuiautomator2 / ADB"]
    MODEL --> IOS["iOS Driver\nfacebook-wda / tidevice"]
    API --> FASTBOT["Fastbot / 性能与卡顿分析"]
    INSPECT --> ANDROID
    INSPECT --> ASSET["内容寻址证据资产"]
    AUTH --> DB["SQLite / SQLModel"]
    CASE --> DB
    REPORT --> DB
    ASSET --> DB
```

### 技术栈

| 层级 | 技术 |
|---|---|
| 前端 | Vue 3、Element Plus、Vite、Pinia、ECharts |
| 后端 | Python、FastAPI、SQLModel、SQLite、APScheduler |
| Android | ADB、uiautomator2、adbutils、Scrcpy |
| iOS | tidevice、facebook-wda、WebDriverAgent、Xcode devicectl |
| 图像与性能 | OpenCV、Pillow、Perfetto、framestats |
| 实时通信 | WebSocket、Scrcpy H.264、WDA MJPEG |
| 报告与通知 | Jinja2 HTML 报告、飞书机器人 Webhook |

## 平台能力矩阵

| 能力 | Android | iOS | 说明 |
|---|---|---|---|
| 设备发现与管理 | 支持 | 支持 | iOS 状态包含 `WDA_DOWN` |
| 安装包推送 | 支持 | 支持 | Android 使用 ADB；iOS 17+ 使用 devicectl，IPA 必须为包含设备 UDID 的 Ad Hoc 包 |
| 静态截图录制 | 支持 | 支持 | iOS 通过 WDA 获取截图和页面层级，交互后刷新截图 |
| 实时画面 | 支持交互 | 支持只读预览 | Android 使用 Scrcpy；iOS 使用 WDA MJPEG，录制仍以静态层级为准 |
| 用例执行 | 支持 | 支持 | iOS 需开启执行开关且 WDA 健康 |
| 场景并发执行 | 支持 | 支持 | 执行前按设备进行预检 |
| 图像/OCR 步骤 | 支持 | 支持 | 具体动作与参数见执行规范 |
| 实时 Scrcpy 画面 | 支持 | 不支持 | 用于 Android 预览和触控 |
| Fastbot 探索 | 支持 | 不支持 | Android 专项能力 |
| 冷热启动专项 | 支持 | 不支持 | Android 专项能力，首页就绪 P90 为主指标 |
| 视觉兼容性测试 | 支持 | 不支持 | Android 生产 APK 专项能力，按页面集合采集截图和 UI XML |
| 模型化智能巡检与路径回放 | 支持 | 不支持 | 单台 Android 设备独占执行，可形成可复用的稳定路径 |

## 移动端适配

Web 前端支持 PC、移动和自动识别三种客户端模式。自动模式会在视口宽度不超过 `768px` 或检测到触控型指针时启用移动布局，用户也可以通过页面上的客户端模式开关手动切换，选择结果会保存在浏览器本地。

移动端定位为“查看与轻量执行入口”，目前支持：

- 登录、注册和退出登录
- 运行概览、KPI、最近异常与最近执行
- Android/iOS 设备状态、设备同步、快照、iOS WDA 启动/检测和设备解锁
- 用例搜索、分页、选择设备与环境并发起执行
- 场景搜索、状态筛选、执行场景和查看最近报告
- UI/Fastbot 报告列表、状态筛选以及报告详情和失败截图查看
- 固定底部导航：概览、设备、用例、场景、报告

移动端还支持巡检报告列表与详情查看。用例编辑、场景复杂编排、变量与安装包管理、定时任务、专项测试配置、用户管理和系统配置仍需使用 PC 模式。

## 项目结构

```text
AutoDroid/
├── backend/
│   ├── api/                    # 认证、用例、场景、设备、报告、任务等 API
│   ├── device_stream/          # Scrcpy 视频流、设备监听与触控转发
│   ├── drivers/                # Android/iOS 驱动与跨端执行器
│   ├── inspection/             # 巡检语义建模、探索引擎、回放、实时状态和覆盖审计
│   ├── templates/              # 用例与场景 HTML 报告模板
│   ├── tests/                  # 后端自动化测试
│   ├── artifact_store.py       # 内容寻址资产、引用关系、分层保留和 GC
│   ├── compatibility_replay.py # 巡检冻结路径的兼容性回放计划
│   ├── device_execution_lease.py # 长任务设备独占租约
│   ├── main.py                 # FastAPI 入口、录制接口、WebSocket、SPA 托管
│   ├── models.py               # SQLModel 数据模型
│   ├── step_contract.py        # 标准步骤与 Legacy 步骤转换
│   ├── scheduler_service.py    # APScheduler 定时调度
│   └── fastbot_runner.py       # Fastbot、冷热启动与性能监控执行引擎
├── frontend/
│   ├── src/api/                # HTTP API 封装
│   ├── src/components/         # 设备画面、步骤编辑、日志等公共组件
│   ├── src/layout/             # 桌面端与移动端布局
│   ├── src/stores/             # 用户与用例状态管理
│   └── src/views/              # 设备、用例、场景、任务、报告等页面
├── docs/
│   ├── PROJECT_OVERVIEW_CN.md  # 深度架构与实现说明
│   ├── EXECUTION_SPEC.md       # 标准步骤、动作矩阵和错误码
│   ├── INSPECTION_REPLAY_ASSETS.md # 巡检、回放与证据资产运维指南
│   └── IOS_WDA_OPS.md          # iOS WDA 运维手册
├── scripts/start_lan.sh        # 构建前端并启动一体化服务
├── resources/                  # Fastbot 与 Scrcpy 运行资源
├── requirements*.txt           # 全量及分能力 Python 依赖
└── README.md
```

`database.db`、`reports/`、`asset_store/`、`uploads/`、`static/images/` 和运行日志属于本地数据或生成物，不应作为源码提交。

## 快速开始

### 环境要求

- Python 3.9+，推荐使用 Python 3.10 或更新版本
- Node.js `^20.19.0` 或 `>=22.12.0`
- npm
- Android：已安装 ADB，设备已开启 USB 调试并授权当前主机
- iOS：设备已信任当前主机，已安装并可启动 WebDriverAgent；首次构建或修复 WDA 推荐使用 macOS + Xcode

### 1. 安装后端依赖

推荐使用虚拟环境：

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
pip install -r requirements.txt
```

`requirements.txt` 会安装完整能力。也可以按部署目标组合安装：

```bash
# 后端基础能力
pip install -r requirements-base.txt

# 按需追加
pip install -r requirements-android.txt
pip install -r requirements-ios.txt
pip install -r requirements-ai.txt
```

### 2. 一体化启动

```bash
bash scripts/start_lan.sh
```

脚本会在缺少 `frontend/node_modules` 时安装前端依赖、构建 `frontend/dist`，然后由 FastAPI 同时提供 API 和前端页面。

- 本机访问：`http://127.0.0.1:8000`
- API 文档：`http://127.0.0.1:8000/docs`
- 局域网访问：启动日志会输出当前主机地址

可用启动参数：

```bash
HOST=127.0.0.1 PORT=9000 bash scripts/start_lan.sh
SKIP_BUILD=1 bash scripts/start_lan.sh
PYTHON_BIN=/path/to/python bash scripts/start_lan.sh
```

### 3. 开发模式

后端：

```bash
source .venv/bin/activate
uvicorn backend.main:app --host 0.0.0.0 --port 8000 --reload
```

前端：

```bash
cd frontend
npm install
npm run dev -- --host
```

开发模式默认访问 `http://127.0.0.1:5173`，Vite 会将 `/api` 和 `/ws` 代理到 `127.0.0.1:8000`。

### 4. 首次登录

后端首次启动时会自动创建管理员：

```text
用户名：admin
密码：123456
```

登录后请立即在账号设置中修改密码。JWT 密钥默认会自动生成并持久化到项目根 `.jwt_secret`；对外部署时建议通过 `AUTODROID_SECRET_KEY` 环境变量显式指定，并根据需要关闭公开注册。注意：从旧版本（硬编码密钥）升级后，所有已登录用户需要重新登录一次。

## 推荐使用流程

1. 在设备中心同步 Android/iOS 设备并确认状态。
2. 在 App 包管理和全局变量库中维护应用与环境数据。
3. 使用 Android 实时投屏/静态截图或 iOS 静态截图录制用例，补充断言、图像、OCR、等待和容错策略。
4. 为跨端步骤设置 `execute_on` 和 `platform_overrides`，运行预检后执行用例。
5. 将多个用例编排为场景，通过场景上下文传递运行时变量。
6. 选择一台或多台设备执行场景；不满足条件的设备会被预检过滤。
7. 在定时任务中创建单次、每日、每周或循环任务。
8. 在“专项测试 -> 冷热启动”中配置包名、启动模式、启动次数和首页就绪 locator，查看首页就绪 P90 与慢启动 Trace。
9. 在“专项测试 -> 兼容性测试 -> 页面合集”中维护预设页面，每个页面引用已有用例作为进入脚本。
10. 在“专项测试 -> 兼容性测试 -> 测试配置”中选择旧版 APK、新版 APK、Android 设备和页面合集，发起升级兼容或干净安装对比。
11. 开启 `model_inspection` 后，在“专项测试 -> 模型化智能巡检”维护未登录/已登录业务线，运行巡检并在报告中审核稳定状态。
12. 将审核后的巡检快照用于版本/机型兼容性对比，或对设备当前安装版本执行冻结路径回放。
13. 在报告中心和运行大盘查看结果、失败截图、巡检拓扑、趋势和设备告警。

## 模型化智能巡检与回放

模型化智能巡检用于在单台 Android 设备上自动发现业务页面和安全动作，不等同于随机点击。Profile 固定包含 `guest` 与 `authenticated` 两条业务线，每条业务线通过准备/入口用例和就绪断言进入稳定起点；输入规则、危险动作规则、脱敏规则及预算共同约束探索范围。

- **页面建模**：将采集结果拆分为 Page Template、State、Observation 和 Transition，并以页面族聚合同构页面，避免重复遍历。
- **安全探索**：付款、删除、提交等危险语义默认阻断；坐标动作、输入值和敏感截图受规则与脱敏约束。
- **覆盖调度**：按页面族、动作角色和 Coverage Contract 选择下一步，滚动视口作为 Observation 保留，不挤占业务 State。
- **实时报告**：运行时可查看只读设备画面、阶段、当前页面和动作覆盖；断开报告页不会中止巡检。
- **稳定路径**：人工勾选稳定 State/Observation 后冻结为回放路径，可用作兼容性快照基线。
- **已安装版本回放**：预检设备当前包版本和冻结计划，确认后只执行选中的安全链路，不上传或安装 APK。

巡检默认关闭，启用顺序和 Graph v8、回放、资产保留的完整契约见 [巡检、回放与证据资产指南](docs/INSPECTION_REPLAY_ASSETS.md)。

## 兼容性测试专项

兼容性测试面向 Android 生产 APK，不把兼容性简单等同于执行 UI 场景，而是按“基线采集 -> 候选采集 -> 多信号对比”生成报告。

- **页面合集**：页面合集是可复用配置模板，只保存页面名称、进入页面用例、稳定等待时间和必需文本。页面进入步骤复用现有用例编辑器，不重复维护一套步骤编辑能力。
- **当前版本基线**：旧版 APK 可选择“当前版本”。此时不会重新安装旧包，而是直接校验设备上已安装目标包名并采集基线。
- **执行模式**：升级兼容模式会先安装旧包并采集基线，再覆盖安装新版且不清数据；干净安装对比模式会分别清理安装旧包和新包，适合纯视觉回归。
- **采集内容**：每个页面采集截图、UI XML、当前 Activity 和 logcat 错误片段。必需文本只从 UI XML 判断，不使用 OCR。
- **对比结果**：页面结果包含像素差异比例、结构相似度、视觉相似度、XML 差异比例、截图尺寸变化、Crash/ANR 和必需文本缺失等指标。视觉或结构差异默认作为警告，安装失败、页面不可达、Crash/ANR、必需文本缺失判为失败。
- **报告归档**：发起任务时会把页面合集名称和页面列表复制到兼容性任务快照中。后续编辑或删除页面合集，不会影响历史兼容性报告展示。
- **巡检来源**：可从巡检中人工勾选的稳定 State/Observation 创建快照、版本或机型对比；创建时冻结路径、规则和脱敏基线。
- **当前安装版本回放**：在 `compatibility_installed_replay` 开启时，先调用回放预检冻结计划与设备快照，再执行所选完整路径或安全边界前缀。
- **报告删除**：报告中心的兼容性报告支持删除。删除会移除任务、设备单元、页面结果和 `reports/compatibility/{run_id}` 下的截图、XML、差异图等产物；运行中的任务不能删除。

相关入口：

- 测试配置：`/special/compatibility/run`
- 页面合集：`/special/compatibility/page-sets`
- 兼容性报告列表：`/execution/reports?tab=compatibility`
- 兼容性报告详情：`/execution/reports/compatibility/:id`

## 执行模型

执行统一走标准跨端执行链路（Android/iOS）。历史 `case.steps` JSON 保留用于数据兼容，启动时会自动回填为标准步骤表。

标准步骤主要包含：

- `action`、`args`、`timeout`、`description`
- `execute_on`：允许执行的平台
- `platform_overrides`：Android/iOS 专属定位器或参数
- `error_strategy`：`ABORT`、`CONTINUE`、`IGNORE`

当前统一支持点击、输入、元素等待、文本/图像断言、图像点击、OCR 提取、滑动、返回、主页、应用启停等动作。详细参数、平台差异、状态语义与错误码见 [执行规范](docs/EXECUTION_SPEC.md)。

### Feature Flags

以下开关存储在 `SystemSetting` 中，未配置时使用代码内默认值（`backend/feature_flags.py`）：

| 配置项 | 默认值 | 作用 |
|---|---|---|
| `new_step_model` | 开启 | 启用标准步骤表的读写与兼容迁移 |
| `ios_execution` | 关闭 | 允许 iOS 执行 |
| `ws_disconnect_abort` | 关闭 | 实时执行的 WebSocket 断开后立即中止对应执行 |
| `model_inspection` | 关闭 | 显示并允许 Android 模型化智能巡检 |
| `inspection_identity_v2` | 开启 | 新巡检使用 Template/State/Observation 身份模型；依赖巡检总开关 |
| `inspection_exploration_family_convergence` | 开启 | 同构页面族共享增量覆盖；依赖身份模型 |
| `inspection_similarity_convergence` | 关闭 | 高置信相似状态收敛；依赖身份模型 |
| `inspection_coverage_scheduler_v2` | 关闭 | 覆盖导向的优先级调度；依赖身份模型 |
| `inspection_visual_home_actions` | 关闭 | 探测首页无语义图片入口；依赖覆盖调度 |
| `compatibility_installed_replay` | 开启 | 允许对当前安装版本执行巡检冻结路径 |
| `compatibility_legacy_compare_creation` | 关闭 | 保留旧兼容性创建流程的临时回退入口 |
| `content_addressed_assets` | 关闭 | 将报告证据双写到鉴权的内容寻址资产库 |
| `tiered_asset_retention` | 关闭 | 启用 HOT/WARM/PINNED/COLD 分层保留和 GC；依赖内容寻址资产 |

父开关关闭时后端会同时关闭其子开关；前端设置页也会禁用不满足依赖的选项。跨端 Runner 已成为唯一执行链路（原 `cross_platform_runner` 开关已移除），执行必须显式指定设备。标准步骤模型默认启用，在 `SystemSetting` 中显式写入 `false` 可临时回退；iOS 执行和巡检仍需手动开启。

## 配置说明

### 环境变量

| 变量 | 默认值 | 说明 |
|---|---|---|
| `AUTODROID_DB_PATH` | `database.db` | SQLite 文件路径，相对路径基于项目根目录 |
| `AUTODROID_SECRET_KEY` | 自动生成 | JWT 签名密钥；未设置时自动生成并持久化到项目根 `.jwt_secret`（勿提交） |
| `AUTODROID_TOKEN_EXPIRE_MINUTES` | `43200`（30 天） | 登录 token 有效期（分钟），团队部署建议改小 |
| `AUTODROID_CORS_ORIGINS` | `*` | 允许的跨域来源，逗号分隔；为 `*` 时自动关闭 credentials |
| `AUTODROID_LIMIT_GLOBAL` | `20` | 系统全局最大并发执行数 |
| `AUTODROID_LIMIT_PER_USER` | `5` | 单用户最大并发执行数 |
| `AUTODROID_QUEUE_TIMEOUT` | `1800` | 并发超限时的排队等待上限（秒），超时任务标记 ERROR |
| `AUTODROID_DRIVER_POOL` | `0` | 设为 `1` 时按设备复用执行驱动连接（连接池），团队服务器推荐开启 |
| `HOST` | `0.0.0.0` | `start_lan.sh` 监听地址 |
| `PORT` | `8000` | `start_lan.sh` 服务端口 |
| `SKIP_BUILD` | `0` | 设置为 `1` 时跳过前端构建 |
| `PYTHON_BIN` | 自动检测 | 指定启动后端的 Python 可执行文件 |
| `IOS_WDA_XCODEPROJ_PATH` | 自动发现 | 指定 WebDriverAgent `.xcodeproj` 路径 |
| `AUTODROID_TRACE_PROCESSOR_BIN` | 自动发现 | 指定 Perfetto `trace_processor_shell` |

### 系统设置

前端“系统配置”页面支持维护：

- UI 场景报告和 Fastbot 报告的飞书 Webhook
- 通知卡片使用的系统访问地址
- AI 服务的 API Key、Base URL 和模型名称
- 通知与 AI 连接测试

另有仅通过设置 API（`POST /api/settings/`）维护的配置：

- `report_retention_days`：报告保留天数。大于 0 时每日自动清理超期的 UI 执行、Fastbot、兼容性与巡检报告（含磁盘产物）；缺省或 `0` 表示不清理。
- `asset_storage_low_watermark_percent`、`asset_storage_high_watermark_percent`、`asset_storage_critical_watermark_percent`：资产状态与分层 GC 的观察/高压/严重水位，默认 `80/90/95`，且必须满足 `low < high < critical`。当前新巡检/兼容性任务的硬容量保护使用内置 `95%` 阈值。

iOS WDA URL、设备映射、启动参数和故障处理见 [iOS WDA 运维手册](docs/IOS_WDA_OPS.md)。

## API 与兼容性

- OpenAPI：`GET /docs`
- 主要业务 API：`/api/auth`、`/api/cases`、`/api/scenarios`、`/api/devices`、`/api/tasks`、`/api/reports`、`/api/fastbot`
- 冷热启动专项 API：`POST /api/fastbot/startup/run`、`GET /api/fastbot/startup/tasks`
- 巡检 API：`/api/inspections/profiles`、`/api/inspections/runs`、`/api/inspections/runs/{id}/graph`
- 兼容性测试 API：`/api/compatibility/page-sets`、`/api/compatibility/runs`、`POST /api/compatibility/replay-preflight`
- 鉴权证据资产：`GET /api/assets/status`、`GET /api/assets/{asset_id}`
- CI 集成（API Token 机器凭证、上传包 → 触发回归 → 轮询结果）：见 [CI 集成指南](docs/CI_INTEGRATION.md)
- 实时执行与视频流：`/ws/*`
- 部分无 `/api` 前缀的路由继续保留，用于兼容历史客户端

前端和新集成建议统一使用 `/api` 前缀，不要继续依赖兼容别名。

## 验证与测试

运行后端测试：

```bash
source .venv/bin/activate
python -m unittest discover -s backend/tests -p 'test_*.py'
```

运行后端 lint（致命错误级检查）：

```bash
pip install -r requirements-dev.txt
ruff check backend scripts
```

验证前端生产构建：

```bash
cd frontend
npm run build
```

运行前端工具函数与 UI 契约测试：

```bash
cd frontend
node --test tests/*.test.mjs
```

提交前建议至少运行与改动模块相关的后端测试，并完成一次前端构建。push 后 GitHub Actions 会自动执行后端 lint + 全量测试和前端构建（见 `.github/workflows/ci.yml`）。

## 文档索引

- [项目深度说明](docs/PROJECT_OVERVIEW_CN.md)：功能全景、核心实现、数据模型与运维建议
- [执行规范](docs/EXECUTION_SPEC.md)：标准步骤模型、动作参数、平台覆盖与错误码
- [巡检、回放与证据资产指南](docs/INSPECTION_REPLAY_ASSETS.md)：开关依赖、运行流程、Graph/回放契约、资产保留与排障
- [CI 集成指南](docs/CI_INTEGRATION.md)：API Token、场景回归、智能巡检和流水线示例
- [iOS WDA 运维手册](docs/IOS_WDA_OPS.md)：WDA 启动、健康检查、端口映射和故障排查
- [Android 无线 adb 指南](docs/ANDROID_WIRELESS_ADB.md)：无线接入步骤、身份与稳定性边界
- [改进路线图](docs/IMPROVEMENT_ROADMAP.md)：能力补充与架构治理清单（P0/P1/P2）
- [历史优化实施总结](OPTIMIZATION_SUMMARY.md)：早期 OCR、查询与并发治理及其当前演进状态

## 安全与仓库维护

- 不要提交真实 API Key、Webhook、访问令牌、用户数据库或设备隐私数据。
- 不要提交 `database.db`、`.jwt_secret`、`reports/`、`asset_store/`、`uploads/`、运行日志、PID 文件、APK/IPA 等本地产物。
- 默认管理员初始密码仅适用于本地首次启动，部署前必须修改；JWT 密钥已自动生成，可用 `AUTODROID_SECRET_KEY` 显式管理。
- 公开部署时应通过 `AUTODROID_CORS_ORIGINS` 限制来源、启用 HTTPS，并通过网络策略限制设备控制接口的访问范围。
