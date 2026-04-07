# AutoDroid

**Android/iOS 录制+执行 的低代码自动化测试平台**

AutoDroid 是一个基于 Web 的自动化测试工具，支持 Android/iOS 设备可视化录制步骤，并将同一套标准步骤分发到 Android/iOS 执行，无需编写代码。

## 技术栈

| 层级 | 技术 |
|------|------|
| **前端** | Vue 3 + Element Plus + Vite |
| **后端** | Python 3 + FastAPI + SQLModel (SQLite) |
| **设备通信** | uiautomator2 (Android) + facebook-wda/tidevice (iOS) |
| **图像匹配** | OpenCV + findit (模板匹配) |
| **报告** | Jinja2 HTML 模板 |
| **实时通信** | WebSocket |

## 项目结构

```
AutoDroid/
├── backend/                    # 后端 Python 代码
│   ├── __init__.py
│   ├── main.py                 # FastAPI 主入口（路由 + WebSocket）
│   ├── runner.py               # TestRunner 测试执行引擎
│   ├── utils.py                # UI 元素分析与定位策略
│   ├── schemas.py              # Pydantic 数据模型（Step, Variable, ActionType）
│   ├── models.py               # SQLModel 数据库模型（TestCase）
│   ├── json_type.py            # SQLAlchemy JSON 列类型适配器
│   ├── socket_manager.py       # WebSocket 连接管理器
│   ├── report_generator.py     # HTML 测试报告生成器
│   └── templates/
│       └── report.html         # 报告 Jinja2 模板
├── frontend/                   # 前端 Vue 3 代码
│   └── src/
│       ├── components/
│       │   ├── DeviceStage.vue  # 设备调试/录制画布容器
│       │   ├── StepList.vue     # 步骤列表（拖拽排序）
│       │   ├── StepBuilder.vue  # 步骤编辑器
│       │   ├── VariablePanel.vue# 变量管理面板
│       │   ├── CaseExplorer.vue # 用例浏览器
│       │   └── LogConsole.vue   # 执行日志控制台（WebSocket）
│       ├── api/                 # API 请求封装
│       ├── stores/              # Pinia 状态管理
│       └── views/               # 页面视图
├── static/images/              # 图像匹配模板图片
├── docs/                       # 规范与运维文档
│   ├── EXECUTION_SPEC.md       # 执行规范（步骤模型/覆盖规则/错误码）
│   └── IOS_WDA_OPS.md          # iOS WDA 运维手册
├── reports/                    # 生成的 HTML 测试报告
├── database.db                 # SQLite 数据库
├── requirements.txt            # Python 全量依赖（默认）
├── requirements-base.txt       # 基础依赖
├── requirements-android.txt    # Android 能力依赖
├── requirements-ios.txt        # iOS 能力依赖
├── requirements-ai.txt         # AI 能力依赖
└── README.md                   # 本文件
```

### 当前版本补充

- 后端已经扩展为模块化 API 结构，核心目录包括 `backend/api/`、`backend/drivers/`、`backend/device_stream/`、`backend/tests/`。
- 前端页面已覆盖 `cases / scenarios / devices / reports / fastbot / dashboard / tasks / settings` 等业务域。
- `backend/templates/` 当前同时包含 `report.html` 和 `scenario_report.html` 两类报告模板。
- `docs/` 当前除执行规范与 WDA 运维外，还包含技术版/业务版项目介绍文档。
- `scripts/`、`resources/`、`assets/` 为运行与部署相关目录，已是当前项目结构的一部分。
- `static/images/`、`uploads/`、`reports/`、数据库文件属于本地运行数据，默认不提交到仓库。

## 文档索引

- `docs/EXECUTION_SPEC.md`：标准步骤模型、平台覆盖、错误码与执行语义
- `docs/IOS_WDA_OPS.md`：iOS WDA 健康检查、relay 端口策略、故障排查
- `docs/PROJECT_OVERVIEW_CN.md`：面向研发/评审的技术版项目说明
- `docs/PROJECT_OVERVIEW_CN_BUSINESS.md`：面向管理层/客户的业务版项目说明
- `RE.md`：Fastbot 卡顿监控与 AI 分析方案草案

