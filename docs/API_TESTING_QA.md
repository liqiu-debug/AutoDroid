# 接口自动化首期验收记录

日期：2026-09-30。所有接口执行验收使用临时数据库、模拟服务或本地测试 HTTP 服务；未向真实业务系统写数据，未向真实飞书群发送通知。

## 本次体验优化验证

### 面向初级测试的可维护性改造（2026-10-08）

本轮目标是“初级测试也能维护”：接手别人建的场景时能看懂在测什么、知道接口改了该去哪修、不容易写出会误报的断言。所有浏览器验证均在隔离预览程序内完成，未访问真实业务接口、设备或飞书，未调用真实模型。

#### 接口变更影响面与批量同步

- 新增 `GET /interfaces/{id}/usages`：一次批量查询返回引用该接口的场景与步骤，逐项标记 `stale` 与冲突字段路径；场景列表新增 `stale_steps`，接口页顶部显示“被 N 个场景的 M 个步骤引用”，场景列表在名称旁显示“接口有更新”角标。
- 新增 `POST /interfaces/{id}/sync-apply`：只同步没有本步骤修改的旧版本步骤；每项独立事务，版本不匹配（场景已被他人修改）、场景正在运行、存在未决冲突时只跳过该项并返回原因，其余步骤照常更新。成功同步提升场景版本号，因此并发的编辑器保存会得到 409 而不是静默覆盖。
- 隔离预览实测：`查询订单` 接口刻意比场景快照高一个版本。批量同步“订单查询 · 轻量示例”的无冲突步骤成功并升版（场景 v1→v2，步骤升到 v2）；“下单回归 · 示例”中改过 `query` 的步骤被拒绝，返回 `存在需要逐项确认的冲突` 与 `[["request","query"]]`；同步后完整场景（登录 → 创建订单 → 查询订单）重新运行三步全部 PASS。
- 后端覆盖 `test_interface_usages_report_stale_steps_and_conflicts`、`test_sync_apply_updates_clean_steps_and_requires_conflict_decisions`、`test_sync_apply_refuses_stale_version_running_scenario_and_current_steps`。前后端共用同一冲突键（`request.url` 形式的点号拼接），避免依赖 JSON 空格匹配。

#### 编辑器可读性与防误用

- 步骤卡片保持只显示请求方法与校验数（`POST · 2 条校验`）；曾尝试加入请求路径，因卡片宽度放不下而回退。保留的改动是同一卡片在一次渲染里只深拷贝一次步骤（此前调用两次 `effectiveStep`），显示内容与此前完全一致。
- 场景说明常驻标题下方，为空时给出补充提示并可点击进入“场景信息”；说明随运行写入快照（`snapshot.description`），报告页与 HTML 导出都会显示。旧报告没有该字段，读取处使用 `.get()`，打开正常。场景列表新增“说明”列。
- 断言把动态字段识别抽成共享函数 `dynamicField`，自动建议与手动提示使用同一条规则。手动把 id/token/时间类字段改成“等于”时，该行就地提示“该字段每次运行通常不同，固定值容易误报”，并提供一键改为“非空”或“与前序字段一致”，不阻断保存。

#### 步骤级失败重试

- 步骤新增 `retry_count`（0–3，默认 0），入口在请求配置的「高级 → 失败重试」。步骤存在 JSON 列中，旧数据按默认值解析，无需数据库迁移。
- **只重试请求未到达服务端的失败**（连接被拒绝、连接超时、代理错误）；已收到响应、读超时、写超时一律不重试。这与 UI 用例的失败重试语义有意不同：接口步骤可能是写操作，本模块不做业务回滚，重复发送等于重复写入业务数据。
- 内部可重试标记不落库、不出现在结果中（测试断言 `retryable` 不泄漏）；结果记录 `attempts` 与 `retry_history`，报告和响应区在尝试次数大于 1 时标注“共尝试 N 次”并列出前几次的连接失败原因。
- 隔离预览实测：`/flaky` 首次模拟连接失败，`retry_count=1` 的步骤最终 PASS，`attempts=2`、`retry_history` 一条、耗时约 1.1 秒（含 1 秒退避）。
- 后端覆盖 `test_retry_only_repeats_requests_that_never_reached_the_server`、`test_read_timeout_and_received_responses_are_never_repeated`、`test_zero_retries_keep_current_behaviour_and_backoff_is_cancellable`、`test_retried_step_is_labelled_in_the_exported_report`。

