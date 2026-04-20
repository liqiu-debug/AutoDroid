"""
自然语言转测试步骤 (NL2Script) API

提供：
- 接收用户的自然语言描述
- 调用大模型生成标准 JSON 格式的测试步骤
- 返回可用的步骤列表
"""
import uuid
import json
import logging
from typing import Any, Dict, List, Optional
from fastapi import APIRouter, Depends, HTTPException
from sqlmodel import Session
from pydantic import BaseModel

from backend.schemas import ActionType, SelectorType, ErrorStrategy
from backend.database import get_session
from backend.api.log_analysis import _get_setting
from backend.openai_compat import parse_chat_completion_payload
from backend.step_contract import (
    SELECTOR_TYPE_TO_BY,
    normalize_action,
    normalize_error_strategy,
    normalize_execute_on,
    normalize_platform_overrides,
)
import httpx

logger = logging.getLogger(__name__)

router = APIRouter()

AI_DEFAULT_EXECUTE_ON = ["android", "ios"]
AI_LOGICAL_LOCATOR_ACTIONS = {"click", "input", "wait_until_exists"}
AI_ALL_CROSS_PLATFORM_ACTIONS = [
    "click",
    "input",
    "wait_until_exists",
    "assert_text",
    "assert_image",
    "swipe",
    "sleep",
    "start_app",
    "stop_app",
    "back",
    "home",
    "click_image",
    "extract_by_ocr",
]
SELECTOR_TYPE_TO_BY_LOWER = {k.lower(): v for k, v in SELECTOR_TYPE_TO_BY.items()}


# ==================== 请求/响应模型 ====================

class GenerateStepsRequest(BaseModel):
    """生成步骤的请求模型"""
    text: str  # 用户的自然语言描述


class GenerateStepsResponse(BaseModel):
    """生成步骤的响应模型"""
    success: bool
    data: List[dict]  # 生成的步骤列表
    message: Optional[str] = None  # 可选的错误消息


# ==================== 工具函数 ====================

