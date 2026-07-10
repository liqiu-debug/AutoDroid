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

### ⬜ P1.3 异常信息结构化

**现状依据**：预检已有错误码体系（`P1xxx`/`S1001`），但执行期错误多为裸字符串，前端难以给出修复引导。

**建议方案**：执行期错误统一 `{code, message, context, suggestion}` 结构；沿用现有错误码风格扩展执行期码段；前端 LogConsole/报告详情按结构渲染修复建议。

**验收**：常见失败（元素未找到、WDA 断连、设备离线）有明确建议文案。

### ⬜ P1.4 双执行链路统一

**现状依据**：legacy `runner.py`（1072 行）与 `drivers/cross_platform_runner.py` 并存，靠 `new_step_model`/`cross_platform_runner` flag 灰度；双写增加维护与测试成本。

**建议方案**（分三步，每步可回滚）：
1. flag 默认全开，观察一个迭代
2. 存量 `TestCase.steps` 迁移至 `TestCaseStep`（`backend/migrate_case_steps_to_standard.py` 已有雏形，补幂等与校验 `validate_case_steps_migration.py`）
3. 删除 legacy 链路与前端双写逻辑

**验收**：全部执行走跨端 Runner；`runner.py` 删除后全量测试绿。

### ⬜ P1.5 巨型文件拆分

**现状依据**：`fastbot_runner.py` 2899 行、`api/scenarios.py` 2547 行、`ios_driver.py` 2592 行、`main.py` 1741 行、前端 `DeviceStage.vue` 2071 行、`FastbotReportDetail.vue` 2027 行。

**建议方案**（按收益排序）：
- `main.py`：录制端点（`/device/*`）拆到 `api/recording.py`，WS 执行拆到 `ws_run.py`，SPA 托管拆到 `spa.py`
- `fastbot_runner.py`：拆为 `fastbot/` 包（monkey / perf / jank / perfetto / startup 各模块）
- `api/scenarios.py`：CRUD 与并发执行编排分离
- 前端：`DeviceStage.vue` 抽 composables（投屏、触控、录制状态机）

**验收**：行为不变（全量测试绿），单文件不超过 ~800 行。

### ✅ P1.6 SQLite 并发加固

**现状依据**：团队多人并发写场景下 SQLite 默认 journal 模式易出现 `database is locked`。

**已落地方案**：`backend/database.py` 为 engine 附加连接级 PRAGMA（`configure_sqlite_engine`）：WAL（读写并行，写入 DB 文件头持久生效）、`synchronous=NORMAL`、`busy_timeout=30s`；`.gitignore` 补充 `*.db-wal` / `*.db-shm`。

**验收**：`backend/tests/test_database_hardening.py` 验证 PRAGMA 生效与 WAL 持久性。

---

## P2 — 能力补充（按需排期）

### ⬜ P2.1 步骤级重试

`error_strategy` 之外增加 `retry_count`（标准步骤模型字段 + 预检 + 前端 StepBuilder），对不稳定 UI 一次自动重试可显著降低误报。

### ⬜ P2.2 执行排队替代 429 拒绝

限流超限时任务入队等待（带队列上限与超时），前端展示排队位置；比直接 429 更符合团队共用心智。

### ⬜ P2.3 Flaky 用例识别与报告对比

基于 `TestExecution`/`TestResult` 历史统计"最近 N 次通过率"，标记不稳定用例 Top；支持两次执行结果 diff 视图。

### ⬜ P2.4 iOS 实时投屏

WDA 支持 `mjpegServerPort` MJPEG 流，可为 iOS 录制提供近实时画面（替代截图轮询）；改造 `DeviceStage.vue` 支持 MJPEG 源。

### ⬜ P2.5 通知渠道扩展

`notification_service.py` 抽象通知 Provider，扩展钉钉/企微/邮件/通用 Webhook（现仅飞书卡片）。

### ⬜ P2.6 审计日志

记录执行/删除/配置变更的操作者与时间（团队共用必要），可先复用 `SystemSetting` + 轻量 `AuditLog` 表。

### ⬜ P2.7 CI 集成 API Token

长效 API Token（可撤销）供外部 CI 触发用例/场景执行并查询结果，打通"代码合并 → 自动回归"。

### ⬜ P2.8 前端测试与渐进 TS

vitest 覆盖 `stores/`、`composables/` 与关键工具函数；巨型组件拆分后对新模块渐进引入 TS（或 JSDoc 类型标注）。

---

## 已知遗留问题

- `backend/tests` 中曾有 2 个与代码演进脱节的过期断言（录制等待时长、投屏重连状态），已随 P0.2 修复；后续行为变更需同步更新测试。
- OCR（PaddleOCR）为可选依赖且不在 requirements 中，`utils/ocr_compat.py` 做了容错；若团队常用 OCR 步骤，建议固化安装说明。
