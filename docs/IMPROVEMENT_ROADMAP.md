# AutoDroid 改进路线图

> 面向"团队内部共用服务"部署形态的能力补充与架构治理清单。
> 每项包含现状依据、建议方案与验收标准，按优先级分级，供迭代排期与跟踪。
> 生成时间：2026-07（随迭代滚动更新）。

## 状态标记

- ✅ 已完成
- 🚧 进行中
- ⬜ 未开始

---

## P0 — 安全与工程化基线（已实施）

### ✅ P0.1 安全加固包

**现状依据**：JWT `SECRET_KEY` 曾硬编码占位符于 `backend/core/security.py`；token 有效期硬编码 30 天；使用已废弃的 `datetime.utcnow()`；CORS `allow_origins=["*"]` 且 `allow_credentials=True`（不符合 CORS 规范组合）。

**已落地方案**：
- `AUTODROID_SECRET_KEY` 环境变量优先；未设置时自动生成随机密钥并持久化到项目根 `.jwt_secret`（0600 权限，已加入 `.gitignore`），重启不失效
- `AUTODROID_TOKEN_EXPIRE_MINUTES` 可配置 token 有效期（默认仍为 30 天，团队部署建议改小）
- 时间统一 `datetime.now(timezone.utc)`
- `AUTODROID_CORS_ORIGINS` 配置允许来源；为 `*` 时自动关闭 credentials（前端走同源 Bearer header，无 cookie 依赖）

**验收**：`backend/tests/test_security_secret.py` 全绿；升级后旧 token 一次性失效属预期行为。

### ✅ P0.2 CI 与工程化基建

**现状依据**：仓库无任何 CI、无 lint 配置、依赖未锁版本、根目录散落 10 个运维脚本与预览产物。

**已落地方案**：
- `.github/workflows/ci.yml`：backend（ruff + unittest 全量）+ frontend（npm ci + build）双 job
- `ruff.toml`：仅启用致命错误规则（E9/F63/F7/F82）作为底线，首轮即发现并修复 `device_stream/manager.py` 中 `PROJECT_ROOT` 未导入的真实 NameError
- `requirements-*.txt` 按当前验证过的版本加 `>=` 下限（APScheduler 加 `<4` 上限防 4.x 破坏性升级）；新增 `requirements-dev.txt`
- 根目录运维脚本迁至 `scripts/maintenance/`（补 `sys.path` bootstrap），删除空文件与预览 HTML

**验收**：CI 双 job 绿；`ruff check backend scripts` 通过。

### ✅ P0.3 限流参数配置化

**现状依据**：`backend/execution_limiter.py` 并发上限硬编码（全局 20/用户 5）。

**已落地方案**：`AUTODROID_LIMIT_GLOBAL` / `AUTODROID_LIMIT_PER_USER` 环境变量覆盖，非法值回退默认并告警。

**验收**：`backend/tests/test_execution_limiter.py` 环境覆盖用例全绿。

### ✅ P0.4 WebSocket 断开中止执行（灰度开关）

**现状依据**：`/ws/run/{case_id}` 执行期间客户端断开无法被感知（广播吞异常、无 receive 循环），执行持续占用设备直到自然结束。

**已落地方案**：独立 disconnect watcher 协程监听 `receive_text`；`SystemSetting` 开关 `ws_disconnect_abort`（默认关闭）开启后断开即触发 `abort_event`，走既有中止链路（步骤中断、状态 `ABORTED`、设备状态恢复）。

**验收**：`backend/tests/test_case_ws_disconnect_abort.py` 覆盖开关开/关两种行为。

---

## P1 — 近期（建议 1-2 个迭代内）

### ✅ P1.1 报告/截图数据保留策略

**现状依据**：`reports/`、失败截图、Fastbot 产物无限增长，团队服务器磁盘将持续膨胀；报告删除仅有手动入口。

**已落地方案**：新增 `backend/retention_service.py`，每日 03:17 定时清理（启动时注册到 APScheduler）；保留天数由 `SystemSetting` 的 `report_retention_days` 控制（缺省/0 = 关闭，避免升级后静默删数据）。清理复用各业务 API 的产物删除逻辑（UI 执行、Fastbot、兼容性），进行中的记录不清理。

**验收**：`backend/tests/test_retention_service.py` 覆盖配置解析、过期筛选与产物删除调用。

### ✅ P1.2 驱动连接池

**现状依据**：每次执行都新建 uiautomator2/wda 连接，连接建立耗时长且并发时开销放大（`OPTIMIZATION_SUMMARY.md` 遗留项）。

