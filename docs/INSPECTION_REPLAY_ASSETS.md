# 模型化智能巡检、兼容性回放与证据资产指南

本文说明 Android 模型化智能巡检、基于巡检路径的兼容性测试，以及内容寻址证据资产的上线、使用和运维边界。接口示例统一使用 `/api` 前缀，并要求 JWT 或 API Token；系统设置写入只能使用 JWT 登录态。

## 1. 能力边界

- 巡检当前只支持一台显式指定的 Android 设备，创建后独占该设备直到任务结束或取消。
- 巡检不是随机点击。引擎先建立页面语义模型，再按安全规则、页面族覆盖和预算选择动作。
- 每个 Profile 必须同时定义 `guest` 与 `authenticated` 两条业务线；运行时可只选择其中一条。
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
| `inspection_visual_home_actions` | `false` | 首页无语义图片入口探测；依赖覆盖调度 |
| `compatibility_installed_replay` | `true` | 当前安装版本的冻结路径回放 |
| `compatibility_legacy_compare_creation` | `false` | 旧兼容性创建流程的临时回退入口 |
| `content_addressed_assets` | `false` | 报告证据双写到内容寻址资产库 |
| `tiered_asset_retention` | `false` | 分层保留和垃圾回收；依赖内容寻址资产 |

推荐灰度顺序：

1. 开启 `model_inspection`，保留默认的身份模型和页面族收敛。
2. 用真机验证 Profile 的入口、危险动作阻断、脱敏与预算。
3. 开启 `inspection_coverage_scheduler_v2`，观察页面族覆盖和 Coverage Contract；确认后再按需开启视觉首页动作。
4. 开启 `content_addressed_assets`，完成一段双写观察期并执行历史资产回填。
5. 确认资产状态和回滚路径后开启 `tiered_asset_retention`。

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

默认预算为 30 分钟、200 个 State、800 次设备动作、深度 12、400 个 Observation 和 512 MiB 任务资产；单次创建可把持续时间覆盖为 5 至 60 分钟。达到预算、覆盖盲区或安全边界可能产生 `WARNING`，不应自动解释为业务失败。

### 4.2 Web 操作

1. 开启巡检总开关后进入 `/special/inspection`。
2. 新建 Profile，分别配置未登录和已登录的准备/入口用例与就绪断言。
3. 选择一台空闲 Android 设备，可选安装包和业务线后启动。
4. 运行中查看实时阶段、当前页面、动作覆盖和只读画面。
5. 在 `/execution/reports?tab=inspection` 审核页面树、Observation、故障和回放可用性。
6. 勾选稳定 State/Observation；被选证据会冻结为回归来源，内容寻址资产开启时提升为 `PINNED` 保留级别。

删除 Profile 不会破坏历史报告，因为 Run 持有完整 `profile_snapshot`。运行中的 Profile 不可删除；运行中的 Run 只能先取消。巡检也可在定时任务中选择 `inspection` 类型，但必须指定一个 Profile、一台 Android 设备和至少一条业务线。

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

CI 调用示例见 [CI 集成指南](CI_INTEGRATION.md)，产品与数据模型全景见 [项目深度说明](PROJECT_OVERVIEW_CN.md)。
