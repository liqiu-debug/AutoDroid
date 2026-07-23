# AutoDroid Frontend

Vue 3 + Vite 前端，负责设备与资产管理、用例/场景编排、调度、专项测试、报告和系统设置。UI 使用 Element Plus，状态管理使用 Pinia，图表使用 ECharts。

## 环境要求

- Node.js `^20.19.0` 或 `>=22.12.0`
- npm（以 `package-lock.json` 为准）
- 本地开发时后端默认运行在 `http://127.0.0.1:8000`

## 安装与运行

```bash
npm ci
npm run dev -- --host
```

Vite 默认监听 `5173`，并将 `/api`、`/ws` 和兼容流媒体路径代理到后端。生产构建：

```bash
npm run build
```

产物写入 `frontend/dist/`，根目录 `scripts/start_lan.sh` 会构建该目录，再由 FastAPI 托管 SPA。

## 目录结构

```text
src/
├── api/          # Axios 实例和业务 API 封装
├── components/   # 设备画面、步骤编辑、巡检实时面板等组件
├── composables/  # 客户端模式、录制、流媒体和页面行为复用
├── layout/       # PC 侧栏与移动端页头/底栏
├── router/       # 路由、移动端可用性和 Feature Flag 元数据
├── stores/       # 登录用户、Feature Flags 和用例状态
├── utils/        # 报告、巡检 Graph、回放和任务呈现纯函数
└── views/        # 页面级视图
tests/            # Node test runner 的工具函数与 UI 契约测试
```

## 关键页面

| 路径 | 页面 |
|---|---|
| `/assets/*` | 设备、变量和 App 包管理 |
| `/ui/*` | 用例与场景 |
| `/special/inspection` | 模型化智能巡检，受 `model_inspection` 控制 |
| `/special/compatibility/*` | 兼容性配置与页面合集 |
| `/execution/tasks` | UI、Fastbot 和巡检定时任务 |
| `/execution/reports` | UI、Fastbot、启动、兼容性和巡检报告 |
| `/execution/reports/inspection/:id` | 巡检 Graph、Observation、实时状态和回放选择 |
| `/settings/notifications` | 通知、AI、Feature Flags 和资产保留设置 |

路由可通过 `meta.mobileAvailable` 声明移动端支持，通过 `meta.featureFlag` 声明功能开关。菜单和直接路由访问都必须检查开关；当前移动端支持巡检报告查看，但不支持巡检配置和启动。

## API、认证与实时连接

- Axios 统一从用户 Store 注入 Bearer JWT 或 API Token；收到 `401` 会清理登录态。
- 新代码使用 `/api/*` canonical 路径，不依赖无 `/api` 的历史 alias。
- `/api/assets/{asset_id}` 必须带 Authorization。图片/视频证据应通过 Axios 获取 Blob 后生成对象 URL，不能直接把鉴权 URL 写进 `<img src>`。
- 巡检实时连接先用 JWT 调用 `/api/inspections/runs/{id}/live-session`，再分别消费事件和视频的一次性票据；票据不可复用。
- 组件卸载时必须关闭 WebSocket、轮询器和对象 URL，避免报告切换后继续占用资源。

## 巡检与兼容性呈现

- `utils/inspectionMindMap.js` 按响应的 `schema_version` / `hierarchy_version` 构建有限页面树，循环和跨边以引用节点表示。
- `utils/inspectionPresentation.js` 与 `inspectionRunPresentation.js` 负责用户可读状态、覆盖率和回放摘要，不在组件中重复解释后端枚举。
- `utils/compatibilityReplay.js` 统一处理回放预检、包快照、路径范围、安全边界和 terminal outcome。
- 内部 ID 只出现在技术详情；页面标题、动作和状态优先使用稳定的用户可读标签。

## 验证

运行工具函数与 UI 契约测试：

```bash
node --test tests/*.test.mjs
```

运行生产构建：

```bash
npm run build
```

提交前应同时执行两者。当前 GitHub Actions 已执行 `npm ci` 和生产构建；Node 测试尚未接入 CI，路线图中保持为进行中。

项目级安装、后端启动、Feature Flags 和完整测试命令见根目录 [README](../README.md)；巡检与资产协议见 [巡检、回放与证据资产指南](../docs/INSPECTION_REPLAY_ASSETS.md)。