#### 接口文档导入

- 新增 `backend/api_testing/specs.py` 与 `POST /imports/spec`（只读解析）、`POST /imports/apply`（一次事务全建或全不建）。支持 OpenAPI 3、Swagger 2.0、Postman v2.1，JSON 与 YAML 均可；YAML 使用 `safe_load`，拒绝任意对象构造；不读文件、不起 shell、不访问网络；单次上限 2 MiB、300 个接口，超出时显式提示只导入前 300 个。
- 路径参数保留 `{id}` 占位并生成同名路径参数；JSON 请求体按文档结构生成可编辑示例；OpenAPI 的 2xx 示例与 Postman 保存的响应会一并存为样例，导入后无需先调试即可选取字段。
- **Postman 声明的凭证不作为值导入**：Bearer/Basic/API Key 只映射类型、凭证留空；请求头中的凭证按 cURL 导入的既有策略保留原文并提示替换为敏感环境变量。此策略与 cURL 导入保持一致，未引入新的脱敏规则。
- 前端入口：「接口管理 → 导入文档」与场景「添加步骤 → 导入接口文档」（后者导入后同时加入场景草稿）。粘贴 → 解析预览 → 勾选（可搜索、跨筛选保留选择）→ 选目标目录 → 导入。
- 后端覆盖 `backend/tests/test_api_testing_specs.py` 8 项（OpenAPI JSON/YAML、`$ref` 与自引用终止、Swagger 2 host/basePath/body/formData、Postman 嵌套目录与 raw/urlencoded 请求体、危险 YAML 标签与超大输入拒绝、候选数量上限、重复 key 去重），以及 `test_document_import_parses_then_creates_only_selected_entries`、`test_document_import_is_all_or_nothing`。
- 新增依赖 `pyyaml` 到 `requirements-base.txt`（此前只是 androguard/paddleocr 的传递依赖）；未安装时 JSON 文档仍可解析，YAML 会给出明确提示。

#### 工程保障

- 前端测试接入 CI：`.github/workflows/ci.yml` 的 frontend job 在 `npm ci` 后先执行 `node --test tests/*.test.mjs` 再构建。此前 17 个测试文件只在本地运行，CI 只做构建。
- 新增 `frontend/tests/apiMaintainabilityUiContracts.test.mjs`（7 项）覆盖本轮界面契约；`tests/apiTesting.test.mjs` 新增 `stepSummary`、`dynamicField`、`conflictKey` 用例。
- 预览种子扩展为 2 个场景 / 4 个接口，覆盖过期检测、批量同步、冲突确认与失败重试四条路径。

自动检查：

| 检查 | 本轮结果 |
|---|---|
| 接口自动化后端（values/engine/api/ai/specs） | 78 项通过 |
| 后端全量回归 | 1334 项通过 |
| 前端全量测试 | 153 项通过 |
| 前端生产构建 | 通过 |
| Ruff（backend + scripts） | 通过 |

复现命令：

```bash
.venv/bin/python -m unittest backend.tests.test_api_testing_values backend.tests.test_api_testing_engine backend.tests.test_api_testing_api backend.tests.test_api_testing_ai backend.tests.test_api_testing_specs -v
cd frontend
node --test tests/*.test.mjs
npm run build
```

隔离预览（种子已包含过期接口、无冲突步骤、冲突步骤和一次性连接失败）：

```bash
API_PREVIEW_DIR=$(mktemp -d /tmp/autodroid-api-preview.XXXXXX)
AUTODROID_API_PREVIEW=1 AUTODROID_DB_PATH="$API_PREVIEW_DIR/preview.db" \
  .venv/bin/python -m uvicorn backend.tests.api_testing_preview_app:app --host 127.0.0.1 --port 18765
```

