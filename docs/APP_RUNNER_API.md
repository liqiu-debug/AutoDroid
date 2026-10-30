# AutoDroid App 接口契约

Android 客户端 [AutoDroid-Android](https://github.com/liqiu-debug/AutoDroid-Android)（内置 AutoJs6 引擎，`com.autodroid.runner`）和 [AutoDroid-iOS](https://github.com/liqiu-debug/AutoDroid-iOS)（SwiftUI，通过同机 AutoDroid-WDA 执行）通过以下接口与平台交互。所有请求需登录态（`Authorization: Bearer <access_token>` 或 API Token）。

> 本文档是平台与 App 之间的契约：改动 `/app-runs/*` 与 `/device-runs/*` 的路径、字段或语义时，需同步两个 App 客户端。
>
> 桌面客户端 [AutoDroid-Desktop](https://github.com/liqiu-debug/AutoDroid-Desktop) **不走本契约**：它在本机执行并经
> `/api/client-devices/*`、`/api/client-runs/*`、`/api/client-templates`、`/api/client-fastbot/*`、`/api/client-compatibility/*`、`/api/client-tasks`
> 上报（`backend/api/client_*.py`、`backend/api/tasks.py::client_router`），其余请求反代到平台。

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
  "generator": "autodroid/1",           // backend/autojs_export/packager.py 的 GENERATOR_NAME
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
再写 `task_config.json`（见下），最后经 AutoJs6 引擎执行 `main.js`。Android App 对准备与执行阶段统一互斥，并等待引擎和上报结束后读取结果。

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
- 幂等：同一 `run_id` 的已完成结果重复上报时返回原 `execution_id / status / report_id`，不重新落库或再次发送飞书通知；当前 SQLite 实现对并发回传也串行处理。其他账号复用该 `run_id` 返回 409；
- 场景中重复引用同一个用例时，每次引用分别消费一条结果；缺失的引用记为执行中断，不复用前一次成功结果；
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

## 8. iOS：设备端执行器（AutoDroid-WDA）

iOS 侧的设备端执行器是 WebDriverAgent 的 fork（<https://github.com/liqiu-debug/AutoDroid-WDA>，
分支 `autodroid`）：手机上点图标启动 WDA 会话后，平台把脚本包直接下发到执行器，
由执行器在本机用 JavaScriptCore 跑同一份 `runtime.js`，结果同样回传 `/device-runs/report`。

### 8.1 取 iOS 脚本包

```
GET /app-runs/cases/{case_id}/script?platform=ios
GET /app-runs/scenarios/{scenario_id}/script?platform=ios
```

与 Android 包的差异：

| 项 | Android（默认） | iOS（platform=ios） |
|---|---|---|
| `manifest.platform` | `android` | `ios` |
| 步骤定位器 | 取步骤模型里的 `android` 覆盖 | 取 `ios` 覆盖（`by: label/id/...`） |
| `start_app/stop_app` 的 `args.package` | Android 包名 | iOS bundle id（应用映射的 `ios` 字段） |
| `adapter_js` | 不包含 | **包含**：AutoJs6 API 兼容层（`backend/autojs_export/adapters/ios_adapter.js`），执行器优先使用包内适配层，改适配层无需重装 WDA |

`runtime.js` 两侧完全一致，按 `ctx.platform`（执行器在运行配置里传 `platform: "ios"`）
判定 `execute_on`，不匹配的步骤标记 SKIP。

### 8.2 执行器端点（手机局域网地址，默认 8100 端口）

执行器路由不要求 WebDriver 会话，也没有鉴权（只应暴露在可信局域网，或由 App 走 `127.0.0.1`）。

```
GET  /autodroid/status            → {running, run_id, last_result, log_count, version, stop_requested}
POST /autodroid/run               → {accepted, run_id} | {accepted: false, error}
     body: {bundle, run_id, platform, server, token, notify,
            target_bundle_id?, device_serial?, name?,
            notify_app?, completion_bundle_id?, system_notify?}
GET  /autodroid/logs?since=N      → {lines, next}
POST /autodroid/stop              → {stopped}
POST /autodroid/report            → {found, report}        body: {run_id}   （补传用：取回落盘报告）
GET  /autodroid/schedules         → {items, events, ...}   （WDA 内定时任务状态）
POST /autodroid/schedules         → 整体替换定时任务       body: {items: [...], server, token, username}
GET  /autodroid/notifications     → 执行器侧通知权限状态
POST /autodroid/notifications     → 申请通知权限
```

- `bundle` 即 8.1 返回的 JSON 包；`server`/`token` 供 runtime.js 回传结果，token 只驻留执行器内存。
- `target_bundle_id` 指定被自动化应用，缺省则由 `start_app` 动态设置；**当前 iOS 客户端不发送该字段**，平台 `selftest` 会发送。
- `notify` 缺省时，执行器为"脚本未产出报告"场景生成的兜底失败报告按 `true` 处理，客户端应始终显式传值。
- `notify_app` + `completion_bundle_id`：执行结束后把客户端 App 拉回前台（执行器侧校验 bundle id）；`system_notify` 控制是否由 WDA 自己发系统通知（新版 App 关闭，改由 App 发）。
- `logs` 的 `since/next` 游标在缓冲区截断（上限 6000 行，超出丢最旧 2000 行）后会错位，iOS 客户端每次取完整快照而不用游标。
- 执行中再次 `POST /autodroid/run` 返回 `accepted: false`；`stop` 只置标志，脚本在下一个原语边界中止。
- 执行器只保留最近 10 次运行目录（`Documents/autodroid_runs/<run_id>/`）。

定时任务项字段：`id, enabled, rule(once|daily|weekly|interval), hour, minute, weekday, interval, fetch_url, next_run`；
`fetch_url` 为触发时向平台取脚本的完整地址。

### 8.3 设备注册与上报

- 注册：`POST /device-runs/register` 传 `platform: "ios"` 与 `udid`（拿不到 UDID 时用 IDFV），
  平台生成 `ios:<udid>` 形式的 serial，`connection_type=wda`；
- 上报：`POST /device-runs/report` 带 `platform: "ios"`，serial 推导与注册一致（`ios:` 前缀），
  其余字段（`run_id` 幂等、`notify` 控制飞书）与 Android 完全一致。

### 8.4 自检与验证

```bash
# 无真机：Node 试验台验证「脚本包 + runtime.js + 适配层 + 报告载荷」
python -m unittest backend.tests.test_ios_runtime_harness

# 真机：下发自检包（启动设置 → 截图+OCR → 滑动 → 返回 → 回桌面）并等结果
python -m backend.autojs_export.selftest --url http://<手机IP>:8100
python -m backend.autojs_export.selftest --output /tmp/ios_selftest.json   # 只生成包
```

也可用 WDA fork 自带脚本做设备侧检查：`autodroid/verify_device.sh <WDA_URL> --platform-repo .`
（在线、会话稳定性、日志、完整自检）。
