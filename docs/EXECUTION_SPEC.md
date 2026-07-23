# 执行规范（Android/iOS 录制与执行）

## 1. 边界定义

- 录制：Android 支持 Scrcpy 实时投屏和静态截图录制；iOS 支持 WDA MJPEG 只读预览，并基于静态截图、层级解析、坐标交互完成步骤录制。
- 执行：Android 与 iOS 统一走标准步骤模型。
- 兼容：`case.steps`（legacy）保留，用于灰度期间兼容；执行优先读取 `TestCaseStep` 标准步骤表。
- 巡检：模型化智能巡检只支持 Android。巡检动作由页面模型生成并冻结为路径，不直接写入 `TestCaseStep`；稳定路径进入兼容性回放前会再次做定位质量与安全边界校验。

## 2. 标准步骤模型

每个步骤包含以下核心字段：

- `order`: 步骤顺序。
- `action`: 统一动作名（小写）。
- `args`: 动作参数对象。
- `execute_on`: 允许平台列表（`["android","ios"]`）。
- `platform_overrides`: 平台覆盖配置（定位器与平台专属参数）。
- `timeout`: 超时秒数。
- `error_strategy`: 容错策略（`ABORT/CONTINUE/IGNORE`）。
- `retry_count`: 失败自动重试次数（`0-3`，默认 `0` 不重试）。
- `description`: 步骤描述。

### 2.1 失败自动重试（retry_count）

用于降低 UI 不稳定（页面加载慢、瞬时渲染抖动）导致的误报，建议仅对不稳定步骤开启。语义如下：

1. 步骤失败（`FAIL`）且 `retry_count > 0` 时自动重试，最多再试 `retry_count` 次（总尝试 = `1 + retry_count`）。
2. 每次重试前有约 1s 的短退避；退避期间用户中止（abort）会立即停止重试并走中止路径（`error="执行已被用户中止"`）。
3. `SKIP`（平台不允许，`P1001`）不重试；断言类失败（`assert_text/assert_image` 等）同样会重试——页面加载慢正是重试的目标场景。
4. 重试后成功 → 最终 `status=PASS`；重试耗尽仍失败 → 原失败语义不变，`error/error_code/suggestion/artifacts` 取最后一次尝试的结果。
5. 结果 dict 新增 `attempts` 字段记录总尝试次数（`1` 表示无重试，纯增量字段）；`attempts > 1` 时会并入 `report_display`，供报告与 Flaky 统计使用。
6. `error_strategy` 只在最终结果上生效：重试期间的中间失败不触发 `ABORT/CONTINUE/IGNORE` 判断。

校验：`retry_count` 必须为非负整数且不超过 `3`，非法值预检失败（`P1006_INVALID_ARGS`）；legacy JSON 转换链路对非法值宽松回退（收敛到 `0-3`）。

## 3. 平台覆盖与兼容规则

- `execute_on` 不包含当前平台：步骤状态为 `SKIP`。
- 定位器解析优先级：
  - 优先使用 `platform_overrides.{platform}`；
  - 再回退公共 `selector + selector_type`；
  - iOS 会兼容部分 Android `text/description` 定位候选。
- 严格要求定位器的动作只有 `click`、`wait_until_exists`：
  - 缺少 `selector/by` 时预检失败，错误码 `P1003_SELECTOR_MISSING`。
- `input` 的定位器为可选：
  - 有定位器时走元素输入；
  - 无定位器时走当前焦点输入（`input_focused`）。
- `assert_text` 为页面级文本断言，不要求定位器。
- `click_image` / `assert_image` / `extract_by_ocr` 会优先读取 `args.image_path` / `args.region`，并兼容从 `selector`、平台 override 回退。
- `start_app/stop_app` 建议使用 `args.app_key`：
  - Android 未配置映射时兼容直接使用 package；
  - iOS 若 `app_key` 本身是 Bundle ID 形式，也允许直接透传。

## 4. 当前支持的步骤

以下矩阵以执行引擎当前实现为准：`backend/drivers/cross_platform_runner.py` + `backend/cross_platform_execution.py`。