本轮未做（已知遗留，非本轮范围）：报告读取端点的归属校验（见路线图 P2.9）、执行线程池固定 4 worker 的容量问题、环境变量缺少审计、AI `debug_context` 中循环泄漏的 `result` 变量可读性。

#### 同日跟进修正

按实际使用反馈处理的几处问题，均为既有行为的修正而非新功能：

- **确认弹窗横向铺满**：Element Plus 的 `.el-message-box` 以 `width:100%` 配合 `max-width` 限宽，而全局样式只覆盖了 `max-width: calc(100vw - 32px)`，等于把 420px 的上限换成了接近视口宽度，所有 `ElMessageBox.confirm` 弹窗（退出登录、删除确认等）都被拉成通栏。修正为保留 `width: var(--el-messagebox-width, 420px)`、仅用 `max-width` 适配窄屏；弹窗由遮罩层的 `text-align:center` 居中，无需额外处理。
- **账户菜单回到顶栏右侧**：界面重构曾把账户菜单（头像 + 姓名 + 修改密码 / 退出登录）移到侧边栏底部，顶栏右侧只剩模式切换。现搬回顶栏右侧并移除侧栏底部账户区，侧栏全部留给菜单。
- **请求头以配置为准**：预检不再对 `Host` / `Content-Length` / `Connection` / `Transfer-Encoding` 报“由执行器管理，请移除或禁用”。真实 socket 验证：httpx 对显式 `Host`、匹配的 `Content-Length`、`Connection` 按原值发送；`Transfer-Encoding: chunked` 改为以流式请求体发送，由传输层做分块并省略 `Content-Length`（若直接把该头随定长请求体发出，报文会同时带两种长度标记而被服务端误读）。顺带修正一个既有缺口：传输层拒绝的组合会抛出 `h11.LocalProtocolError`，它不在 httpx 异常体系内、此前会逃出执行引擎记为“执行器内部异常”，现在在引擎内捕获并记为配置错误，明确提示检查 `Content-Length` 与 `Transfer-Encoding`。后端新增 `test_configured_host_frame_headers_are_sent_as_written`、`test_chunked_body_is_framed_by_the_transport`、`test_headers_the_transport_refuses_report_a_configuration_error`、`test_configured_frame_headers_are_no_longer_blocked_by_precheck`。
- **定时任务列表「执行内容」列**：与相邻的「下次运行 / 状态 / 操作」一致改为居中（`align="center"`），并给单元格内的 flex 容器加 `justify-content:center`——仅靠列的 `text-align` 不会移动 flex 子元素，表头与内容才会一起居中。
- **修改密码页标题**：页头同时带 `page-header` 与全局 `ad-page-header`（`justify-content: space-between`），导致「修改密码」被推到最右。页内样式显式设为靠左，与「返回」并排；顶栏面包屑仍显示「账号设置 / 修改密码」。
- 步骤卡片的请求路径按反馈回退（见上文）。
- **运行大盘加入接口自动化**：大盘此前只统计 `TestExecution`（设备执行），接口运行记录在独立的 `ApiRun` 表里，因此完全不出现。新增 `DashboardOverview.api_automation` 独立板块（次数 / 通过率 / 失败 / 运行中 / 平均耗时、同时间范围的趋势、最近运行并可跳转接口报告），复用既有趋势分桶逻辑。**有意不合并进顶部设备 KPI**：`ApiRun.scenario_id` 与 `TestExecution.scenario_id` 属于不同的 id 空间，平台筛选对接口无意义，强行合并会让“高失败场景”和告警把两类场景混在一起；接口板块只跟随时间范围。隔离预览实测：跑完一次场景后 24h 板块显示 1 次、通过率 100%、最近运行一条并指向接口报告。后端新增 `test_dashboard_includes_api_automation_in_its_own_block`、`test_dashboard_api_block_is_empty_but_present_without_runs`，并断言设备 KPI 与高失败场景不受影响。移动端大盘未加该板块，因为接口报告详情路由不支持移动模式，点进去会落到“不可用”页。
- **UI 用例编辑页恢复三栏**：界面重构把「通用步骤」从 220px 常驻列改成了「添加动作」抽屉，并把步骤列从 350px 放宽为 `1fr`（宽屏约半屏），同时删掉了步骤卡片按动作类型着色的两条规则（`StepBuilder` 至今仍在为每个步骤计算 `--action-color`，只是没有样式再引用它；同类的场景编排页保留着这套着色）。现恢复为设备画面（弹性）+ 通用步骤 220px + 步骤构建器 350px 三栏，移除抽屉，恢复卡片左侧色条与序号色块；保留重构带来的“点击即可添加动作”和设计令牌化。通用步骤面板在视口较矮时自身滚动，不再被裁切。

