import json
from typing import Any, Dict, List

# LLM 调用的网络总时长上限（秒）。httpx 的 timeout 是逐次网络读之间的超时，
# 上游慢速滴流（如无视 stream=false 仍返回 SSE）时总耗时无上限；而 Cloudflare
# 等反代约 100s 即掐断回源连接并回错误页，前端只能看到「响应无效/不完整」。
# 所有同步等 LLM 的请求都必须在此之前返回干净的 JSON。
LLM_TOTAL_DEADLINE_S = 80.0


def _flatten_content(content: Any) -> str:
    if isinstance(content, str):
        return content

    if not isinstance(content, list):
        return ""

    parts: List[str] = []
    for item in content:
        if isinstance(item, str):
            parts.append(item)
            continue
        if not isinstance(item, dict):
            continue

        text = item.get("text")
        if isinstance(text, str):
            parts.append(text)

    return "".join(parts)


def _normalize_usage(usage: Any) -> Dict[str, Any]:
    """usage 可能是 null / 缺失 / 非 dict，统一成 dict。

    上游不规范时 `usage: null` 是真实会出现的，调用方直接 `usage.get(...)`
    会 AttributeError，这里先兜平。
    """
    return dict(usage) if isinstance(usage, dict) else {}


def _normalize_completion(data: Dict[str, Any]) -> Dict[str, Any]:
    """把 JSON 分支的 content 归一化成 str，与 SSE 分支同一套语义。

    思考型模型（Qwen 等）在 max_tokens 吃紧时会把正文留在 reasoning 里，
    `content` 给 null 或分片数组；原样透传会让调用方在 pydantic 的 str 字段
    上抛 ValidationError，跑到路由外变成纯文本 500。
    """
    choices = data.get("choices")
    if isinstance(choices, list):
        for choice in choices:
            if not isinstance(choice, dict):
                continue
            message = choice.get("message")
            if not isinstance(message, dict):
                continue
            message["content"] = _flatten_content(message.get("content"))

    data["usage"] = _normalize_usage(data.get("usage"))
    return data


def extract_message_content(data: Any) -> str:
    """取 chat completion 的正文，缺失/非文本一律返回空串。

    各调用方统一走这里，不再自己写 `data["choices"][0]["message"]["content"]`：
    后者在 content 为 null / list 时会把校验异常抛到路由外。
    """
    if not isinstance(data, dict):
        return ""

    choices = data.get("choices")
    if not isinstance(choices, list) or not choices:
        return ""

    first = choices[0]
    if not isinstance(first, dict):
        return ""

    message = first.get("message")
    if not isinstance(message, dict):
        return ""

    return _flatten_content(message.get("content"))


def parse_chat_completion_payload(raw_text: str) -> Dict[str, Any]:
    """
    兼容标准 JSON 与 SSE(data: ...) 形式的 OpenAI chat completion 响应。

    部分第三方 OpenAI 兼容服务会忽略 `stream=false`，仍然返回
    `text/event-stream` 分块。这里将其拼接回标准 completion 结构，
    统一为 `choices[0].message.content`。
    """
    text = (raw_text or "").strip()
    if not text:
        raise ValueError("empty response")

    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        # 不是合法 JSON，再按 SSE 拼接一次
        pass
    else:
        if not isinstance(data, dict):
            # 合法 JSON 但不是 completion 对象（数组/字符串等），明确报格式问题
            raise ValueError("response is not a chat completion object")
        # 纯 JSON 分支：content 可能是 null / list，这里归一化成 str；
        # **不因 content 为空抛异常**——连接探针（max_tokens=4）依赖
        # 「空内容也算连通」。空正文该不该算失败由调用方决定。
        return _normalize_completion(data)

    chunks: List[Dict[str, Any]] = []
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line.startswith("data:"):
            continue

        payload = line[5:].strip()
        if not payload or payload == "[DONE]":
            continue

        try:
            chunk = json.loads(payload)
        except json.JSONDecodeError as exc:
            raise ValueError(f"invalid SSE chunk: {payload[:120]}") from exc

        if isinstance(chunk, dict):
            chunks.append(chunk)

    if not chunks:
        raise ValueError("response is neither JSON nor SSE chat completion")

    delta_parts: List[str] = []
    full_messages: List[str] = []
    usage: Dict[str, Any] = {}
    role = "assistant"

    for chunk in chunks:
        chunk_usage = chunk.get("usage")
        if isinstance(chunk_usage, dict):
            usage.update(chunk_usage)

        choices = chunk.get("choices")
        if not isinstance(choices, list):
            continue

        for choice in choices:
            if not isinstance(choice, dict):
                continue

            message = choice.get("message")
            if isinstance(message, dict):
                role = message.get("role") or role
                content = _flatten_content(message.get("content"))
                if content:
                    full_messages.append(content)

            delta = choice.get("delta")
            if isinstance(delta, dict):
                role = delta.get("role") or role
                content = _flatten_content(delta.get("content"))
                if content:
                    delta_parts.append(content)

    content = "".join(delta_parts).strip()
    if not content and full_messages:
        content = full_messages[-1].strip()

    if not content:
        raise ValueError("SSE response does not contain assistant content")

    return {
        "choices": [
            {
                "message": {
                    "role": role,
                    "content": content,
                }
            }
        ],
        "usage": usage,
    }
