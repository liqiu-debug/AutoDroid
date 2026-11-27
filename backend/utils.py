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

IOS_APPLICATION_TYPE = "XCUIElementTypeApplication"
_IOS_STRUCTURAL_TYPES = frozenset(
    {
        IOS_APPLICATION_TYPE,
        "XCUIElementTypeWindow",
        "XCUIElementTypeToolbar",
        "XCUIElementTypeNavigationBar",
        "XCUIElementTypeTabBar",
        "XCUIElementTypeScrollView",
        "XCUIElementTypeTable",
        "XCUIElementTypeCollectionView",
        "XCUIElementTypeWebView",
        "XCUIElementTypeStatusBar",
        "XCUIElementTypeMap",
    }
)
_IOS_SCROLL_BAR_PATTERN = re.compile(r"(垂直|水平)滚动条|(vertical|horizontal) scroll bar", re.IGNORECASE)
_IOS_LARGE_CONTAINER_RATIO = 0.72

_ALWAYS_NOISE_IDS = frozenset(
    {
        "android:id/content",
        "android:id/statusBarBackground",
        "android:id/navigationBarBackground",
        "android:id/background",
        "android:id/list",
        "android:id/empty",
    }
)
_ALWAYS_NOISE_ID_SUFFIXES = (
    ":id/action_bar_root",
    ":id/decor_content_parent",
    ":id/action_bar_container",
)
_CONTAINER_NOISE_ID_SUFFIXES = (
    ":id/content",
    ":id/root",
    ":id/container",
    ":id/react_root_view",
    ":id/flutter_view",
)
_AUTO_GENERATED_ID_RE = re.compile(r"(?:^|:id/)(?:0x[0-9a-fA-F]+|\d+)$")
_GENERIC_LAYOUT_SHORT_NAMES = frozenset(
    {
        "View",
        "ViewGroup",
        "FrameLayout",
        "LinearLayout",
        "RelativeLayout",
        "ConstraintLayout",
        "CoordinatorLayout",
        "ScrollView",
        "HorizontalScrollView",
        "NestedScrollView",
        "RecyclerView",
        "ListView",
        "GridView",
        "WebView",
        "DecorView",
        "AppiumAUT",
        "hierarchy",
    }
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
    动态文案（如进度/时间区间）与 Swift 运行时类名不适合作为录制定位值。
    例如: [00:00, 03:55] / 00:00-03:55 / _TtGC7SwiftUI32NavigationStackHosting
    """
    text = _normalize_locator_text(raw)
    if not text:
        return False
    if text.startswith("_Tt"):
        return True
    return bool(_DYNAMIC_TIME_RANGE_PATTERN.match(text))


def _visible_area_ratio(bounds: Tuple[int, int, int, int], screen_bounds) -> float:
    if not screen_bounds:
        return 0.0
    x1, y1, x2, y2 = bounds
    sx1, sy1, sx2, sy2 = screen_bounds
    visible = max(0, min(x2, sx2) - max(x1, sx1)) * max(0, min(y2, sy2) - max(y1, sy1))
    return visible / max((sx2 - sx1) * (sy2 - sy1), 1)


def _is_ios_non_element(node: ET.Element, bounds: Tuple[int, int, int, int], screen_bounds) -> bool:
    node_type = node.attrib.get("type")
    if node_type in _IOS_STRUCTURAL_TYPES:
        return True
    if node_type != "XCUIElementTypeOther":
        return False
    label = _normalize_locator_text(node.attrib.get("label") or node.attrib.get("value") or "")
    name = _normalize_locator_text(node.attrib.get("name") or "")
    if _IOS_SCROLL_BAR_PATTERN.search(f"{label} {name}"):
        return True
    if len(node) == 0:
        return False
    if not label or _is_unstable_locator_text(label):
        return True
    return _visible_area_ratio(bounds, screen_bounds) >= _IOS_LARGE_CONTAINER_RATIO


def _is_generic_layout_class(class_name: str) -> bool:
    cls = str(class_name or "").strip()
    if not cls:
        return True
    if cls in _IOS_STRUCTURAL_TYPES or cls == "XCUIElementTypeOther":
        return True
    short_name = cls.rsplit(".", 1)[-1]
    if short_name in _GENERIC_LAYOUT_SHORT_NAMES:
        return True
    return any(kw in cls for kw in ("Layout", "ViewGroup", "ScrollView"))


def _is_usable_resource_id(raw_id: str, *, is_leaf: bool, area_ratio: float) -> bool:
    rid = str(raw_id or "").strip()
    if not rid:
        return False
    rid_lower = rid.lower()
    if rid in _ALWAYS_NOISE_IDS or rid_lower in _ALWAYS_NOISE_IDS:
        return False
    if rid_lower.endswith(_ALWAYS_NOISE_ID_SUFFIXES):
        return False
    if _AUTO_GENERATED_ID_RE.search(rid):
        return False
    if not is_leaf and area_ratio >= 0.45 and rid_lower.endswith(_CONTAINER_NOISE_ID_SUFFIXES):
        return False
    return True


def _is_global_container_node(item: Dict[str, Any], max_area: float) -> bool:
    safe_max = max(float(max_area or 0), 1.0)
    area_ratio = float(item["area"]) / safe_max
    depth = int(item["depth"])
    is_leaf = bool(item["is_leaf"])
    class_name = str(item["class"] or "")
    child_count = int(item["child_count"])

    if area_ratio >= 0.55 and depth <= 1 and not is_leaf:
        return True
    if area_ratio < 0.72:
        return False

    if class_name in ("AppiumAUT", "hierarchy") or IOS_APPLICATION_TYPE in class_name:
        return True
    if "XCUIElementTypeWindow" in class_name and depth <= 2:
        return True
    if (
        not is_leaf
        and child_count > 0
        and re.search(
            r"(XCUIElementTypeOther|XCUIElementTypeScrollView|XCUIElementTypeCollectionView|XCUIElementTypeTable|XCUIElementTypeWebView|android\.webkit\.WebView|ScrollView|RecyclerView|ListView|CollectionView|TableView|ViewGroup|FrameLayout|CoordinatorLayout|ConstraintLayout|LinearLayout|RelativeLayout)",
            class_name,
        )
    ):
        return True
    if not item["res_id"] and (item["clean_text"] or item["clean_desc"]) and depth <= 3 and not is_leaf:
        return True
    if depth <= 1 and re.search(
        r"(DecorView|FrameLayout|ViewGroup|CoordinatorLayout|ConstraintLayout|LinearLayout|RelativeLayout)",
        class_name,
    ):
        return True
    return False


def _legacy_calculate_element_from_coordinates(
    root: ET.Element,
    target_x: int,
    target_y: int,
    coordinate_scale: float = 1.0,
) -> Dict:
    candidate_nodes = []
    screen_bounds = parse_node_bounds(root.attrib) if root.attrib.get("type") == IOS_APPLICATION_TYPE else None

    def traverse(node):
        bounds = parse_node_bounds(node.attrib)
        matches = False

        if bounds:
            scaled_bounds = _scale_bounds(bounds, coordinate_scale)
            x1, y1, x2, y2 = scaled_bounds
            if x1 <= target_x <= x2 and y1 <= target_y <= y2:
                matches = True
                if not _is_ios_non_element(node, bounds, screen_bounds):
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
            matches = True

        if matches or screen_bounds is not None:
            for child in node:
                traverse(child)

    traverse(root)

    if not candidate_nodes:
        return {"error": "在该坐标未找到任何元素"}

    def score_node(item):
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

    info = _extract_node_locator_fields(node, best_match["bounds"])
    if _is_unstable_locator_text(info["text"]):
        info["text"] = ""
    if _is_unstable_locator_text(info["description"]):
        info["description"] = ""

    if info["text"]:
        return {"selector": info["text"], "strategy": "text", "element": info}

    if info["description"]:
        return {"selector": info["description"], "strategy": "description", "element": info}

    return {
        "selector": f"//{info['className']}",
        "strategy": "xpath",
        "element": info,
    }


def calculate_element_from_coordinates(
    hierarchy_xml: str,
    target_x: int,
    target_y: int,
    coordinate_scale: float = 1.0,
    *,
    enable_resource_id: bool = False,
    enable_class_name: bool = False,
    locator_priority: str = "semantic_first",
) -> Dict:
    """
    根据点击坐标在 UI 层级中找到最佳匹配元素，并生成定位策略。

    - 当 enable_resource_id / enable_class_name 均为 False 时，保持历史行为 (text > description > xpath)。
    - 当任一开关开启时，执行单次全树扫描 + (enter, exit) O(1) 同谱系唯一性校验 + 最小充分条件合成。
    """
    try:
        if isinstance(hierarchy_xml, str):
            if "<?xml" in hierarchy_xml:
                hierarchy_xml = re.sub(r"<\?xml.*?\?>", "", hierarchy_xml)
            root = ET.fromstring(hierarchy_xml)
    except ET.ParseError:
        return {"error": "无效的 XML"}

    if not enable_resource_id and not enable_class_name:
        return _legacy_calculate_element_from_coordinates(
            root,
            target_x,
            target_y,
            coordinate_scale=coordinate_scale,
        )

    from backend.utils.composite_locator import serialize_composite_selector

    screen_bounds = parse_node_bounds(root.attrib) if root.attrib.get("type") == IOS_APPLICATION_TYPE else None
    screen_area = 0.0
    if screen_bounds:
        sx1, sy1, sx2, sy2 = _scale_bounds(screen_bounds, coordinate_scale)
        screen_area = float(max(0, sx2 - sx1) * max(0, sy2 - sy1))

    all_nodes: List[Dict[str, Any]] = []
    seq = 0

    def traverse(node: ET.Element, depth: int = 0) -> None:
        nonlocal seq
        seq += 1
        enter_seq = seq
        attrs = node.attrib if isinstance(node.attrib, dict) else {}
        class_name = str(
            attrs.get("class")
            or attrs.get("type")
            or getattr(node, "tag", "")
            or ""
        ).strip()
        bounds = parse_node_bounds(attrs)
        entry: Optional[Dict[str, Any]] = None

        is_root_shell = (
            class_name in ("hierarchy", "AppiumAUT")
            or IOS_APPLICATION_TYPE in class_name
            or ("XCUIElementTypeWindow" in class_name and depth <= 2)
        )
        if bounds and not is_root_shell:
            scaled_bounds = _scale_bounds(bounds, coordinate_scale)
            x1, y1, x2, y2 = scaled_bounds
            area = max(0, x2 - x1) * max(0, y2 - y1)
            raw_text = _normalize_locator_text(
                attrs.get("text") or attrs.get("label") or attrs.get("value") or ""
            )
            raw_desc = _normalize_locator_text(
                attrs.get("content-desc") or attrs.get("name") or ""
            )
            raw_id = str(
                attrs.get("resource-id") or attrs.get("resourceId") or attrs.get("id") or ""
            ).strip()
            clean_text = "" if _is_unstable_locator_text(raw_text) else raw_text
            clean_desc = "" if _is_unstable_locator_text(raw_desc) else raw_desc

            entry = {
                "idx": len(all_nodes),
                "node": node,
                "bounds": scaled_bounds,
                "raw_bounds": bounds,
                "area": area,
                "depth": depth,
                "child_count": len(node),
                "is_leaf": len(node) == 0,
                "clickable": str(attrs.get("clickable") or "").strip().lower() == "true",
                "class": class_name,
                "res_id": raw_id,
                "raw_text": raw_text,
                "clean_text": clean_text,
                "raw_desc": raw_desc,
                "clean_desc": clean_desc,
                "enter": enter_seq,
                "exit": enter_seq,
                "ios_non_element": _is_ios_non_element(node, bounds, screen_bounds),
            }
            all_nodes.append(entry)

        for child in node:
            traverse(child, depth + 1)

        seq += 1
        if entry is not None:
            entry["exit"] = seq

    traverse(root, 0)

    if not all_nodes:
        return {"error": "在该坐标未找到任何元素"}

    max_area = max(screen_area, max((float(item["area"]) for item in all_nodes), default=0.0), 1.0)
    by_id: Dict[str, List[int]] = {}
    by_text: Dict[str, List[int]] = {}
    by_desc: Dict[str, List[int]] = {}
    by_class: Dict[str, List[int]] = {}
    hit_nodes: List[Dict[str, Any]] = []

    for item in all_nodes:
        item["area_ratio"] = float(item["area"]) / max_area
        item["usable_id"] = (
            item["res_id"]
            if _is_usable_resource_id(item["res_id"], is_leaf=item["is_leaf"], area_ratio=item["area_ratio"])
            else ""
        )
        if item["res_id"]:
            by_id.setdefault(item["res_id"], []).append(item["idx"])
        if item["raw_text"]:
            by_text.setdefault(item["raw_text"], []).append(item["idx"])
        if item["raw_desc"]:
            by_desc.setdefault(item["raw_desc"], []).append(item["idx"])
        if item["class"]:
            by_class.setdefault(item["class"], []).append(item["idx"])

        x1, y1, x2, y2 = item["bounds"]
        if (
            x1 <= target_x <= x2
            and y1 <= target_y <= y2
            and not item["ios_non_element"]
            and not _is_global_container_node(item, max_area)
        ):
            hit_nodes.append(item)

    if not hit_nodes:
        return {"error": "在该坐标未找到任何元素"}

    bucket_map = {
        "id": by_id,
        "text": by_text,
        "description": by_desc,
        "class_name": by_class,
    }

    def is_unique_target(conditions: Dict[str, str], target_item: Dict[str, Any]) -> bool:
        lists: List[List[int]] = []
        for key, value in conditions.items():
            if not value:
                return False
            bucket = bucket_map.get(key)
            if bucket is None:
                return False
            matched = bucket.get(value)
            if not matched:
                return False
            lists.append(matched)
        if not lists:
            return False

        lists.sort(key=len)
        candidates_idx = set(lists[0])
        for extra in lists[1:]:
            candidates_idx.intersection_update(extra)
            if not candidates_idx:
                return False

        t_enter = target_item["enter"]
        t_exit = target_item["exit"]
        if target_item["idx"] not in candidates_idx:
            return False

        for idx in candidates_idx:
            other = all_nodes[idx]
            o_enter = other["enter"]
            o_exit = other["exit"]
            same_lineage = (
                (o_enter <= t_enter and t_exit <= o_exit)
                or (t_enter <= o_enter and o_exit <= t_exit)
            )
            if not same_lineage:
                return False
        return True

    effective_priority = (
        "id_first"
        if (enable_resource_id and str(locator_priority or "").strip().lower() == "id_first")
        else "semantic_first"
    )

    def synthesize_for_node(item: Dict[str, Any]) -> Optional[Tuple[int, str, str]]:
        txt = item["clean_text"]
        desc = item["clean_desc"]
        rid = item["usable_id"] if enable_resource_id else ""
        cls = item["class"] if enable_class_name else ""
        non_layout_cls = cls if (cls and not _is_generic_layout_class(cls)) else ""

        # 无文案的通用布局容器：仅小面积或显式 clickable 的按钮容器才允许参与 ID/Class 合成
        if (
            not txt
            and not desc
            and _is_generic_layout_class(item["class"])
            and not item["is_leaf"]
            and (item["area_ratio"] >= 0.30 or (not item["clickable"] and item["area_ratio"] >= 0.08))
        ):
            return None

        def try_spec(conditions: Dict[str, str], strategy: str, selector: str, tier: int) -> Optional[Tuple[int, str, str]]:
            if is_unique_target(conditions, item):
                return (tier, strategy, selector)
            return None

        def try_semantic_unique(tier_base: int) -> Optional[Tuple[int, str, str]]:
            if txt:
                res = try_spec({"text": txt}, "text", txt, tier_base)
                if res:
                    return res
            if desc:
                res = try_spec({"description": desc}, "description", desc, tier_base)
                if res:
                    return res
            return None

        def try_semantic_disambiguate(tier_base: int) -> Optional[Tuple[int, str, str]]:
            if txt:
                if rid:
                    res = try_spec({"id": rid, "text": txt}, "composite", serialize_composite_selector({"id": rid, "text": txt}), tier_base)
                    if res:
                        return res
                    res = try_spec({"id": rid}, "resourceId", rid, tier_base)
                    if res:
                        return res
                if cls:
                    res = try_spec({"class_name": cls, "text": txt}, "composite", serialize_composite_selector({"class_name": cls, "text": txt}), tier_base)
                    if res:
                        return res
                if rid and cls:
                    res = try_spec({"class_name": cls, "id": rid, "text": txt}, "composite", serialize_composite_selector({"class_name": cls, "id": rid, "text": txt}), tier_base)
                    if res:
                        return res

            if desc:
                if rid:
                    res = try_spec({"description": desc, "id": rid}, "composite", serialize_composite_selector({"description": desc, "id": rid}), tier_base)
                    if res:
                        return res
                    res = try_spec({"id": rid}, "resourceId", rid, tier_base)
                    if res:
                        return res
                if cls:
                    res = try_spec({"class_name": cls, "description": desc}, "composite", serialize_composite_selector({"class_name": cls, "description": desc}), tier_base)
                    if res:
                        return res
                if rid and cls:
                    res = try_spec({"class_name": cls, "description": desc, "id": rid}, "composite", serialize_composite_selector({"class_name": cls, "description": desc, "id": rid}), tier_base)
                    if res:
                        return res
            return None

        def try_id_and_combinations(tier_base: int) -> Optional[Tuple[int, str, str]]:
            if rid:
                res = try_spec({"id": rid}, "resourceId", rid, tier_base)
                if res:
                    return res
                if txt:
                    res = try_spec({"id": rid, "text": txt}, "composite", serialize_composite_selector({"id": rid, "text": txt}), tier_base)
                    if res:
                        return res
                if desc:
                    res = try_spec({"description": desc, "id": rid}, "composite", serialize_composite_selector({"description": desc, "id": rid}), tier_base)
                    if res:
                        return res
                if cls:
                    res = try_spec({"class_name": cls, "id": rid}, "composite", serialize_composite_selector({"class_name": cls, "id": rid}), tier_base)
                    if res:
                        return res
                    if txt:
                        res = try_spec({"class_name": cls, "id": rid, "text": txt}, "composite", serialize_composite_selector({"class_name": cls, "id": rid, "text": txt}), tier_base)
                        if res:
                            return res
                    if desc:
                        res = try_spec({"class_name": cls, "description": desc, "id": rid}, "composite", serialize_composite_selector({"class_name": cls, "description": desc, "id": rid}), tier_base)
                        if res:
                            return res
            return None

        if effective_priority == "id_first":
            hit = try_id_and_combinations(0)
            if hit:
                return hit
            hit = try_semantic_unique(1)
            if hit:
                return hit
            hit = try_semantic_disambiguate(1)
            if hit:
                return hit
        else:
            hit = try_semantic_unique(0)
            if hit:
                return hit
            hit = try_semantic_disambiguate(1)
            if hit:
                return hit
            hit = try_id_and_combinations(1)
            if hit:
                return hit

        if non_layout_cls:
            hit = try_spec({"class_name": non_layout_cls}, "className", non_layout_cls, 2)
            if hit:
                return hit

        return None

    synthesized_hits: List[Tuple[Tuple[int, int, int, int], Dict[str, Any], str, str]] = []
    for item in hit_nodes:
        synthesized = synthesize_for_node(item)
        if not synthesized:
            continue
        tier, strategy, selector = synthesized
        is_layout_flag = 1 if _is_generic_layout_class(item["class"]) else 0
        sort_key = (tier, is_layout_flag, 0 if item["is_leaf"] else 1, int(item["area"]))
        synthesized_hits.append((sort_key, item, strategy, selector))

    if synthesized_hits:
        synthesized_hits.sort(key=lambda entry: entry[0])
        _, best_item, best_strategy, best_selector = synthesized_hits[0]
        info = _extract_node_locator_fields(best_item["node"], best_item["bounds"])
        if _is_unstable_locator_text(info["text"]):
            info["text"] = ""
        if _is_unstable_locator_text(info["description"]):
            info["description"] = ""
        return {
            "selector": best_selector,
            "strategy": best_strategy,
            "element": info,
        }

    # 无任何唯一定位方案：判断是因为「页面存在重复属性」还是「元素完全无可用属性」
    def fallback_score(item: Dict[str, Any]) -> Tuple[int, int, int]:
        return (
            1 if _is_generic_layout_class(item["class"]) else 0,
            0 if item["is_leaf"] else 1,
            int(item["area"]),
        )

    hit_nodes.sort(key=fallback_score)
    fallback_item = hit_nodes[0]
    info = _extract_node_locator_fields(fallback_item["node"], fallback_item["bounds"])
    if _is_unstable_locator_text(info["text"]):
        info["text"] = ""
    if _is_unstable_locator_text(info["description"]):
        info["description"] = ""

    has_duplicate_candidate = any(
        bool(
            item["clean_text"]
            or item["clean_desc"]
            or (enable_resource_id and item["usable_id"])
            or (enable_class_name and not _is_generic_layout_class(item["class"]))
        )
        for item in hit_nodes
    )
    if has_duplicate_candidate:
        return {
            "selector": "",
            "strategy": "ambiguous",
            "reason": "duplicate_locator",
            "element": info,
        }

    return {
        "selector": f"//{info['className']}",
        "strategy": "xpath",
        "element": info,
    }
