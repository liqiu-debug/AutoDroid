"""
UI 元素分析工具模块

核心功能：
- 解析 Android / iOS UI 层级 XML
- 根据点击坐标找到最佳匹配元素
- 生成元素定位策略 (text / description / xpath)
"""
import re
import logging
import xml.etree.ElementTree as ET
from typing import Any, Dict, Iterable, List, Optional, Tuple

logger = logging.getLogger(__name__)

_DYNAMIC_TIME_RANGE_PATTERN = re.compile(
    r"^[\[\(]?\s*\d{1,2}:\d{2}\s*[,/\-~]\s*\d{1,2}:\d{2}\s*[\]\)]?$"
)


def _truncate_preview(text: str, max_len: int = 120) -> str:
    raw = str(text or "")
    if len(raw) <= max_len:
        return raw
    return f"{raw[: max_len - 3]}..."


def _build_match_preview(text: str, expected_text: str, radius: int = 24, max_len: int = 120) -> str:
    haystack = str(text or "")
    needle = str(expected_text or "")
    if not haystack:
        return ""
    if not needle:
        return _truncate_preview(haystack, max_len=max_len)

    start = haystack.find(needle)
    if start < 0:
        return _truncate_preview(haystack, max_len=max_len)

    end = start + len(needle)
    snippet_start = max(start - radius, 0)
    snippet_end = min(end + radius, len(haystack))
    snippet = haystack[snippet_start:snippet_end]

    if len(snippet) > max_len:
        room = max(max_len - len(needle) - 6, 0)
        left_room = room // 2
        right_room = room - left_room
        snippet_start = max(start - left_room, 0)
        snippet_end = min(end + right_room, len(haystack))
        snippet = haystack[snippet_start:snippet_end]

    prefix = "..." if snippet_start > 0 else ""
    suffix = "..." if snippet_end < len(haystack) else ""
    return f"{prefix}{snippet}{suffix}"


def evaluate_page_text_assertion(
    candidates: Iterable[Any],
    expected_text: str,
) -> Dict[str, Any]:
    """
    评估页面级文本断言。

    规则：
    1. 先尝试单节点命中，兼容现有行为。
    2. 再按页面顺序拼接整页文本，支持跨节点命中。
    """
    normalized_candidates: List[str] = []
    for item in candidates or []:
        value = str(item or "").strip()
        if value:
            normalized_candidates.append(value)

    expected = str(expected_text or "")
    matched_candidates = [item for item in normalized_candidates if expected and expected in item]
    if matched_candidates:
        return {
            "matched": True,
            "preview": matched_candidates[:5],
            "match_source": "candidate",
            "candidates": normalized_candidates,
        }

    aggregate_sources = []
    joined_text = "".join(normalized_candidates)
    if joined_text:
        aggregate_sources.append(("page_joined", joined_text))

    joined_with_newline = "\n".join(normalized_candidates)
    if joined_with_newline and joined_with_newline != joined_text:
        aggregate_sources.append(("page_lines", joined_with_newline))

    joined_with_space = " ".join(normalized_candidates)
    if joined_with_space and joined_with_space not in {joined_text, joined_with_newline}:
        aggregate_sources.append(("page_spaced", joined_with_space))

    for source_name, aggregate_text in aggregate_sources:
        if expected and expected in aggregate_text:
            return {
                "matched": True,
                "preview": [_build_match_preview(aggregate_text, expected)],
                "match_source": source_name,
                "candidates": normalized_candidates,
            }

    return {
        "matched": False,
        "preview": normalized_candidates[:5],
        "match_source": "",
        "candidates": normalized_candidates,
    }


def parse_bounds(bounds_str: str) -> Optional[Tuple[int, int, int, int]]:
    """
    解析 Android UI 元素的 bounds 字符串。
    
    Args:
        bounds_str: 格式为 '[x1,y1][x2,y2]' 的边界字符串
        
    Returns:
        (x1, y1, x2, y2) 坐标元组，解析失败返回 None
    """
    if not bounds_str:
        return None
    match = re.search(r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]", bounds_str)
    if match:
        return tuple(map(int, match.groups()))
    return None