def _build_system_prompt() -> str:
    """
    构造 System Prompt，指导 LLM 生成合法的 JSON 步骤。

    当前生成策略为“单份跨端标准步骤”：
    - 用户只配置一份步骤（偏 Android 可录制语义）
    - 执行时由后端进行 Android/iOS 映射
    """
    available_actions = [e.value for e in ActionType]
    available_selectors = [e.value for e in SelectorType]
    error_strategies = [e.value for e in ErrorStrategy]

    return f"""你是一位专业的跨端 UI 自动化测试脚本生成专家。
你的任务是生成“单份标准步骤”，用于 Android 与 iOS 共用执行。

请根据用户的自然语言描述，生成一组合法的 JSON 格式测试步骤。

## 可用的动作类型 (action):
{json.dumps(available_actions, ensure_ascii=False)}

## 可用的定位方式 (`platform_overrides.*.by` / legacy `selector_type`):
{json.dumps(available_selectors, ensure_ascii=False)}

## 可用的容错策略 (error_strategy):
{json.dumps(error_strategies, ensure_ascii=False)}

## 输出格式要求:
必须返回一个合法的 JSON 数组。优先输出“标准步骤结构”，每个步骤对象包含以下字段：
- action: 动作类型 (必填)
- args: 动作参数对象 (必填，若无参数则填 `{{}}`)
- value: 兼容字段，可为空字符串或 null
- execute_on: 默认 `['android','ios']`
- platform_overrides: 平台覆盖定位信息，优先只填 `android`
- description: 步骤描述建议
- timeout: 超时时间 (默认 10)
- error_strategy: 容错策略 (默认 "ABORT")

标准步骤示例：
{{
  "action": "click",
  "args": {{}},
  "value": null,
  "execute_on": ["android", "ios"],
  "platform_overrides": {{
    "android": {{ "selector": "登录", "by": "text" }}
  }},
  "timeout": 10,
  "error_strategy": "ABORT",
  "description": "点击登录按钮"
}}

兼容要求：如果你一时无法完整表达标准结构，也允许输出 legacy 字段 `selector/selector_type/value`，后端会自动转换；但最终目标仍是优先输出标准结构。

## 跨端统一规则 (必须遵守):
1. 只生成一份步骤，不要输出 `android_step` / `ios_step` 这种平台分裂数组。
2. 标准结构里允许存在 `platform_overrides`，但通常只填 `platform_overrides.android`，`ios` 留空即可。
3. 定位优先使用可跨端映射的语义定位：
   - 优先 "text"
   - 其次 "description"
   - 非用户明确要求时，不要优先使用 "resourceId" 或 "xpath"。
4. 对于 text/description 语义定位，不需要手填 iOS 的 label/name；运行时会自动映射。
5. 不要在 selector 中填设备序列号、UDID、平台标签等非定位信息。
6. output 必须是纯 JSON 数组，不要附加解释文字。

## 动作契约 (必须遵守):

1. **点击动作 (action: "click")**
   - `args`: `{{}}`
   - `platform_overrides.android.selector`: 元素定位符，如 "登录按钮"
   - `platform_overrides.android.by`: 默认 "text"
   - 示例: {{"action": "click", "args": {{}}, "platform_overrides": {{"android": {{"selector": "登录按钮", "by": "text"}}}}, "execute_on": ["android", "ios"], "value": null, "error_strategy": "ABORT"}}

2. **输入动作 (action: "input")**
   - `args.text`: (必填) 要输入的文本
   - 可选 `platform_overrides.android` 作为输入框定位
   - 当用户语义明确是“当前已聚焦输入框输入”时，可省略定位
   - 示例: {{"action": "input", "args": {{"text": "test@example.com"}}, "platform_overrides": {{"android": {{"selector": "用户名输入框", "by": "text"}}}}, "execute_on": ["android", "ios"], "value": "test@example.com", "error_strategy": "ABORT"}}

3. **文本断言 (action: "assert_text")**
   - `args.expected_text`: (必填) 期望文本
   - `args.match_mode`: 可选，`contains` / `not_contains`，默认 `contains`
   - 这是“页面全局文本断言”，不要为它生成 `selector` / `selector_type` / `platform_overrides`
   - 当用户表达“页面包含/出现某文案”时，用 `contains`
   - 当用户表达“页面不包含/不出现某文案”时，用 `not_contains`
   - 示例: {{"action": "assert_text", "args": {{"expected_text": "登录成功", "match_mode": "contains"}}, "execute_on": ["android", "ios"], "value": "登录成功", "error_strategy": "ABORT"}}

4. **滑动动作 (action: "swipe")**
   - `args.direction`: 必须是 "up", "down", "left", "right" 之一
   - 示例: {{"action": "swipe", "args": {{"direction": "up"}}, "execute_on": ["android", "ios"], "value": null, "error_strategy": "ABORT"}}

5. **启动应用 (action: "start_app")**
   - `args.app_key`: (必填) 应用业务键，如 `mall_app`
   - 不要直接输出 Android package / iOS bundleId 字段名
   - 示例: {{"action": "start_app", "args": {{"app_key": "mall_app"}}, "execute_on": ["android", "ios"], "value": null, "error_strategy": "ABORT"}}

6. **停止应用 (action: "stop_app")**
   - `args.app_key`: (必填) 应用业务键，如 `mall_app`
   - 示例: {{"action": "stop_app", "args": {{"app_key": "mall_app"}}, "execute_on": ["android", "ios"], "value": null, "error_strategy": "ABORT"}}

7. **返回动作 (action: "back")**
   - `args`: `{{}}`
   - 示例: {{"action": "back", "args": {{}}, "execute_on": ["android", "ios"], "value": null, "error_strategy": "ABORT"}}

8. **主页动作 (action: "home")**
   - `args`: `{{}}`
   - 示例: {{"action": "home", "args": {{}}, "execute_on": ["android", "ios"], "value": null, "error_strategy": "ABORT"}}

9. **等待元素 (action: "wait_until_exists")**
   - `args`: `{{}}`
   - `platform_overrides.android`: 要等待的元素定位
   - 示例: {{"action": "wait_until_exists", "args": {{}}, "platform_overrides": {{"android": {{"selector": "加载完成提示", "by": "text"}}}}, "execute_on": ["android", "ios"], "value": null, "error_strategy": "ABORT"}}

10. **强制等待 (action: "sleep")**
    - 当用户要求"强制等待"、"停留"、"休眠"多少秒时使用
    - `args.seconds`: (必填) 等待秒数，数值型或数字字符串均可
    - 示例: {{"action": "sleep", "args": {{"seconds": 5}}, "execute_on": ["android", "ios"], "value": "5", "error_strategy": "ABORT", "description": "强制等待 5 秒"}}

11. **图像点击 / 图像断言 / OCR 提取**
    - 只有当用户明确给出模板图路径、OCR 区域或类似信息时才生成 `click_image` / `assert_image` / `extract_by_ocr`
    - `click_image.args.image_path`: 模板图路径
    - `assert_image.args.image_path`: 模板图路径，`args.match_mode` 只能是 `exists` / `not_exists`
    - 当用户表达“图片存在/出现”时，用 `exists`
    - 当用户表达“图片不存在/不出现”时，用 `not_exists`
    - `extract_by_ocr.args.region`: 区域字符串；如需导出变量，使用 `args.output_var`

12. **容错策略 (error_strategy)**
    - 默认值: "ABORT"
    - 仅当用户明确表达"失败也继续"、"忽略错误"、"继续执行"时，才使用 "CONTINUE" 或 "IGNORE"

## 注意事项:
1. 只输出 JSON 数组，不要包含其他文字说明
2. 确保每个步骤的必填字段都正确填写
3. 如果使用 legacy 字段，selector_type 默认使用 "text"，除非用户明确指定其他定位方式；但 `assert_text` 不应生成定位字段
4. 为每个步骤添加简洁、可读的 description 描述
5. 返回的 JSON 必须可直接通过 json.loads() 解析
"""


