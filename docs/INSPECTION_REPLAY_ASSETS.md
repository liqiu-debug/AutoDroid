# 模型化智能巡检、兼容性回放与证据资产指南

本文说明 Android 模型化智能巡检、基于巡检路径的兼容性测试，以及内容寻址证据资产的上线、使用和运维边界。接口示例统一使用 `/api` 前缀，并要求 JWT 或 API Token；系统设置写入只能使用 JWT 登录态。

## 1. 能力边界

- 巡检当前只支持一台显式指定的 Android 设备，创建后独占该设备直到任务结束或取消。
- 巡检不是随机点击。引擎先建立页面语义模型，再按安全规则、页面族覆盖和预算选择动作。
- 巡检不是全控件穷举器。页面族采样、安全阻断和 Coverage Contract 会有意减少重复或高风险操作。
- 每个 Profile 必须同时定义 `guest` 与 `authenticated` 两条业务线；运行时可只选择其中一条。
- 海尔商城业务覆盖只适用于 `com.ehaier.zgq.shop.mall`；其他包仍提供页面族探索指标，但不会套用海尔清单。
- `run.status` 表示设备、故障、告警和队列等执行健康；`coverage_verdict` 表示业务完整性，不能互相替代。
- 巡检可以使用设备当前安装版本，也可以先安装一个已上传的 Android 包。
- 巡检报告支持运行中只读 Scrcpy 画面、实时阶段和完整快照；关闭报告页不会中止任务。
- 兼容性测试可继续使用页面合集，也可使用巡检冻结的稳定路径作为来源。
- 已安装版本回放只验证当前设备上的包，不接受 APK ID，不执行安装或版本切换。

## 2. Feature Flags 与启用顺序

开关保存在 `SystemSetting`，可在“系统配置 -> 系统设置”中维护。

| 开关 | 默认值 | 作用与依赖 |
|---|---:|---|
| `model_inspection` | `false` | 巡检总开关 |
| `inspection_identity_v2` | `true` | Template/State/Observation 身份模型；依赖巡检总开关 |
| `inspection_exploration_family_convergence` | `true` | 同构页面族共享增量覆盖；依赖身份模型 |
| `inspection_similarity_convergence` | `false` | 高置信相似状态收敛；依赖身份模型 |
| `inspection_coverage_scheduler_v2` | `false` | 页面族与动作组覆盖调度；依赖身份模型 |
| `inspection_business_coverage_v2` | `false` | 海尔商城 v2 清单冻结与影子评估；与覆盖调度同时开启时启用定向补齐和 15% 终点复验 |
| `inspection_visual_home_actions` | `false` | 首页无语义图片入口探测；依赖覆盖调度 |
| `compatibility_installed_replay` | `true` | 当前安装版本的冻结路径回放 |
| `compatibility_legacy_compare_creation` | `false` | 旧兼容性创建流程的临时回退入口 |
| `content_addressed_assets` | `false` | 报告证据双写到内容寻址资产库 |
| `tiered_asset_retention` | `false` | 分层保留和垃圾回收；依赖内容寻址资产 |

推荐灰度顺序：

1. 开启 `model_inspection`，保留默认的身份模型和页面族收敛。
2. 用真机验证 Profile 的入口、危险动作阻断、脱敏与预算。
3. 海尔商城先开启 `inspection_business_coverage_v2`，只观察冻结清单和影子评估，不改变普通探索顺序。
4. 核对旅程证据和盲区后开启 `inspection_coverage_scheduler_v2`，此时缺失旅程动作会优先于普通页面族探索；确认后再按需开启视觉首页动作。
5. 开启 `content_addressed_assets`，完成一段双写观察期并执行历史资产回填。
6. 确认资产状态和回滚路径后开启 `tiered_asset_retention`。

后端会校验父子依赖。关闭父开关时，已开启的子开关会同时写为 `false`；历史巡检报告继续按创建时的 `profile_snapshot` 和协议版本解释，不会被当前开关重新计算。

## 3. 巡检数据模型

| 对象 | 含义 |
|---|---|
| Profile | 可复用配置，包含包名、双业务线入口、输入/安全/脱敏规则、动态文本规则、预算和监控选项 |
| Run / BranchRun | 一次不可变任务快照及其业务线执行状态 |
| PageTemplate | 跨运行复用的页面结构、动作、锚点和风险签名 |
| State | 报告中的业务页面节点，保存层级、页面角色、稳定性和首条到达路径 |
| Observation | State 的一次实际采集，可作为代表采集或兼容性基线 |
| Transition | 动作边，记录定位候选、执行结果、安全边界和拓扑关系 |
| ExplorationFamily | 同构页面实例组成的页面族，用于增量覆盖 |
| CoverageContract | 页面族与动作组的覆盖要求及采样结果 |
| Coverage Manifest / Assessment | Run 创建时冻结的海尔清单、哈希、逐项证据、盲区和双层结论 |
| Fault | Crash、ANR、基础设施或自动化故障及其证据 |

