import re
import logging
from typing import Any, Dict

logger = logging.getLogger(__name__)

_PATTERN = re.compile(r"{{\s*([A-Z0-9_]+)\s*}}")


def format_variable_placeholder(key: str) -> str:
    """Return the canonical variable placeholder format: {{KEY}}."""
    return "{{" + str(key or "").strip() + "}}"


def normalize_variable_placeholders(value: Any) -> Any:
    """Normalize variable placeholders from {{ KEY }} to {{KEY}} recursively."""
    if isinstance(value, str):
        return _PATTERN.sub(lambda match: format_variable_placeholder(match.group(1)), value)
    if isinstance(value, list):
        return [normalize_variable_placeholders(item) for item in value]
    if isinstance(value, tuple):
        return tuple(normalize_variable_placeholders(item) for item in value)
    if isinstance(value, dict):
        return {key: item if key == "image_match" else normalize_variable_placeholders(item) for key, item in value.items()}
    return value


def render_step_data(text: str, variables_dict: Dict[str, str]) -> str:
    """将文本中的 {{KEY}} 占位符替换为 variables_dict 中的真实值。

    兼容历史 {{ KEY }} 写法；未命中的占位符保留原字符串并打印 warning 日志。
    """
    if not text:
        return text

    def _replacer(match: re.Match) -> str:
        key = match.group(1)
        if key in variables_dict:
            return variables_dict[key]
        logger.warning(f"变量 '{key}' 未在当前变量字典中找到，保留原始占位符 {match.group(0)}")
        return match.group(0)

    return _PATTERN.sub(_replacer, text)