def _coerce_string(value: Any) -> Optional[str]:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _coerce_timeout(value: Any, default: int = 10) -> int:
    try:
        timeout = int(value)
        return timeout if timeout > 0 else default
    except Exception:
        return default


def _coerce_seconds(value: Any, default: float = 1.0) -> float:
    try:
        seconds = float(value)
        return seconds if seconds >= 0 else default
    except Exception:
        return default


def _format_seconds_value(seconds: float) -> str:
    if float(seconds).is_integer():
        return str(int(seconds))
    return str(seconds)


def _normalize_locator_by(raw_by: Any, action: str) -> Optional[str]:
    if raw_by is None:
        if action in {"click_image", "assert_image"}:
            return "image"
        if action in AI_LOGICAL_LOCATOR_ACTIONS:
            return "text"
        if action == "extract_by_ocr":
            return "text"
        return None

    text = str(raw_by).strip()
    if not text:
        return _normalize_locator_by(None, action)

    mapped = SELECTOR_TYPE_TO_BY.get(text) or SELECTOR_TYPE_TO_BY_LOWER.get(text.lower())
    if mapped:
        return str(mapped).lower()

    normalized = text.lower()
    alias_map = {
        "desc": "description",
        "content-desc": "description",
        "resourceid": "id",
        "resource_id": "id",
    }
    return alias_map.get(normalized, normalized)


def _infer_android_override(step: Dict[str, Any], action: str, args: Dict[str, Any]) -> Optional[Dict[str, str]]:
    selector = _coerce_string(step.get("selector"))
    if action in {"click_image", "assert_image"} and not selector:
        selector = _coerce_string(args.get("image_path") or args.get("path") or step.get("value"))
    if action == "extract_by_ocr" and not selector:
        selector = _coerce_string(args.get("region"))

    by = _normalize_locator_by(step.get("selector_type"), action)
    if selector and by:
        return {"selector": selector, "by": by}
    return None


