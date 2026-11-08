"""iPhone / iPad 机型标识（ProductType，如 iPhone15,2）→ 市场名（iPhone 14 Pro）。

独立于 ios_scanner（后者顶层导入 tidevice）：设备登记、迁移等不扫描设备的路径也要用。
新机型以 Xcode `xcrun devicectl list devices` 的 marketingName 为准补充；
桌面客户端 engine/core/fastbot_ios/models.py 维护同一张表。
"""
import re
from typing import Dict, Optional

# ==================== iPhone 机型映射表 ====================
# ProductType -> 市场名称（覆盖 iPhone 8 ~ iPhone 17 系列）
IPHONE_MODEL_MAP: Dict[str, str] = {
    # iPhone 8 / X 系列
    "iPhone10,1": "iPhone 8",
    "iPhone10,2": "iPhone 8 Plus",
    "iPhone10,3": "iPhone X",
    "iPhone10,4": "iPhone 8",
    "iPhone10,5": "iPhone 8 Plus",
    "iPhone10,6": "iPhone X",
    # iPhone XS / XR 系列
    "iPhone11,2": "iPhone XS",
    "iPhone11,4": "iPhone XS Max",
    "iPhone11,6": "iPhone XS Max",
    "iPhone11,8": "iPhone XR",
    # iPhone 11 系列
    "iPhone12,1": "iPhone 11",
    "iPhone12,3": "iPhone 11 Pro",
    "iPhone12,5": "iPhone 11 Pro Max",
    "iPhone12,8": "iPhone SE (2nd)",
    # iPhone 12 系列
    "iPhone13,1": "iPhone 12 mini",
    "iPhone13,2": "iPhone 12",
    "iPhone13,3": "iPhone 12 Pro",
    "iPhone13,4": "iPhone 12 Pro Max",
    # iPhone 13 系列
    "iPhone14,2": "iPhone 13 Pro",
    "iPhone14,3": "iPhone 13 Pro Max",
    "iPhone14,4": "iPhone 13 mini",
    "iPhone14,5": "iPhone 13",
    "iPhone14,6": "iPhone SE (3rd)",
    "iPhone14,7": "iPhone 14",
    "iPhone14,8": "iPhone 14 Plus",
    # iPhone 14 Pro 系列
    "iPhone15,2": "iPhone 14 Pro",
    "iPhone15,3": "iPhone 14 Pro Max",
    "iPhone15,4": "iPhone 15",
    "iPhone15,5": "iPhone 15 Plus",
    # iPhone 15 Pro 系列
    "iPhone16,1": "iPhone 15 Pro",
    "iPhone16,2": "iPhone 15 Pro Max",
    # iPhone 16 系列
    "iPhone17,1": "iPhone 16 Pro",
    "iPhone17,2": "iPhone 16 Pro Max",
    "iPhone17,3": "iPhone 16",
    "iPhone17,4": "iPhone 16 Plus",
    "iPhone17,5": "iPhone 16e",
    # iPhone 17 系列（2025）
    "iPhone18,1": "iPhone 17 Pro",
    "iPhone18,2": "iPhone 17 Pro Max",
    "iPhone18,3": "iPhone 17",
    "iPhone18,4": "iPhone Air",
    # iPad 常见型号（可按需扩充）
    "iPad13,18": "iPad (10th)",
    "iPad13,19": "iPad (10th)",
    "iPad14,3":  "iPad Pro 11-inch (4th)",
    "iPad14,4":  "iPad Pro 11-inch (4th)",
    "iPad14,5":  "iPad Pro 12.9-inch (6th)",
    "iPad14,6":  "iPad Pro 12.9-inch (6th)",
}

# 机型标识形态：iPhone15,2 / iPad14,5 / iPod9,1
_PRODUCT_TYPE_RE = re.compile(r"^(iPhone|iPad|iPod)\d+,\d+$")


def is_product_type(value: Optional[str]) -> bool:
    return bool(_PRODUCT_TYPE_RE.match(str(value or "").strip()))


def ios_marketing_name(value: Optional[str]) -> str:
    """机型标识翻译为市场名；已经是市场名 / 未收录的标识原样返回。"""
    text = str(value or "").strip()
    return IPHONE_MODEL_MAP.get(text, text)
