"""
IOSDriver — 基于 facebook-wda 的 iOS 端驱动

继承 BaseDriver 抽象基类，封装 wda (WebDriverAgent) 的 API 调用。
特殊处理：物理坐标 ÷ scale = 逻辑坐标，再传给 WDA。

按内聚性拆分为 backend/drivers/ios/ 包内的 Mixin 组合：
- IOSDriverSupportMixin: 日志与诊断辅助
- IOSLocatorMixin: 定位与候选回退、弹窗点击链路
- IOSVisionMixin: 截图 / OCR / 图像匹配
- IOSAppControlMixin: 应用控制、back/home 与页面签名

本文件保留 IOSDriver 类定义（`from backend.drivers.ios_driver import IOSDriver`
导入路径不变），以及 BaseDriver 公开接口方法的实现。
"""
import logging
import threading
import time
from typing import Any, Dict, List, Optional

import wda

from .base_driver import BaseDriver
from .ios.app_control import IOSAppControlMixin
from .ios.locator import IOSLocatorMixin
from .ios.support import IOSDriverSupportMixin
from .ios.vision import IOSVisionMixin
from backend.execution_errors import (
    E2001_ELEMENT_NOT_FOUND,
    E2002_WAIT_TIMEOUT,
    E2003_ASSERT_TEXT_FAILED,
    E2004_ASSERT_IMAGE_FAILED,
    E2006_OCR_NO_RESULT,
    E2007_INPUT_FAILED,
    E2009_CLICK_NO_EFFECT,
    ExecutionStepAssertionError,
    ExecutionStepError,
    ExecutionStepRuntimeError,
)
from backend.utils import evaluate_page_text_assertion
from backend.ocr_service import start_ocr_prewarm

logger = logging.getLogger(__name__)


