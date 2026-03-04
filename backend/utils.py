"""
UI 元素分析工具模块

核心功能：
- 解析 Android UI 层级 XML
- 根据点击坐标找到最佳匹配元素
- 生成元素定位策略 (text / description / image / xpath)
"""
import re
import os
import uuid
import base64
import io
import logging
import xml.etree.ElementTree as ET
from typing import Dict, Optional, Tuple

from PIL import Image

logger = logging.getLogger(__name__)

# 项目根目录（用于图片存储路径计算）
PROJECT_ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")


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


def calculate_element_from_coordinates(
    hierarchy_xml: str,
    target_x: int,
    target_y: int,
    screenshot_base64: str = None
) -> Dict:
    """
    根据点击坐标在 UI 层级中找到最佳匹配元素，并生成定位策略。
    
    算法流程:
    1. 遍历 XML 层级树，找到所有包含该坐标的元素
    2. 按优先级排序：有 desc > 有 text > 有 resourceId > 无属性
    3. 优先选择叶子节点、小面积节点
    4. 无可用属性时，裁剪元素区域图片用于图像匹配
    
    Args:
        hierarchy_xml: Android dump_hierarchy 返回的 XML 字符串
        target_x: 点击的 X 坐标
        target_y: 点击的 Y 坐标
        screenshot_base64: 当前屏幕截图的 base64 编码（用于图像裁剪兜底）
        
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
        bounds = parse_bounds(node.attrib.get("bounds"))
        matches = False

        if bounds:
            x1, y1, x2, y2 = bounds
            if x1 <= target_x <= x2 and y1 <= target_y <= y2:
                matches = True
                candidate_nodes.append({
                    "node": node,
                    "bounds": bounds,
                    "area": (x2 - x1) * (y2 - y1),
                    "is_leaf": len(node) == 0,
                    "class": node.attrib.get("class", ""),
                    "res_id": node.attrib.get("resource-id", ""),
                    "text": node.attrib.get("text", ""),
                    "desc": node.attrib.get("content-desc", "")
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
        - 维度1: 属性优先级 (desc=0, text=1, resourceId=2, 无属性=3)
        - 维度2: 节点类型 (非容器=0, 容器=1)
        - 维度3: 叶子优先 (叶子=0, 非叶子=1)
        - 维度4: 面积 (越小越优先)
        """
        node_class = item["class"]
        is_layout = any(kw in node_class for kw in ["Layout", "ViewGroup", "ScrollView"]) or node_class == "View"

        if item["desc"]:
            score_attr = 0
        elif item["text"]:
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
    info = {
        "resourceId": best_match["res_id"],
        "text": best_match["text"],
        "description": best_match["desc"],
        "className": best_match["class"],
        "bounds": node.attrib.get("bounds")
    }

    # 优先级: text > description > 图像匹配 > xpath兜底
    if info["text"]:
        return {"selector": info["text"], "strategy": "text", "element": info}

    if info["description"]:
        return {"selector": info["description"], "strategy": "description", "element": info}

    # 无可用属性 → 尝试图像匹配
    if screenshot_base64:
        image_result = _try_image_crop(info, target_x, target_y, screenshot_base64)
        if image_result:
            return image_result

    # 最后兜底：弱 xpath
    return {
        "selector": f"//{info['className']}",
        "strategy": "xpath",
        "element": info
    }


def _try_image_crop(
    info: dict,
    target_x: int,
    target_y: int,
    screenshot_base64: str
) -> Optional[Dict]:
    """
    裁剪元素区域图片用于图像匹配。
    
    当元素面积超过屏幕 50% 时（说明选中了容器），
    改用点击坐标周围 100x100 像素区域作为模板。
    
    Returns:
        成功返回 {"selector": 图片路径, "strategy": "image", ...}
        失败返回 None
    """
    try:
        bounds = parse_bounds(info["bounds"])
        if not bounds:
            return None

        x1, y1, x2, y2 = bounds
        img = Image.open(io.BytesIO(base64.b64decode(screenshot_base64)))
        width_s, height_s = img.size

        # 检查面积占比
        node_area = (x2 - x1) * (y2 - y1)
        screen_area = width_s * height_s
        area_ratio = node_area / screen_area if screen_area > 0 else 1

        if area_ratio > 0.5:
            # 节点过大，改用点击坐标周围 100x100 区域
            logger.info(f"节点面积过大({area_ratio:.0%})，使用坐标周围区域裁剪")
            half = 50
            x1 = max(0, target_x - half)
            y1 = max(0, target_y - half)
            x2 = min(width_s, target_x + half)
            y2 = min(height_s, target_y + half)
        else:
            # 正常裁剪元素区域
            x1 = max(0, x1)
            y1 = max(0, y1)
            x2 = min(width_s, x2)
            y2 = min(height_s, y2)

        if x2 <= x1 or y2 <= y1:
            return None

        cropped = img.crop((x1, y1, x2, y2))

        # 保存裁剪图片
        image_filename = f"element_{uuid.uuid4().hex[:8]}.png"
        image_dir = os.path.join(PROJECT_ROOT, "static", "images")
        os.makedirs(image_dir, exist_ok=True)
        cropped.save(os.path.join(image_dir, image_filename))

        return {
            "selector": f"static/images/{image_filename}",
            "strategy": "image",
            "element": info,
            "fallback_reason": "无可用属性，使用图像匹配"
        }

    except Exception as e:
        logger.warning(f"图像裁剪失败: {e}")
        return None