Graph 当前返回 `schema_version=8`、`hierarchy_version=2`。客户端必须以响应中的版本为准：v2 层级使用 `BRANCH_ROOT / PEER / PAGE / VIEWPORT / ORPHAN` 和 `SELF / VIEWPORT / PEER / CHILD` 关系；设备真实回放始终使用冻结的路径步骤，而不是按画布连线推导。

## 4. 配置与运行流程

### 4.1 Profile 必填项

- `name`、`package_name`。
- `branches.guest` 与 `branches.authenticated`，每条包含准备 case、入口 case、可选环境和就绪断言。
- 就绪断言的 `by` 只允许 `description`、`text` 或 `xpath`。
- 输入规则必须提供至少一个匹配条件，值来源为字面量或环境变量；敏感输入默认不允许。
- 安全规则用正则描述阻断或允许的目标；明确的支付、删除和提交语义仍是硬边界。
- 脱敏规则用于在持久化前移除敏感节点和证据。

默认预算为 30 分钟、200 个 State、800 次设备动作、深度 12、400 个 Observation 和 512 MiB 任务资产；Profile 与单次创建均可把持续时间设为 5 至 120 分钟。达到预算、覆盖盲区或安全边界可能产生 `WARNING`，不应自动解释为业务失败。

海尔商城建议按目标选择时长：

| 目标 | 建议时长 | 说明 |
|---|---:|---|
| 单业务线常规验收 | 60 分钟 | 优先保证 v2 必达旅程和终点复验 |
| 单业务线深度探索 | 90 分钟 | 核心完成后继续发现长尾页面与动作组 |
| `guest + authenticated` 全应用验收 | 120 分钟 | 两条业务线共享任务预算，约为每条业务线保留一个标准窗口 |

启用海尔 v2 定向调度时，前 85% 预算用于定向补齐和开放式探索，最后 15% 用于终点复验。延长时长不会重新执行 `SAMPLED_OUT`、页面族/Contract 复用和安全阻断；`BUDGET_NOT_REACHED` 适合增加预算，`QUEUE_TRUNCATED` 或 `PATH_DIVERGED` 应优先修复入口和父路径恢复。

### 4.2 Web 操作

1. 开启巡检总开关后进入 `/special/inspection`。
2. 新建 Profile，分别配置未登录和已登录的准备/入口用例与就绪断言。
3. 选择一台空闲 Android 设备，可选安装包和业务线后启动。
4. 运行中查看实时阶段、当前页面、动作覆盖和只读画面。
5. 在 `/execution/reports?tab=inspection` 审核页面树、Observation、故障和回放可用性。
6. 勾选稳定 State/Observation；被选证据会冻结为回归来源，内容寻址资产开启时提升为 `PINNED` 保留级别。

删除 Profile 不会破坏历史报告，因为 Run 持有完整 `profile_snapshot`。运行中的 Profile 不可删除；运行中的 Run 只能先取消。巡检也可在定时任务中选择 `inspection` 类型，但必须指定一个 Profile、一台 Android 设备和至少一条业务线。

### 4.3 海尔商城可信业务覆盖

创建海尔商城 Run 时冻结 `haier-mall-v2` 清单、版本、哈希和所选业务线。必达旅程如下：

| 范围 | 必达旅程 |
|---|---|
| 双业务线 | 五底栏真实到达、分类到固定搜索及商品详情、许愿池内容、服务列表到详情、门店列表到详情 |
| 已登录 | 商品详情到规格/结算/收银台/支付拦截、订单中心、设置到地址、会员权益、收藏、历史浏览 |
| 未登录 | 个人中心登录门槛、商品购买登录门槛（页面类型 `AUTH_GATE`） |
| 可选 | 地址编辑、门店预约、耗材专区、服务订单 |

搜索旅程固定使用非敏感关键词“冰箱”，必须形成 `SEARCH -> PRODUCT_LIST -> PRODUCT_DETAIL` 的真实 Transition 链。逐项状态与结论语义如下：

