"""
TestRunner - 测试用例执行引擎

负责连接 Android 设备、执行测试步骤、处理重试逻辑和变量替换。
支持的动作类型: click, input, wait_until_exists, scroll_to, assert_text, click_image, assert_image
"""
import os
import time
import logging
import threading
import re
import xml.etree.ElementTree as ET
from typing import Dict, Any, Optional, Tuple, List

import uiautomator2 as u2
import numpy as np
import cv2

from .schemas import Step, ActionType, SelectorType, Variable
from .utils import evaluate_page_text_assertion
from .utils.ocr_compat import create_paddle_ocr_engine, extract_ocr_text, run_paddle_ocr
from .utils.template_match import find_template_match, image_to_bgr, load_image_bgr

logger = logging.getLogger(__name__)

_ocr_engine: Optional[Any] = None
_ocr_lock = threading.Lock()
_ASSERT_IMAGE_TEMPLATE_THRESHOLD = 0.95
_ASSERT_IMAGE_SSIM_THRESHOLD = 0.9
_ASSERT_IMAGE_FAST_FAIL_SIMILARITY = 0.98
_ASSERT_IMAGE_FAST_FAIL_SSIM = 0.97


def _is_strong_assert_image_match(match: Optional[Dict[str, Any]]) -> bool:
    if not isinstance(match, dict):
        return False
    similarity = float(match.get("similarity") or 0.0)
    ssim = match.get("ssim")
    if ssim is None:
        return similarity >= 0.995
    return similarity >= _ASSERT_IMAGE_FAST_FAIL_SIMILARITY and float(ssim) >= _ASSERT_IMAGE_FAST_FAIL_SSIM


def _should_retry_step_error(step: Step, exc: Exception) -> bool:
    if isinstance(exc, (ValueError, FileNotFoundError, NotImplementedError)):
        return False
    if step.action == ActionType.ASSERT_IMAGE and isinstance(exc, AssertionError):
        return False
    return True

# ============ 全局设备中止注册表 ============
# 用于从 unlock 接口中止正在执行测试的 Python 线程
_device_abort_events: Dict[str, threading.Event] = {}
_abort_lock = threading.Lock()


def register_device_abort(serial: str) -> threading.Event:
    """注册设备中止事件，返回 Event 给 runner 监听"""
    with _abort_lock:
        event = threading.Event()
        _device_abort_events[serial] = event
        return event


def trigger_device_abort(serial: str):
    """触发设备中止信号（由 unlock 接口调用）"""
    with _abort_lock:
        event = _device_abort_events.get(serial)
        if event:
            event.set()
            logger.warning(f"已发送中止信号到设备 {serial}")


def unregister_device_abort(serial: str):
    """清除设备中止事件"""
    with _abort_lock:
        _device_abort_events.pop(serial, None)


def _get_ocr_engine() -> Any:
    global _ocr_engine
    if _ocr_engine is not None:
        return _ocr_engine

    with _ocr_lock:
        if _ocr_engine is None:
            logger.debug("Legacy runner OCR engine loading")
            _ocr_engine = create_paddle_ocr_engine(use_angle_cls=False, lang="ch")
            logger.debug("Legacy runner OCR engine ready")
    return _ocr_engine


def _dump_model(value: Any) -> Any:
    if hasattr(value, "model_dump"):
        return value.model_dump()
    if hasattr(value, "dict"):
        return value.dict()
    return value


