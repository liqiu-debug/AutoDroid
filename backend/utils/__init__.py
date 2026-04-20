# backend/utils/__init__.py
# 重新导出 UI 元素分析工具函数（原 backend/utils.py 中定义）
import sys
import os
import importlib.util

# utils.py 和 utils/ 包同级共存，需要手动加载 utils.py
_legacy_path = os.path.join(os.path.dirname(__file__), "..", "utils.py")
if os.path.exists(_legacy_path):
    _spec = importlib.util.spec_from_file_location("backend._utils_compat", _legacy_path)
    _module = importlib.util.module_from_spec(_spec)
    _spec.loader.exec_module(_module)
    sys.modules["backend._utils_compat"] = _module

    # 导出公开 API
    parse_bounds = _module.parse_bounds
    calculate_element_from_coordinates = _module.calculate_element_from_coordinates
    evaluate_page_text_assertion = _module.evaluate_page_text_assertion