def _normalize_extract_rule(raw_rule: Any) -> Dict[str, Any]:
    if raw_rule is None:
        return {}
    if isinstance(raw_rule, dict):
        return dict(raw_rule)
    text = _coerce_string(raw_rule)
    return {"extract_rule": text} if text else {}


def _normalize_assert_text_match_mode(raw_mode: Any) -> str:
    text = str(raw_mode or "").strip().lower()
    if text in {"not_contains", "not-contains", "not contains", "exclude", "不包含", "不含", "不应包含"}:
        return "not_contains"
    return "contains"


def _normalize_assert_image_match_mode(raw_mode: Any) -> str:
    text = str(raw_mode or "").strip().lower()
    if text in {"not_exists", "not-exists", "not exists", "missing", "不存在", "不应存在"}:
        return "not_exists"
    return "exists"


def _build_default_ai_description(action: str, args: Dict[str, Any], value: Any) -> str:
    if action == "click":
        return "点击元素"
    if action == "input":
        return "输入文本"
    if action == "wait_until_exists":
        return "等待元素出现"
    if action == "assert_text":
        expected = _coerce_string(args.get("expected_text") or value)
        prefix = "文本断言不包含" if args.get("match_mode") == "not_contains" else "文本断言包含"
        return f"{prefix} {expected}" if expected else prefix
    if action == "assert_image":
        return "图像断言不存在" if args.get("match_mode") == "not_exists" else "图像断言存在"
    if action == "swipe":
        direction = str(args.get("direction") or "up").lower()
        label = {"up": "上", "down": "下", "left": "左", "right": "右"}.get(direction, direction)
        return f"向{label}滑动"
    if action == "sleep":
        return f"强制等待 {_format_seconds_value(_coerce_seconds(args.get('seconds'), default=1.0))} 秒"
    if action == "click_image":
        return "图像点击"
    if action == "extract_by_ocr":
        return "OCR 提取变量"
    if action == "start_app":
        return "启动应用"
    if action == "stop_app":
        return "停止应用"
    if action == "back":
        return "返回"
    if action == "home":
        return "回到主页"
    return f"AI 生成: {action}"