def _coerce_int(value: Any) -> Optional[int]:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return int(round(float(text)))
    except Exception:
        return None


def _parse_ios_bounds(attributes: Dict[str, Any]) -> Optional[Tuple[int, int, int, int]]:
    """
    解析 iOS source 常见的几何属性。

    优先级：
    1. x/y + width/height
    2. left/top + right/bottom
    3. x/y + right/bottom
    """
    left = _coerce_int(attributes.get("left"))
    top = _coerce_int(attributes.get("top"))
    right = _coerce_int(attributes.get("right"))
    bottom = _coerce_int(attributes.get("bottom"))

    if None not in (left, top, right, bottom):
        return (left, top, right, bottom)

    x = _coerce_int(attributes.get("x"))
    y = _coerce_int(attributes.get("y"))
    width = _coerce_int(attributes.get("width"))
    height = _coerce_int(attributes.get("height"))

    if None not in (x, y, width, height):
        return (x, y, x + max(width, 0), y + max(height, 0))

    if None not in (x, y, right, bottom):
        return (x, y, right, bottom)

    return None


def parse_node_bounds(attributes: Dict[str, Any]) -> Optional[Tuple[int, int, int, int]]:
    if not isinstance(attributes, dict):
        return None
    bounds = parse_bounds(str(attributes.get("bounds") or ""))
    if bounds:
        return bounds
    return _parse_ios_bounds(attributes)


def _scale_bounds(
    bounds: Tuple[int, int, int, int],
    coordinate_scale: float,
) -> Tuple[int, int, int, int]:
    safe_scale = float(coordinate_scale or 1.0)
    if safe_scale <= 0 or safe_scale == 1.0:
        return bounds
    x1, y1, x2, y2 = bounds
    return (
        int(round(x1 * safe_scale)),
        int(round(y1 * safe_scale)),
        int(round(x2 * safe_scale)),
        int(round(y2 * safe_scale)),
    )


def _extract_node_locator_fields(node: ET.Element, bounds: Tuple[int, int, int, int]) -> Dict[str, str]:
    attributes = node.attrib if isinstance(node.attrib, dict) else {}
    text = (
        attributes.get("text")
        or attributes.get("label")
        or attributes.get("value")
        or ""
    )
    description = (
        attributes.get("content-desc")
        or attributes.get("name")
        or ""
    )
    class_name = (
        attributes.get("class")
        or attributes.get("type")
        or str(getattr(node, "tag", "") or "")
    )
    resource_id = (
        attributes.get("resource-id")
        or attributes.get("resourceId")
        or attributes.get("id")
        or ""
    )
    x1, y1, x2, y2 = bounds
    return {
        "resourceId": str(resource_id or "").strip(),
        "text": _normalize_locator_text(text),
        "description": _normalize_locator_text(description),
        "className": str(class_name or "").strip(),
        "bounds": f"[{x1},{y1}][{x2},{y2}]",
    }


def _normalize_locator_text(raw: str) -> str:
    return str(raw or "").strip()


def _is_unstable_locator_text(raw: str) -> bool:
    """
    动态文案（如进度/时间区间）不适合作为录制定位值。
    例如: [00:00, 03:55] / 00:00-03:55
    """
    text = _normalize_locator_text(raw)
    if not text:
        return False
    return bool(_DYNAMIC_TIME_RANGE_PATTERN.match(text))


