"""Validated image-click metadata; no device or image dependencies."""
from __future__ import annotations

import math
import re
from typing import Any, Dict, Optional


def _numbers(value: Any, length: int, name: str) -> list:
    if not isinstance(value, (list, tuple)) or len(value) != length:
        raise ValueError(f"{name} 需要 {length} 个数值")
    if any(isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) for v in value):
        raise ValueError(f"{name} 必须为有限数值")
    return [float(v) for v in value]


def normalize_image_options(config: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    if config is None:
        return {}
    if not isinstance(config, dict):
        raise ValueError("image_options 平台配置必须是对象")
    if not config:
        return {}
    allowed = {"template_sha256", "tap_anchor", "source_size", "search_region"}
    if set(config) - allowed:
        raise ValueError("image_options 包含不支持的配置")
    digest = config.get("template_sha256")
    if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-fA-F]{64}", digest):
        raise ValueError("image_options 需要绑定模板的 template_sha256，请重新截取模板")
    result: Dict[str, Any] = {"template_sha256": digest.lower()}
    anchor = _numbers(config.get("tap_anchor", [0.5, 0.5]), 2, "tap_anchor")
    if any(v < 0 or v > 1 for v in anchor):
        raise ValueError("tap_anchor 必须在模板的 0..1 范围内")
    result["tap_anchor"] = anchor
    if "source_size" in config:
        size = _numbers(config["source_size"], 2, "source_size")
        if any(v <= 0 or int(v) != v for v in size):
            raise ValueError("source_size 必须为正整数截图尺寸")
        result["source_size"] = [int(v) for v in size]
    if "search_region" in config:
        region = _numbers(config["search_region"], 4, "search_region")
        x1, y1, x2, y2 = region
        if not (0 <= x1 < x2 <= 1 and 0 <= y1 < y2 <= 1):
            raise ValueError("search_region 必须是 0..1 范围内的有效矩形")
        result["search_region"] = region
    return result


def select_image_options(options: Any, platform: str) -> Dict[str, Any]:
    if options is None:
        return {}
    if not isinstance(options, dict) or set(options) - {"android", "ios", "default"}:
        raise ValueError("image_options 必须按 android/ios/default 配置")
    if any(not isinstance(value, dict) or not value for value in options.values()):
        raise ValueError("image_options 平台条目必须是非空配置对象")
    normalized = {key: normalize_image_options(value) for key, value in options.items()}
    if not normalized:
        return {}
    if platform in normalized:
        return normalized[platform]
    if "default" in normalized:
        return normalized["default"]
    raise ValueError(f"image_options 缺少 {platform} 平台配置，请确认该平台模板和点击位置")
