import re
import logging
from typing import Dict

logger = logging.getLogger(__name__)

_PATTERN = re.compile(r"{{\s*([A-Z0-9_]+)\s*}}")


def render_step_data(text: str, variables_dict: Dict[str, str]) -> str:
    """将文本中的 {{ KEY }} 占位符替换为 variables_dict 中的真实值。

    未命中的占位符保留原字符串并打印 warning 日志。
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