def calculate_element_from_coordinates(
    hierarchy_xml: str,
    target_x: int,
    target_y: int,
    coordinate_scale: float = 1.0,
) -> Dict:
    """
    根据点击坐标在 UI 层级中找到最佳匹配元素，并生成定位策略。
    
    算法流程:
    1. 遍历 XML 层级树，找到所有包含该坐标的元素
    2. 按优先级排序：有 text > 有 desc > 有 resourceId > 无属性
       动态时间区间文案（如 [00:00, 03:55]）会被降级为无属性
    3. 优先选择叶子节点、小面积节点
    4. 无可用属性时走 xpath 兜底（图像模板匹配已改为用户手动框选截取）
    
    Args:
        hierarchy_xml: Android dump_hierarchy / iOS source 返回的 XML 字符串
        target_x: 点击的 X 坐标
        target_y: 点击的 Y 坐标
        
    Returns:
        {"selector": str, "strategy": str, "element": dict}
        或 {"error": str}
    """
    try:
        if isinstance(hierarchy_xml, str):
            if "<?xml" in hierarchy_xml:
                hierarchy_xml = re.sub(r"<\?xml.*?\?>", "", hierarchy_xml)
            root = ET.fromstring(hierarchy_xml)
    except ET.ParseError:
        return {"error": "无效的 XML"}

    # ---- 第一步：遍历收集所有候选节点 ----
    candidate_nodes = []

    def traverse(node):
        bounds = parse_node_bounds(node.attrib)
        matches = False

        if bounds:
            scaled_bounds = _scale_bounds(bounds, coordinate_scale)
            x1, y1, x2, y2 = scaled_bounds
            if x1 <= target_x <= x2 and y1 <= target_y <= y2:
                matches = True
                candidate_nodes.append({
                    "node": node,
                    "bounds": scaled_bounds,
                    "area": (x2 - x1) * (y2 - y1),
                    "is_leaf": len(node) == 0,
                    "class": (
                        node.attrib.get("class")
                        or node.attrib.get("type")
                        or str(getattr(node, "tag", "") or "")
                    ),
                    "res_id": (
                        node.attrib.get("resource-id")
                        or node.attrib.get("resourceId")
                        or node.attrib.get("id")
                        or ""
                    ),
                    "text": (
                        node.attrib.get("text")
                        or node.attrib.get("label")
                        or node.attrib.get("value")
                        or ""
                    ),
                    "desc": (
                        node.attrib.get("content-desc")
                        or node.attrib.get("name")
                        or ""
                    )
                })
        else:
            matches = True  # 无 bounds 的根节点，继续遍历子节点

        if matches:
            for child in node:
                traverse(child)

    traverse(root)

    if not candidate_nodes:
        return {"error": "在该坐标未找到任何元素"}

    # ---- 第二步：排序选出最佳匹配 ----
    def score_node(item):
        """
        节点评分规则（值越小优先级越高）：
        - 维度1: 属性优先级 (text=0, desc=1, resourceId=2, 无属性=3)
        - 维度2: 节点类型 (非容器=0, 容器=1)
        - 维度3: 叶子优先 (叶子=0, 非叶子=1)
        - 维度4: 面积 (越小越优先)
        """
        node_class = item["class"]
        is_layout = any(kw in node_class for kw in ["Layout", "ViewGroup", "ScrollView"]) or node_class == "View"
        text = _normalize_locator_text(item["text"])
        desc = _normalize_locator_text(item["desc"])
        if _is_unstable_locator_text(text):
            text = ""
        if _is_unstable_locator_text(desc):
            desc = ""

        if text:
            score_attr = 0
        elif desc:
            score_attr = 1
        elif item["res_id"]:
            score_attr = 2
        else:
            score_attr = 3

        return (score_attr, 1 if is_layout else 0, 0 if item["is_leaf"] else 1, item["area"])

    candidate_nodes.sort(key=score_node)
    best_match = candidate_nodes[0]
    node = best_match["node"]

    # ---- 第三步：生成定位策略 ----
    info = _extract_node_locator_fields(node, best_match["bounds"])
    if _is_unstable_locator_text(info["text"]):
        info["text"] = ""
    if _is_unstable_locator_text(info["description"]):
        info["description"] = ""

    # 优先级: text > description > xpath兜底
    # 注: 图像模板匹配已改为用户手动框选截取，不再自动裁切
    if info["text"]:
        return {"selector": info["text"], "strategy": "text", "element": info}

    if info["description"]:
        return {"selector": info["description"], "strategy": "description", "element": info}

    # 无可用属性 → xpath 兜底
    return {
        "selector": f"//{info['className']}",
        "strategy": "xpath",
        "element": info
    }
