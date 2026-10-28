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
        return json.loads(text)
    except json.JSONDecodeError:
        pass

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
