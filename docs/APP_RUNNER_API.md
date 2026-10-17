# AutoDroid App 接口契约

Android 客户端 [AutoDroid-Android](https://github.com/liqiu-debug/AutoDroid-Android)（内置 AutoJs6 引擎，`com.autodroid.runner`）通过以下接口与平台交互。所有请求需登录态（`Authorization: Bearer <access_token>` 或 API Token）。

> 本文档是平台与 App 之间的契约：改动 `/app-runs/*` 与 `/device-runs/*` 的路径、字段或语义时，需同步 App 客户端。iOS 客户端将复用同一套接口。

## 1. 登录

```
POST /auth/token
Content-Type: application/x-www-form-urlencoded

username=<账号>&password=<密码>
→ 200 {"access_token": "...", "token_type": "bearer", ...}
→ 400 {"detail": "用户名或密码错误"}
```

## 2. 设备注册（登录后调用一次；设备中心可见）

```
POST /device-runs/register
{"android_id": "...", "name": "工位A-OPPO", "model": "...", "brand": "...", "android_version": "...", "resolution": "..."}
→ 200 {"device_serial": "autojs6:<android_id>", "server_time": "..."}
```

## 3. 数据浏览（与 Web 端同一套接口）

| 能力 | 接口 |
|---|---|
| 用例目录树 | `GET /folders/tree` |
| 用例列表 | `GET /cases/?skip=0&limit=100&keyword=&folder_id=` |
| 用例详情（含步骤） | `GET /cases/{id}`、`GET /cases/{id}/steps` |
| 场景目录树 | `GET /scenario-folders/tree` |
| 场景列表 | `GET /scenarios/?skip=0&limit=100&keyword=&folder_id=` |
| 场景详情（含子用例） | `GET /scenarios/{id}`、`GET /scenarios/{id}/steps` |

## 4. 拉取可执行脚本包（App 直跑核心）

```
GET /app-runs/cases/{case_id}/script?env_id=<可选>
GET /app-runs/scenarios/{scenario_id}/script?env_id=<可选>
→ 200 {
  "format_version": 1,
  "generator": "autodroid-autojs6/1",
  "kind": "case" | "scenario",
  "manifest": {...},                    // 写入运行目录 manifest.json
  "runtime_js": "<源码>",               // 写入运行目录 runtime.js
  "main_js": "<源码>",                  // 写入运行目录 main.js
  "case_files": [{"path": "cases/case_3.js", "content": "<源码>"}],
  "assets": {"element_xx.png": "<base64>"},
  "precheck": {"ok": true, "issues": []}
}
→ 400 {"detail": {"message": "...", "issues": [{step_order, severity, code, message}]}}  预检未通过
```

App 侧处理：把 `manifest.json / runtime.js / main.js / case_files / assets` 原样写入运行目录，
再写 `task_config.json`（见下），最后执行 `main.js`（经 AutoJs6 引擎，`Scripts.run`）。

**task_config.json（App 写入运行目录）**：
```json
{
  "server": "http://<平台地址>:8000",
  "token": "<JWT>",
  "runId": "<客户端生成的 uuid，用于幂等>",
  "notify": true
}
```

## 5. 执行结果回传

```
POST /device-runs/report
{
  "run_id": "<与 task_config 相同>",     // App 直跑必填（无 task_id 时）
  "notify": true,                        // false 时不发飞书卡片
  "kind": "case" | "scenario",
  "case_id": 3,                          // 或 scenario_id
  "device": {"model": "...", "android_id": "...", "resolution": "..."},
  "started_at": 1792000000000,           // epoch ms
  "finished_at": 1792000018000,
  "success": true,
  "cases": [{"case_id": 3, "success": true, "steps": [
     {"order": 1, "action": "start_app", "status": "PASS", "duration": 3.1,
      "error": null, "error_code": null, "attempts": 1,
      "screenshot_base64": null, "output": null}
  ]}]
}
→ 200 {"execution_id": 21, "status": "PASS", "report_id": "report_3_....html"}
```

语义：
- 幂等：同一 `run_id` 重复上报会覆盖同一条执行记录（不会产生重复记录）；
- 步骤状态：`PASS / FAIL / WARNING / SKIP`；`error_code` 使用 E2xxx 语义；
- 该接口即 App 与 Agent 共用的回传入口（Agent 任务带 `task_id`，App 直跑带 `run_id`）。

## 6. 报告中心

| 能力 | 接口 |
|---|---|
| 执行列表 | `GET /reports/executions?skip=&limit=&status=&keyword=` |
| 执行详情（步骤结果） | `GET /reports/executions/{execution_id}` |
| HTML 报告下载 | `GET /reports/executions/{execution_id}/download`（带 Authorization，返回 HTML 文本） |
| 报告内嵌资源（截图等） | `/api/report-assets/<path>`（静态资源，无需鉴权，HTML 内引用可直接加载） |

App 渲染建议：带鉴权下载 HTML → `WebView.loadDataWithBaseURL(serverUrl, html, "text/html", "utf-8", null)`。

## 7. 兼容说明

- 官方 AutoJs6 + `scripts/autojs6/autodroid_agent.js` 的旧链路继续可用（任务接口 `/autojs/tasks`、`/device-runs/pending|packages` 不变）；
- App 专用端点统一走 `/app-runs/*` 前缀，后续变更视为契约变更。