def _normalize_generated_step(step: Dict[str, Any], order: int) -> Dict[str, Any]:
    action = normalize_action(step.get("action"))
    raw_args = step.get("args") if isinstance(step.get("args"), dict) else {}
    raw_options = step.get("options") if isinstance(step.get("options"), dict) else {}
    args = dict(raw_args)
    value = step.get("value")

    platform_overrides = normalize_platform_overrides(step.get("platform_overrides"))
    inferred_android = _infer_android_override(step, action, args)
    if inferred_android and "android" not in platform_overrides:
        platform_overrides["android"] = inferred_android

    execute_on = (
        normalize_execute_on(step.get("execute_on"))
        if step.get("execute_on") is not None
        else list(AI_DEFAULT_EXECUTE_ON)
    )

    if action == "input":
        text = _coerce_string(args.get("text"))
        if text is None:
            text = "" if value is None else str(value)
        args = {"text": text}
        value = text
    elif action == "assert_text":
        expected = _coerce_string(args.get("expected_text"))
        if expected is None:
            expected = "" if value is None else str(value)
        match_mode = _normalize_assert_text_match_mode(
            args.get("match_mode")
            or step.get("match_mode")
            or raw_options.get("match_mode")
        )
        args = {"expected_text": expected, "match_mode": match_mode}
        value = expected
        platform_overrides = {}
    elif action == "swipe":
        direction = _coerce_string(args.get("direction")) or _coerce_string(step.get("selector")) or _coerce_string(value) or "up"
        args = {"direction": direction.lower()}
        value = None
    elif action == "sleep":
        seconds = _coerce_seconds(args.get("seconds", value), default=1.0)
        args = {"seconds": seconds}
        value = _format_seconds_value(seconds)
    elif action in ("start_app", "stop_app"):
        app_key = _coerce_string(args.get("app_key")) or _coerce_string(step.get("selector")) or _coerce_string(value)
        if app_key is None:
            raise ValueError(f"{action} requires args.app_key")
        args = {"app_key": app_key}
        value = None
    elif action == "click_image":
        image_path = _coerce_string(args.get("image_path")) or _coerce_string(args.get("path")) or _coerce_string(step.get("selector")) or _coerce_string(value)
        if image_path is None:
            raise ValueError("click_image requires args.image_path")
        args = {"image_path": image_path}
        value = None
        platform_overrides.setdefault("android", {"selector": image_path, "by": "image"})
    elif action == "assert_image":
        image_path = _coerce_string(args.get("image_path")) or _coerce_string(args.get("path")) or _coerce_string(step.get("selector")) or _coerce_string(value)
        if image_path is None:
            raise ValueError("assert_image requires args.image_path")
        match_mode = _normalize_assert_image_match_mode(args.get("match_mode") or step.get("match_mode"))
        args = {"image_path": image_path, "match_mode": match_mode}
        value = None
        platform_overrides.setdefault("android", {"selector": image_path, "by": "image"})
    elif action == "extract_by_ocr":
        region = _coerce_string(args.get("region")) or _coerce_string(step.get("selector"))
        if region is None:
            raise ValueError("extract_by_ocr requires args.region")
        normalized_args: Dict[str, Any] = {"region": region}
        extract_rule = _normalize_extract_rule(args.get("extract_rule") or step.get("options"))
        if extract_rule:
            normalized_args["extract_rule"] = extract_rule
        output_var = _coerce_string(args.get("output_var")) or _coerce_string(value)
        if output_var:
            normalized_args["output_var"] = output_var
            value = output_var
        else:
            value = ""
        args = normalized_args
        platform_overrides.setdefault("android", {"selector": region, "by": "text"})
    else:
        args = args if isinstance(args, dict) else {}
        value = _coerce_string(value) if value is not None else None

    normalized_step = {
        "uuid": _coerce_string(step.get("uuid")) or str(uuid.uuid4()),
        "order": order,
        "action": action,
        "args": args,
        "value": value,
        "execute_on": execute_on,
        "platform_overrides": platform_overrides,
        "timeout": _coerce_timeout(step.get("timeout"), default=10),
        "error_strategy": normalize_error_strategy(step.get("error_strategy", "ABORT")),
        "description": _coerce_string(step.get("description")) or _build_default_ai_description(action, args, value),
    }
    return normalized_step


def _normalize_generated_steps(steps_list: List[Any]) -> List[Dict[str, Any]]:
    normalized_steps: List[Dict[str, Any]] = []
    for index, step in enumerate(steps_list or [], start=1):
        if not isinstance(step, dict):
            logger.warning("跳过非字典步骤: %r", step)
            continue
        try:
            normalized_steps.append(_normalize_generated_step(step, index))
        except Exception as exc:
            logger.warning("跳过无效 AI 步骤: index=%s error=%s raw=%r", index, exc, step)
    return normalized_steps


async def _call_llm_service(text: str, session: Session) -> str:
    """
    调用 LLM API 生成测试步骤

    从数据库 SystemSetting 读取配置：
    - ai_api_key: API Key
    - ai_api_base: API Base URL
    - ai_model: 模型名
    """
    api_key = _get_setting(session, "ai_api_key", "")
    api_base = _get_setting(session, "ai_api_base", "https://api.openai.com/v1")
    model = _get_setting(session, "ai_model", "gpt-3.5-turbo")

    if not api_key:
        logger.warning("未配置 AI API Key，使用兜底模板步骤")
        return None

    # 处理 API Base URL
    api_base = api_base.rstrip('/')
    if api_base.endswith('/chat/completions'):
        api_base = api_base[:-len('/chat/completions')]

    request_url = f"{api_base}/chat/completions"

    system_prompt = _build_system_prompt()

    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": f"请根据以下描述生成测试步骤：\n\n{text}"},
        ],
        "temperature": 0.3,
        "max_tokens": 3000,
        "stream": False,
    }

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }

    logger.info(f"LLM 请求: {request_url}, 模型: {model}")

    try:
        async with httpx.AsyncClient(timeout=60) as client:
            resp = await client.post(
                request_url,
                json=payload,
                headers=headers,
            )

            raw_text = resp.text
            logger.info(f"LLM 响应状态: {resp.status_code}, 长度: {len(raw_text)}")

            if resp.status_code != 200:
                logger.error(f"LLM API 返回非200: {resp.status_code} - {raw_text[:500]}")
                return None

            if not raw_text or not raw_text.strip():
                logger.error("LLM API 返回空响应")
                return None

            try:
                data = parse_chat_completion_payload(raw_text)
            except Exception as json_err:
                logger.error(f"JSON 解析失败: {json_err}")
                return None

            if "choices" not in data or not data["choices"]:
                logger.error("LLM 返回缺少 choices 字段")
                return None

            content = data["choices"][0]["message"]["content"]
            return content

    except httpx.TimeoutException:
        logger.error("LLM API 调用超时")
        return None
    except httpx.ConnectError as e:
        logger.error(f"LLM API 连接失败: {e}")
        return None
    except Exception as e:
        logger.error(f"LLM API 调用异常: {e}")
        return None