| 动作 | Android | iOS | 是否要求定位器 | 关键参数 | 说明 |
|---|---|---|---|---|---|
| `click` | 支持 | 支持 | 是 | `selector` + `selector_type` 或 `platform_overrides.{platform}` | 通过定位器点击元素 |
| `input` | 支持 | 支持 | 否 | `args.text` 或 `value` | 无定位器时输入到当前焦点控件 |
| `wait_until_exists` | 支持 | 支持 | 是 | `selector` + `selector_type`，可配 `timeout` | 等待元素出现 |
| `assert_text` | 支持 | 支持 | 否 | `args.expected_text` 或 `value`，可配 `args.match_mode` | 页面级文本断言 |
| `assert_image` | 支持 | 支持 | 否 | `args.image_path` 或 `selector`，可配 `args.match_mode` | 图像存在/不存在断言 |
| `click_image` | 支持 | 支持 | 否 | `args.image_path` 或 `selector` | 按模板图像点击 |
| `extract_by_ocr` | 支持 | 支持 | 否 | `args.region` 或 `selector`，可配 `args.extract_rule`、`args.output_var` | OCR 提取并可导出运行时变量 |
| `sleep` | 支持 | 支持 | 否 | `args.seconds` 或 `value` | 强制等待 |
| `swipe` | 支持 | 支持 | 否 | `args.direction` 或兼容 `selector/value` | 方向仅支持 `up/down/left/right` |
| `back` | 支持 | 支持 | 否 | 无 | 返回上一层 |
| `home` | 支持 | 支持 | 否 | 无 | 回到系统主页 |
| `start_app` | 支持 | 支持 | 否 | `args.app_key`（推荐）/ `args.app_id` | 启动应用 |
| `stop_app` | 支持 | 支持 | 否 | `args.app_key`（推荐）/ `args.app_id` | 停止应用 |

补充说明：

- 当前 Case 步骤编辑器默认下拉主要展示 `click / input / wait_until_exists / assert_text / assert_image / sleep / swipe / extract_by_ocr / click_image`。
- `start_app / stop_app / back / home` 当前更多通过通用步骤面板、场景编辑器或标准步骤/API 写入。

## 5. 动作参数规范

- `click`
  - 必须能解析出 `selector + by`。
- `input`
  - 必填 `args.text` 或 `value`。
  - 有定位器时对元素输入；无定位器时对当前焦点输入。
- `wait_until_exists`
  - 必须能解析出 `selector + by`。
  - `timeout` 为步骤级超时秒数，默认 `10`。
- `assert_text`
  - 必填 `args.expected_text` 或 `value`。
  - `args.match_mode` 支持 `contains` / `not_contains`，默认 `contains`。
- `assert_image`
  - 必填 `args.image_path` 或可回退出的 `selector`。
  - `args.match_mode` 支持 `exists` / `not_exists`，默认 `exists`。
- `click_image`
  - 必填 `args.image_path` 或可回退出的 `selector`。
- `extract_by_ocr`
  - 必填 `args.region` 或可回退出的 `selector`。
  - 区域格式为 `[x1, y1, x2, y2]`，支持绝对像素，也支持 `0-1` 相对坐标。
  - `args.extract_rule` 支持：
    - `extract_rule=regex` + `custom_regex`
    - `extract_rule=boundary` + `left_bound/right_bound`
    - 预置 `preset_type=number_only/price/alphanumeric/chinese`
  - `args.output_var` 可将 OCR 结果写入运行时变量，供后续步骤引用。
- `sleep`
  - `args.seconds` 或 `value` 为等待秒数，要求 `>= 0`。
- `swipe`
  - `args.direction` 支持 `up/down/left/right`。
- `start_app/stop_app`
  - 推荐使用 `args.app_key`。
  - Android 会解析为 package，iOS 会解析为 bundleId。

## 6. 错误码规范

### 6.1 预检/执行通用码（P1xxx）

- `P1001_PLATFORM_NOT_ALLOWED`: 当前平台不允许执行该步骤。
- `P1002_ACTION_NOT_SUPPORTED`: 当前平台不支持该动作。
- `P1003_SELECTOR_MISSING`: 平台覆盖定位器缺失。
- `P1004_APP_MAPPING_MISSING`: `app_key` 映射缺失。
- `P1005_WDA_UNAVAILABLE`: iOS WDA 不可用。
- `P1006_INVALID_ARGS`: 步骤参数结构非法。

### 6.2 场景级阻断码

- `S1001_SCENARIO_PRECHECK_FAILED`: 场景在选定设备上全部预检失败。

### 6.3 平台边界码

- `P2002_ADB_ANDROID_ONLY`: APK 安装、ADB 等 Android 专属能力被 iOS 调用。
- `P3001_FASTBOT_ANDROID_ONLY`: Fastbot 仅支持 Android。
- `P3002_WDA_IOS_ONLY`: WDA 健康检查仅支持 iOS。

### 6.4 执行期错误码（E2xxx）

执行期失败的步骤结果附带结构化字段（纯增量，原 `error` 字符串格式保持不变）：

- `error_code`: 错误码（E2xxx，或透传的 P1xxx 预检码）。
- `error_context`: 失败上下文 dict（至少含 `action`、`platform`、`device_id`，按动作附带 `selector/by`、`image_path`、`region`、`timeout` 等）。
- `suggestion`: 中文修复建议（默认建议集中维护于 `backend/execution_errors.py`）。

驱动层在语义明确的失败点抛出 `ExecutionStepError`（保持原异常消息与类型兼容）；
未结构化的异常由 `classify_exception` 按异常类型（uiautomator2 / facebook-wda / requests / 内置连接与超时）与消息关键词兜底归类。