class IOSDriver(
    IOSLocatorMixin,
    IOSVisionMixin,
    IOSAppControlMixin,
    IOSDriverSupportMixin,
    BaseDriver,
):
    """
    iOS 设备驱动，使用 facebook-wda 库通过 WebDriverAgent 操控设备。

    特殊属性:
        scale (float): 屏幕物理像素与逻辑点之比（如 Retina 为 2 或 3）。
            `click_by_coordinates` 会自动将物理坐标转为逻辑坐标。

    支持的定位策略 (by):
        - "label"               : 按元素 label 属性定位
        - "name"                : 按元素 name 属性定位
        - "id" / "accessibilityId" : 按 accessibility identifier 定位
        - "xpath"               : 通过 XPath 表达式定位
        - "class_name"          : 按元素类名定位
        - "predicate"           : 使用 NSPredicate 表达式定位
    """
    _ocr_engine: Any = None
    _ocr_prewarm_started = False
    _ocr_prewarm_lock = threading.Lock()
    _WDA_HTTP_TIMEOUT_SECONDS = 15.0
    _SCREENSHOT_TIMEOUT_SECONDS = 15.0
    _IMAGE_MATCH_THRESHOLD = 0.72
    _IMAGE_ASSERT_RECHECK_DELAY_SECONDS = 0.25
    _IMAGE_ASSERT_FAST_FAIL_SCORE = 0.9

    def __init__(self, device_id: str, wda_url: str = "http://localhost:8100") -> None:
        """
        初始化 iOS 驱动并连接 WDA。

        Args:
            device_id: 设备 UDID。
            wda_url: WebDriverAgent 服务地址（默认 localhost:8100）。
        """
        super().__init__(device_id)
        self.wda_url = wda_url
        # 收紧 WDA 全局 HTTP 超时，避免单次请求卡住 180s 无反馈。
        try:
            current_timeout = float(getattr(wda, "HTTP_TIMEOUT", 180.0))
            target_timeout = float(self._WDA_HTTP_TIMEOUT_SECONDS)
            if current_timeout != target_timeout:
                setattr(wda, "HTTP_TIMEOUT", target_timeout)
                logger.info(
                    "iOS WDA HTTP timeout adjusted: %.1fs -> %.1fs",
                    current_timeout,
                    target_timeout,
                )
        except Exception as exc:
            logger.warning("iOS WDA HTTP timeout adjust failed: %s", exc)

        try:
            self.client: wda.Client = wda.Client(wda_url)
            # 获取屏幕缩放比，用于物理坐标 → 逻辑坐标转换
            self.scale: float = self.client.scale
            status = self.client.status()
            logger.info(
                "iOS 设备已连接: UDID=%s, WDA=%s, scale=%.1f",
                device_id,
                status.get("build", {}).get("version", "?"),
                self.scale,
            )
            self._ensure_ocr_prewarm_started()
        except Exception as exc:
            logger.error("iOS 设备连接失败 [%s]: %s", device_id, exc)
            raise ConnectionError(f"iOS WDA 连接失败: {exc}") from exc

    @classmethod
    def _ensure_ocr_prewarm_started(cls) -> None:
        """启动 OCR 预热（使用全局服务）"""
        start_ocr_prewarm(use_angle_cls=False, lang="ch")

    @classmethod
    def _prewarm_ocr_engine_worker(cls) -> None:
        """预热工作线程（已由 OCRService 处理，保留空实现以兼容）"""
        pass

    # ------------------------------------------------------------------ #
    #  BaseDriver 接口实现
    # ------------------------------------------------------------------ #

    def click(self, selector: str, by: str) -> None:
        """点击元素。"""
        started_at = time.time()
        logger.info("iOS.click start: selector=%s by=%s udid=%s", selector, by, getattr(self, "device_id", "?"))
        by_lower = str(by or "").lower()
        normalized_by = self._normalize_locator_by(by_lower)
        popup_semantic_by = {
            "text",
            "label",
            "name",
            "description",
            "desc",
            "id",
            "accessibilityid",
            "accessibility_id",
        }
        require_click_effect = (
            by_lower in popup_semantic_by and self._is_confirm_action_text(selector)
        )

        # 弹窗场景优先尝试弹窗按钮，避免点到被遮挡的同名元素导致“假成功”。
        if by_lower in popup_semantic_by and self._has_alert_or_sheet():
            pre_errors: List[str] = []
            if self._tap_alert_button(
                selector=selector,
                timeout=2,
                errors=pre_errors,
                require_page_change=require_click_effect,
            ):
                self._log_action_success(
                    "click",
                    started_at,
                    selector=selector,
                    by=by,
                    strategy="alert-button-priority",
                    attempts=1,
                )
                return

        try:
            before_signature = ""
            had_alert_before = False
            if require_click_effect:
                had_alert_before = self._has_alert_or_sheet(timeout=0.15, reuse_window=0.0)
                before_signature = self._capture_page_signature(
                    mode="quick",
                    screenshot_timeout=1.0,
                )
            el = self._get_element(selector, by, timeout=5)
            el.tap()
            if require_click_effect:
                if not self._wait_confirm_click_effect(
                    selector=str(selector or "").strip(),
                    before_signature=before_signature,
                    timeout=0.8,
                    interval=0.12,
                    mode="quick",
                    screenshot_timeout=1.0,
                    selector_by=normalized_by,
                    had_alert_before=had_alert_before,
                ):
                    raise ExecutionStepRuntimeError(
                        E2009_CLICK_NO_EFFECT,
                        "tap-no-effect",
                        context={"selector": str(selector or "").strip(), "by": normalized_by},
                    )
            self._log_action_success(
                "click",
                started_at,
                selector=selector,
                by=by,
                strategy="primary",
                attempts=1,
            )
            return
        except Exception as primary_exc:
            fallback_errors: List[str] = []
            if by_lower in popup_semantic_by:
                if self._tap_alert_button(
                    selector=selector,
                    timeout=2,
                    errors=fallback_errors,
                    require_page_change=require_click_effect,
                ):
                    self._log_action_success(
                        "click",
                        started_at,
                        selector=selector,
                        by=by,
                        strategy="alert-button-fallback",
                        attempts=len(fallback_errors) + 2,
                    )
                    return

            if by_lower in popup_semantic_by:
                if self._tap_by_ocr_text(
                    selector=selector,
                    errors=fallback_errors,
                    require_page_change=require_click_effect,
                    prefer_popup_crop=require_click_effect,
                ):
                    self._log_action_success(
                        "click",
                        started_at,
                        selector=selector,
                        by=by,
                        strategy="ocr-fallback",
                        attempts=len(fallback_errors) + 2,
                    )
                    return

            detail = "; ".join(fallback_errors) if fallback_errors else "no-fallback-hit"
            # 主定位异常已结构化时继承其错误码，否则按元素未找到归类。
            primary_code = (
                primary_exc.code
                if isinstance(primary_exc, ExecutionStepError)
                else E2001_ELEMENT_NOT_FOUND
            )
            final_exc = ExecutionStepRuntimeError(
                primary_code,
                f"iOS.click 失败: selector={selector!r}, by={by!r}, error={primary_exc}, fallback={detail}",
                context={"selector": selector, "by": by},
            )
            self._log_action_failure(
                "click",
                started_at,
                final_exc,
                selector=selector,
                by=by,
                fallback=detail,
                attempts=len(fallback_errors) + 1,
            )
            raise final_exc from primary_exc

    def input(self, selector: str, by: str, text: str) -> None:
        """
        向元素输入文本。

        流程：定位 → 点击聚焦 → 清空 → 逐字输入。
        """
        started_at = time.time()
        logger.info("iOS.input start: selector=%s by=%s text_len=%s", selector, by, len(str(text or "")))
        try:
            el = self._get_element(selector, by, timeout=5)
            el.tap()
            try:
                el.clear_text()
            except Exception:
                # 某些控件不支持 clear_text，走 set_text 覆盖
                pass
            el.set_text(text)
            self._log_action_success(
                "input",
                started_at,
                selector=selector,
                by=by,
                text_len=len(str(text or "")),
            )
        except Exception as exc:
            self._log_action_failure(
                "input",
                started_at,
                exc,
                selector=selector,
                by=by,
                text_len=len(str(text or "")),
            )
            raise

    def input_focused(self, text: str) -> None:
        """
        向当前焦点输入框输入文本（无定位器模式）。
        """
        started_at = time.time()
        logger.info("iOS.input_focused start: text_len=%s", len(str(text or "")))
        session = self.client.session()
        errors = []

        for method_name in ("send_keys", "set_text"):
            method = getattr(session, method_name, None)
            if not callable(method):
                continue
            try:
                method(text)
                self._log_action_success(
                    "input_focused",
                    started_at,
                    text_len=len(str(text or "")),
                    strategy=f"session.{method_name}",
                    attempts=len(errors) + 1,
                )
                return
            except Exception as exc:
                errors.append(f"session.{method_name}: {exc}")

        focus_predicates = [
            "hasKeyboardFocus == 1",
            "hasFocus == 1",
            "type == 'XCUIElementTypeTextField' AND hasKeyboardFocus == 1",
            "type == 'XCUIElementTypeSecureTextField' AND hasKeyboardFocus == 1",
            "type == 'XCUIElementTypeTextView' AND hasKeyboardFocus == 1",
        ]
        for predicate in focus_predicates:
            try:
                focused_selector = session(predicate=predicate)
                if not self._wait_selector(focused_selector, timeout=1):
                    continue
                focused = focused_selector.get(timeout=1, raise_error=True)
                try:
                    focused.tap()
                except Exception:
                    pass
                try:
                    focused.clear_text()
                except Exception:
                    pass
                focused.set_text(text)
                self._log_action_success(
                    "input_focused",
                    started_at,
                    text_len=len(str(text or "")),
                    strategy=f"predicate:{predicate}",
                    attempts=len(errors) + 1,
                )
                return
            except Exception as exc:
                errors.append(f"predicate={predicate!r}: {exc}")

        detail = "; ".join(errors) if errors else "unknown"
        final_exc = ExecutionStepRuntimeError(
            E2007_INPUT_FAILED,
            f"iOS.input_focused 执行失败: {detail}",
            context={"text_len": len(str(text or ""))},
        )
        self._log_action_failure(
            "input_focused",
            started_at,
            final_exc,
            text_len=len(str(text or "")),
            attempts=len(errors),
            detail=detail,
        )
        raise final_exc

    def screenshot(self) -> bytes:
        """截取当前屏幕，返回 PNG 字节流。"""
        return self._capture_screenshot_bytes(timeout=self._SCREENSHOT_TIMEOUT_SECONDS)

    def click_by_coordinates(self, x: float, y: float) -> None:
        """
        按物理坐标点击屏幕。

        ⚠️ 核心逻辑：WDA 使用逻辑坐标（points），传入的物理像素坐标
        必须先除以 self.scale 转换为逻辑坐标后才能正确点击。

        Args:
            x: 物理像素 X 坐标。
            y: 物理像素 Y 坐标。
        """
        started_at = time.time()
        logical_x = x / self.scale
        logical_y = y / self.scale
        logger.info(
            "iOS.click_by_coordinates: 物理(%.1f, %.1f) → 逻辑(%.1f, %.1f) [scale=%.1f]",
            x, y, logical_x, logical_y, self.scale,
        )
        try:
            self.client.session().tap(logical_x, logical_y)
            self._log_action_success(
                "click_by_coordinates",
                started_at,
                x=round(x, 1),
                y=round(y, 1),
                logical_x=round(logical_x, 1),
                logical_y=round(logical_y, 1),
                scale=getattr(self, "scale", None),
            )
        except Exception as exc:
            self._log_action_failure(
                "click_by_coordinates",
                started_at,
                exc,
                x=round(x, 1),
                y=round(y, 1),
                logical_x=round(logical_x, 1),
                logical_y=round(logical_y, 1),
                scale=getattr(self, "scale", None),
            )
            raise

    def wait_until_exists(self, selector: str, by: str, timeout: int = 10) -> None:
        started_at = time.time()
        logger.info(
            "iOS.wait_until_exists start: selector=%s by=%s timeout=%s",
            selector, by, timeout,
        )
        try:
            self._resolve_selector(selector, by, timeout=timeout)
            self._log_action_success(
                "wait_until_exists",
                started_at,
                selector=selector,
                by=by,
                timeout=timeout,
            )
        except Exception as exc:
            self._log_action_failure(
                "wait_until_exists",
                started_at,
                exc,
                selector=selector,
                by=by,
                timeout=timeout,
            )
            if isinstance(exc, ExecutionStepError) and exc.code == E2001_ELEMENT_NOT_FOUND:
                # 等待场景语义为超时未出现，重标记为 E2002（消息保持不变）。
                raise ExecutionStepRuntimeError(
                    E2002_WAIT_TIMEOUT,
                    str(exc),
                    context={**exc.context, "timeout": timeout},
                ) from exc
            raise

    def assert_text(
        self,
        selector: str = "",
        by: str = "",
        expected_text: str = "",
        match_mode: str = "contains",
    ) -> None:
        started_at = time.time()
        normalized_mode = "not_contains" if str(match_mode or "").strip().lower() == "not_contains" else "contains"
        logger.info(
            "iOS.assert_text start: expected=%s match_mode=%s",
            expected_text,
            normalized_mode,
        )
        try:
            expected = str(expected_text or "")
            if not expected.strip():
                raise ValueError("assert_text expected_text 不能为空")

            candidates = self._collect_page_text_candidates()
            evaluation = evaluate_page_text_assertion(candidates, expected)
            matched = bool(evaluation.get("matched"))
            preview = evaluation.get("preview") or candidates[:5]
            match_source = evaluation.get("match_source") or ""

            if normalized_mode == "contains" and matched:
                self._log_action_success(
                    "assert_text",
                    started_at,
                    expected_text=expected,
                    match_mode=normalized_mode,
                    candidates=len(candidates),
                    match_source=match_source,
                )
                return
            if normalized_mode == "not_contains" and not matched:
                self._log_action_success(
                    "assert_text",
                    started_at,
                    expected_text=expected,
                    match_mode=normalized_mode,
                    candidates=len(candidates),
                )
                return

            final_exc = ExecutionStepAssertionError(
                E2003_ASSERT_TEXT_FAILED,
                f"断言失败: 期望页面{'不包含' if normalized_mode == 'not_contains' else '包含'} {expected!r}, 实际={preview!r}",
                context={"expected_text": expected, "match_mode": normalized_mode},
            )
            self._log_action_failure(
                "assert_text",
                started_at,
                final_exc,
                expected_text=expected,
                match_mode=normalized_mode,
                candidates=len(candidates),
            )
            raise final_exc
        except Exception as exc:
            if isinstance(exc, AssertionError):
                raise
            self._log_action_failure(
                "assert_text",
                started_at,
                exc,
                expected_text=expected_text,
                match_mode=normalized_mode,
            )
            raise

    def swipe(self, direction: str) -> None:
        started_at = time.time()
        direction = (direction or "up").lower()
        logger.info("iOS.swipe start: direction=%s", direction)
        try:
            if direction == "up":
                self.client.swipe_up()
                self._log_action_success("swipe", started_at, direction=direction)
                return
            if direction == "down":
                self.client.swipe_down()
                self._log_action_success("swipe", started_at, direction=direction)
                return
            if direction == "left":
                self.client.swipe_left()
                self._log_action_success("swipe", started_at, direction=direction)
                return
            if direction == "right":
                self.client.swipe_right()
                self._log_action_success("swipe", started_at, direction=direction)
                return
            final_exc = ValueError(f"不支持的滑动方向: {direction}")
            self._log_action_failure("swipe", started_at, final_exc, direction=direction)
            raise final_exc
        except Exception as exc:
            if isinstance(exc, ValueError) and "不支持的滑动方向" in str(exc):
                raise
            self._log_action_failure("swipe", started_at, exc, direction=direction)
            raise

    def click_image(self, image_path: str) -> None:
        started_at = time.time()
        target = ""
        try:
            target = self._resolve_template_image_path(image_path, action_name="click_image")
            center_x, center_y, score = self._locate_image_target(
                image_path=target,
                threshold=self._IMAGE_MATCH_THRESHOLD,
                action_name="click_image",
            )
            logger.info(
                "iOS.click_image: path=%s, score=%.4f, point=(%.1f, %.1f)",
                target,
                score,
                center_x,
                center_y,
            )
            self.click_by_coordinates(center_x, center_y)
            self._log_action_success(
                "click_image",
                started_at,
                image_path=target,
                score=round(score, 4),
                x=round(center_x, 1),
                y=round(center_y, 1),
            )
        except Exception as exc:
            self._log_action_failure(
                "click_image",
                started_at,
                exc,
                image_path=target,
            )
            raise

    def assert_image(self, image_path: str, match_mode: str = "exists") -> None:
        started_at = time.time()
        target = ""
        normalized_mode = "not_exists" if str(match_mode or "").strip().lower() == "not_exists" else "exists"
        logger.info(
            "iOS.assert_image start: path=%s match_mode=%s",
            image_path,
            normalized_mode,
        )
        try:
            target = self._resolve_template_image_path(image_path, action_name="assert_image")

            if normalized_mode == "exists":
                center_x, center_y, score = self._locate_image_target(
                    image_path=target,
                    threshold=self._IMAGE_MATCH_THRESHOLD,
                    action_name="assert_image",
                )
                self._log_action_success(
                    "assert_image",
                    started_at,
                    image_path=target,
                    match_mode=normalized_mode,
                    score=round(score, 4),
                    x=round(center_x, 1),
                    y=round(center_y, 1),
                )
                return

            first_match = self._try_locate_image_target(
                image_path=target,
                threshold=self._IMAGE_MATCH_THRESHOLD,
                action_name="assert_image",
            )
            if first_match is None:
                self._log_action_success(
                    "assert_image",
                    started_at,
                    image_path=target,
                    match_mode=normalized_mode,
                )
                return

            if first_match is not None:
                center_x, center_y, score = first_match
                if score >= self._IMAGE_ASSERT_FAST_FAIL_SCORE:
                    raise ExecutionStepAssertionError(
                        E2004_ASSERT_IMAGE_FAILED,
                        f"断言失败: 期望页面不存在图像 {target!r}，但已高置信度匹配到目标 "
                        f"(score={score:.4f}, x={center_x:.1f}, y={center_y:.1f})",
                        context={"image_path": target, "match_mode": normalized_mode},
                    )

            time.sleep(self._IMAGE_ASSERT_RECHECK_DELAY_SECONDS)
            second_match = self._try_locate_image_target(
                image_path=target,
                threshold=self._IMAGE_MATCH_THRESHOLD,
                action_name="assert_image",
            )
            confirmed_match = second_match if second_match is not None else None
            if confirmed_match is not None:
                center_x, center_y, score = confirmed_match
                raise ExecutionStepAssertionError(
                    E2004_ASSERT_IMAGE_FAILED,
                    f"断言失败: 期望页面不存在图像 {target!r}，但仍匹配到目标 "
                    f"(score={score:.4f}, x={center_x:.1f}, y={center_y:.1f})",
                    context={"image_path": target, "match_mode": normalized_mode},
                )

            self._log_action_success(
                "assert_image",
                started_at,
                image_path=target,
                match_mode=normalized_mode,
            )
        except Exception as exc:
            self._log_action_failure(
                "assert_image",
                started_at,
                exc,
                image_path=target or image_path,
                match_mode=normalized_mode,
            )
            raise

    def extract_by_ocr(self, region: str, extract_rule: Optional[Dict[str, Any]] = None) -> str:
        started_at = time.time()
        if not region:
            final_exc = ValueError("extract_by_ocr 需要 region")
            self._log_action_failure("extract_by_ocr", started_at, final_exc)
            raise final_exc
        try:
            x1, y1, x2, y2 = self._parse_region(region)
            screenshot = self.screenshot()
            screen_bgr = self._decode_png_to_bgr(screenshot, source="screenshot")
            height, width = screen_bgr.shape[:2]

            if x2 <= 1 and y2 <= 1:
                rx1, ry1, rx2, ry2 = int(x1 * width), int(y1 * height), int(x2 * width), int(y2 * height)
            else:
                rx1, ry1, rx2, ry2 = int(x1), int(y1), int(x2), int(y2)

            rx1 = max(0, min(rx1, width))
            ry1 = max(0, min(ry1, height))
            rx2 = max(0, min(rx2, width))
            ry2 = max(0, min(ry2, height))
            if rx2 <= rx1 or ry2 <= ry1:
                raise ValueError(f"extract_by_ocr 区域无效: [{rx1},{ry1},{rx2},{ry2}]")

            crop = screen_bgr[ry1:ry2, rx1:rx2]
            raw_text = self._extract_text_from_image(crop)
            if not raw_text:
                final_exc = ExecutionStepRuntimeError(
                    E2006_OCR_NO_RESULT,
                    "extract_by_ocr 未识别到文本",
                    context={"region": region, "crop": f"[{rx1},{ry1},{rx2},{ry2}]"},
                )
                self._log_action_failure(
                    "extract_by_ocr",
                    started_at,
                    final_exc,
                    region=region,
                    crop=f"[{rx1},{ry1},{rx2},{ry2}]",
                )
                raise final_exc
            extracted = self._apply_extract_rule(raw_text, extract_rule or {})
            self._log_action_success(
                "extract_by_ocr",
                started_at,
                region=region,
                crop=f"[{rx1},{ry1},{rx2},{ry2}]",
                raw_len=len(raw_text),
                extracted_len=len(str(extracted or "")),
            )
            return extracted
        except Exception as exc:
            if isinstance(exc, RuntimeError) and "未识别到文本" in str(exc):
                raise
            self._log_action_failure(
                "extract_by_ocr",
                started_at,
                exc,
                region=region,
            )
            raise

    def disconnect(self) -> None:
        """断开 WDA 连接。"""
        started_at = time.time()
        try:
            self.client.session().close()
            self._log_action_success("disconnect", started_at)
        except Exception as exc:
            self._log_action_failure("disconnect", started_at, exc)
        finally:
            super().disconnect()

    def health_check(self) -> bool:
        """快速探测 WDA 会话是否仍可用（供连接池复用前调用）。"""
        try:
            return bool(self.client.status())
        except Exception as exc:
            logger.info("iOS 驱动健康检查失败 [%s]: %s", self.device_id, exc)
            return False