#### AI 校验建议：重叠修复、批量生成与一个既有清洗缺陷

- **重叠错乱（已定位并修复）**：Element Plus 的 `.el-checkbox-group` 自带 `font-size:0; line-height:0`，而建议的说明段落 `<p>` 就渲染在该 group 内，只覆盖了 `font-size`、没覆盖 `line-height` → 每行行框高度为零，多行文字叠在一起。修复放在新的共享展示组件 `AiSuggestionList.vue` 上（`.suggestions{font-size:13px;line-height:1.6}`），单步对话框与批量面板共用同一份行渲染，DOM 结构不变（checkbox 仍需留在 group 内才能 v-model）。`AiAssertions.vue` 的 `<script>` 未改动，其 11 项行为测试（过期 token、迟到响应、重复点击、撤销保护、反馈去重）零改动通过。
- **场景级批量生成**：新增入口「更多 → AI 校验建议（全部步骤）」（仅 AI 可用时显示）与新抽屉 `AiBatchAssertions.vue`。流程为：统一确认（真实执行警告）→ 从头执行到最后一步 → **顺序**为每个 PASS/未校验且拿到响应的步骤调用现有 `/ai/suggest-assertions`（顺序调用天然满足服务端每用户 2 并发上限）→ 按步骤分组审阅、默认全选 → 应用全部到草稿 → **按顺序**调用 `/debug-sessions/{id}/assertions` 重新校验（不重发请求）→ 用户保存。失败步骤不生成（避免把错误响应固化），其后步骤标「未执行：前序步骤未通过」。
- **时序约束被测试锁住**：`test_batch_flow_must_generate_for_every_step_before_applying` 断言"一次 through 执行后可为每一步生成 → 给第 1 步应用断言后第 2 步生成返回 409 → 按顺序 recheck 修复链路后第 3 步又可生成"。这正是界面不能边生成边应用的原因。
- **`useApiDebug` 的两处可选参数**：`run(stepId, mode, { confirmed })` 让批量流程复用统一的确认框，`recheck(stepId, { silent })` 返回 `{status, error}` 供批量流程判断是否继续；两者默认行为不变，现有调用点零改动。
- **顺带修掉一个既有清洗缺陷**：预览里实测发现登录响应的 `user_id` 根本没被送给模型。原因是 `Sanitizer.allowed_path` 用 `text(part) == part` 判断字段名，而步骤文档会登记出值为 `id` 的"密钥"（`add()` 对每个值同时登记原文、URL 编码和 base64），于是 `user_id` 被当成"包含密钥"整体剔除 —— `text("user_id")` 变成 `user_` + `[已隐藏]`。后果不只是少一个字段：预览里"动态 ID 建议应被过滤"的演示也因此失效（动态字段压根没进上下文）。修复为新增 `identifier()`，字段名只按**长度 ≥ 4** 的密钥值判定，正文清洗（`text()`）仍使用全部已登记值，因此脱敏强度不变。修复后预览三步各自恢复出 2 条建议 + 1 条"动态字段不能使用固定值比较"的过滤提示。
- **重复点击累加 + 一次 20 条**（同日反馈）：根因两处——`apply()` 对草稿是追加且应用后建议列表与勾选仍在，再点一次就再追加；提示词明确要求"优先建议类型、存在和非空"，模型便为每个字段各给一套。修正：服务端 `validate_suggestions` 新增 existing 过滤（相同指纹或该字段已有结构性校验的不再列出并提示）、按字段合并、按价值排序（下游引用 > 业务结果字段 > 状态码 > 浅层）、截断到 `MAX_SUGGESTIONS=6`（有业务目标 8）；提示词同步改写并把下游引用路径作为事实送入上下文。前端 `newAssertions()` 在单步与批量两处应用时再去重，已应用建议置灰标"已应用"，应用按钮只统计新选中项。预览实测：首次生成 `body.code` 只剩 1 条（type），应用后再生成提示"1 条建议已存在"且不再给同字段的非空。新增后端测试 3 项（合并/排序/截断、已存在过滤、下游引用与目标上限），前端 `assertionKey/newAssertions` 单测与"重复应用不累加"行为测试。
- 隔离预览实测（API 层，确定性 mock 模型）：through 执行三步全 PASS → 三步各生成建议 → 应用全部 → 顺序 recheck 全部 PASS（`3/3/4` 条断言，未发新请求）→ 草稿保存成功（场景升到 v2）。后端新增 `test_short_secrets_do_not_hide_unrelated_field_names`、`test_batch_flow_must_generate_for_every_step_before_applying`；前端新增 `batchSuggestionPlan` 与 3 项界面契约测试。
- 未做：不自动保存；不自动"再跑一遍以继续后面的步骤"（through 模式会从第 1 步重发请求，重复写入），链路在某步停下时由用户修好后再执行；不改 AI 过滤规则。