def _clean_llm_response(content: str) -> str:
    """
    清理 LLM 返回的内容，去除 Markdown 格式标记

    处理情况：
    1. ```json ... ```
    2. ``` ... ```
    3. 纯净的 JSON 字符串
    """
    content = content.strip()

    # 去除 Markdown 代码块标记
    if content.startswith("```"):
        content = content[3:]
        # 去除语言标识
        if content.startswith("json"):
            content = content[4:]
        elif content.startswith("Python"):
            content = content[6:]
        # 去除末尾的 ```
        if content.rstrip().endswith("```"):
            content = content.rstrip()[:-3]

    return content.strip()


def _generate_mock_steps(text: str) -> List[dict]:
    """
    生成兜底模板步骤。

    当 AI 配置缺失或服务不可用时，根据关键词生成可编辑的示意步骤，
    保障前端流程仍可继续。
    """
    mock_steps: List[Dict[str, Any]] = []

    # 如果用户提到登录
    if "登录" in text:
        mock_steps.extend([
            {
                "uuid": str(uuid.uuid4()),
                "action": "click",
                "selector": "用户名输入框",
                "selector_type": "text",
                "value": None,
                "description": "点击用户名输入框",
                "timeout": 10,
                "error_strategy": "ABORT"
            },
            {
                "uuid": str(uuid.uuid4()),
                "action": "input",
                "selector": "用户名输入框",
                "selector_type": "text",
                "value": "test@example.com",
                "description": "输入用户名",
                "timeout": 10,
                "error_strategy": "ABORT"
            },
            {
                "uuid": str(uuid.uuid4()),
                "action": "click",
                "selector": "密码输入框",
                "selector_type": "text",
                "value": None,
                "description": "点击密码输入框",
                "timeout": 10,
                "error_strategy": "ABORT"
            },
            {
                "uuid": str(uuid.uuid4()),
                "action": "input",
                "selector": "密码输入框",
                "selector_type": "text",
                "value": "password123",
                "description": "输入密码",
                "timeout": 10,
                "error_strategy": "ABORT"
            },
            {
                "uuid": str(uuid.uuid4()),
                "action": "click",
                "selector": "登录按钮",
                "selector_type": "text",
                "value": None,
                "description": "点击登录按钮",
                "timeout": 10,
                "error_strategy": "ABORT"
            }
        ])

    # 如果用户提到滑动
    elif "滑动" in text or "下拉" in text or "上拉" in text:
        direction = "down" if "下拉" in text else "up"
        mock_steps.append({
            "uuid": str(uuid.uuid4()),
            "action": "swipe",
            "selector": direction,
            "selector_type": None,
            "value": None,
            "description": f"向{'下' if direction == 'down' else '上'}滑动列表",
            "timeout": 10,
            "error_strategy": "ABORT"
        })

    # 如果用户提到等待/休眠（优先级高，需要放在前面）
    elif "等待" in text or "休眠" in text or "停留" in text or "秒" in text:
        # 提取秒数（默认 5 秒）
        seconds = 5
        import re
        match = re.search(r'(\d+)\s*秒', text)
        if match:
            seconds = int(match.group(1))
            seconds = max(1, min(120, seconds))  # 限制在 1-120 秒

        mock_steps.append({
            "uuid": str(uuid.uuid4()),
            "action": "sleep",
            "selector": None,
            "selector_type": None,
            "value": str(seconds),
            "description": f"强制等待 {seconds} 秒",
            "timeout": 10,
            "error_strategy": "ABORT"
        })

    # 如果用户提到启动应用
    elif "启动" in text or "打开" in text:
        mock_steps.extend([
            {
                "uuid": str(uuid.uuid4()),
                "action": "start_app",
                "selector": "com.ehaier.zgq.shop.mall",
                "selector_type": None,
                "value": None,
                "description": "启动应用",
                "timeout": 10,
                "error_strategy": "ABORT"
            },
            {
                "uuid": str(uuid.uuid4()),
                "action": "wait_until_exists",
                "selector": "首页",
                "selector_type": "text",
                "value": None,
                "description": "等待首页加载完成",
                "timeout": 10,
                "error_strategy": "ABORT"
            }
        ])

    # 默认：生成一个简单的点击步骤
    if not mock_steps:
        mock_steps.append({
            "uuid": str(uuid.uuid4()),
            "action": "click",
            "selector": "目标元素",
            "selector_type": "text",
            "value": None,
            "description": f"执行: {text}",
            "timeout": 10,
            "error_strategy": "ABORT"
        })

    return mock_steps