| 字段 | 语义 |
|---|---|
| `COVERED` | 同一业务线内真实成功 Transition、可读 XML 和一次终点复验均满足；支付终点接受明确的 `BLOCKED/PAYMENT` |
| `MISSING` | 执行完整但没有形成要求的独立路径证据 |
| `INCONCLUSIVE` | XML 缺失、预算停止、终点复验失败、清单冲突等导致无法可靠判定 |
| `NOT_IN_SCOPE` | 本次未选择该业务线或旅程不适用于该业务线 |
| `selected_scope_verdict` | 本次所选业务线的完整性；单业务线可为 `COMPLETE` |
| `full_app_verdict` | 仅当 guest、authenticated 都运行且全部必达项通过时为 `COMPLETE` |

报告主指标使用核心旅程数量、运行范围和证据质量；`exploration_coverage` 只表示“已发现页面族展开率”。预算停止、未运行业务线、XML 缺失、未知/不透明页面、Contract 冲突和复验失败均作为显著盲区展示。评估完成后 State 的 `coverage_status` 会回写为 `REQUIRED_EVIDENCE`、`OPTIONAL_EVIDENCE`、`EXPLORED` 或 `INCOMPLETE`。

## 5. 主要 API

| 方法与路径 | 用途 |
|---|---|
| `GET/POST /api/inspections/profiles` | 查询或创建 Profile |
| `GET/PUT/DELETE /api/inspections/profiles/{id}` | Profile 详情、更新或删除 |
| `POST /api/inspections/runs` | 创建巡检任务 |
| `GET /api/inspections/runs` | 分页查询巡检任务 |
| `GET/DELETE /api/inspections/runs/{id}` | 详情或删除已结束任务 |
| `POST /api/inspections/runs/{id}/cancel` | 取消运行中任务 |
| `GET /api/inspections/runs/{id}/graph` | 获取版本化页面拓扑 |
| `GET /api/inspections/runs/{id}/coverage` | 获取冻结清单、逐项证据、盲区及所选范围/全应用结论 |
| `GET /api/inspections/runs/{id}/families` | 获取页面族与覆盖摘要 |
| `GET /api/inspections/runs/{id}/replay-paths` | 分页获取冻结回放路径 |
| `GET /api/inspections/runs/{id}/states/{state_id}/observations` | 获取 State 的采集历史 |
| `PUT /api/inspections/runs/{id}/regression-selection` | 冻结回归 State/Observation |
| `GET /api/inspections/runs/{id}/live` | 获取最新实时快照 |
| `POST /api/inspections/runs/{id}/live-session` | 用 JWT 领取一次性实时票据 |

实时事件与视频分别使用：

- `WS /ws/inspections/runs/{id}/live?ticket=...`
- `WS /ws/inspections/runs/{id}/video?ticket=...`

票据短效且单次消费。`live-session` 禁止 API Token，避免长效机器凭证进入浏览器 WebSocket URL。

## 6. 三种兼容性来源与已安装回放

| 模式 | 输入 | 设备要求 | 目的 |
|---|---|---|---|
| 页面合集版本对比 | 旧包/当前版本、新包、页面合集 | 一台或多台 Android | 升级或干净安装前后对比 |
| 巡检快照/版本/机型对比 | 巡检 Run 和稳定 State/Observation | 快照/版本按所选设备；机型至少两台 | 复用真实发现的页面和脱敏基线 |
| 已安装版本回放 | 巡检 Run、业务线、冻结路径、设备快照摘要 | 恰好一台 Android | 不安装 APK，验证当前安装版本仍可到达稳定路径 |

已安装版本回放分两步：

1. `POST /api/compatibility/replay-preflight` 检查源 Run、业务线、当前安装包身份、安全边界和可选路径，返回 `plan_digest`、`device_snapshot_digest`、blocker/warning 和路径列表。
2. 用户确认安装状态后，`POST /api/compatibility/runs` 使用 `execution_mode=installed_replay`、同一设备与摘要、所选路径 ID，并显式传 `manual_install_confirmed=true`。

创建接口会重新校验摘要，防止预检后设备包或回放计划变化。完整路径可执行到目标页；遇到安全边界的路径只能执行已冻结的安全前缀。回放 Trace 不记录输入明文。

`replay_evidence_available` 只说明报告内存在可人工选择的稳定证据；`replay_default_eligible` 还要求 Run 为 `PASS` 且所选业务线覆盖为 `COMPLETE`。部分、告警或失败 Run 仍可人工明确选择稳定 State/Observation，但空选择不会自动采用全部路径。兼容性任务会冻结来源 Run 的清单 ID、版本、哈希和覆盖结论，局部回放不能被解释为全应用覆盖。

## 7. 内容寻址证据资产

开启 `content_addressed_assets` 后，巡检和兼容性证据会在保留 legacy `reports/` 路径的同时双写到 `asset_store/`：