**已落地方案**：新增 `backend/drivers/driver_pool.py`，按 `(platform, device_id)` 复用驱动；复用前调用 `driver.health_check()`（Android: `info` RPC；iOS: WDA `status()`），失效即销毁重建；空闲 10 分钟 TTL 惰性回收；同设备并发获取锁等待超时降级为一次性驱动。通过环境变量 `AUTODROID_DRIVER_POOL=1` 启用（默认关闭灰度，团队服务器推荐开启），`TestCaseRunner` 透明接入，服务关闭时统一释放。

**验收**：`backend/tests/test_driver_pool.py` 覆盖复用、健康重建、参数变化、锁超时降级、TTL 回收与 Runner 集成。

### ✅ P1.3 异常信息结构化（后端）

**现状依据**：预检已有错误码体系（`P1xxx`/`S1001`），但执行期错误多为裸字符串，前端难以给出修复引导。

**已落地方案**：新增 `backend/execution_errors.py`——12 个执行期错误码（E2xxx）+ 集中维护的中文修复建议；异常归类基于 uiautomator2/wda 真实异常类型逐一映射；驱动层在语义明确的失败点抛 `ExecutionStepError`（多继承保持 RuntimeError/AssertionError 血统，74 个既有断言测试零改动）；步骤结果纯增量新增 `error_code`/`error_context`/`suggestion` 字段。`docs/EXECUTION_SPEC.md` 新增 6.4 节。

**遗留**：前端 LogConsole/报告详情按结构渲染建议（排入前端会话）。

### ✅ P1.4 双执行链路统一

**现状依据**：legacy `runner.py`（1072 行）与 `drivers/cross_platform_runner.py` 并存，靠 flag 灰度；双写增加维护与测试成本。

**已落地方案**：`feature_flags.py` 引入按 key 默认值表（`new_step_model` 默认开、DB 显式 `false` 可回退）；启动时幂等回填 legacy 步骤（`database.py::backfill_case_steps_to_standard`，单条失败跳过）；删除 `runner.py` 与全部 legacy 分支（净 -3100 行），`cross_platform_runner` 开关随链路一起移除；abort 注册表迁入 `run_control.py`；所有执行入口设备必选校验。

### ✅ P1.5 巨型文件拆分

**已落地方案（第一批）**：`fastbot_runner.py` 2899→169 行（re-export shim + `backend/fastbot/` 10 模块，AST 验证名称覆盖 100%）；`DeviceStage.vue` 2071→1047 行（6 composables + 2 utils，defineExpose API 不变）；`FastbotReportDetail.vue` 2027→670 行（10 子组件）。

**已落地方案（第二批）**：`main.py` 1741→316 行（录制端点→`api/recording.py`、WS 执行→`api/ws_run.py`、SPA→`spa.py`）；`api/scenarios.py` →929 行路由（执行编排→`scenario_execution.py`、结果持久化→`scenario_results.py`）；`ios_driver.py` →753 行类骨架（`drivers/ios/` mixin 包：locator/vision/app_control/support）。路由表与 OpenAPI JSON 前后逐字节一致，77 个 IOSDriver 方法 AST 逐字比对不变，15 个测试文件 patch 路径同步迁移。

### ✅ P1.6 SQLite 并发加固

**现状依据**：团队多人并发写场景下 SQLite 默认 journal 模式易出现 `database is locked`。

**已落地方案**：`backend/database.py` 为 engine 附加连接级 PRAGMA（`configure_sqlite_engine`）：WAL（读写并行，写入 DB 文件头持久生效）、`synchronous=NORMAL`、`busy_timeout=30s`；`.gitignore` 补充 `*.db-wal` / `*.db-shm`。

**验收**：`backend/tests/test_database_hardening.py` 验证 PRAGMA 生效与 WAL 持久性。

---

## P2 — 能力补充（按需排期）

### ✅ P2.1 步骤级重试

**已落地方案**：`TestCaseStep.retry_count`（0-3，默认 0，版本化迁移补列）；执行语义——总尝试 = 1+retry_count、每次重试 1s 退避且 abort 立即中断、SKIP 不重试、断言失败可重试、`error_strategy` 仅对最终结果生效、耗尽取最后一次 error/error_code；`attempts` 字段入结果与 `report_display`（供 Flaky 统计识别"靠重试通过"）。前端 StepBuilder 容错策略旁"失败重试"输入；legacy 双向转换宽松兼容、预检严格校验（超限报 P1006）。

### ✅ P2.2 执行排队替代 429 拒绝