# ==================== API 路由 ====================

@router.post("/generate-steps", response_model=GenerateStepsResponse)
async def generate_steps(
    req: GenerateStepsRequest,
    session: Session = Depends(get_session),
):
    """
    自然语言转测试步骤接口

    流程:
    1. 接收用户的自然语言描述
    2. 调用 LLM 生成 JSON 格式的步骤列表
    3. 清理和解析 LLM 返回的内容
    4. 为每个步骤生成唯一 UUID
    5. 返回标准格式的步骤列表
    """
    logger.info(f"收到 NL2Script 请求: {req.text}")

    # session 来自 FastAPI Depends，必定可用
    llm_response = await _call_llm_service(req.text, session)

    # LLM 调用失败时回退到模板步骤
    if llm_response is None:
        logger.info("使用兜底模板步骤生成结果")
        mock_steps = _normalize_generated_steps(_generate_mock_steps(req.text))
        return GenerateStepsResponse(
            success=True,
            data=mock_steps,
            message="(使用兜底模板步骤 - 请配置 AI API Key 以启用大模型)"
        )

    # 清理 LLM 返回的内容
    cleaned_content = _clean_llm_response(llm_response)

    # 尝试解析 JSON
    try:
        steps_list = json.loads(cleaned_content)

        # 确保是数组
        if not isinstance(steps_list, list):
            logger.error(f"LLM 返回的不是数组: {type(steps_list)}")
            # 尝试包装成数组
            if isinstance(steps_list, dict):
                steps_list = [steps_list]
            else:
                raise ValueError("Invalid format")

        validated_steps = _normalize_generated_steps(steps_list)

        if not validated_steps:
            raise ValueError("未生成有效步骤")

        logger.info(f"成功生成 {len(validated_steps)} 个步骤")

        return GenerateStepsResponse(
            success=True,
            data=validated_steps
        )

    except json.JSONDecodeError as e:
        logger.error(f"JSON 解析失败: {e}, 原始内容: {cleaned_content[:500]}")
        return GenerateStepsResponse(
            success=False,
            data=[],
            message=f"LLM 返回的格式不符合 JSON 标准: {str(e)}"
        )
    except Exception as e:
        logger.error(f"处理步骤失败: {e}")
        return GenerateStepsResponse(
            success=False,
            data=[],
            message=f"生成步骤时出错: {str(e)}"
        )