跟进后自动检查：后端全量 1346 项通过、前端 166 项通过（新增 `layoutUiContracts.test.mjs` 7 项与本轮 3 项 AI 契约测试）、生产构建与 Ruff 通过。弹窗居中、顶栏账户菜单、表格对齐、用例编辑页三栏与卡片着色、AI 建议行距与批量面板排版属于视觉效果，未经真实浏览器目视确认。

### 提交前 CI 兼容性修正（2026-10-02）

合并前补跑全量回归时，确认下文记录的两个旧日期依赖失败在 `origin/main` 的隔离副本中同样存在。测试随后固定服务时钟到夹具时间，保留真实查询和清理逻辑，避免随日历推进失效。

GitHub 新安装环境拉取了 SQLModel 0.0.47，时间字段新增的时区要求与项目现有无时区存储方式不兼容，导致 465 项测试在数据初始化阶段报错。依赖已固定为本地验证的 SQLModel 0.0.32；不在本次发布中迁移既有时间数据。

### 低代码流程与可选 AI 实施验收（2026-10-02）

本节为最新实现记录；下方保留前几轮验收历史。当前行为以本节和 [使用指南](API_TESTING.md) 为准。

- 场景可直接新建请求、粘贴 cURL 或选择接口库，无须先建立公共接口。浏览器完成场景内 cURL 导入、调试、保存、重开和正式运行；接口首次保存后的“加入场景”正确进入未保存草稿。
- 浏览器执行“登录 → 创建订单 → 查询订单”三步模拟业务链，全部通过。查询步骤的路径参数及订单 ID 校验引用创建步骤；另存到接口库时，含前序引用会显示替换提示并禁用保存，不修改原场景。
- 删除全部校验后，正式运行预检定位到校验区域；调试仍保留响应并标记“未校验”。添加建议后使用“重新校验响应”转为通过；自动测试验证 HTTP 请求次数不增加，未校验/失败前序阻止后续调试。HTTP 500 无校验、预期 HTTP 400、手动运行 API 和定时执行门槛均有后端覆盖。
- 浏览器就地新建环境和 `BASE_URL` 变量，列表立即刷新且旧调试上下文失效。主选择器保存默认环境，临时切换另一个环境后保存并重开，默认环境仍是原值，临时标签消失。
- 请求与校验分层、紧凑空响应、可调整响应分隔线和可点击引用标签已接入。1280×720 与 1440×900 下页面宽度分别为 1280/1440，无页面级横向溢出；保存、运行、调试、重新校验按钮可见。分隔线键盘调整正常。
- AI 模拟生成建议可勾选并应用到草稿，固定动态 ID 建议被过滤。切换到校验结果仍可撤销，撤销只移除本次未修改的建议。失败解释区分事实、可能原因与排查步骤，报告原始失败状态保持不变。
- AI 后端覆盖默认关闭、缺少配置、用户及编辑器会话隔离、脱敏出站、非法路径/类型/引用、超时、无效输出、结构化格式回退和反馈统计。前端真实 Vue 响应式测试覆盖草稿/目标/响应变化、迟到响应、重复点击、撤销保护及反馈去重。
- 修复异步提交边界：首次保存并运行在提交结束前不切换编辑器；预检、保存、运行全程禁止重复提交；冻结临时环境和通知选项；慢预检不会改为执行后来选中的步骤。页面卸载后不继续确认或提交运行。