**已落地方案**：`execution_limiter.py` 重写为单 Condition + FIFO deque（弃用不保证唤醒顺序的 Semaphore），等待者状态机 WAITING→GRANTED→CLAIMED/CANCELLED；用例与场景（含定时任务）链路超限入队而非 429，HTTP 立即返回 `queued/queue_position`；QUEUED 状态入 run registry 与执行记录，`/runs/active`、`/limiter/stats` 暴露队列；排队可取消（独立事件防误杀设备当前占用者）、超时默认 30 分钟（`AUTODROID_QUEUE_TIMEOUT`）；单用例后台任务改独立守护线程防拖垮共享线程池；WS 交互式执行保持直接执行。前端 Case/Scenario 列表显示"排队中（第 N 位）"。

### ✅ P2.3 Flaky 用例识别与报告对比

**已落地方案**：`backend/flaky_analysis.py` 纯查询计算（不建新表）——评分 = 60%×翻转率 + 40%×失败均衡度，样本 <5 不排名、持续失败/全过不入榜；场景级 + 步骤级 Top（用例级因 TestResult 无 case_id 降级，步骤名天然带用例前缀）。端点 `GET /reports/flaky`、`GET /reports/executions/compare`（同场景强制、diff 分类 regressed/fixed/still-failing/unchanged/added/removed）。前端：报告中心"稳定性分析"抽屉 + ReportDetail"与上一次对比"对话框。

### ✅ P2.4 iOS 实时投屏

后端：WDA MJPEG relay（9300-9399）+ `WS /ws/ios-mjpeg/{serial}`（二进制 JPEG 帧）与 `GET /api/stream/ios-mjpeg/{serial}`（multipart 透传）双端点，单上游连接多客户端广播，帧率/质量经 WDA settings 可配（`ios_mjpeg_*` SystemSetting）。前端：`IosMjpegPlayer` 以纯预览层接入 `DeviceStage`（`pointer-events:none`，点击/框选仍走静态截图+层级流程，框选时自动隐藏实时层保证所见即所得）；WDA 不可用（4005）引导检测，异常断开退避自动重连。待真机验证。

### ✅ 专项：Android Scrcpy 投屏质量（P1 批次新增）

参数配置化（`AUTODROID_SCRCPY_MAX_SIZE/BITRATE/MAX_FPS/GOP`，默认 1920/8Mbps/60fps/1s）；花屏根因修复（客户端级"丢帧后等关键帧"状态机 + init 原子播种）；核查确认崩溃复现录制取原始流、不受观看端丢帧影响。前端 `ScrcpyPlayer` 已接入 WebCodecs 主解码路径（Annex-B 直喂、SPS 变化才重配、连续失败自动降级 jmuxer，`?scrcpyDecoder=jmuxer` 可强制回退），Safari 兼容性待真机验证。

### ⬜ P2.5 通知渠道扩展

`notification_service.py` 抽象通知 Provider，扩展钉钉/企微/邮件/通用 Webhook（现仅飞书卡片）。

### ⬜ P2.6 审计日志

记录执行/删除/配置变更的操作者与时间（团队共用必要），可先复用 `SystemSetting` + 轻量 `AuditLog` 表。

### ✅ P2.7 CI 集成 API Token

**已落地方案**：`ApiToken` 模型（只存 sha256 哈希 + 展示前缀，明文仅创建时返回一次）；`adk_` 前缀在认证层与 JWT 分流，恒时比较、last_used 节流记录；机器凭证禁入 admin/设置写/改密/Token 管理（`get_current_user_no_token` 依赖，admin 路由一处上游替换全覆盖）；管理页 `/settings/tokens`（创建/列表/吊销，admin 可查全部）；补齐 `GET /reports/executions?batch_id=` CI 轮询过滤；`docs/CI_INTEGRATION.md` 含完整回归脚本与 GitHub Actions/Jenkins 示例。

### ⬜ P2.9 报告端点认证收口（新增治理项）

**现状依据**：P2.7 实施时发现 `GET /reports/executions` 等报告读取端点自基线起就无认证依赖，无凭证可读。内网风险可控，但与全站 JWT/Token 体系不一致。

**建议方案**：报告读取端点统一挂 `get_current_user`；注意 HTML 报告静态资源（`/api/report-assets/`）与报告分享链路的兼容评估。

### ⬜ P2.8 前端测试与渐进 TS

vitest 覆盖 `stores/`、`composables/` 与关键工具函数；巨型组件拆分后对新模块渐进引入 TS（或 JSDoc 类型标注）。

---

## 已知遗留问题

- `backend/tests` 中曾有 2 个与代码演进脱节的过期断言（录制等待时长、投屏重连状态），已随 P0.2 修复；后续行为变更需同步更新测试。
- OCR（PaddleOCR）为可选依赖且不在 requirements 中，`utils/ocr_compat.py` 做了容错；若团队常用 OCR 步骤，建议固化安装说明。
