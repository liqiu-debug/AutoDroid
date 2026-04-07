# 执行规范（Android 录制 + Android/iOS 执行）

## 1. 边界定义

- 录制：仅 Android 设备支持（`/device/*` 录制接口为 Android-only）。
- 执行：Android 与 iOS 统一走标准步骤模型。
- 兼容：`case.steps`（legacy）保留，用于灰度期间兼容；执行优先读取 `TestCaseStep` 标准步骤表。

## 2. 标准步骤模型

每个步骤包含以下核心字段：

- `order`: 步骤顺序。
- `action`: 统一动作名（小写）。
- `args`: 动作参数对象。
- `execute_on`: 允许平台列表（`["android","ios"]`）。
- `platform_overrides`: 平台覆盖配置（定位器与平台专属参数）。
- `timeout`: 超时秒数。
- `error_strategy`: 容错策略（`ABORT/CONTINUE/IGNORE`）。
- `description`: 步骤描述。

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

### 6.3 录制/平台边界码

- `P2001_RECORDING_ANDROID_ONLY`: 录制能力仅支持 Android。
- `P2002_ADB_ANDROID_ONLY`: Android-only 录制能力被 iOS 调用。
- `P3001_FASTBOT_ANDROID_ONLY`: Fastbot 仅支持 Android。
- `P3002_WDA_IOS_ONLY`: WDA 健康检查仅支持 iOS。

## 7. 结果状态语义

- 步骤级：`PASS/SKIP/WARNING/FAIL`。
- 用例级：`PASS/WARNING/FAIL`（全 `SKIP` 归类为 `WARNING`）。
- 场景级：`PASS/WARNING/FAIL`（全 `SKIP` 归类为 `WARNING`）。

## 8. 推荐执行顺序

1. 读取标准步骤（无则 fallback 到 legacy JSON）。
2. 变量渲染（环境变量 + 用例变量）。
3. 按设备平台执行预检（动作/参数/定位器/WDA/app_key）。
4. 按 `error_strategy` 执行并汇总报告。
