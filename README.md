# AutoDroid-Pro

**Android UI 自动化低代码测试平台**

AutoDroid-Pro 是一个基于 Web 的 Android UI 自动化测试工具，支持通过可视化界面录制、编辑和回放 UI 测试用例，无需编写代码。

## 技术栈

| 层级 | 技术 |
|------|------|
| **前端** | Vue 3 + Element Plus + Vite |
| **后端** | Python 3 + FastAPI + SQLModel (SQLite) |
| **设备通信** | uiautomator2 (USB/WiFi) |
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
│       │   ├── DeviceStage.vue  # 主布局容器
│       │   ├── DeviceCanvas.vue # 设备屏幕画布（截图显示+点击交互）
│       │   ├── StepList.vue     # 步骤列表（拖拽排序）
│       │   ├── StepBuilder.vue  # 步骤编辑器
│       │   ├── VariablePanel.vue# 变量管理面板
│       │   ├── CaseExplorer.vue # 用例浏览器
│       │   └── LogConsole.vue   # 执行日志控制台（WebSocket）
│       ├── api/                 # API 请求封装
│       ├── stores/              # Pinia 状态管理
│       └── views/               # 页面视图
├── static/images/              # 图像匹配模板图片
├── reports/                    # 生成的 HTML 测试报告
├── database.db                 # SQLite 数据库
├── requirements.txt            # Python 依赖
└── README.md                   # 本文件
```

## 工作原理

### 整体架构

```
┌─────────────────────────────────────────────────────────┐
│                    Vue 3 Frontend                       │
│  ┌──────────┐  ┌───────────┐  ┌──────────┐  ┌───────┐  │
│  │ Device   │  │ Step      │  │ Variable │  │ Log   │  │
│  │ Canvas   │  │ List      │  │ Panel    │  │Console│  │
│  └────┬─────┘  └─────┬─────┘  └────┬─────┘  └───┬───┘  │
│       │              │             │             │       │
└───────┼──────────────┼─────────────┼─────────────┼───────┘
        │ HTTP         │ HTTP        │ HTTP        │ WebSocket
        ▼              ▼             ▼             ▼
┌───────────────────────────────────────────────────────────┐
│                   FastAPI Backend                          │
│  ┌─────────────┐  ┌──────────────┐  ┌──────────────────┐  │
│  │ Device API  │  │ Case CRUD    │  │ WS Run Engine    │  │
│  │ /device/*   │  │ /cases/*     │  │ /ws/run/{id}     │  │
│  └──────┬──────┘  └──────┬───────┘  └────────┬─────────┘  │
│         │                │                   │             │
│  ┌──────▼──────┐  ┌──────▼───────┐  ┌────────▼─────────┐  │
│  │ utils.py    │  │ SQLite DB    │  │ TestRunner       │  │
│  │ 元素分析    │  │ (SQLModel)   │  │ 步骤执行+重试    │  │
│  └─────────────┘  └──────────────┘  └────────┬─────────┘  │
└──────────────────────────────────────────────┼─────────────┘
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

1. 前端 `DeviceCanvas.vue` 捕获用户点击坐标 `(x, y)`
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

当 UI 元素没有可用的 `text`、`description`、`resourceId` 时，系统自动使用图像匹配：

1. **录制时**：从全屏截图中裁剪目标元素区域，保存为模板图片
   - 如果元素面积 > 屏幕 50%，改用点击坐标周围 100×100 像素区域
2. **回放时**：使用 `uiautomator2.image.click()` 在当前屏幕上查找匹配区域并点击
   - 底层使用 OpenCV 模板匹配算法
   - 超时时间 5 秒

### API 接口

| 方法 | 路径 | 说明 |
|------|------|------|
| `GET` | `/device/dump` | 获取设备截图 + 层级 XML + 设备信息 |
| `POST` | `/device/inspect` | 审查指定坐标的元素（不执行操作） |
| `POST` | `/device/interact` | 点击并返回新状态 |
| `POST` | `/cases` | 创建测试用例 |
| `GET` | `/cases` | 获取所有用例 |
| `GET` | `/cases/{id}` | 获取单个用例 |
| `PUT` | `/cases/{id}` | 更新用例 |
| `DELETE` | `/cases/{id}` | 删除用例 |
| `POST` | `/run/{id}` | 同步执行用例 |
| `WS` | `/ws/run/{id}` | WebSocket 实时执行 |
| `GET` | `/api/reports` | 列出测试报告 |
| `GET` | `/api/reports/{id}` | 获取报告文件 |

## 快速启动

### 前置条件

- Python 3.8+
- Node.js 16+
- Android 设备（USB 连接或 WiFi 同网段）
- ADB 已安装并可用

### 安装与启动

```bash
# ----- 后端服务 -----
# 1. (推荐) 使用虚拟环境并安装依赖
# python -m venv .venv
# source .venv/bin/activate
pip install -r requirements.txt

# 2. 启动后端服务 (运行在 8000 端口)
uvicorn backend.main:app --host 0.0.0.0 --port 8000 --reload

# ----- 前端服务 -----
# 3. 进入前端目录并安装依赖
cd frontend
npm install

# 4. 启动前端服务 (允许局域网访问)
npm run dev -- --host
```

访问 `http://localhost:5173` 开始使用。

### 使用流程

1. **连接设备**：确保 Android 设备通过 USB 连接并开启 USB 调试
2. **刷新画布**：点击画布刷新获取设备截图
3. **录制步骤**：点击画布上的元素，系统自动识别并生成步骤
4. **编辑步骤**：在步骤列表中编辑、排序、删除步骤
5. **保存用例**：输入用例名称并保存
6. **执行回放**：点击运行按钮，实时观看执行日志
7. **查看报告**：执行完成后查看 HTML 测试报告