class TestRunner:
    """
    测试用例执行器。
    
    通过 uiautomator2 连接 Android 设备，支持：
    - 变量替换 (${var} → value)
    - 重试机制 (失败后重试3次，间隔1秒)
    - 多种定位策略 (resourceId / text / description / xpath / 图像匹配)
    """

    def __init__(self, device_serial: Optional[str] = None, abort_event: Optional[threading.Event] = None):
        self.device_serial = device_serial
        self.d = None  # uiautomator2 设备对象
        self.abort_event = abort_event  # 外部中止信号

    def connect(self):
        """连接 Android 设备"""
        try:
            if self.device_serial:
                self.d = u2.connect(self.device_serial)
            else:
                self.d = u2.connect()
            logger.info(f"已连接设备: {self.d.info}")
        except Exception as e:
            logger.error(f"设备连接失败: {e}")
            raise

    def _prepare_case_variables(
        self,
        test_case,
        extra_variables: Optional[Dict[str, str]] = None,
    ) -> Dict[str, str]:
        variables_map = {v.key: v.value for v in test_case.variables}
        if extra_variables:
            for k, v in extra_variables.items():
                if k not in variables_map:
                    variables_map[k] = v
        return variables_map

    def _build_case_result(
        self,
        test_case,
        *,
        success: bool,
        results: List[Dict[str, Any]],
        variables_map: Dict[str, str],
    ) -> Dict[str, Any]:
        payload = {
            "case_id": test_case.id,
            "success": success,
            "steps": results,
            "exported_variables": dict(variables_map),
        }
        if success and any(item.get("is_warning") for item in results):
            payload["is_warning"] = True
        return payload

    def iter_case_execution(self, test_case, extra_variables: Optional[Dict[str, str]] = None):
        """同步逐步执行用例，逐步 yield 结果，最后 return 完整 case_result。"""
        if not self.d:
            self.connect()

        variables_map = self._prepare_case_variables(test_case, extra_variables)
        results: List[Dict[str, Any]] = []
        success = True
        total_steps = len(test_case.steps or [])

        logger.info(f"开始执行用例: {test_case.name} (ID: {test_case.id})")

        for step_index, step in enumerate(test_case.steps):
            should_stop = False

            if self.abort_event and self.abort_event.is_set():
                logger.warning("收到中止信号，停止执行用例")
                step_result = {
                    "step": _dump_model(step),
                    "success": False,
                    "error": "已被用户中止",
                    "duration": 0,
                }
                success = False
                results.append(step_result)
                yield {
                    "step": step,
                    "step_index": step_index,
                    "total_steps": total_steps,
                    "step_result": step_result,
                    "variables_map": dict(variables_map),
                }
                break

            step_result = self.execute_step(step, variables_map)
            results.append(step_result)
            if not step_result["success"]:
                strategy = getattr(step, "error_strategy", "ABORT")

                if strategy == "IGNORE":
                    step_result["is_warning"] = True
                    logger.warning("步骤失败，容错策略为 IGNORE，标记为 WARNING 继续执行。")
                elif strategy == "CONTINUE":
                    success = False
                    logger.warning("步骤失败，容错策略为 CONTINUE，标记失败但继续执行剩余步骤。")
                else:  # ABORT
                    success = False
                    should_stop = True
                    logger.error(f"步骤失败: {step_result}")

            yield {
                "step": step,
                "step_index": step_index,
                "total_steps": total_steps,
                "step_result": step_result,
                "variables_map": dict(variables_map),
            }

            if should_stop:
                break

        return self._build_case_result(
            test_case,
            success=success,
            results=results,
            variables_map=variables_map,
        )

    def run_case(self, test_case, extra_variables: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
        """
        执行完整测试用例（同步模式，供 REST API 调用）。
        
        Returns:
            包含执行结果的字典 {"case_id", "success", "steps"}
        """
        case_iter = self.iter_case_execution(test_case, extra_variables=extra_variables)
        while True:
            try:
                next(case_iter)
            except StopIteration as stop:
                return stop.value

    def run_scenario(self, scenario_cases: list) -> list:
        """
        顺序执行场景中的多个测试用例，并在用例间桥接变量上下文。
        """
        scenario_context = {}
        scenario_results = []

        for case in scenario_cases:
            case_result = self.run_case(case, extra_variables=scenario_context)
            scenario_results.append(case_result)

            new_vars = case_result.get("exported_variables", {})
            scenario_context.update(new_vars)

            if not case_result.get("success"):
                break

        return scenario_results

    def execute_step(self, step: Step, variables: Dict[str, str]) -> Dict[str, Any]:
        """
        执行单个步骤，包含变量替换和重试逻辑。
        
        Args:
            step: 步骤对象
            variables: 变量映射表 {"key": "value"}
            
        Returns:
            {"step": dict, "success": bool, "error"?: str, "duration": float}
        """
        start_time = time.time()
        logger.debug(f"准备执行步骤: action={step.action}, selector={step.selector}, value={step.value}, variables={list(variables.keys())}")

        # 1. 变量替换
        try:
            target_selector = self._substitute_variables(step.selector, variables)
            target_value = self._substitute_variables(step.value, variables)
        except Exception as e:
            return {
                "step": _dump_model(step),
                "success": False,
                "error": f"变量替换失败: {str(e)}",
                "duration": time.time() - start_time
            }

        # 2. 重试执行 (最多重试3次，间隔1秒)
        max_retries = 3
        retry_interval = 1.0
        error_message = None

        for attempt in range(max_retries + 1):
            # 检查中止信号
            if self.abort_event and self.abort_event.is_set():
                return {
                    "step": _dump_model(step),
                    "success": False,
                    "error": "已被用户中止",
                    "duration": time.time() - start_time
                }
            try:
                self._perform_action(
                    step.action,
                    target_selector,
                    step.selector_type,
                    target_value,
                    step.options or {},
                    variables
                )
                return {
                    "step": _dump_model(step),
                    "success": True,
                    "duration": time.time() - start_time
                }
            except Exception as e:
                error_message = str(e)
                logger.warning(f"第 {attempt + 1}/{max_retries + 1} 次尝试失败: {e}")
                if not _should_retry_step_error(step, e):
                    logger.info("步骤失败无需重试: action=%s error=%s", step.action, e)
                    break
                if attempt < max_retries:
                    time.sleep(retry_interval)
                else:
                    logger.error(f"所有重试均失败: {step}")

        return {
            "step": _dump_model(step),
            "success": False,
            "error": error_message,
            "duration": time.time() - start_time
        }

    def _substitute_variables(self, text: Optional[str], variables: Dict[str, str]) -> Optional[str]:
        """将 {{ VAR }} 占位符替换为实际变量值"""
        from backend.utils.variable_render import render_step_data
        if not text:
            return text
        return render_step_data(text, variables)

    def _find_element(self, selector: str, selector_type: SelectorType):
        """
        根据选择器类型查找 UI 元素。
        
        支持的选择器类型:
        - RESOURCE_ID: 通过 resourceId 定位
        - TEXT: 先精确匹配，失败后尝试模糊匹配 (textContains)
        - XPATH: XPath 表达式
        - DESCRIPTION: content-desc 属性
        - IMAGE: 图像匹配（由 _perform_action 单独处理）
        """
        if not selector:
            return None

        if selector_type == SelectorType.RESOURCE_ID:
            return self.d(resourceId=selector)

        elif selector_type == SelectorType.TEXT:
            # 先精确匹配，不存在则降级为模糊匹配
            el = self.d(text=selector)
            if not el.exists(timeout=1):
                el = self.d(textContains=selector)
            return el

        elif selector_type == SelectorType.XPATH:
            return self.d.xpath(selector)

        elif selector_type == SelectorType.DESCRIPTION:
            return self.d(description=selector)

        elif selector_type == SelectorType.IMAGE:
            return None  # 图像匹配在 _perform_action 中直接处理

        else:
            # 自动推断：以 / 开头视为 XPath，否则视为 resourceId
            if selector.startswith("//") or selector.startswith("/"):
                return self.d.xpath(selector)
            return self.d(resourceId=selector)

    def _collect_page_text_candidates(self) -> List[str]:
        values: List[str] = []
        xml_text = ""
        try:
            xml_text = str(self.d.dump_hierarchy() or "")
        except Exception as exc:
            logger.warning(f"获取页面层级失败: {exc}")

        if xml_text:
            try:
                root = ET.fromstring(xml_text)
                for node in root.iter():
                    for attr_name in ("text", "content-desc", "contentDescription"):
                        value = str(node.attrib.get(attr_name) or "").strip()
                        if value:
                            values.append(value)
            except Exception as exc:
                logger.warning(f"解析页面层级失败: {exc}")
                values.extend(
                    match.strip()
                    for match in re.findall(r'(?:text|content-desc|contentDescription)="([^"]+)"', xml_text)
                    if str(match).strip()
                )

        return values

    def _perform_action(
        self,
        action: ActionType,
        selector: Optional[str],
        selector_type: Optional[SelectorType],
        value: Optional[str],
        options: Optional[Dict[str, Any]] = None,
        variables: Optional[Dict[str, str]] = None
    ):
        """
        执行具体的 UI 动作。
        
        支持的动作:
        - click: 点击元素
        - click_image: 图像模板匹配点击
        - assert_image: 图像模板存在/不存在断言
        - input: 输入文本
        - wait_until_exists: 等待元素出现
        - scroll_to: 滚动到元素可见
        - assert_text: 页面全局文本断言
        """
        options = options or {}

        def _input_on_focused(text_value: str) -> None:
            errors = []

            try:
                focused = self.d(focused=True)
                if focused is not None and focused.exists(timeout=1):
                    try:
                        focused.clear_text()
                    except Exception:
                        pass
                    focused.set_text(text_value)
                    return
                errors.append("focused element not found")
            except Exception as exc:
                errors.append(f"focused element failed: {exc}")

            try:
                self.d.send_keys(text_value, clear=True)
                return
            except TypeError:
                pass
            except Exception as exc:
                errors.append(f"send_keys(clear=True) failed: {exc}")

            try:
                self.d.send_keys(text_value)
                return
            except Exception as exc:
                errors.append(f"send_keys failed: {exc}")

            try:
                shell_value = str(text_value).replace(" ", "%s")
                self.d.shell(f"input text {shell_value}")
                return
            except Exception as exc:
                errors.append(f"shell input failed: {exc}")

            detail = "; ".join(errors) if errors else "unknown"
            raise Exception(f"输入失败（当前焦点模式）: {detail}")

        # ---- 图像匹配点击（无需先查找元素）----
        if action == ActionType.CLICK_IMAGE:
            selector_text = str(selector or "").strip()
            if not selector_text:
                raise ValueError("click_image 动作必须提供 selector/image_path")
            image_path = os.path.join(os.path.dirname(__file__), "..", selector_text)
            if not os.path.exists(image_path):
                raise FileNotFoundError(f"图像文件不存在: {image_path}")

            logger.info(f"图像匹配点击: {image_path}")
            try:
                self.d.image.click(image_path, timeout=5)
                logger.info("图像匹配点击成功")
            except Exception as e:
                logger.warning(f"图像匹配失败: {e}")
                raise Exception(f"图像匹配点击失败: {e}，建议使用 text/desc 定位或重新录制")
            return

        if action == ActionType.ASSERT_IMAGE:
            selector_text = str(selector or "").strip()
            if not selector_text:
                raise ValueError("assert_image 动作必须提供 selector/image_path")
            image_path = os.path.join(os.path.dirname(__file__), "..", selector_text)
            if not os.path.exists(image_path):
                raise FileNotFoundError(f"图像文件不存在: {image_path}")

            match_mode = str((options or {}).get("match_mode") or "exists").strip().lower()
            if match_mode not in {"exists", "not_exists"}:
                raise ValueError(f"assert_image 不支持的 match_mode: {match_mode}")

            logger.info("图像断言: path=%s match_mode=%s", image_path, match_mode)
            first_match = None
            try:
                screen_image = self.d.screenshot(format="opencv")
                screen_bgr = image_to_bgr(screen_image, source="screenshot")
                template_bgr = load_image_bgr(image_path)
                first_match = find_template_match(
                    screen_bgr=screen_bgr,
                    template_bgr=template_bgr,
                    threshold=_ASSERT_IMAGE_TEMPLATE_THRESHOLD,
                    ssim_threshold=_ASSERT_IMAGE_SSIM_THRESHOLD,
                )
            except Exception as exc:
                logger.warning("图像断言严格匹配失败，降级到 image.wait: %s", exc)
                first_match = self.d.image.wait(image_path, timeout=1, threshold=0.9)

            if match_mode == "exists":
                if isinstance(first_match, dict):
                    logger.info("图像断言成功: 页面存在目标图像")
                    return
                raise AssertionError(f"断言失败: 期望页面存在图像 '{selector_text}'，但未匹配到")

            if not isinstance(first_match, dict):
                logger.info("图像断言成功: 页面不存在目标图像")
                return

            if _is_strong_assert_image_match(first_match):
                raise AssertionError(f"断言失败: 期望页面不存在图像 '{selector_text}'，但已高置信度匹配到目标")

            time.sleep(0.25)
            second_match = None
            try:
                screen_image = self.d.screenshot(format="opencv")
                screen_bgr = image_to_bgr(screen_image, source="screenshot")
                template_bgr = load_image_bgr(image_path)
                second_match = find_template_match(
                    screen_bgr=screen_bgr,
                    template_bgr=template_bgr,
                    threshold=_ASSERT_IMAGE_TEMPLATE_THRESHOLD,
                    ssim_threshold=_ASSERT_IMAGE_SSIM_THRESHOLD,
                )
            except Exception as exc:
                logger.warning("图像断言二次严格匹配失败，降级到 image.wait: %s", exc)
                second_match = self.d.image.wait(image_path, timeout=1, threshold=0.9)
            if isinstance(second_match, dict):
                raise AssertionError(f"断言失败: 期望页面不存在图像 '{selector_text}'，但仍匹配到目标")
            logger.info("图像断言成功: 页面不存在目标图像")
            return

        # ---- 等待元素存在 ----
        if action == ActionType.WAIT_UNTIL_EXISTS:
            el = self._find_element(selector, selector_type)
            if not el.exists(timeout=10):
                raise Exception(f"等待超时，元素未出现: {selector}")
            return

        if action == ActionType.SLEEP:
            try:
                seconds = float(value) if value else 1.0
            except (TypeError, ValueError):
                raise ValueError(f"sleep 动作 value 必须是数字字符串，当前: {value}")
            if seconds < 0:
                raise ValueError("sleep 动作 value 不能小于 0")
            logger.info(f"强制等待 {seconds} 秒")
            time.sleep(seconds)
            return

        if action == ActionType.EXTRACT_BY_OCR:
            if not value:
                raise ValueError("extract_by_ocr 动作必须提供 value 作为变量名")
            if not variables:
                raise ValueError("extract_by_ocr 执行失败：变量上下文不存在")
            raw_text = self._extract_text_from_region(selector)
            logger.debug(f"OCR原始识别结果({value}): {raw_text}")
            extracted = self._apply_extract_rule(raw_text, options)
            logger.debug(f"OCR提取后结果({value}): {extracted}")
            variables[value] = extracted
            logger.info(f"OCR提取成功: {value}={extracted}")
            return

        # ---- 不需要查找元素的全局动作 ----
        if action == ActionType.START_APP:
            if not selector:
                raise ValueError("start_app 动作必须提供包名 (selector)")
            self.d.app_start(selector)
            return

        if action == ActionType.STOP_APP:
            if not selector:
                raise ValueError("stop_app 动作必须提供包名 (selector)")
            self.d.app_stop(selector)
            return

        if action == ActionType.BACK:
            self.d.press("back")
            return

        if action == ActionType.HOME:
            self.d.press("home")
            return

        if action == ActionType.SWIPE:
            # selector 存储方向: up, down, left, right
            direction = selector.lower() if selector else "up"
            self.d.swipe_ext(direction, scale=0.8)
            return

        if action == ActionType.INPUT and not str(selector or "").strip():
            if value is None:
                raise ValueError("input 动作必须提供 value 参数")
            _input_on_focused(str(value))
            return

        if action == ActionType.ASSERT_TEXT:
            if value is None:
                raise ValueError("assert_text 动作必须提供 value 参数")

            expected_text = str(value)
            if not expected_text.strip():
                raise ValueError("assert_text 动作必须提供非空文本")

            match_mode = str((options or {}).get("match_mode") or "contains").strip().lower()
            if match_mode not in {"contains", "not_contains"}:
                raise ValueError(f"assert_text 不支持的 match_mode: {match_mode}")

            candidates = self._collect_page_text_candidates()
            evaluation = evaluate_page_text_assertion(candidates, expected_text)
            matched = bool(evaluation.get("matched"))
            preview = evaluation.get("preview") or candidates[:5]

            if match_mode == "contains" and matched:
                logger.info(f"全局断言成功: 页面包含 '{expected_text}'")
                return
            if match_mode == "not_contains" and not matched:
                logger.info(f"全局断言成功: 页面不包含 '{expected_text}'")
                return

            if match_mode == "not_contains":
                raise AssertionError(f"断言失败: 期望页面不包含 '{expected_text}'，实际命中={preview}")
            raise AssertionError(f"断言失败: 期望页面包含 '{expected_text}'，实际候选={preview}")

        # ---- 需要先查找元素的动作 ----
        el = self._find_element(selector, selector_type)
        if el is None:
            raise Exception(f"元素未找到: {selector}")
        if not el.exists(timeout=3):
            raise Exception(f"元素未找到: {selector}")

        if action == ActionType.CLICK:
            el.click()

        elif action == ActionType.INPUT:
            if value is None:
                raise ValueError("input 动作必须提供 value 参数")
            
            logger.info(f"Input action: selector={selector}, value={value}")
            # 1. 获取元素信息
            info = el.info
            logger.info(f"Target element info: class={info.get('className')}, res={info.get('resourceName')}, text={info.get('text')}")

            # 2. 智能修正：如果当前元素不是 EditText，尝试查找子元素中的 EditText
            target_el = el
            if info.get('className') != "android.widget.EditText":
                logger.info("Target is not EditText, searching for child EditText...")
                child_edit = el.child(className="android.widget.EditText")
                if child_edit.exists(timeout=1):
                    logger.info("Found child EditText, switching target.")
                    target_el = child_edit
                else:
                    logger.warning("No child EditText found, using original element.")

            # 3. 点击聚焦
            try:
                target_el.click()
                time.sleep(0.5)
            except Exception as e:
                logger.warning(f"Click to focus failed: {e}")

            # 4. 清除现有文本再输入
            input_success = False
            try:
                target_el.clear_text()
                time.sleep(0.2)
                target_el.set_text(value)
                time.sleep(0.3)
                logger.info("set_text executed")
                input_success = True
            except Exception as e1:
                logger.warning(f"set_text failed: {e1}")

            # 5. 验证输入结果
            if input_success:
                try:
                    actual = target_el.get_text() or ""
                    if value in actual or actual in value:
                        logger.info(f"Input verified OK: actual='{actual}'")
                        return  # 成功，直接返回
                    else:
                        logger.warning(f"Input verification mismatch: expected='{value}', actual='{actual}', retrying...")
                        input_success = False
                except Exception:
                    logger.info("Cannot verify input (may be password field), assuming success")
                    return  # 无法验证（如密码框），信任 set_text

            # 6. 回退策略：ADB shell input
            if not input_success:
                logger.info("Falling back to 'adb shell input text'")
                try:
                    target_el.click()
                    time.sleep(0.3)
                    target_el.clear_text()
                    time.sleep(0.2)
                    # 使用 ADB input text（自动处理特殊字符）
                    self.d.send_keys(value)
                    time.sleep(0.3)
                    logger.info("send_keys executed")
                except Exception as e2:
                    logger.warning(f"send_keys failed: {e2}, trying shell input...")
                    try:
                        self.d.shell(f"input text '{value}'")
                    except Exception as e3:
                        raise Exception(f"所有输入方式均失败: set_text, send_keys, shell input. 最后错误: {e3}")

        else:
            raise NotImplementedError(f"不支持的动作类型: {action}")

    def _extract_text_from_region(self, selector: Optional[str]) -> str:
        """
        从指定区域提取文本。selector 格式: [x1, y1, x2, y2]，支持 0~1 百分比坐标。
        严格按截图裁剪区域执行 OCR，不再混入层级文本。
        """
        if not selector:
            raise ValueError("extract_by_ocr 动作必须提供截取区域 selector")

        x1, y1, x2, y2 = self._parse_region(selector)
        image = self.d.screenshot()
        if not image:
            raise Exception("无法获取设备截图")

        width, height = image.size
        if x2 <= 1 and y2 <= 1:
            rx1, ry1, rx2, ry2 = int(x1 * width), int(y1 * height), int(x2 * width), int(y2 * height)
        else:
            rx1, ry1, rx2, ry2 = int(x1), int(y1), int(x2), int(y2)

        rx1 = max(0, min(rx1, width))
        ry1 = max(0, min(ry1, height))
        rx2 = max(0, min(rx2, width))
        ry2 = max(0, min(ry2, height))
        if rx2 <= rx1 or ry2 <= ry1:
            raise ValueError(f"截取区域无效: [{rx1},{ry1},{rx2},{ry2}]")

        logger.warning(
            f"OCR裁剪区域像素: [{rx1},{ry1},{rx2},{ry2}], screenshot={width}x{height}, selector={selector}"
        )
        ocr_text = self._extract_text_from_screenshot(image, rx1, ry1, rx2, ry2)
        if not ocr_text:
            raise Exception(
                f"区域内未识别到可提取文本: [{rx1},{ry1},{rx2},{ry2}]，"
                "请确认该区域内存在可识别文本，或调整框选区域"
            )
        return ocr_text

    def _extract_text_from_screenshot(self, image, x1: int, y1: int, x2: int, y2: int) -> str:
        """截图裁剪后执行 OCR 识别，返回拼接文本。"""
        crop = image.crop((x1, y1, x2, y2))
        img_arr = cv2.cvtColor(np.array(crop), cv2.COLOR_RGB2BGR)
        if img_arr.size == 0:
            return ""

        try:
            result = run_paddle_ocr(_get_ocr_engine(), img_arr, use_cls=False)
        except Exception as e:
            logger.warning(f"PaddleOCR 识别失败: {e}")
            return ""

        merged = extract_ocr_text(result)
        if merged:
            logger.info(f"OCR fallback识别到文本: {merged}")
        return merged

    def _apply_extract_rule(self, raw_text: str, options: Dict[str, Any]) -> str:
        """根据 options 中的提取规则，从原始文本中抽取目标值。"""
        rule = (options.get("extract_rule") or "preset").lower()

        if rule == "regex":
            pattern = options.get("custom_regex")
            if not pattern:
                raise ValueError("extract_rule=regex 时必须提供 custom_regex")
            match = re.search(pattern, raw_text, re.S)
            if not match:
                raise Exception(f"正则未匹配到内容: {pattern}")
            if match.groups():
                for group in match.groups():
                    if group is not None:
                        return str(group).strip()
            return match.group(0).strip()

        if rule == "boundary":
            left = options.get("left_bound", "")
            right = options.get("right_bound", "")
            start = raw_text.find(left) + len(left) if left else 0
            if left and raw_text.find(left) < 0:
                raise Exception(f"未找到左边界: {left}")
            end = raw_text.find(right, start) if right else len(raw_text)
            if right and end < 0:
                raise Exception(f"未找到右边界: {right}")
            result = raw_text[start:end].strip()
            if not result:
                raise Exception("边界提取后结果为空")
            return result

        preset = (options.get("preset_type") or "number_only").lower()
        if preset == "number_only":
            match = re.search(r"\d+(?:\.\d+)?", raw_text)
        elif preset == "price":
            match = re.search(r"(?:¥|￥|\$)?\s*\d+(?:\.\d{1,2})?", raw_text)
        elif preset == "alphanumeric":
            match = re.search(r"[A-Za-z0-9]+", raw_text)
        elif preset == "chinese":
            match = re.search(r"[\u4e00-\u9fff]+", raw_text)
        else:
            raise ValueError(f"不支持的 preset_type: {preset}")

        if not match:
            raise Exception(f"内置模板未匹配到内容: {preset}")
        result = match.group(0).strip()
        if preset == "price":
            result = re.sub(r"[¥￥$\s]", "", result)
        return result

    def _parse_region(self, selector: str) -> Tuple[float, float, float, float]:
        """
        解析区域字符串，支持:
        - [0.1, 0.2, 0.5, 0.3]
        - 0.1,0.2,0.5,0.3
        """
        nums = re.findall(r"-?\d+(?:\.\d+)?", selector)
        if len(nums) != 4:
            raise ValueError(f"区域格式非法，应为 [x1, y1, x2, y2]，当前: {selector}")
        x1, y1, x2, y2 = map(float, nums)
        if x2 <= x1 or y2 <= y1:
            raise ValueError(f"区域坐标非法，需满足 x2>x1 且 y2>y1，当前: {selector}")
        return x1, y1, x2, y2

    def _parse_bounds(self, bounds: str) -> Optional[Tuple[int, int, int, int]]:
        """解析 Android bounds 字符串: [x1,y1][x2,y2]"""
        m = re.match(r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]", bounds)
        if not m:
            return None
        return tuple(map(int, m.groups()))


class ScenarioRunner:
    """
    场景执行器，负责按顺序执行多个 Test Case。
    核心特性：
    - 复用 Device 连接
    - 全局上下文
    - 聚合报告
    """
    def __init__(self, device_serial: Optional[str] = None, abort_event: Optional[threading.Event] = None):
        self.device_serial = device_serial
        self.abort_event = abort_event
        self.runner = TestRunner(device_serial, abort_event=abort_event)
        self.results = []

    def _resolve_case_failure_strategy(self, case_result: Dict[str, Any]) -> str:
        for step_result in reversed(case_result.get("steps", [])):
            if not step_result.get("success") and not step_result.get("is_warning"):
                return str((step_result.get("step") or {}).get("error_strategy") or "ABORT")
        return "ABORT"

    def iter_scenario_execution(self, scenario_id: int, session: Any, env_id: Optional[int] = None):
        """同步逐 case 执行场景，逐步 yield 事件，最后 return 完整 scenario result。"""
        from .models import TestScenario, ScenarioStep, TestCase, GlobalVariable
        from sqlmodel import select

        scenario = session.get(TestScenario, scenario_id)
        if not scenario:
            raise ValueError(f"Scenario not found: {scenario_id}")

        statement = select(ScenarioStep).where(ScenarioStep.scenario_id == scenario_id).order_by(ScenarioStep.order)
        steps = session.exec(statement).all()
        total_cases = len(steps)

        logger.info(f"开始执行场景: {scenario.name} (ID: {scenario.id}), 共 {total_cases} 个步骤")

        if not self.runner.d:
            try:
                self.runner.connect()
            except Exception as e:
                return {"success": False, "error": f"设备连接失败: {e}", "scenario_id": scenario_id}

        self.results = []
        success = True
        scenario_context: Dict[str, str] = {}
        if env_id:
            global_vars = session.exec(
                select(GlobalVariable).where(GlobalVariable.env_id == env_id)
            ).all()
            for gv in global_vars:
                scenario_context[gv.key] = gv.value

        for case_index, scenario_step in enumerate(steps):
            if self.abort_event and self.abort_event.is_set():
                logger.warning("收到中止信号，停止场景执行")
                success = False
                yield {
                    "type": "scenario_abort",
                    "case_index": case_index,
                    "total_cases": total_cases,
                }
                break

            step_name = scenario_step.alias or f"Step {case_index + 1}"
            case = session.get(TestCase, scenario_step.case_id)
            if not case:
                logger.warning(f"关联的用例不存在: {scenario_step.case_id}，跳过")
                raw_item = {
                    "step_order": scenario_step.order,
                    "scenario_step_id": scenario_step.id,
                    "alias": scenario_step.alias,
                    "case_name": "Unknown",
                    "case_id": scenario_step.case_id,
                    "error": f"Case not found: {scenario_step.case_id}",
                }
                self.results.append(raw_item)
                success = False
                yield {
                    "type": "case_missing",
                    "case_index": case_index,
                    "total_cases": total_cases,
                    "scenario_step": scenario_step,
                    "step_name": step_name,
                    "case_name": "Unknown",
                    "case_id": scenario_step.case_id,
                    "error": raw_item["error"],
                    "raw_result": raw_item,
                }
                continue

            logger.info(f"--> 执行步骤 {scenario_step.order}: {step_name}")
            yield {
                "type": "case_start",
                "case_index": case_index,
                "total_cases": total_cases,
                "scenario_step": scenario_step,
                "case": case,
                "step_name": step_name,
                "case_name": case.name,
            }

            try:
                case_iter = self.runner.iter_case_execution(case, extra_variables=scenario_context)
                while True:
                    try:
                        case_event = next(case_iter)
                    except StopIteration as stop:
                        case_result = stop.value or {
                            "case_id": case.id,
                            "success": True,
                            "steps": [],
                            "exported_variables": dict(scenario_context),
                        }
                        break

                    yield {
                        "type": "step_result",
                        "case_index": case_index,
                        "total_cases": total_cases,
                        "scenario_step": scenario_step,
                        "case": case,
                        "step_name": step_name,
                        "case_name": case.name,
                        **case_event,
                    }

                raw_item = {
                    "step_order": scenario_step.order,
                    "scenario_step_id": scenario_step.id,
                    "alias": scenario_step.alias,
                    "case_name": case.name,
                    "result": case_result,
                }
                self.results.append(raw_item)

                exported_variables = case_result.get("exported_variables", {})
                if isinstance(exported_variables, dict):
                    scenario_context.update(exported_variables)

                if not case_result.get("success"):
                    logger.error(f"步骤 {scenario_step.order} 执行失败")
                    try:
                        if self.runner.d:
                            image = self.runner.d.screenshot()
                            case_result["last_error_screenshot"] = image
                    except Exception as e:
                        logger.error(f"Failed to capture screenshot: {e}")

                yield {
                    "type": "case_complete",
                    "case_index": case_index,
                    "total_cases": total_cases,
                    "scenario_step": scenario_step,
                    "case": case,
                    "step_name": step_name,
                    "case_name": case.name,
                    "case_result": case_result,
                    "raw_result": raw_item,
                    "scenario_context": dict(scenario_context),
                }

                if not case_result.get("success"):
                    strategy = self._resolve_case_failure_strategy(case_result)
                    logger.info(f"容错策略分析: 采用 {strategy}")
                    if strategy == "CONTINUE":
                        logger.warning("由于容错策略为 CONTINUE，场景标记为失败，但继续执行下游。")
                        success = False
                        continue

                    logger.error("容错策略为 ABORT，立即中断场景执行。")
                    success = False
                    break
            except Exception as e:
                logger.error(f"步骤 {scenario_step.order} 执行异常: {e}")
                raw_item = {
                    "step_order": scenario_step.order,
                    "scenario_step_id": scenario_step.id,
                    "alias": scenario_step.alias,
                    "case_name": case.name,
                    "case_id": case.id,
                    "error": str(e),
                }
                self.results.append(raw_item)
                success = False
                yield {
                    "type": "case_exception",
                    "case_index": case_index,
                    "total_cases": total_cases,
                    "scenario_step": scenario_step,
                    "case": case,
                    "step_name": step_name,
                    "case_name": case.name,
                    "error": str(e),
                    "raw_result": raw_item,
                }

        return {
            "scenario_id": scenario.id,
            "scenario_name": scenario.name,
            "success": success,
            "results": list(self.results),
        }

    def run_scenario(self, scenario_id: int, session: Any, env_id: Optional[int] = None) -> Dict[str, Any]:
        """执行完整场景（同步模式，供后台线程调用）。"""
        scenario_iter = self.iter_scenario_execution(scenario_id, session, env_id=env_id)
        while True:
            try:
                next(scenario_iter)
            except StopIteration as stop:
                return stop.value
