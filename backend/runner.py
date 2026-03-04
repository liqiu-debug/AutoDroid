"""
TestRunner - 测试用例执行引擎

负责连接 Android 设备、执行测试步骤、处理重试逻辑和变量替换。
支持的动作类型: click, input, wait_until_exists, scroll_to, assert_text, click_image
"""
import os
import time
import logging
import threading
import re
from typing import Dict, Any, Optional, Tuple, List

import uiautomator2 as u2
import numpy as np
import cv2
from paddleocr import PaddleOCR

from .schemas import Step, ActionType, SelectorType, Variable

logger = logging.getLogger(__name__)

logger.info("🤖 正在预加载 PaddleOCR 模型 (首次加载可能需要几秒)...")
ocr_engine = PaddleOCR(use_angle_cls=False, lang="ch")
logger.info("✅ PaddleOCR 模型加载完毕！")

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
        self._ocr_engine = None  # PaddleOCR lazy init

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

    def run_case(self, test_case, extra_variables: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
        """
        执行完整测试用例（同步模式，供 REST API 调用）。
        
        Returns:
            包含执行结果的字典 {"case_id", "success", "steps"}
        """
        if not self.d:
            self.connect()

        variables_map = {v.key: v.value for v in test_case.variables}
        if extra_variables:
            for k, v in extra_variables.items():
                if k not in variables_map:
                    variables_map[k] = v

        results = []
        success = True

        logger.info(f"开始执行用例: {test_case.name} (ID: {test_case.id})")

        for step in test_case.steps:
            # 检查中止信号
            if self.abort_event and self.abort_event.is_set():
                logger.warning("收到中止信号，停止执行用例")
                results.append({"step": step.dict(), "success": False, "error": "已被用户中止", "duration": 0})
                success = False
                break

            step_result = self.execute_step(step, variables_map)
            results.append(step_result)
            if not step_result["success"]:
                strategy = getattr(step, "error_strategy", "ABORT")

                if strategy == "IGNORE":
                    step_result["is_warning"] = True
                    logger.warning(f"步骤失败，容错策略为 IGNORE，标记为 WARNING 继续执行。")
                elif strategy == "CONTINUE":
                    success = False
                    logger.warning(f"步骤失败，容错策略为 CONTINUE，标记失败但继续执行剩余步骤。")
                else:  # ABORT
                    success = False
                    logger.error(f"步骤失败: {step_result}")
                    break

        return {
            "case_id": test_case.id,
            "success": success,
            "steps": results,
            "exported_variables": variables_map
        }

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
        logger.warning(f"准备执行步骤: action={step.action}, selector={step.selector}, value={step.value}, variables={list(variables.keys())}")

        # 1. 变量替换
        try:
            target_selector = self._substitute_variables(step.selector, variables)
            target_value = self._substitute_variables(step.value, variables)
        except Exception as e:
            return {
                "step": step.dict(),
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
                    "step": step.dict(),
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
                    "step": step.dict(),
                    "success": True,
                    "duration": time.time() - start_time
                }
            except Exception as e:
                error_message = str(e)
                logger.warning(f"第 {attempt + 1}/{max_retries + 1} 次尝试失败: {e}")
                if attempt < max_retries:
                    time.sleep(retry_interval)
                else:
                    logger.error(f"所有重试均失败: {step}")

        return {
            "step": step.dict(),
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
        - input: 输入文本
        - wait_until_exists: 等待元素出现
        - scroll_to: 滚动到元素可见
        - assert_text: 断言元素包含指定文本
        """
        options = options or {}

        # ---- 图像匹配点击（无需先查找元素）----
        if action == ActionType.CLICK_IMAGE:
            image_path = os.path.join(os.path.dirname(__file__), "..", selector)
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
            logger.warning(f"OCR原始识别结果({value}): {raw_text}")
            extracted = self._apply_extract_rule(raw_text, options)
            logger.warning(f"OCR提取后结果({value}): {extracted}")
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

        # ---- 需要先查找元素的动作 ----
        el = self._find_element(selector, selector_type)
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

        elif action == ActionType.ASSERT_TEXT:
            if value is None:
                raise ValueError("assert_text 动作必须提供 value 参数")
            
            # 获取元素信息 (兼容 UiObject 和 XPath)
            try:
                info = el.info
                actual_text = info.get('text') or ""
                actual_desc = info.get('contentDescription') or ""
            except Exception:
                # 回退机制
                actual_text = el.get_text() or ""
                actual_desc = ""

            # 1. 检查当前节点
            if value in actual_text or value in actual_desc:
                logger.info(f"断言成功: '{value}' found in text/desc")
                return

            # 2. 如果当前节点是容器，尝试检查子节点 (仅针对非XPath定位的 UiObject)
            # XPath 对象通常不支持 child() 链式调用，或者 API 不同
            if selector_type != SelectorType.XPATH:
                try:
                    if el.child(textContains=value).exists(timeout=0.1):
                        logger.info(f"断言成功: '{value}' found in child(text)")
                        return
                    if el.child(descriptionContains=value).exists(timeout=0.1):
                        logger.info(f"断言成功: '{value}' found in child(desc)")
                        return
                except Exception as e:
                    logger.warning(f"子节点检查失败: {e}")

            raise AssertionError(f"断言失败: 期望包含 '{value}'，实际 text='{actual_text}', desc='{actual_desc}'")

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
            result = ocr_engine.ocr(img_arr, cls=False)
        except Exception as e:
            logger.warning(f"PaddleOCR 识别失败: {e}")
            return ""

        texts: List[str] = []
        if not result:
            return ""

        lines = result[0] if isinstance(result, list) and len(result) > 0 else []
        if lines is None:
            lines = []
        for line in lines:
            # 每一项通常为 [box, [text, score]]
            if not line or len(line) < 2:
                continue
            text_info = line[1]
            if isinstance(text_info, (list, tuple)) and len(text_info) >= 1:
                txt = str(text_info[0]).strip()
                if txt:
                    texts.append(txt)

        merged = "\n".join(texts).strip()
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
    - 全局上下文 (TODO)
    - 聚合报告
    """
    def __init__(self, device_serial: Optional[str] = None, abort_event: Optional[threading.Event] = None):
        self.device_serial = device_serial
        self.abort_event = abort_event
        self.runner = TestRunner(device_serial, abort_event=abort_event)
        self.results = []

    def run_scenario(self, scenario_id: int, session: Any, env_id: Optional[int] = None) -> Dict[str, Any]:
        """
        执行场景
        """
        from .models import TestScenario, ScenarioStep, TestCase, GlobalVariable
        from sqlmodel import select

        # 1. 获取场景信息
        scenario = session.get(TestScenario, scenario_id)
        if not scenario:
            raise ValueError(f"Scenario not found: {scenario_id}")

        # 2. 获取步骤 (按 order 排序)
        statement = select(ScenarioStep).where(ScenarioStep.scenario_id == scenario_id).order_by(ScenarioStep.order)
        steps = session.exec(statement).all()

        logger.info(f"开始执行场景: {scenario.name} (ID: {scenario.id}), 共 {len(steps)} 个步骤")

        # 3. 连接设备 (一次连接)
        try:
            self.runner.connect()
        except Exception as e:
            return {"success": False, "error": f"设备连接失败: {e}", "scenario_id": scenario_id}

        success = True
        scenario_context: Dict[str, str] = {}
        if env_id:
            global_vars = session.exec(
                select(GlobalVariable).where(GlobalVariable.env_id == env_id)
            ).all()
            for gv in global_vars:
                scenario_context[gv.key] = gv.value
        
        # 4. 循环执行
        for step in steps:
            # 检查中止信号
            if self.abort_event and self.abort_event.is_set():
                logger.warning("收到中止信号，停止场景执行")
                success = False
                break

            # 获取 TestCase
            case = session.get(TestCase, step.case_id)
            if not case:
                logger.warning(f"关联的用例不存在: {step.case_id}，跳过")
                self.results.append({
                    "step_order": step.order,
                    "case_id": step.case_id,
                    "success": False,
                    "error": "Case not found"
                })
                success = False
                continue

            logger.info(f"--> 执行步骤 {step.order}: {step.alias or case.name}")

            # 复用 self.runner.d 连接执行
            # 由于 run_case 内部也会以此检查 connect，这里复用实例即可
            try:
                # 注意：runner.run_case 设计为无状态，传入 test_case 即可
                # scenario_context 在场景内跨用例接力传递动态变量（如 OCR 提取结果）
                case_result = self.runner.run_case(case, extra_variables=scenario_context)
                
                # 记录结果
                self.results.append({
                    "step_order": step.order,
                    "scenario_step_id": step.id,
                    "alias": step.alias,
                    "case_name": case.name,
                    "result": case_result
                })
                new_vars = case_result.get("exported_variables", {})
                if isinstance(new_vars, dict):
                    scenario_context.update(new_vars)

                if not case_result["success"]:
                    logger.error(f"步骤 {step.order} 执行失败")
                    
                    # Capture screenshot on failure
                    try:
                        if self.runner.d:
                            image = self.runner.d.screenshot()
                            case_result["last_error_screenshot"] = image
                    except Exception as e:
                        logger.error(f"Failed to capture screenshot: {e}")
                        
                    # 查找真正导致失败的步骤（跳过 IGNORE 产生的 WARNING）
                    failed_step_data = None
                    for r in reversed(case_result.get("steps", [])):
                        if not r.get("success") and not r.get("is_warning"):
                            failed_step_data = r.get("step")
                            break
                    
                    strategy = "ABORT"
                    if failed_step_data and "error_strategy" in failed_step_data:
                        strategy = failed_step_data["error_strategy"]
                    
                    logger.info(f"容错策略分析: 采用 {strategy}")
                    
                    if strategy == "CONTINUE":
                        logger.warning(f"由于容错策略为 CONTINUE，场景标记为失败，但继续执行下游。")
                        success = False
                    else: # ABORT
                        logger.error(f"容错策略为 ABORT，立即中断场景执行。")
                        success = False
                        break
                else:
                    # 用例成功，但检查是否包含 IGNORE 产生的 WARNING
                    has_warnings = any(r.get("is_warning") for r in case_result.get("steps", []))
                    if has_warnings:
                        case_result["is_warning"] = True

            except Exception as e:
                logger.error(f"步骤 {step.order} 执行异常: {e}")
                self.results.append({
                    "step_order": step.order,
                    "success": False,
                    "error": str(e)
                })
                success = False

        return {
            "scenario_id": scenario.id,
            "scenario_name": scenario.name,
            "success": success,
            "results": self.results
        }
