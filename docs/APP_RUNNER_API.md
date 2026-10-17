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

```
GET  /autodroid/status            → {running, run_id, last_result, log_count, version}
POST /autodroid/run               → {accepted, run_id}
     body: {bundle, run_id, platform, target_bundle_id, server, token, notify}
GET  /autodroid/logs?since=N      → {lines, next}
POST /autodroid/stop              → {stopped}
```

`bundle` 即 8.1 返回的 JSON 包；`server`/`token` 供 runtime.js 回传结果；
`target_bundle_id` 指定被自动化应用（后续 `start_app` 也会更新它）。

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