## 仓库维护约定

- GitHub 建议只提交源码、脚本、依赖清单和正式文档。
- 以下内容默认作为本地运行数据，不提交到仓库：数据库文件、`uploads/`、`reports/`、`static/images/`、`*.apk`、`*.ipa`、本机 PID 文件。
- 临时截图、调试产物不要放在仓库根目录；如果需要长期保留并随仓库共享，建议整理到 `docs/` 下的文档资源目录后再提交。

## 工作原理

### 整体架构

```
┌──────────────────────────────────────────────────────────┐
│                    Vue 3 Frontend                        │
│  ┌──────────┐  ┌───────────┐  ┌──────────┐  ┌───────┐    │
│  │ Device   │  │ Step      │  │ Variable │  │ Log   │    │
│  │ Stage    │  │ List      │  │ Panel    │  │Console│    │
│  └────┬─────┘  └─────┬─────┘  └────┬─────┘  └───┬───┘    │
│       │              │             │             │       │
└───────┼──────────────┼─────────────┼─────────────┼───────┘
        │ HTTP         │ HTTP        │ HTTP        │ WebSocket
        ▼              ▼             ▼             ▼
┌───────────────────────────────────────────────────────────┐
│                   FastAPI Backend                         │
│  ┌─────────────┐  ┌──────────────┐  ┌──────────────────┐  │
│  │ Device API  │  │ Case CRUD    │  │ WS Run Engine    │  │
│  │ /device/*   │  │ /cases/*     │  │ /ws/run/{id}     │  │
│  └──────┬──────┘  └──────┬───────┘  └────────┬─────────┘  │
│         │                │                   │            │
│  ┌──────▼──────┐  ┌──────▼───────┐  ┌────────▼─────────┐  │
│  │ utils.py    │  │ SQLite DB    │  │ TestRunner       │  │
│  │ 元素分析     │  │ (SQLModel)   │  │ 步骤执行+重试      │   │
│  └─────────────┘  └──────────────┘  └────────┬─────────┘  │
└──────────────────────────────────────────────┼────────────┘
                                               │ uiautomator2
                                               ▼
                                    ┌─────────────────────┐
                                    │   Android Device    │
                                    │   (USB / WiFi)      │
                                    └─────────────────────┘
```

### 核心流程

#### 1. 录制（Recording）

```
用户点击画布 → 前端发送坐标 → 后端分析 UI 层级 → 生成步骤 → 返回新截图
```

1. 前端 `DeviceStage.vue` 捕获用户点击坐标 `(x, y)`
2. 发送 `POST /device/interact` 到后端
3. 后端通过 uiautomator2 获取当前 UI 层级 XML
4. `utils.py` 的 `calculate_element_from_coordinates()` 分析坐标：
   - 遍历 XML 树，找到所有包含该坐标的元素
   - 按优先级排序：**desc > text > resourceId > 无属性**
   - 优先选择**叶子节点**和**小面积**元素
5. 根据结果生成定位策略：
   - 有 `text` → 使用文本定位
   - 有 `description` → 使用描述定位
   - 无可用属性 → 裁剪元素区域图片 → 图像匹配定位
6. 在设备上执行点击，等待 UI 稳定后返回新截图

#### 2. 回放（Playback）

```
加载用例 → 逐步执行 → WebSocket 推送状态 → 生成报告
```

1. 前端通过 WebSocket 连接 `/ws/run/{case_id}`
2. 后端 `TestRunner` 逐步执行：
   - **变量替换**：将 `${var}` 替换为实际值
   - **元素定位**：根据 `selector_type` 查找元素
     - `text`：先精确匹配，失败后尝试模糊匹配 (`textContains`)
     - `image`：使用 OpenCV 模板匹配在屏幕上定位
   - **重试机制**：失败后重试 3 次，每次间隔 1 秒