- `StoredAsset` 记录逻辑内容哈希与落盘 blob 哈希并据此去重；API 使用不透明的 asset ID。
- `AssetReference` 以 owner、role 和保留级别引用 blob；删除一个报告只释放自己的引用。
- `asset_store/` 不挂公开静态目录，只能通过鉴权的 `GET /api/assets/{asset_id}` 读取。
- 读取支持 `ETag`、`If-None-Match` 和单段 `Range`；已知但已删除的资产返回 `410`，未知 ID 返回 `404`。

分层保留语义：

| 级别 | 默认期限 | 内容 |
|---|---:|---|
| `HOT` | 7 天 | 新近完整截图、XML 和动作图 |
| `WARM` | 90 天 | 重要证据及降采样派生物 |
| `PINNED` | 不自动过期 | 人工选择的回归基线和兼容性冻结证据 |
| `COLD` | 仅元数据/缩略图 | 普通历史采集降级后的最小可审计信息 |

释放最后一个引用后仍有 24 小时恢复窗口。每日 03:17 的保留任务分别执行旧报告清理和资产分层 GC；`report_retention_days=0` 只关闭旧报告按天清理，不会替代资产分层开关。

### 7.1 容量水位

`GET /api/assets/status` 返回文件系统使用率、资产库字节数、各层字节数、可回收空间和 `can_start`。水位设置为：

- `asset_storage_low_watermark_percent`，默认 80。
- `asset_storage_high_watermark_percent`，默认 90。
- `asset_storage_critical_watermark_percent`，默认 95。

三者必须满足 `low < high < critical`，否则状态与分层 GC 回退到 `80/90/95`。当前新巡检和兼容性任务的硬容量保护使用内置 `95%`，达到后返回 HTTP `507`；自定义 critical 尚不改变该创建阈值。已有报告和读取不受影响。

### 7.2 历史回填与回滚

先备份数据库和 `reports/`，再执行幂等回填：

```bash
.venv/bin/python scripts/maintenance/backfill_artifacts.py --limit 500
.venv/bin/python scripts/maintenance/backfill_artifacts.py --after-id 500
```

需要在应用回滚前从 CAS 重建 legacy 路径时执行：

```bash
.venv/bin/python scripts/maintenance/backfill_artifacts.py --materialize-legacy
```

仅在明确需要覆盖现有 legacy 文件时追加 `--force`。

历史海尔 Run 只能按 `haier-mall-v1` 回填，禁止使用 v2 规则追溯重判。先预览，再按需指定 Run 执行：

```bash
.venv/bin/python scripts/maintenance/backfill_haier_business_coverage.py --dry-run
.venv/bin/python scripts/maintenance/backfill_haier_business_coverage.py --run-id 44 --run-id 49
```

脚本默认跳过已有评估；`--force` 也不会覆盖已冻结为 `haier-mall-v2` 或其他清单版本的 Run。v1 只定义已登录范围，因此即使历史必达项全部通过，`full_app_verdict` 仍为 `INCOMPLETE`。

## 8. 验收与排障

离线检查历史巡检的覆盖调度：

```bash
.venv/bin/python scripts/maintenance/replay_inspection_coverage.py <run_id> --strict
.venv/bin/python scripts/maintenance/audit_haier_coverage.py <run_id> --strict
```

常见问题：

- `404 模型化智能巡检尚未启用`：开启 `model_inspection`，并重新登录或刷新前端 Feature Flags。
- `409 设备非空闲`：检查设备状态和 `lease_task_id`；不要手工把正在执行的设备改成 `IDLE`。
- `507 asset storage`：查看 `/api/assets/status`，清理无引用资产或扩容；不要直接删除仍被引用的 blob。
- 回放预检 blocker：先修复包名/签名/版本、设备快照或源路径问题，再重新预检；不要复用旧摘要。
- 报告图片 `401/403`：资产 URL 需要 Authorization 头，前端应通过 API 请求 Blob，不能直接匿名嵌入。
- 历史 Graph 展示异常：按响应 `schema_version` 和 `hierarchy_version` 选择解析器，不要用当前规则重算旧报告。
- 核心旅程完整但仍有“预算未到达”：先区分 `BUDGET_NOT_REACHED` 与 `QUEUE_TRUNCATED/PATH_DIVERGED`；前者可尝试 90/120 分钟，后者优先修复路径恢复，不能只靠延长任务。
- 页面族展开率高但核心旅程低：以 `/coverage` 的冻结评估为准；页面族指标的分母不包含未发现页面，不能作为业务完整性结论。

CI 调用示例见 [CI 集成指南](CI_INTEGRATION.md)，产品与数据模型全景见 [项目深度说明](PROJECT_OVERVIEW_CN.md)。