| 错误码 | 含义 | 修复建议 |
| --- | --- | --- |
| `E2001_ELEMENT_NOT_FOUND` | 元素未找到 | 检查定位器是否正确、页面是否已加载；可在录制模式重新抓取该元素，或配置备选定位器。 |
| `E2002_WAIT_TIMEOUT` | 元素定位等待超时 | 确认页面跳转是否完成，适当增大步骤 timeout，或在该步骤前增加等待。 |
| `E2003_ASSERT_TEXT_FAILED` | 文本断言失败 | 核对期望文本与页面实际文案是否一致；页面加载较慢时在断言前增加等待。 |
| `E2004_ASSERT_IMAGE_FAILED` | 图像断言失败 | 确认模板图与当前页面一致；分辨率、主题或文案变化会影响匹配，必要时重新截取模板图。 |
| `E2005_IMAGE_NOT_MATCHED` | 图像模板未匹配 | 确认目标已出现在屏幕上，或重新截取更清晰、范围更小的模板图。 |
| `E2006_OCR_NO_RESULT` | OCR 未提取到文本 | 检查识别区域坐标与提取规则是否正确、区域内是否有清晰文本；可扩大区域或增加等待。 |
| `E2007_INPUT_FAILED` | 输入失败 | 确认目标为可输入控件且已获得焦点；可先点击输入框再输入，或检查键盘是否弹出。 |
| `E2008_APP_CONTROL_FAILED` | 应用启动/停止失败 | 检查 app_key 映射的包名/BundleID 是否正确、应用是否已安装在该设备上。 |
| `E2009_CLICK_NO_EFFECT` | 点击无效果（点击已执行但页面未变化） | 检查元素是否可点击或被遮挡，必要时改用坐标点击/图像点击。 |
| `E2101_DEVICE_CONNECTION_LOST` | Android 设备连接丢失 | 检查 USB/网络连接与 adb devices 状态，必要时重新插拔设备或重新初始化 uiautomator2 服务。 |
| `E2102_WDA_SESSION_ERROR` | iOS WDA 会话异常 | 确认 WebDriverAgent 正在运行且端口转发正常，必要时重启 WDA 后重试。 |
| `E2201_INVALID_ARGS` | 执行期参数非法（预检已拦截的沿用 `P1006`） | 检查步骤的 args/value/selector 配置是否符合执行规范。 |
| `E2999_EXECUTION_ERROR` | 未知执行异常（兜底） | 结合错误信息与后端日志排查；与设备状态相关时可重试或重启设备。 |

补充说明：

- 用户主动中止（`error="执行已被用户中止"`）不属于执行失败，不附带错误码与建议。
- 步骤 `SKIP`（平台不匹配）会透传 `P1001_PLATFORM_NOT_ALLOWED` 作为 `error_code`。

## 7. 结果状态语义

- 步骤级：`PASS/SKIP/WARNING/FAIL`。
- 用例级：`PASS/WARNING/FAIL/ABORTED`（全 `SKIP` 归类为 `WARNING`，人工终止归类为 `ABORTED`）。
- 场景级：`PASS/WARNING/FAIL/ABORTED`（全 `SKIP` 归类为 `WARNING`，人工终止归类为 `ABORTED`）。
- 调度/长任务级还可能出现 `PENDING/QUEUED/RUNNING/ERROR`；`QUEUED` 表示等待执行名额或设备租约，不应当作步骤失败。

## 8. 设备独占与中止

- 用例和场景通过执行限流器排队；巡检与兼容性等长任务同时持有内存限流租约和数据库 owner 租约。
- DB 租约字段记录 `lease_task_id`、`lease_kind` 和获取时间。释放时只有同一 owner 可以把设备恢复为 `IDLE`，避免旧任务结束时误解锁新任务。
- 用户中止会设置对应的 `abort_event`；执行器应在步骤、退避、排队和长循环边界检查该事件。
- 不要通过直接修改数据库状态解决 `BUSY`。应先使用业务取消接口；确认没有活跃 owner 后再使用设备解锁入口。

## 9. 推荐执行顺序

1. 读取标准步骤（无则 fallback 到 legacy JSON）。
2. 变量渲染（环境变量 + 用例变量）。
3. 按设备平台执行预检（动作/参数/定位器/WDA/app_key）。
4. 获取执行名额和设备 owner 租约，进入 `RUNNING`。
5. 按 `retry_count` 与 `error_strategy` 执行并汇总报告。
6. 在 `finally` 中按 owner 释放租约并恢复设备状态。

巡检生成动作、冻结路径和已安装版本回放的协议不属于标准 Case 步骤契约，详见 [巡检、回放与证据资产指南](INSPECTION_REPLAY_ASSETS.md)。