自动检查：

| 检查 | 本轮结果 |
|---|---|
| 接口引擎、API、AI、迁移及设置/定时预检/报告回归 | 101 项通过 |
| 接口工具、调试、AI、编辑器及调度/报告/设置前端测试 | 51 项通过 |
| 前端生产构建 | 通过；保留原有 jmuxer `stream` 浏览器兼容提示 |
| 新增接口模块 Ruff F/E9 与 diff 空白检查 | 通过 |

后端整组选定测试中，本地 socket 测试最初受沙箱禁止绑定端口影响；获准仅绑定 `127.0.0.1` 后单独重跑通过，其余 100 项直接通过。所有模拟 AI、接口和通知请求均在隔离验收程序内处理，数据库位于系统临时目录。未调用真实模型、真实业务接口、设备或飞书；尚未评估真实模型建议质量。

验收截图：[1440×900 场景与引用校验](images/api-testing-low-code-1440.jpg)、[1280×720 紧凑布局](images/api-testing-low-code-1280.jpg)、[AI 失败解释](images/api-testing-ai-failure-1280.jpg)。

复现命令：

```bash
.venv/bin/python -m unittest backend.tests.test_api_testing_values backend.tests.test_api_testing_engine backend.tests.test_api_testing_api backend.tests.test_api_testing_ai backend.tests.test_database_migrations backend.tests.test_settings_api backend.tests.test_scheduled_task_precheck_filter backend.tests.test_report_display backend.tests.test_reports_filters -v
cd frontend
node --test tests/apiTesting.test.mjs tests/apiDebug.test.mjs tests/apiAi.test.mjs tests/apiEditors.test.mjs tests/scheduledTaskPresentation.test.mjs tests/reportListUiContracts.test.mjs tests/settingsUiContracts.test.mjs
npm run build
```

### 先前验收记录

从响应新增断言的布局修复（2026-10-02）：

- 复现对象断言的期望值过高，导致字段与条件垂直居中后落出配置区的问题；同时确认嵌套期望值会向右溢出。
- 字段与条件改为顶部对齐，对象和数组的期望值独占下一行；窄栏自动换行，深层编辑控件不再被固定列宽挤压。新增后滚动到断言开头并聚焦字段，完整路径可换行展示。
- 隔离浏览器在 1280×720、1440×900 验证接口页和场景页，从响应新增文本、深层数字、对象及数组断言；字段与条件均在可见区域内，期望值和嵌套控件无横向溢出。
- 10 项前端接口自动化测试、生产构建及 diff 空白检查通过；仅修改编辑器布局与定位，不改动请求执行、断言规则或保存数据结构。

低代码编辑流程重整（本轮）：