3. 每步执行结果通过 WebSocket 实时推送到前端 `LogConsole.vue`
4. 全部执行完成后生成 HTML 测试报告

#### 3. 图像匹配（Image Matching）

当 UI 元素没有可用的 `text`、`description` 时，系统自动使用图像匹配：

1. **录制时**：从全屏截图中裁剪目标元素区域，保存为模板图片
   - 如果元素面积 > 屏幕 50%，改用点击坐标周围 100×100 像素区域
2. **回放时**：使用 `uiautomator2.image.click()` 在当前屏幕上查找匹配区域并点击
   - 底层使用 OpenCV 模板匹配算法
   - 超时时间 5 秒

### API 接口

| 方法 | 路径 | 说明 |
|------|------|------|
| `GET` | `/device/dump` | Android 录制态：获取截图 + 层级 XML + 设备信息 |
| `POST` | `/device/inspect` | Android 录制态：坐标审查元素（不执行） |
| `POST` | `/device/interact` | Android 录制态：交互并返回新状态 |
| `POST` | `/cases` | 创建测试用例 |
| `GET` | `/cases/{id}/steps` | 读取标准步骤（跨端模型） |
| `PUT` | `/cases/{id}/steps` | 覆盖写入标准步骤（双写兼容 legacy） |
| `POST` | `/cases/{id}/run` | 启动用例后台执行（推荐） |
| `GET` | `/cases/{id}/precheck` | 用例执行前预检（平台/动作/选择器/WDA/app_key） |
| `POST` | `/scenarios/{id}/run` | 场景批量执行 |
| `GET` | `/scenarios/{id}/precheck` | 场景执行前预检（case 级汇总） |
| `POST` | `/run/{id}` | 兼容别名，内部转发到 `/cases/{id}/run`（已弃用） |
| `WS` | `/ws/run/{id}` | WebSocket 实时执行（Case） |
| `GET` | `/executions` | 执行报告列表 |
| `GET` | `/executions/{id}` | 执行报告详情 |

## 快速启动

### 前置条件

- Python 3.8+
- Node.js 16+
- Android 设备（录制/执行，USB 连接或 WiFi 同网段）
- ADB 已安装并可用
- iOS 执行（可选）：需安装并启动 WebDriverAgent，且依赖 `tidevice` / `facebook-wda`


### 安装与启动

```bash
# ----- 后端服务 -----
# 1. (推荐) 使用虚拟环境
# python -m venv .venv
# source .venv/bin/activate

# 2. 安装依赖（默认全量）
pip install -r requirements.txt

# 2.1 可选：按能力安装（降低部署耦合）
# 仅 Android 录制/执行
# pip install -r requirements-base.txt -r requirements-android.txt
# Android + iOS 执行
# pip install -r requirements-base.txt -r requirements-android.txt -r requirements-ios.txt
# AI 日志分析（在以上任一组合后追加）
# pip install -r requirements-ai.txt

# 3. 启动后端服务 (运行在 8000 端口)
uvicorn backend.main:app --host 0.0.0.0 --port 8000 --reload

# ----- 前端服务 -----
# 4. 进入前端目录并安装依赖
cd frontend
npm install

# 5. 启动前端服务 (允许局域网访问)
npm run dev -- --host
```

访问 `http://localhost:5173` 开始使用。

### 使用流程

1. **连接设备**：Android、iOS 设备用于录制与执行
2. **刷新画布**：点击画布刷新获取设备截图
3. **录制步骤**：点击画布上的元素，系统自动识别并生成步骤
4. **编辑步骤**：在步骤列表中编辑、排序、删除步骤
5. **保存用例**：输入用例名称并保存
6. **执行回放**：点击运行按钮，实时观看执行日志
7. **查看报告**：执行完成后查看 HTML 测试报告
