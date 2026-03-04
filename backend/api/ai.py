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
from typing import List, Optional
from fastapi import APIRouter, Depends, HTTPException
from sqlmodel import Session
from pydantic import BaseModel

from backend.schemas import ActionType, SelectorType, ErrorStrategy, Step
from backend.models import SystemSetting
from backend.database import get_session
from backend.api.log_analysis import _get_setting
import httpx

logger = logging.getLogger(__name__)

router = APIRouter()


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

    包含以下硬性业务契约：
    1. input/assert_text: value 必须填入目标文本
    2. swipe: selector 必须是方向，selector_type/value 为 null
    3. start_app/stop_app: selector 是包名，selector_type/value 为 null
    4. back/home: 全部为 null
    5. error_strategy: 默认 ABORT
    6. selector_type: 默认 text
    """
    available_actions = [e.value for e in ActionType if e.name != 'CLICK_IMAGE']
    available_selectors = [e.value for e in SelectorType if e.name != 'IMAGE']
    error_strategies = [e.value for e in ErrorStrategy]

    return f"""你是一位专业的 Android UI 自动化测试脚本生成专家。

请根据用户的自然语言描述，生成一组合法的 JSON 格式测试步骤。

## 可用的动作类型 (action):
{json.dumps(available_actions, ensure_ascii=False)}

## 可用的定位方式 (selector_type):
{json.dumps(available_selectors, ensure_ascii=False)}

## 可用的容错策略 (error_strategy):
{json.dumps(error_strategies, ensure_ascii=False)}

## 输出格式要求:
必须返回一个合法的 JSON 数组，每个元素为一个步骤对象，包含以下字段：
- uuid: (可选) 步骤唯一标识
- action: 动作类型 (必填)
- selector: 元素定位符或特定值 (根据 action 类型而定)
- selector_type: 定位方式 (默认 "text")
- value: 输入值或断言目标 (根据 action 类型而定)
- description: 步骤描述建议
- timeout: 超时时间 (默认 10)
- error_strategy: 容错策略 (默认 "ABORT")

## 硬性业务契约 (必须遵守):

1. **点击动作 (action: "click")**
   - selector: 元素定位符 (如 "登录按钮", "com.example.app:id/btn")
   - selector_type: 定位方式，默认 "text"
   - value: null
   - 示例: {{"action": "click", "selector": "登录按钮", "selector_type": "text", "value": null, "error_strategy": "ABORT"}}

2. **输入动作 (action: "input")**
   - selector: 目标元素定位符
   - selector_type: 定位方式
   - value: (必填) 要输入的文本
   - 示例: {{"action": "input", "selector": "用户名输入框", "selector_type": "text", "value": "test@example.com", "error_strategy": "ABORT"}}

3. **断言文本 (action: "assert_text")**
   - selector: 要检查的元素
   - selector_type: 定位方式
   - value: (必填) 期望包含的文本
   - 示例: {{"action": "assert_text", "selector": "状态提示", "selector_type": "text", "value": "登录成功", "error_strategy": "ABORT"}}

4. **滑动动作 (action: "swipe")**
   - selector: (必填) 滑动方向，必须是 "up", "down", "left", "right" 之一
   - selector_type: null
   - value: null
   - 示例: {{"action": "swipe", "selector": "up", "selector_type": null, "value": null, "error_strategy": "ABORT"}}

5. **启动应用 (action: "start_app")**
   - selector: (必填) 应用包名，如未指定则使用 "com.ehaier.zgq.shop.mall"
   - selector_type: null
   - value: null
   - 示例: {{"action": "start_app", "selector": "com.example.app", "selector_type": null, "value": null, "error_strategy": "ABORT"}}

6. **停止应用 (action: "stop_app")**
   - selector: (必填) 应用包名
   - selector_type: null
   - value: null
   - 示例: {{"action": "stop_app", "selector": "com.example.app", "selector_type": null, "value": null, "error_strategy": "ABORT"}}

7. **返回动作 (action: "back")**
   - selector: null
   - selector_type: null
   - value: null
   - 示例: {{"action": "back", "selector": null, "selector_type": null, "value": null, "error_strategy": "ABORT"}}

8. **主页动作 (action: "home")**
   - selector: null
   - selector_type: null
   - value: null
   - 示例: {{"action": "home", "selector": null, "selector_type": null, "value": null, "error_strategy": "ABORT"}}

9. **等待元素 (action: "wait_until_exists")**
   - selector: 要等待的元素定位符
   - selector_type: 定位方式，默认 "text"
   - value: null
   - 示例: {{"action": "wait_until_exists", "selector": "加载完成提示", "selector_type": "text", "value": null, "error_strategy": "ABORT"}}

10. **等待/睡眠 (action: "sleep")**
    - 当用户要求"等待"、"停留"、"休眠"多少秒时使用
    - value: (必填) 等待秒数，只能是纯数字字符串（如 "5"）
    - selector: null
    - selector_type: null
    - 示例: {{"action": "sleep", "selector": null, "selector_type": null, "value": "5", "error_strategy": "ABORT"}}

11. **容错策略 (error_strategy)**
    - 默认值: "ABORT"
    - 仅当用户明确表达"失败也继续"、"忽略错误"、"继续执行"时，才使用 "CONTINUE" 或 "IGNORE"

## 注意事项:
1. 只输出 JSON 数组，不要包含其他文字说明
2. 确保每个步骤的必填字段都正确填写
3. selector_type 默认使用 "text"，除非用户明确指定其他定位方式
4. 为每个步骤添加合理的 description 描述
5. 返回的 JSON 必须可直接通过 json.loads() 解析
"""


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
        logger.warning("未配置 AI API Key，使用 Mock 数据")
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
    }

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
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
                data = resp.json()
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
    生成 Mock 数据用于前端联调

    根据用户输入的关键词生成简单的示意步骤
    """
    mock_steps = []

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
            "description": f"等待 {seconds} 秒",
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

    # 尝试调用 LLM
    if session:
        llm_response = await _call_llm_service(req.text, session)
    else:
        llm_response = None

    # 如果 LLM 调用失败，使用 Mock 数据
    if llm_response is None:
        logger.info("使用 Mock 数据生成步骤")
        mock_steps = _generate_mock_steps(req.text)
        return GenerateStepsResponse(
            success=True,
            data=mock_steps,
            message="(使用 Mock 数据 - 请配置 AI API Key 以启用大模型)"
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

        # 为每个步骤生成 UUID 并确保字段完整
        validated_steps = []
        for step in steps_list:
            if not isinstance(step, dict):
                logger.warning(f"跳过非字典项: {step}")
                continue

            # 生成或保留 UUID
            if "uuid" not in step or not step["uuid"]:
                step["uuid"] = str(uuid.uuid4())

            # 特殊处理：sleep 动作的 value 必须是字符串
            if step.get("action") == "sleep" and step.get("value") is not None:
                if isinstance(step["value"], (int, float)):
                    step["value"] = str(int(step["value"]))
                elif not isinstance(step["value"], str):
                    step["value"] = str(step["value"])

            # 设置默认值
            step.setdefault("timeout", 10)
            step.setdefault("error_strategy", "ABORT")
            step.setdefault("description", f"AI 生成: {step.get('action', 'unknown')}")

            validated_steps.append(step)

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