- 场景页改为双栏，接口库移入支持搜索和批量选择的抽屉；元信息、环境变量和定时配置提供独立抽屉。菜单改为「自动化场景」。断言采用紧凑行，调试按钮固定，响应区可调高度。
- 引用字段显示短名称、真实示例与来源，保留标准响应入口；保存重开仍可点选历史字段。引用标签支持重选和跳转，样例或保存结构绝不作为执行输入。修复字段结构序列化将对象错误显示为 `null` 示例的问题。
- 浏览器完成登录→创建订单→查询订单；从状态字段新增业务断言，改错期望值后仅校验响应得到失败，再改正得到通过。后端测试验证 HTTP 调用次数不增加、失败或待校验的前序阻止继续调试、修改请求使下游执行缓存失效。
- 改名保留结果和稳定引用；批量 token 鉴权对已有配置逐项保留或替换，三步重新执行通过。错误拖动排序可定位到具体鉴权字段；未保存离开有提示。预检忽略未启用参数和未使用的鉴权/请求体，未完成后续步骤不再阻止前面步骤调试。
- 浏览器双窗口验证并发保存冲突：旧版本“保存并运行”停留在草稿并显示冲突，不启动运行。正式运行三步通过；失败报告显示期望值和实际值，失败断言优先，可返回对应步骤。后续改名未改变旧报告中的冻结名称和配置版本。
- 列表显示接口地址、场景步骤数及最近运行；定时抽屉在临时数据库创建每日任务，显示下次运行时间，暂停后显示无下次运行。通知配置状态不暴露 Webhook。
- 接口编辑器实测 cURL JSON 导入预览，不显示 `kind/literal/fields`；导入后切换到请求体。从响应的 `user_id` 创建断言保持数字 7，重新校验通过且不重发请求。
- 1280×720 与 1440×900 下页面无横向溢出，保存/运行/调试按钮全部在可见视口内；1440×900 下三条断言同时可见。保存了场景与失败报告验收截图。
- 49 项接口自动化及迁移测试、45 项现有 UI 场景/定时/报告相关回归、17 项前端测试全部通过；生产构建、Ruff F/E9 和 diff 空白检查通过。构建仍有原有 jmuxer `stream` 浏览器兼容提示。
- 验收仅使用隔离预览与测试数据库，未访问真实业务系统、设备或飞书群。本轮不加入飞书附件、循环、分支或自定义脚本。

明文展示调整：

- 按团队要求移除接口自动化的自动脱敏及敏感响应字段设置；Cookie、AT/token、鉴权头、已标记敏感的环境变量实际值和旧敏感路径字段按实际值展示。旧记录中的掩码不自动还原。
- 31 项后端测试、6 项前端接口测试、生产构建、Ruff F/E9 及 diff 空白检查通过。
- 模拟请求验证登录 Cookie 保持、后续 Bearer 鉴权、跨步骤引用、明文响应样例、调试结果、报告持久化及 HTML 导出；HTML 特殊字符仍转义。通知仍仅发送摘要，不包含请求响应内容，未发送真实飞书消息。
- 下方各轮验收中的“脱敏”描述为当时行为，已由本轮明文展示要求替代。

场景断言引用补充修复：

- 前端接口自动化测试 6 项及生产构建通过。
- 隔离浏览器执行三步场景后新增断言，响应展示及字段树保留，可选取 `body → data → items → [0] → count`（该字段不在保存样例中）。
- 修改前序步骤名称后，下游断言引用仍显示完整前序嵌套字段，引用标签同步新名称，失效响应标注“上次调试，仅供选字段”；当前及后续步骤不显示旧的通过状态。历史结果只用于展示和选取路径，不作为执行输入。

第二轮编辑体验修复：

- 31 项接口自动化后端测试、10 项前端接口/设置测试和前端构建通过。
- 复现当前代理环境下 HTTPX 初始化的 `Invalid port: ':1'`。使用真实 HTTPX 构造器和模拟传输验证测试消息、接口报告、UI 报告及探索报告四类通知均不受错误代理变量影响；未发送外部通知。
- 浏览器完成发送登录请求→添加断言→点选 `body → data → user_id`→修改请求地址，原响应和深层字段保留，显示历史结果提示。重新发送后历史提示消失，新增断言实际值 7、结果通过。
- 接口编辑器将请求方法、地址、变量按钮及断言行重新对齐；接口管理列表移除归属列。
- 粘贴未保存样例后，可直接展开 `body → data → orders → [0] → a.b → count`。1440px 双栏和 1024px 单栏布局完成检查，1024px 页面及两个面板均无横向溢出。

上一轮验证：

- 后端接口自动化及迁移测试 40 项通过；新增覆盖跨用户编辑的创建人/修改人、旧表幂等升级、接口 Webhook 独立投递且不回退到 UI 通知地址。
- 前端接口自动化、调度展示、报告中心测试 12 项通过；覆盖 JSON 类型往返、特殊字符字段名以及拒绝把运行时引用转成静态 JSON。
- 生产构建、Ruff F/E9 和 diff 空白检查通过。
- 隔离浏览器验证了 JSON 编辑、格式化、保存后重载，无效 JSON 阻止保存和调试，接口编辑只显示环境变量选择器，通知页面独立保存并重载两类 Webhook。
- 场景中的前序步骤字段树及已有 token 引用正常；保存后执行登录→创建订单→查询订单，报告三步全部通过。
- 目标白名单及 IP 范围限制已按团队要求移除；本地真实 HTTP 测试无需测试专用放行补丁。

## 首期验证记录（优化前）

| 检查 | 结果 |
|---|---|
| 新增后端测试 | 30 项通过 |
| 前端接口自动化、调度展示、报告中心关联测试 | 11 项通过 |
| 前端生产构建 | 通过；保留原有 jmuxer 的 stream 浏览器兼容提示 |
| 新增 Python 模块 Ruff F/E9 检查 | 通过 |
| Git diff 空白检查 | 通过 |
| 后端全量回归 | 1284 项中 1282 项通过，2 项旧日期相关测试失败 |
| 前端全量回归 | 89 项中 88 项通过，1 项旧巡检文案契约测试失败 |

浏览器在隔离环境中实际完成：

- 左侧菜单「UI 自动化」下方出现「接口自动化」，包含接口管理、场景编排。
- 粘贴 cURL，查看解析预览，确认导入；JSON 对象与布尔值正确生成编辑控件，接口保存后可重新加载。
- 登录单步调试成功；token 与 Cookie 脱敏展示。
- 从字段选择器点选 `body → data → token`，下游创建订单单步调试成功，无需重跑登录。
- 保存并正式执行登录→创建订单→查询订单，三步全部通过；查询断言实际值与上游订单 ID 均为数字 42。
- 报告中心接口页签显示运行记录；报告详情展示步骤、请求、响应、引用和断言实际/期望值。
- 从场景进入定时任务表单，预选接口自动化及场景，无设备选择项。
- 桌面 1440px 宽度下报告页面没有页面级横向溢出。

自动测试另外覆盖：真实本地 HTTP 连接、超时/超大响应、取消、失败后跳过、Cookie 快照和调试失效、并发版本冲突、依赖删除保护、报告 HTML 转义/脱敏/下载、定时重叠抑制、启动失败报告、保留清理及重启恢复。旧白名单相关用例随功能移除而删除。

飞书卡片通过模拟发送验证了报告链接、摘要内容、成功、未配置和发送失败状态。真实飞书联调尚未执行；需要在部署环境配置 Webhook 和系统访问地址后验证投递与链接可达性。

## 先前记录的旧测试问题

1. `test_flaky_analysis.FlakyScoringTests.test_endpoint_returns_pydantic_model`：样本日期固定为 2026-07-10 附近，端点使用当前日期的 30 天窗口，9 月运行时样本已过期。
2. `test_retention_service.RetentionCleanupTests.test_run_retention_cleanup_respects_setting`：测试“近期”记录固定在 2026-07-09，实际清理按当前日期运行，因此清理数量由 1 变为 2。
3. `inspectionReportUiContracts.test.mjs` 的首项：期待旧文案“运行范围”，现有未修改的巡检页面展示“核心旅程 / 应用面覆盖 / 残留未覆盖”。

在先前功能验收中，前两项仅在测试进程中临时固定时钟后通过，确认是时间依赖；此次提交前已将时钟固定写入测试，业务逻辑保持不变。第三项为既有前端巡检文案契约问题，不属于本轮选定回归范围。

## 上线前配置

详见 [接口自动化使用指南](API_TESTING.md)。无需配置目标白名单；使用飞书时配置独立的接口自动化 Webhook 与共用系统访问地址。升级前备份数据库，构建前端并重启常规后端，新迁移补充修改人字段。不要部署隔离预览应用，也不要为当前进程内调试/执行服务开启多个后端 worker。
