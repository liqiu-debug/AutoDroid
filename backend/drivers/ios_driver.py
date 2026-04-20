"""
IOSDriver — 基于 facebook-wda 的 iOS 端驱动

继承 BaseDriver 抽象基类，封装 wda (WebDriverAgent) 的 API 调用。
特殊处理：物理坐标 ÷ scale = 逻辑坐标，再传给 WDA。
"""
import base64
import hashlib
import io
import logging
import os
import re
import threading
import time
import xml.etree.ElementTree as ET
from typing import Any, Dict, List, Optional, Tuple

import wda

from .base_driver import BaseDriver
from backend.utils import evaluate_page_text_assertion
from backend.utils.ocr_compat import create_paddle_ocr_engine, extract_ocr_text, iter_ocr_text_items, run_paddle_ocr

logger = logging.getLogger(__name__)


class IOSDriver(BaseDriver):
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

    @staticmethod
    def _truncate_log_value(value: Any, max_len: int = 96) -> str:
        if value is None:
            return ""
        text = str(value).strip()
        if len(text) <= max_len:
            return text
        return f"{text[: max_len - 3]}..."

    @classmethod
    def _ensure_ocr_prewarm_started(cls) -> None:
        with cls._ocr_prewarm_lock:
            if cls._ocr_prewarm_started:
                return
            cls._ocr_prewarm_started = True
        thread = threading.Thread(
            target=cls._prewarm_ocr_engine_worker,
            name="ios-ocr-prewarm",
            daemon=True,
        )
        thread.start()

    @classmethod
    def _prewarm_ocr_engine_worker(cls) -> None:
        try:
            ocr_engine = cls._get_ocr_engine()
        except Exception as exc:
            logger.warning("iOS OCR engine prewarm skipped: %s", exc)
            return

        try:
            _, np = cls._load_opencv_numpy()
            blank = np.zeros((48, 192, 3), dtype=np.uint8)
            run_paddle_ocr(ocr_engine, blank, use_cls=False)
            logger.debug("iOS OCR engine warmup done")
        except Exception as exc:
            logger.warning("iOS OCR engine warmup partial failure: %s", exc)


    def _diag_common(self) -> Dict[str, Any]:
        return {
            "udid": getattr(self, "device_id", "?"),
            "wda": getattr(self, "wda_url", "?"),
        }

    @staticmethod
    def _ms(seconds: Any) -> float:
        try:
            return round(max(float(seconds or 0.0), 0.0) * 1000.0, 1)
        except Exception:
            return 0.0

    @staticmethod
    def _merge_timing_metrics(target: Dict[str, Any], source: Optional[Dict[str, Any]]) -> Dict[str, Any]:
        if not isinstance(source, dict):
            return target
        for key, value in source.items():
            if key.endswith("_ms"):
                try:
                    target[key] = round(float(target.get(key) or 0.0) + float(value or 0.0), 1)
                except Exception:
                    continue
            elif key == "ocr_cache_hit":
                target[key] = bool(target.get(key)) or bool(value)
            elif value not in (None, "", False):
                target[key] = value
        return target

    @staticmethod
    def _metric_float(metrics: Optional[Dict[str, Any]], key: str) -> float:
        if not isinstance(metrics, dict):
            return 0.0
        try:
            return round(float(metrics.get(key) or 0.0), 1)
        except Exception:
            return 0.0

    def _log_click_plan_timing(self, stage: str, timing_metrics: Optional[Dict[str, Any]]) -> None:
        metrics = timing_metrics if isinstance(timing_metrics, dict) else {}
        logger.info(
            "iOS.click_plan timing: stage=%s locator_ms=%.1f popup_probe_ms=%.1f alert_ms=%.1f "
            "effect_sig_ms=%.1f effect_wait_ms=%.1f "
            "ocr_scope=%s ocr_cache_hit=%s ocr_screenshot_ms=%.1f ocr_decode_ms=%.1f "
            "ocr_engine_ms=%.1f ocr_run_ms=%.1f ocr_parse_ms=%.1f ocr_total_ms=%.1f",
            stage,
            self._metric_float(metrics, "locator_ms"),
            self._metric_float(metrics, "popup_probe_ms"),
            self._metric_float(metrics, "alert_ms"),
            self._metric_float(metrics, "effect_sig_ms"),
            self._metric_float(metrics, "effect_wait_ms"),
            str(metrics.get("ocr_scope") or "none"),
            bool(metrics.get("ocr_cache_hit")),
            self._metric_float(metrics, "ocr_screenshot_ms"),
            self._metric_float(metrics, "ocr_decode_ms"),
            self._metric_float(metrics, "ocr_engine_ms"),
            self._metric_float(metrics, "ocr_run_ms"),
            self._metric_float(metrics, "ocr_parse_ms"),
            self._metric_float(metrics, "ocr_total_ms"),
        )

    def _classify_exception(self, exc: Exception) -> str:
        text = str(exc or "").strip().lower()
        exc_name = exc.__class__.__name__.lower()

        if any(token in text for token in ("timeout", "timed out", "request timeout", "deadline")):
            return "TIMEOUT"
        if any(token in text for token in ("connection refused", "failed to establish", "connection aborted", "connection reset", "wda 连接失败")):
            return "CONNECTION"
        if any(token in text for token in ("元素未找到", "not found", "no such element", "attempts=")):
            return "ELEMENT_NOT_FOUND"
        if any(token in text for token in ("page-unchanged", "tap-no-effect", "页面未变化")):
            return "NO_EFFECT"
        if "assert" in exc_name or "断言失败" in text:
            return "ASSERTION"
        if "ocr" in text or "未识别到文本" in text:
            return "OCR"
        if any(token in text for token in ("activate", "launch", "terminate", "app_state", "bundleid", "bundle id")):
            return "APP_CONTROL"
        return "UNKNOWN"

    def _log_action_success(self, action: str, started_at: float, **fields: Any) -> None:
        payload = {**self._diag_common(), **fields}
        field_text = " ".join(
            f"{key}={self._truncate_log_value(value)!r}"
            for key, value in payload.items()
            if value not in (None, "")
        )
        logger.info(
            "iOS.%s success: duration=%.3fs %s",
            action,
            max(time.time() - started_at, 0.0),
            field_text,
        )

    def _log_action_failure(self, action: str, started_at: float, exc: Exception, **fields: Any) -> None:
        payload = {**self._diag_common(), **fields}
        field_text = " ".join(
            f"{key}={self._truncate_log_value(value)!r}"
            for key, value in payload.items()
            if value not in (None, "")
        )
        logger.warning(
            "iOS.%s failed: duration=%.3fs category=%s %s error=%s",
            action,
            max(time.time() - started_at, 0.0),
            self._classify_exception(exc),
            field_text,
            self._truncate_log_value(exc),
        )

    # ------------------------------------------------------------------ #
    #  内部：根据 by 策略定位元素
    # ------------------------------------------------------------------ #

    @staticmethod
    def _escape_predicate_literal(value: str) -> str:
        return str(value or "").replace("\\", "\\\\").replace("'", "\\'")

    def _build_contains_predicate(self, selector: str) -> str:
        escaped = self._escape_predicate_literal(selector)
        return (
            f"(label CONTAINS[c] '{escaped}' OR "
            f"name CONTAINS[c] '{escaped}' OR "
            f"value CONTAINS[c] '{escaped}')"
        )

    def _build_alert_button_predicates(self, selector: str) -> List[str]:
        escaped = self._escape_predicate_literal(selector)
        exact = (
            "type == 'XCUIElementTypeButton' AND "
            f"(label == '{escaped}' OR name == '{escaped}' OR value == '{escaped}')"
        )
        contains = (
            "type == 'XCUIElementTypeButton' AND "
            f"(label CONTAINS[c] '{escaped}' OR name CONTAINS[c] '{escaped}' OR value CONTAINS[c] '{escaped}')"
        )
        return [exact, contains]

    def _probe_alert_or_sheet(
        self,
        timeout: float = 1.0,
        reuse_window: float = 0.25,
    ) -> Tuple[bool, Optional[Tuple[int, int, int, int]]]:
        wait_timeout = max(0.08, float(timeout or 0))
        now = time.time()
        cached = getattr(self, "_popup_probe_cache", None)
        if isinstance(cached, dict):
            cached_ts = float(cached.get("ts") or 0.0)
            cached_timeout = float(cached.get("timeout") or 0.0)
            if (now - cached_ts) <= max(0.05, float(reuse_window or 0)) and cached_timeout >= wait_timeout:
                return bool(cached.get("has_alert")), cached.get("bounds")

        session = self.client.session()
        has_alert = False
        bounds: Optional[Tuple[int, int, int, int]] = None
        for class_name in ("XCUIElementTypeAlert", "XCUIElementTypeSheet"):
            try:
                selector_obj = session(className=class_name)
                if not self._wait_selector(selector_obj, timeout=wait_timeout):
                    continue
                has_alert = True
                try:
                    popup = selector_obj.get(timeout=wait_timeout, raise_error=True)
                    bounds = self._normalize_crop_bounds(getattr(popup, "bounds", None))
                except Exception:
                    bounds = None
                break
            except Exception:
                continue

        setattr(
            self,
            "_popup_probe_cache",
            {
                "ts": now,
                "timeout": wait_timeout,
                "has_alert": has_alert,
                "bounds": bounds,
            },
        )
        return has_alert, bounds

    def _has_alert_or_sheet(self, timeout: float = 1.0, reuse_window: float = 0.25) -> bool:
        has_alert, _ = self._probe_alert_or_sheet(timeout=timeout, reuse_window=reuse_window)
        return has_alert

    def _is_selector_present(self, selector: str, by: str, timeout: float = 0.2) -> bool:
        selector_text = str(selector or "").strip()
        by_text = self._normalize_locator_by(by)
        if not selector_text or not by_text:
            return False
        try:
            selector_obj = self._build_selector(selector_text, by_text)
        except Exception:
            return False
        return self._wait_selector(selector_obj, timeout=max(0.05, float(timeout or 0.0)))

    def _is_alert_button_present(self, selector: str, timeout: float = 0.2) -> bool:
        text = str(selector or "").strip()
        if not text:
            return False
        wait_timeout = max(0.05, float(timeout or 0.0))
        for predicate in self._build_alert_button_predicates(text):
            try:
                selector_obj = self._build_selector(predicate, "predicate")
                if self._wait_selector(selector_obj, timeout=wait_timeout):
                    return True
            except Exception:
                continue
        return False

    def _wait_confirm_click_effect(
        self,
        selector: str,
        before_signature: str,
        timeout: float = 0.8,
        interval: float = 0.12,
        mode: str = "quick",
        screenshot_timeout: Optional[float] = None,
        selector_by: Optional[str] = None,
        had_alert_before: bool = False,
    ) -> bool:
        wait_timeout = max(0.2, float(timeout or 0.0))
        changed = False
        if before_signature:
            changed = self._wait_page_changed(
                before_signature,
                timeout=wait_timeout,
                interval=interval,
                mode=mode,
                screenshot_timeout=screenshot_timeout,
            )

        post_probe_timeout = min(0.35, max(0.1, wait_timeout * 0.45))
        alert_after = self._has_alert_or_sheet(timeout=post_probe_timeout, reuse_window=0.0)
        alert_button_after = self._is_alert_button_present(selector, timeout=post_probe_timeout)
        target_after = False
        if selector_by:
            target_after = self._is_selector_present(
                selector,
                selector_by,
                timeout=post_probe_timeout,
            )

        if had_alert_before:
            if not alert_after:
                return True
            if alert_button_after or target_after:
                return False
            return changed

        if changed:
            if alert_after and (alert_button_after or target_after):
                return False
            return True

        if selector_by and not target_after:
            return True
        return False

    def _build_selector(self, selector: str, by: str) -> Any:
        """
        根据 by 策略构建 WDA Selector。

        Args:
            selector: 定位值。
            by: 定位策略名称。

        Returns:
            wda.Selector 实例（由 session(...) 返回）。

        Raises:
            ValueError: 不支持的 by 策略。
        """
        by_lower = by.lower()
        session = self.client.session()

        if by_lower == "label":
            return session(label=selector)
        if by_lower == "name":
            return session(name=selector)
        if by_lower in ("id", "accessibilityid", "accessibility_id", "description", "desc"):
            return session(id=selector)
        if by_lower == "text":
            # iOS 没有统一 text 字段，优先按 label 再按 name 兜底。
            return session(label=selector)
        if by_lower == "xpath":
            return session(xpath=selector)
        if by_lower in ("class_name", "classname", "type"):
            return session(className=selector)
        if by_lower == "predicate":
            return session(predicate=selector)

        raise ValueError(f"iOS 不支持的定位策略: by={by!r}")

    @staticmethod
    def _normalize_locator_by(by: str) -> str:
        by_lower = str(by or "").strip().lower()
        if by_lower == "text":
            return "label"
        if by_lower in ("accessibilityid", "accessibility_id", "description", "desc"):
            return "id"
        if by_lower in ("classname", "type"):
            return "class_name"
        return by_lower

    def _wait_selector(self, selector_obj: Any, timeout: float = 5) -> bool:
        try:
            # wda.Selector.wait(timeout, raise_error=False)
            return bool(selector_obj.wait(timeout=timeout, raise_error=False))
        except Exception:
            pass

        try:
            return bool(selector_obj.exists)
        except Exception:
            return False

    def _build_fallback_locator_specs(self, selector: str, by: str) -> List[Tuple[str, str]]:
        by_lower = self._normalize_locator_by(by)
        if by_lower == "label":
            return [
                ("name", selector),
                ("predicate", self._build_contains_predicate(selector)),
            ]
        if by_lower == "name":
            return [
                ("label", selector),
                ("predicate", self._build_contains_predicate(selector)),
            ]
        if by_lower == "id":
            return [
                ("label", selector),
                ("name", selector),
                ("predicate", self._build_contains_predicate(selector)),
            ]
        return []

    def _build_click_locator_attempts(self, locator_candidates: List[Dict[str, Any]]) -> List[Dict[str, str]]:
        attempts: List[Dict[str, str]] = []
        seen = set()

        for item in locator_candidates or []:
            selector = str(item.get("selector") or "").strip()
            by = self._normalize_locator_by(item.get("by") or "")
            if not selector or not by:
                continue

            for attempt_by, attempt_selector in [(by, selector), *self._build_fallback_locator_specs(selector, by)]:
                normalized_by = self._normalize_locator_by(attempt_by)
                attempt_selector_text = str(attempt_selector or "").strip()
                if not attempt_selector_text or not normalized_by:
                    continue
                key = (attempt_selector_text, normalized_by)
                if key in seen:
                    continue
                seen.add(key)
                attempts.append(
                    {
                        "selector": attempt_selector_text,
                        "by": normalized_by,
                        "source_by": by,
                    }
                )

        return attempts

    def _build_fallback_selectors(self, selector: str, by: str) -> List[Tuple[str, Any]]:
        return [
            (fallback_by, self._build_selector(fallback_selector, fallback_by))
            for fallback_by, fallback_selector in self._build_fallback_locator_specs(selector, by)
        ]

    @staticmethod
    def _encode_png_base64(raw_png: bytes) -> str:
        return base64.b64encode(raw_png).decode("utf-8")

    def _capture_screenshot_bytes(self, timeout: Optional[float] = None) -> bytes:
        effective_timeout = max(
            0.2,
            float(timeout if timeout is not None else self._SCREENSHOT_TIMEOUT_SECONDS),
        )
        started_at = time.time()
        try:
            value = self.client.http.get("screenshot", timeout=effective_timeout).value
            raw_value = base64.b64decode(value)
        except Exception as exc:
            final_exc = RuntimeError(
                f"iOS.screenshot 请求失败或超时(>{effective_timeout:.1f}s): {exc}"
            )
            self._log_action_failure(
                "screenshot",
                started_at,
                final_exc,
                timeout=effective_timeout,
            )
            raise final_exc from exc

        png_header = b"\x89PNG\r\n\x1a\n"
        if not raw_value.startswith(png_header):
            final_exc = RuntimeError("iOS.screenshot 返回内容不是有效 PNG")
            self._log_action_failure(
                "screenshot",
                started_at,
                final_exc,
                timeout=effective_timeout,
            )
            raise final_exc
        self._log_action_success(
            "screenshot",
            started_at,
            timeout=effective_timeout,
            bytes=len(raw_value),
        )
        return raw_value

    def _capture_screenshot_cached(
        self,
        step_context: Optional[Dict[str, Any]] = None,
        timeout: Optional[float] = None,
    ) -> bytes:
        if not isinstance(step_context, dict):
            return self._capture_screenshot_bytes(timeout=timeout)

        cache = step_context.setdefault("cache", {})
        raw_png = cache.get("screenshot_png")
        if raw_png:
            return raw_png

        raw_png = self._capture_screenshot_bytes(timeout=timeout)
        cache["screenshot_png"] = raw_png
        artifacts = step_context.setdefault("artifacts", {})
        if raw_png and not artifacts.get("screenshot_base64"):
            artifacts["screenshot_base64"] = self._encode_png_base64(raw_png)
        return raw_png

    @staticmethod
    def _normalize_crop_bounds(
        bounds: Any,
        width: Optional[int] = None,
        height: Optional[int] = None,
    ) -> Optional[Tuple[int, int, int, int]]:
        if bounds is None:
            return None

        left = top = right = bottom = None
        if all(hasattr(bounds, attr) for attr in ("left", "top", "right", "bottom")):
            left = float(getattr(bounds, "left"))
            top = float(getattr(bounds, "top"))
            right = float(getattr(bounds, "right"))
            bottom = float(getattr(bounds, "bottom"))
        elif isinstance(bounds, dict):
            if all(key in bounds for key in ("x", "y", "width", "height")):
                left = float(bounds.get("x") or 0)
                top = float(bounds.get("y") or 0)
                right = left + float(bounds.get("width") or 0)
                bottom = top + float(bounds.get("height") or 0)
            elif all(key in bounds for key in ("left", "top", "right", "bottom")):
                left = float(bounds.get("left") or 0)
                top = float(bounds.get("top") or 0)
                right = float(bounds.get("right") or 0)
                bottom = float(bounds.get("bottom") or 0)
        elif isinstance(bounds, (list, tuple)) and len(bounds) >= 4:
            left = float(bounds[0])
            top = float(bounds[1])
            third = float(bounds[2])
            fourth = float(bounds[3])
            if third > left and fourth > top:
                right = third
                bottom = fourth
            else:
                right = left + third
                bottom = top + fourth

        if left is None or top is None or right is None or bottom is None:
            return None

        x1 = int(round(left))
        y1 = int(round(top))
        x2 = int(round(right))
        y2 = int(round(bottom))
        if width is not None:
            x1 = max(0, min(x1, int(width)))
            x2 = max(0, min(x2, int(width)))
        if height is not None:
            y1 = max(0, min(y1, int(height)))
            y2 = max(0, min(y2, int(height)))
        if x2 <= x1 or y2 <= y1:
            return None
        return x1, y1, x2, y2

    def _get_active_popup_bounds(self, timeout: float = 0.25) -> Optional[Tuple[int, int, int, int]]:
        _, bounds = self._probe_alert_or_sheet(timeout=timeout)
        return bounds

    def _get_step_ocr_result(
        self,
        step_context: Optional[Dict[str, Any]] = None,
        timeout: Optional[float] = None,
        crop_bounds: Optional[Tuple[int, int, int, int]] = None,
        prefer_popup_crop: bool = False,
    ) -> Dict[str, Any]:
        cache_enabled = isinstance(step_context, dict)
        cache = step_context.setdefault("cache", {}) if cache_enabled else {}
        timing = {
            "ocr_total_ms": 0.0,
            "ocr_screenshot_ms": 0.0,
            "ocr_decode_ms": 0.0,
            "ocr_engine_ms": 0.0,
            "ocr_run_ms": 0.0,
            "ocr_scope": "full",
            "ocr_cache_hit": False,
        }
        overall_started = time.time()

        screen_bgr = cache.get("ocr_screen_bgr_full") if cache_enabled else None
        if screen_bgr is None:
            shot_started = time.time()
            raw_png = self._capture_screenshot_cached(step_context=step_context, timeout=timeout)
            timing["ocr_screenshot_ms"] = self._ms(time.time() - shot_started)
            decode_started = time.time()
            screen_bgr = self._decode_png_to_bgr(raw_png, source="screenshot")
            timing["ocr_decode_ms"] = self._ms(time.time() - decode_started)
            if cache_enabled:
                cache["ocr_screen_bgr_full"] = screen_bgr

        height, width = screen_bgr.shape[:2]
        normalized_bounds = self._normalize_crop_bounds(crop_bounds, width=width, height=height)
        hint_crop_used = False
        if normalized_bounds is None and prefer_popup_crop:
            if height >= width:
                normalized_bounds = self._normalize_crop_bounds(
                    (
                        int(width * 0.10),
                        int(height * 0.36),
                        int(width * 0.90),
                        int(height * 0.92),
                    ),
                    width=width,
                    height=height,
                )
            else:
                normalized_bounds = self._normalize_crop_bounds(
                    (
                        int(width * 0.18),
                        int(height * 0.18),
                        int(width * 0.95),
                        int(height * 0.88),
                    ),
                    width=width,
                    height=height,
                )
            hint_crop_used = normalized_bounds is not None
        ocr_offset = (0.0, 0.0)
        ocr_input = screen_bgr
        cache_key: Tuple[Any, ...] = ("full", width, height)

        if normalized_bounds is not None:
            x1, y1, x2, y2 = normalized_bounds
            padding = min(max(8, int(min(width, height) * 0.02)), 36)
            x1 = max(0, x1 - padding)
            y1 = max(0, y1 - padding)
            x2 = min(width, x2 + padding)
            y2 = min(height, y2 + padding)
            if x2 > x1 and y2 > y1:
                ocr_input = screen_bgr[y1:y2, x1:x2]
                ocr_offset = (float(x1), float(y1))
                if hint_crop_used:
                    cache_key = ("popup_hint", x1, y1, x2, y2)
                    timing["ocr_scope"] = "popup-hint"
                else:
                    cache_key = ("crop", x1, y1, x2, y2)
                    timing["ocr_scope"] = "popup"
                if cache_enabled:
                    artifacts = step_context.setdefault("artifacts", {})
                    artifacts["ocr_crop_bounds"] = f"[{x1},{y1},{x2},{y2}]"
                    artifacts["ocr_crop_source"] = "popup_hint" if hint_crop_used else "popup_bounds"

        if cache_enabled and cache.get("ocr_result_key") == cache_key and "ocr_result" in cache:
            cached_timing = dict(cache.get("ocr_timing") or timing)
            cached_timing["ocr_cache_hit"] = True
            cached_timing["ocr_screenshot_ms"] = 0.0
            cached_timing["ocr_decode_ms"] = 0.0
            cached_timing["ocr_engine_ms"] = 0.0
            cached_timing["ocr_run_ms"] = 0.0
            cached_timing["ocr_total_ms"] = self._ms(time.time() - overall_started)
            logger.info(
                "iOS.ocr timing: scope=%s cache_hit=%s screenshot_ms=%.1f decode_ms=%.1f engine_ms=%.1f run_ms=%.1f total_ms=%.1f",
                cached_timing.get("ocr_scope"),
                cached_timing.get("ocr_cache_hit"),
                float(cached_timing.get("ocr_screenshot_ms") or 0.0),
                float(cached_timing.get("ocr_decode_ms") or 0.0),
                float(cached_timing.get("ocr_engine_ms") or 0.0),
                float(cached_timing.get("ocr_run_ms") or 0.0),
                float(cached_timing.get("ocr_total_ms") or 0.0),
            )
            return {
                "result": cache.get("ocr_result"),
                "offset": cache.get("ocr_offset") or (0.0, 0.0),
                "metrics": cached_timing,
            }

        engine_started = time.time()
        ocr_engine = self._get_ocr_engine()
        timing["ocr_engine_ms"] = self._ms(time.time() - engine_started)
        run_started = time.time()
        result = run_paddle_ocr(ocr_engine, ocr_input, use_cls=False)
        timing["ocr_run_ms"] = self._ms(time.time() - run_started)
        timing["ocr_total_ms"] = self._ms(time.time() - overall_started)
        logger.info(
            "iOS.ocr timing: scope=%s cache_hit=%s screenshot_ms=%.1f decode_ms=%.1f engine_ms=%.1f run_ms=%.1f total_ms=%.1f",
            timing.get("ocr_scope"),
            timing.get("ocr_cache_hit"),
            float(timing.get("ocr_screenshot_ms") or 0.0),
            float(timing.get("ocr_decode_ms") or 0.0),
            float(timing.get("ocr_engine_ms") or 0.0),
            float(timing.get("ocr_run_ms") or 0.0),
            float(timing.get("ocr_total_ms") or 0.0),
        )
        if cache_enabled:
            cache["ocr_result_key"] = cache_key
            cache["ocr_result"] = result
            cache["ocr_offset"] = ocr_offset
            cache["ocr_timing"] = dict(timing)
        return {"result": result, "offset": ocr_offset, "metrics": timing}

    def _should_require_click_effect(self, selector: str, by: str) -> bool:
        popup_semantic_by = {
            "label",
            "name",
            "id",
        }
        return self._normalize_locator_by(by) in popup_semantic_by and self._is_confirm_action_text(selector)

    def _click_locator_once(self, selector: str, by: str, timeout: float = 1.0) -> None:
        selector_text = str(selector or "").strip()
        by_text = self._normalize_locator_by(by)
        wait_timeout = max(0.25, float(timeout or 0.0))
        require_click_effect = self._should_require_click_effect(selector_text, by_text)
        selector_obj = self._build_selector(selector_text, by_text)
        if not self._wait_selector(selector_obj, timeout=wait_timeout):
            raise RuntimeError(
                f"元素未找到: selector={selector_text!r}, by={by_text!r}, timeout={wait_timeout:.2f}"
            )
        element = selector_obj.get(timeout=wait_timeout, raise_error=True)
        before_signature = ""
        had_alert_before = False
        if require_click_effect:
            had_alert_before = self._has_alert_or_sheet(
                timeout=min(0.18, wait_timeout),
                reuse_window=0.0,
            )
            before_signature = self._capture_page_signature(mode="quick", screenshot_timeout=min(1.0, wait_timeout))
        element.tap()
        if require_click_effect:
            effect_timeout = min(0.8, max(0.3, wait_timeout))
            if not self._wait_confirm_click_effect(
                selector=selector_text,
                before_signature=before_signature,
                timeout=effect_timeout,
                interval=0.12,
                mode="quick",
                screenshot_timeout=min(1.0, effect_timeout + 0.3),
                selector_by=by_text,
                had_alert_before=had_alert_before,
            ):
                raise RuntimeError("tap-no-effect")

    def _collect_popup_target_texts(self, locator_candidates: List[Dict[str, Any]]) -> List[str]:
        popup_semantic_by = {"label", "name", "id"}
        targets: List[str] = []
        seen = set()
        for item in locator_candidates or []:
            selector = str(item.get("selector") or "").strip()
            by = self._normalize_locator_by(item.get("by") or "")
            if not selector or by not in popup_semantic_by or selector in seen:
                continue
            seen.add(selector)
            targets.append(selector)
        return targets

    def click_with_fallback_plan(
        self,
        locator_candidates: List[Dict[str, Any]],
        timeout: int = 10,
        step_context: Optional[Dict[str, Any]] = None,
    ) -> None:
        started_at = time.time()
        timing_metrics: Dict[str, Any] = {
            "locator_ms": 0.0,
            "popup_probe_ms": 0.0,
            "alert_ms": 0.0,
            "effect_sig_ms": 0.0,
            "effect_wait_ms": 0.0,
            "ocr_total_ms": 0.0,
            "ocr_screenshot_ms": 0.0,
            "ocr_decode_ms": 0.0,
            "ocr_engine_ms": 0.0,
            "ocr_run_ms": 0.0,
            "ocr_parse_ms": 0.0,
            "ocr_scope": "none",
            "ocr_cache_hit": False,
        }
        direct_attempts = self._build_click_locator_attempts(locator_candidates)
        if not direct_attempts:
            raise ValueError("iOS.click_with_fallback_plan 缺少有效定位候选")

        total_timeout = max(1.0, float(timeout or 0))
        deadline = started_at + total_timeout
        popup_targets = self._collect_popup_target_texts(locator_candidates)
        has_confirm = any(self._is_confirm_action_text(selector) for selector in popup_targets)
        popup_rescue_reserved = 0.0
        if popup_targets and has_confirm:
            popup_rescue_reserved = min(
                3.0,
                max(1.0, total_timeout * 0.45),
                total_timeout * 0.75,
            )
        direct_budget_cap = min(
            1.8 if has_confirm else 5.0,
            max(0.9 if has_confirm else 1.2, total_timeout * (0.4 if has_confirm else 0.55)),
        )
        direct_budget = direct_budget_cap
        if popup_rescue_reserved > 0:
            direct_budget = min(direct_budget_cap, max(0.25, total_timeout - popup_rescue_reserved))
        direct_deadline = min(deadline, started_at + direct_budget)
        direct_timeout_cap = 0.9 if has_confirm else 1.5
        errors: List[str] = []

        for index, attempt in enumerate(direct_attempts):
            remaining_phase = max(0.0, direct_deadline - time.time())
            if remaining_phase <= 0.05:
                errors.append("locator:budget-exhausted")
                break
            remaining_attempts = max(1, len(direct_attempts) - index)
            attempt_timeout = min(direct_timeout_cap, max(0.25, remaining_phase / remaining_attempts))
            attempt_started = time.time()
            try:
                self._click_locator_once(
                    selector=attempt["selector"],
                    by=attempt["by"],
                    timeout=attempt_timeout,
                )
                timing_metrics["locator_ms"] = round(
                    float(timing_metrics.get("locator_ms") or 0.0) + self._ms(time.time() - attempt_started),
                    1,
                )
                self._log_click_plan_timing("locator-hit", timing_metrics)
                self._log_action_success(
                    "click_plan",
                    started_at,
                    strategy=f"locator:{attempt['by']}",
                    selector=attempt["selector"],
                    timeout=round(total_timeout, 2),
                    attempts=index + 1,
                    locator_ms=timing_metrics.get("locator_ms"),
                    popup_probe_ms=timing_metrics.get("popup_probe_ms"),
                    alert_ms=timing_metrics.get("alert_ms"),
                    effect_sig_ms=timing_metrics.get("effect_sig_ms"),
                    effect_wait_ms=timing_metrics.get("effect_wait_ms"),
                    ocr_scope=timing_metrics.get("ocr_scope"),
                    ocr_cache_hit=timing_metrics.get("ocr_cache_hit"),
                    ocr_total_ms=timing_metrics.get("ocr_total_ms"),
                    ocr_screenshot_ms=timing_metrics.get("ocr_screenshot_ms"),
                    ocr_decode_ms=timing_metrics.get("ocr_decode_ms"),
                    ocr_engine_ms=timing_metrics.get("ocr_engine_ms"),
                    ocr_run_ms=timing_metrics.get("ocr_run_ms"),
                    ocr_parse_ms=timing_metrics.get("ocr_parse_ms"),
                )
                return
            except Exception as exc:
                timing_metrics["locator_ms"] = round(
                    float(timing_metrics.get("locator_ms") or 0.0) + self._ms(time.time() - attempt_started),
                    1,
                )
                errors.append(
                    f"locator:{attempt['by']}:{self._truncate_log_value(attempt['selector'])}:{exc}"
                )

        has_alert = False
        popup_crop_bounds: Optional[Tuple[int, int, int, int]] = None
        remaining_total = max(0.0, deadline - time.time())
        if popup_targets and remaining_total > 0.05:
            probe_timeout = min(0.18 if has_confirm else 0.22, max(0.08, remaining_total))
            probe_started = time.time()
            has_alert = self._has_alert_or_sheet(timeout=probe_timeout)
            if has_alert:
                popup_crop_bounds = self._get_active_popup_bounds(timeout=probe_timeout)
            timing_metrics["popup_probe_ms"] = round(
                float(timing_metrics.get("popup_probe_ms") or 0.0) + self._ms(time.time() - probe_started),
                1,
            )
        should_try_popup_rescue = bool(popup_targets) and (has_alert or has_confirm)

        if should_try_popup_rescue:
            remaining_total = max(0.0, deadline - time.time())
            ocr_budget_floor = 0.0
            if remaining_total > 0.05:
                ocr_budget_floor = min(
                    max(0.25, remaining_total * (0.6 if has_confirm else 0.4)),
                    1.2 if has_confirm else 0.8,
                )
                alert_budget_available = max(0.0, remaining_total - ocr_budget_floor)
                if has_alert:
                    alert_budget = min(
                        alert_budget_available,
                        min(
                            0.65 if has_confirm else 0.9,
                            max(0.22 if has_confirm else 0.3, total_timeout * (0.08 if has_confirm else 0.12)),
                        ),
                    )
                elif has_confirm:
                    alert_budget = min(alert_budget_available, 0.18)
                else:
                    alert_budget = 0.0
                if alert_budget > 0.05:
                    per_target_timeout = max(0.10, alert_budget / max(1, len(popup_targets)))
                    for selector in popup_targets:
                        alert_errors: List[str] = []
                        require_page_change = self._is_confirm_action_text(selector)
                        alert_started = time.time()
                        alert_hit = self._tap_alert_button(
                            selector=selector,
                            timeout=per_target_timeout,
                            errors=alert_errors,
                            require_page_change=require_page_change,
                            timing_metrics=timing_metrics,
                        )
                        timing_metrics["alert_ms"] = round(
                            float(timing_metrics.get("alert_ms") or 0.0) + self._ms(time.time() - alert_started),
                            1,
                        )
                        if alert_hit:
                            self._log_click_plan_timing("alert-hit", timing_metrics)
                            self._log_action_success(
                                "click_plan",
                                started_at,
                                strategy="alert-button",
                                selector=selector,
                                timeout=round(total_timeout, 2),
                                attempts=len(errors) + 1,
                                locator_ms=timing_metrics.get("locator_ms"),
                                popup_probe_ms=timing_metrics.get("popup_probe_ms"),
                                alert_ms=timing_metrics.get("alert_ms"),
                                effect_sig_ms=timing_metrics.get("effect_sig_ms"),
                                effect_wait_ms=timing_metrics.get("effect_wait_ms"),
                                ocr_scope=timing_metrics.get("ocr_scope"),
                                ocr_cache_hit=timing_metrics.get("ocr_cache_hit"),
                                ocr_total_ms=timing_metrics.get("ocr_total_ms"),
                                ocr_screenshot_ms=timing_metrics.get("ocr_screenshot_ms"),
                                ocr_decode_ms=timing_metrics.get("ocr_decode_ms"),
                                ocr_engine_ms=timing_metrics.get("ocr_engine_ms"),
                                ocr_run_ms=timing_metrics.get("ocr_run_ms"),
                                ocr_parse_ms=timing_metrics.get("ocr_parse_ms"),
                            )
                            return
                        if alert_errors:
                            errors.extend(
                                f"alert:{self._truncate_log_value(selector)}:{detail}"
                                for detail in alert_errors
                            )
                        else:
                            errors.append(f"alert:{self._truncate_log_value(selector)}:not-found")

            remaining_total = max(0.0, deadline - time.time())
            if remaining_total <= 0.05 and ocr_budget_floor > 0.25:
                remaining_total = ocr_budget_floor
            if remaining_total > 0.05:
                ocr_timeout = max(0.25, remaining_total)
                for selector in popup_targets:
                    ocr_errors: List[str] = []
                    require_page_change = self._is_confirm_action_text(selector)
                    if self._tap_by_ocr_text(
                        selector=selector,
                        errors=ocr_errors,
                        require_page_change=require_page_change,
                        step_context=step_context,
                        screenshot_timeout=ocr_timeout,
                        crop_bounds=popup_crop_bounds,
                        timing_metrics=timing_metrics,
                        prefer_popup_crop=(has_confirm and popup_crop_bounds is None),
                    ):
                        self._log_click_plan_timing("ocr-hit", timing_metrics)
                        self._log_action_success(
                            "click_plan",
                            started_at,
                            strategy="ocr-fallback",
                            selector=selector,
                            timeout=round(total_timeout, 2),
                            attempts=len(errors) + 1,
                            locator_ms=timing_metrics.get("locator_ms"),
                            popup_probe_ms=timing_metrics.get("popup_probe_ms"),
                            alert_ms=timing_metrics.get("alert_ms"),
                            effect_sig_ms=timing_metrics.get("effect_sig_ms"),
                            effect_wait_ms=timing_metrics.get("effect_wait_ms"),
                            ocr_scope=timing_metrics.get("ocr_scope"),
                            ocr_cache_hit=timing_metrics.get("ocr_cache_hit"),
                            ocr_total_ms=timing_metrics.get("ocr_total_ms"),
                            ocr_screenshot_ms=timing_metrics.get("ocr_screenshot_ms"),
                            ocr_decode_ms=timing_metrics.get("ocr_decode_ms"),
                            ocr_engine_ms=timing_metrics.get("ocr_engine_ms"),
                            ocr_run_ms=timing_metrics.get("ocr_run_ms"),
                            ocr_parse_ms=timing_metrics.get("ocr_parse_ms"),
                        )
                        return
                    if ocr_errors:
                        errors.extend(
                            f"ocr:{self._truncate_log_value(selector)}:{detail}"
                            for detail in ocr_errors
                        )
                    else:
                        errors.append(f"ocr:{self._truncate_log_value(selector)}:not-found")
            else:
                errors.append("popup:budget-exhausted")

        detail = "; ".join(errors) if errors else "no-click-strategy-succeeded"
        final_exc = RuntimeError(f"iOS.click_plan 失败: {detail}")
        self._log_click_plan_timing("failed", timing_metrics)
        self._log_action_failure(
            "click_plan",
            started_at,
            final_exc,
            timeout=round(total_timeout, 2),
            direct_attempts=len(direct_attempts),
            popup_targets=len(popup_targets),
            popup_rescue=should_try_popup_rescue,
            locator_ms=timing_metrics.get("locator_ms"),
            popup_probe_ms=timing_metrics.get("popup_probe_ms"),
            alert_ms=timing_metrics.get("alert_ms"),
            effect_sig_ms=timing_metrics.get("effect_sig_ms"),
            effect_wait_ms=timing_metrics.get("effect_wait_ms"),
            ocr_scope=timing_metrics.get("ocr_scope"),
            ocr_cache_hit=timing_metrics.get("ocr_cache_hit"),
            ocr_total_ms=timing_metrics.get("ocr_total_ms"),
            ocr_screenshot_ms=timing_metrics.get("ocr_screenshot_ms"),
            ocr_decode_ms=timing_metrics.get("ocr_decode_ms"),
            ocr_engine_ms=timing_metrics.get("ocr_engine_ms"),
            ocr_run_ms=timing_metrics.get("ocr_run_ms"),
            ocr_parse_ms=timing_metrics.get("ocr_parse_ms"),
        )
        raise final_exc

    def _resolve_selector(self, selector: str, by: str, timeout: int = 5) -> Tuple[Any, str]:
        selector_obj = self._build_selector(selector, by)
        if self._wait_selector(selector_obj, timeout=timeout):
            return selector_obj, str(by or "").lower()

        attempts = [f"{by}:not-found"]
        fallback_timeout = min(max(int(timeout), 1), 2)
        for fallback_by, fallback_obj in self._build_fallback_selectors(selector, by):
            if self._wait_selector(fallback_obj, timeout=fallback_timeout):
                logger.info(
                    "iOS locator fallback hit: requested_by=%s, resolved_by=%s, selector=%s",
                    by,
                    fallback_by,
                    selector,
                )
                return fallback_obj, fallback_by
            attempts.append(f"{fallback_by}:not-found")

        raise RuntimeError(
            f"元素未找到: selector={selector!r}, by={by!r}, timeout={timeout}, attempts={attempts}"
        )

    def _get_element(self, selector: str, by: str, timeout: int = 5) -> wda.Element:
        """
        获取元素（带等待）。
        """
        selector_obj, resolved_by = self._resolve_selector(selector, by, timeout=timeout)
        try:
            return selector_obj.get(timeout=timeout, raise_error=True)
        except Exception as exc:
            raise RuntimeError(
                f"iOS 元素获取失败: selector={selector!r}, by={by!r}, resolved_by={resolved_by!r}, error={exc}"
            ) from exc

    def _collect_page_text_candidates(self) -> List[str]:
        session = self.client.session()
        source_candidates = [
            lambda: session.source(),
            lambda: getattr(session, "source", None),
            lambda: self.client.source(),
            lambda: getattr(self.client, "source", None),
        ]

        source_text = ""
        for getter in source_candidates:
            try:
                raw_value = getter()
                if callable(raw_value):
                    raw_value = raw_value()
                source_text = str(raw_value or "").strip()
                if source_text:
                    break
            except Exception:
                continue

        values: List[str] = []
        if source_text:
            try:
                root = ET.fromstring(source_text)
                for node in root.iter():
                    for attr_name in ("text", "label", "name", "value"):
                        value = str(node.attrib.get(attr_name) or "").strip()
                        if value:
                            values.append(value)
            except Exception as exc:
                logger.warning("iOS source parse failed: %s", exc)
                values.extend(
                    match.strip()
                    for match in re.findall(r'(?:text|label|name|value)="([^"]+)"', source_text)
                    if str(match).strip()
                )

        return values

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
                    raise RuntimeError("tap-no-effect")
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
            final_exc = RuntimeError(
                f"iOS.click 失败: selector={selector!r}, by={by!r}, error={primary_exc}, fallback={detail}"
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
        final_exc = RuntimeError(f"iOS.input_focused 执行失败: {detail}")
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

            final_exc = AssertionError(
                f"断言失败: 期望页面{'不包含' if normalized_mode == 'not_contains' else '包含'} {expected!r}, 实际={preview!r}"
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

    def _capture_page_signature(
        self,
        mode: str = "full",
        screenshot_timeout: Optional[float] = None,
    ) -> str:
        """
        抓取当前页面签名，用于判断 back 后是否发生页面跳转。

        优先级：
        1) 当前前台 app 信息（bundle/activity）
        2) page source 的哈希
        3) screenshot 的哈希（兜底）
        """
        signature_parts = []
        session = self.client.session()
        quick_mode = str(mode or "").strip().lower() in {"quick", "fast", "light"}

        try:
            app_current = getattr(session, "app_current", None)
            if callable(app_current):
                app_current = app_current()
            if isinstance(app_current, dict):
                bundle = str(app_current.get("bundleId") or app_current.get("bundle_id") or "").strip()
                activity = str(app_current.get("activity") or app_current.get("name") or "").strip()
                if bundle:
                    signature_parts.append(f"bundle={bundle}")
                if activity:
                    signature_parts.append(f"activity={activity}")
        except Exception:
            pass

        if not quick_mode:
            source_candidates = [
                lambda: session.source(),
                lambda: getattr(session, "source", None),
                lambda: self.client.source(),
                lambda: getattr(self.client, "source", None),
            ]
            for getter in source_candidates:
                try:
                    raw_value = getter()
                    if callable(raw_value):
                        raw_value = raw_value()
                    source_text = str(raw_value or "").strip()
                    if source_text:
                        digest = hashlib.sha1(
                            source_text.encode("utf-8", errors="ignore")
                        ).hexdigest()[:16]
                        signature_parts.append(f"source={digest}")
                        break
                except Exception:
                    continue

        should_use_screenshot = quick_mode or not signature_parts
        if should_use_screenshot:
            try:
                effective_timeout = max(
                    0.2,
                    float(
                        screenshot_timeout
                        if screenshot_timeout is not None
                        else (1.2 if quick_mode else self._SCREENSHOT_TIMEOUT_SECONDS)
                    ),
                )
                screenshot = self._capture_screenshot_bytes(timeout=effective_timeout)
                if screenshot:
                    digest = hashlib.sha1(screenshot).hexdigest()[:16]
                    signature_parts.append(f"screen={digest}")
            except Exception:
                pass

        return "|".join(signature_parts)

    def _wait_page_changed(
        self,
        before_signature: str,
        timeout: float = 1.2,
        interval: float = 0.25,
        mode: str = "full",
        screenshot_timeout: Optional[float] = None,
    ) -> bool:
        deadline = time.time() + max(timeout, 0.2)
        while time.time() < deadline:
            time.sleep(max(interval, 0.05))
            current_signature = self._capture_page_signature(
                mode=mode,
                screenshot_timeout=screenshot_timeout,
            )
            if current_signature and current_signature != before_signature:
                return True

        current_signature = self._capture_page_signature(
            mode=mode,
            screenshot_timeout=screenshot_timeout,
        )
        return bool(current_signature and current_signature != before_signature)

    def _try_edge_back_swipe(self, y_ratio: float = 0.5) -> bool:
        """
        优先尝试 iOS 常见返回手势：左缘向右滑。
        """
        session = self.client.session()
        size = None
        for getter in (
            lambda: session.window_size(),
            lambda: self.client.window_size(),
        ):
            try:
                payload = getter()
                if payload:
                    size = payload
                    break
            except Exception:
                continue

        width = 390
        height = 844
        if isinstance(size, dict):
            width = int(size.get("width") or size.get("w") or width)
            height = int(size.get("height") or size.get("h") or height)
        elif isinstance(size, (tuple, list)) and len(size) >= 2:
            width = int(size[0] or width)
            height = int(size[1] or height)
        else:
            width = int(getattr(size, "width", width) or width)
            height = int(getattr(size, "height", height) or height)

        # WDA 只要收到 float 就会按百分比处理，必须传 int 绝对坐标。
        # 贴近左边缘，提升 iOS 侧滑返回命中率（最小 1px）。
        start_x = max(1, int(width * 0.005))
        safe_ratio = min(max(float(y_ratio), 0.15), 0.85)
        start_y = max(2, int(height * safe_ratio))
        # 尽量滑到屏幕右侧边缘，提升侧滑返回触发率。
        end_x = max(start_x + 2, int(width * 0.95))
        end_y = start_y

        swipe_candidates = [
            # 优先快速侧滑，再逐步降级到较慢参数。
            lambda: session.swipe(start_x, start_y, end_x, end_y, duration=0.06),
            lambda: session.swipe(start_x, start_y, end_x, end_y, duration=0.12),
            lambda: session.swipe(start_x, start_y, end_x, end_y),
            lambda: session.drag(start_x, start_y, end_x, end_y, duration=0.12),
            lambda: session.drag(start_x, start_y, end_x, end_y),
        ]
        for swipe_call in swipe_candidates:
            try:
                swipe_call()
                logger.info(
                    "iOS.back edge-swipe: (%.1f, %.1f) -> (%.1f, %.1f)",
                    start_x,
                    start_y,
                    end_x,
                    end_y,
                )
                return True
            except Exception:
                continue
        return False

    def _try_press_back(self) -> bool:
        try:
            self.client.press("back")
            return True
        except Exception:
            return False

    def _tap_common_back_buttons(self) -> bool:
        """
        点击常见返回按钮文案/标识。
        """
        candidates = [
            ("name", "Back"),
            ("label", "Back"),
            ("name", "back"),
            ("label", "back"),
            ("name", "返回"),
            ("label", "返回"),
            ("name", "返回上一页"),
            ("label", "返回上一页"),
            ("id", "back"),
            ("id", "nav_back"),
            ("id", "btn_back"),
            ("id", "ic_back"),
            (
                "predicate",
                "type == 'XCUIElementTypeButton' AND (name CONTAINS 'Back' OR label CONTAINS 'Back' OR name CONTAINS '返回' OR label CONTAINS '返回')",
            ),
        ]
        for by, selector in candidates:
            try:
                self.click(selector=selector, by=by)
                return True
            except Exception:
                continue
        return False

    def back(self) -> None:
        """
        iOS 无统一系统 back 键。

        执行顺序：
        1) 左缘右滑手势
        2) press("back")
        3) 点击常见返回按钮

        每一步都会检测页面签名变化，若动作执行后页面未变化则继续尝试下一种。
        """
        started_at = time.time()
        logger.info("iOS.back start")
        before_signature = self._capture_page_signature()
        details = []

        edge_swipe_y_ratios = (0.20, 0.50, 0.80)
        for y_ratio in edge_swipe_y_ratios:
            strategy_name = f"edge-swipe@y={y_ratio:.2f}"
            try:
                acted = bool(self._try_edge_back_swipe(y_ratio=y_ratio))
            except Exception as exc:
                details.append(f"{strategy_name}:error={exc}")
                continue

            if not acted:
                details.append(f"{strategy_name}:not-available")
                continue

            if self._wait_page_changed(before_signature, timeout=0.9):
                self._log_action_success(
                    "back",
                    started_at,
                    strategy=strategy_name,
                    attempts=len(details) + 1,
                )
                return
            details.append(f"{strategy_name}:page-unchanged")

        strategies = [
            ("press-back", self._try_press_back),
            ("tap-back-button", self._tap_common_back_buttons),
        ]
        for strategy_name, action in strategies:
            try:
                acted = bool(action())
            except Exception as exc:
                details.append(f"{strategy_name}:error={exc}")
                continue
            if not acted:
                details.append(f"{strategy_name}:not-available")
                continue

            if self._wait_page_changed(before_signature):
                self._log_action_success(
                    "back",
                    started_at,
                    strategy=strategy_name,
                    attempts=len(details) + 1,
                )
                return
            details.append(f"{strategy_name}:page-unchanged")

        detail_text = "; ".join(details) if details else "no-strategy"
        final_exc = RuntimeError(
            f"iOS back 执行失败：页面未变化或未找到可用返回入口 ({detail_text})"
        )
        self._log_action_failure(
            "back",
            started_at,
            final_exc,
            attempts=len(details),
            detail=detail_text,
        )
        raise final_exc

    def home(self) -> None:
        started_at = time.time()
        logger.info("iOS.home start")
        try:
            self.client.home()
            self._log_action_success("home", started_at)
        except Exception as exc:
            self._log_action_failure("home", started_at, exc)
            raise

    def _read_app_state(self, app_id: str) -> int:
        """
        读取应用状态：
        - 1: not running
        - 2: running in background
        - 3/4: running in foreground (WDA 不同版本返回值有差异)
        """
        raw_state = self.client.app_state(app_id)
        if hasattr(raw_state, "value"):
            raw_state = getattr(raw_state, "value")
        if isinstance(raw_state, dict):
            raw_state = raw_state.get("value")
        return int(raw_state)

    def start_app(self, app_id: str) -> None:
        if not app_id:
            raise ValueError("start_app 需要 app_id（iOS bundleId）")
        started_at = time.time()
        logger.info("iOS.start_app start: app_id=%s", app_id)

        errors = []
        launch_methods = [
            ("app_activate", lambda: self.client.app_activate(app_id)),
            ("app_launch", lambda: self.client.app_launch(app_id, wait_for_quiescence=False)),
        ]
        for method_name, method in launch_methods:
            try:
                method()
                time.sleep(0.35)
                try:
                    state = self._read_app_state(app_id)
                    if state in (2, 3, 4):
                        self._log_action_success(
                            "start_app",
                            started_at,
                            app_id=app_id,
                            strategy=method_name,
                            app_state=state,
                            attempts=len(errors) + 1,
                        )
                        return
                    errors.append(f"{method_name}: unexpected app_state={state}")
                except Exception:
                    # 某些 WDA 版本 app_state 不稳定，启动请求成功即可视为成功。
                    self._log_action_success(
                        "start_app",
                        started_at,
                        app_id=app_id,
                        strategy=method_name,
                        app_state="unknown",
                        attempts=len(errors) + 1,
                    )
                    return
            except Exception as exc:
                errors.append(f"{method_name}: {exc}")

        detail = "; ".join(errors) if errors else "unknown"
        final_exc = RuntimeError(f"iOS.start_app 执行失败: {detail}")
        self._log_action_failure(
            "start_app",
            started_at,
            final_exc,
            app_id=app_id,
            attempts=len(errors),
            detail=detail,
        )
        raise final_exc

    def stop_app(self, app_id: str) -> None:
        if not app_id:
            raise ValueError("stop_app 需要 app_id（iOS bundleId）")
        started_at = time.time()
        logger.info("iOS.stop_app start: app_id=%s", app_id)

        errors = []
        terminate_methods = [
            ("app_terminate", lambda: self.client.app_terminate(app_id)),
            ("session.app_terminate", lambda: self.client.session().app_terminate(app_id)),
        ]
        for method_name, method in terminate_methods:
            try:
                method()
                time.sleep(0.2)
            except Exception as exc:
                errors.append(f"{method_name}: {exc}")

            try:
                state = self._read_app_state(app_id)
                if state == 1:
                    self._log_action_success(
                        "stop_app",
                        started_at,
                        app_id=app_id,
                        strategy=method_name,
                        app_state=state,
                        attempts=len(errors) + 1,
                    )
                    return
            except Exception:
                # 无法读取状态时，调用成功视为 best effort。
                if not errors or not errors[-1].startswith(method_name):
                    self._log_action_success(
                        "stop_app",
                        started_at,
                        app_id=app_id,
                        strategy=method_name,
                        app_state="unknown",
                        attempts=len(errors) + 1,
                    )
                    return

        # 兜底再读一次状态，若已退出则视为成功。
        try:
            if self._read_app_state(app_id) == 1:
                self._log_action_success(
                    "stop_app",
                    started_at,
                    app_id=app_id,
                    strategy="final-app-state-check",
                    app_state=1,
                    attempts=len(errors),
                )
                return
        except Exception:
            pass

        detail = "; ".join(errors) if errors else "unknown"
        final_exc = RuntimeError(f"iOS.stop_app 执行失败: {detail}")
        self._log_action_failure(
            "stop_app",
            started_at,
            final_exc,
            app_id=app_id,
            attempts=len(errors),
            detail=detail,
        )
        raise final_exc

    def _resolve_template_image_path(self, image_path: str, action_name: str) -> str:
        target = str(image_path or "").strip()
        if not target:
            raise ValueError(f"{action_name} 需要 image_path")
        if not os.path.isabs(target):
            project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
            target = os.path.abspath(os.path.join(project_root, target))
        if not os.path.exists(target):
            raise FileNotFoundError(f"图像文件不存在: {target}")
        return target

    def _locate_image_target(
        self,
        image_path: str,
        threshold: Optional[float] = None,
        action_name: str = "click_image",
    ) -> Tuple[float, float, float]:
        screenshot = self.screenshot()
        screen_bgr = self._decode_png_to_bgr(screenshot, source="screenshot")
        template_bgr = self._load_image_bgr(image_path)
        return self._match_template_center(
            screen_bgr=screen_bgr,
            template_bgr=template_bgr,
            threshold=threshold or self._IMAGE_MATCH_THRESHOLD,
            action_name=action_name,
        )

    def _try_locate_image_target(
        self,
        image_path: str,
        threshold: Optional[float] = None,
        action_name: str = "assert_image",
    ) -> Optional[Tuple[float, float, float]]:
        try:
            return self._locate_image_target(
                image_path=image_path,
                threshold=threshold,
                action_name=action_name,
            )
        except RuntimeError as exc:
            if "未匹配到足够置信度目标" in str(exc):
                return None
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
                    raise AssertionError(
                        f"断言失败: 期望页面不存在图像 {target!r}，但已高置信度匹配到目标 "
                        f"(score={score:.4f}, x={center_x:.1f}, y={center_y:.1f})"
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
                raise AssertionError(
                    f"断言失败: 期望页面不存在图像 {target!r}，但仍匹配到目标 "
                    f"(score={score:.4f}, x={center_x:.1f}, y={center_y:.1f})"
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
                final_exc = RuntimeError("extract_by_ocr 未识别到文本")
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

    @staticmethod
    def _load_opencv_numpy() -> Tuple[Any, Any]:
        try:
            import cv2
            import numpy as np
        except Exception as exc:
            raise RuntimeError(
                "iOS 图像/OCR 动作依赖缺失: 请安装 opencv-python 与 numpy"
            ) from exc
        return cv2, np

    @classmethod
    def _get_ocr_engine(cls) -> Any:
        if cls._ocr_engine is None:
            last_exc: Optional[Exception] = None
            for _ in range(2):
                try:
                    logger.debug("iOS OCR engine loading")
                    cls._ocr_engine = create_paddle_ocr_engine(use_angle_cls=False, lang="ch")
                    logger.debug("iOS OCR engine ready")
                    break
                except Exception as exc:
                    last_exc = exc
                    time.sleep(0.15)
            if cls._ocr_engine is None:
                detail = str(last_exc or "").strip().splitlines()[0] if last_exc else "unknown"
                raise RuntimeError(
                    f"extract_by_ocr 初始化失败: {detail}"
                ) from last_exc
        return cls._ocr_engine

    def _decode_png_to_bgr(self, png_bytes: bytes, source: str) -> Any:
        cv2, np = self._load_opencv_numpy()
        if not png_bytes:
            raise RuntimeError(f"{source} 数据为空")
        arr = np.frombuffer(png_bytes, dtype=np.uint8)
        image = cv2.imdecode(arr, cv2.IMREAD_COLOR)
        if image is None:
            raise RuntimeError(f"{source} 图片解码失败")
        return image

    def _load_image_bgr(self, image_path: str) -> Any:
        cv2, _ = self._load_opencv_numpy()
        image = cv2.imread(image_path, cv2.IMREAD_COLOR)
        if image is None:
            raise RuntimeError(f"图片解码失败: {image_path}")
        return image

    def _match_template_center(
        self,
        screen_bgr: Any,
        template_bgr: Any,
        threshold: Optional[float] = None,
        action_name: str = "click_image",
    ) -> Tuple[float, float, float]:
        threshold_value = float(threshold or self._IMAGE_MATCH_THRESHOLD)
        cv2, _ = self._load_opencv_numpy()
        screen_gray = cv2.cvtColor(screen_bgr, cv2.COLOR_BGR2GRAY)
        template_gray_raw = cv2.cvtColor(template_bgr, cv2.COLOR_BGR2GRAY)

        sh, sw = screen_gray.shape[:2]
        th_raw, tw_raw = template_gray_raw.shape[:2]
        if th_raw <= 0 or tw_raw <= 0:
            raise RuntimeError("模板图尺寸无效")

        best_score = -1.0
        best_loc = (0, 0)
        best_size = (tw_raw, th_raw)
        scales = [1.0, 0.95, 1.05, 0.9, 1.1, 0.85, 1.15, 0.8, 1.2]
        for scale in scales:
            tw = max(1, int(round(tw_raw * scale)))
            th = max(1, int(round(th_raw * scale)))
            if tw > sw or th > sh:
                continue

            if scale == 1.0:
                template_gray = template_gray_raw
            else:
                template_gray = cv2.resize(
                    template_gray_raw,
                    (tw, th),
                    interpolation=cv2.INTER_AREA if scale < 1.0 else cv2.INTER_LINEAR,
                )
            result = cv2.matchTemplate(screen_gray, template_gray, cv2.TM_CCOEFF_NORMED)
            _, score, _, loc = cv2.minMaxLoc(result)
            if score > best_score:
                best_score = float(score)
                best_loc = (int(loc[0]), int(loc[1]))
                best_size = (tw, th)

        if best_score < threshold_value:
            raise RuntimeError(
                f"{action_name} 未匹配到足够置信度目标: score={best_score:.4f}, threshold={threshold_value:.2f}"
            )

        bx, by = best_loc
        bw, bh = best_size
        center_x = float(bx + bw / 2)
        center_y = float(by + bh / 2)
        return center_x, center_y, best_score

    def _extract_text_from_image(self, image_bgr: Any) -> str:
        if image_bgr is None or getattr(image_bgr, "size", 0) == 0:
            return ""

        ocr_engine = self._get_ocr_engine()
        result = run_paddle_ocr(ocr_engine, image_bgr, use_cls=False)
        return extract_ocr_text(result)

    def _tap_alert_button(
        self,
        selector: str,
        timeout: int = 2,
        errors: Optional[List[str]] = None,
        require_page_change: bool = False,
        timing_metrics: Optional[Dict[str, Any]] = None,
    ) -> bool:
        text = str(selector or "").strip()
        if not text:
            return False

        candidates: List[Tuple[str, str]] = []
        for predicate in self._build_alert_button_predicates(text):
            candidates.append(("predicate", predicate))

        deadline = time.time() + max(0.15, float(timeout or 0))
        for index, (by, candidate) in enumerate(candidates):
            remaining = max(0.0, deadline - time.time())
            if remaining <= 0.03:
                if errors is not None:
                    errors.append("alert:budget-exhausted")
                break
            candidate_timeout = max(0.08, remaining / max(1, len(candidates) - index))
            try:
                local_metrics: Dict[str, Any] = {}
                before_signature = ""
                had_alert_before = False
                if require_page_change:
                    sig_started = time.time()
                    had_alert_before = self._has_alert_or_sheet(
                        timeout=min(0.15, candidate_timeout),
                        reuse_window=0.0,
                    )
                    before_signature = self._capture_page_signature(
                        mode="quick",
                        screenshot_timeout=min(1.0, candidate_timeout + 0.3),
                    )
                    local_metrics["effect_sig_ms"] = self._ms(time.time() - sig_started)
                selector_obj = self._build_selector(candidate, by)
                if not self._wait_selector(selector_obj, timeout=candidate_timeout):
                    continue
                el = selector_obj.get(timeout=candidate_timeout, raise_error=True)
                el.tap()
                if require_page_change:
                    effect_timeout = min(0.5, max(0.12, deadline - time.time()))
                    wait_started = time.time()
                    changed = self._wait_confirm_click_effect(
                        selector=text,
                        before_signature=before_signature,
                        timeout=effect_timeout,
                        interval=0.12,
                        mode="quick",
                        screenshot_timeout=min(1.0, effect_timeout + 0.3),
                        selector_by=None,
                        had_alert_before=had_alert_before,
                    )
                    local_metrics["effect_wait_ms"] = round(
                        float(local_metrics.get("effect_wait_ms") or 0.0) + self._ms(time.time() - wait_started),
                        1,
                    )
                    if isinstance(timing_metrics, dict):
                        self._merge_timing_metrics(timing_metrics, local_metrics)
                    if not changed:
                        if errors is not None:
                            errors.append("alert:tap-no-effect")
                        continue
                elif isinstance(timing_metrics, dict) and local_metrics:
                    self._merge_timing_metrics(timing_metrics, local_metrics)
                return True
            except Exception as exc:
                if errors is not None:
                    errors.append(f"alert:{by}:{exc}")
                continue
        return False

    @staticmethod
    def _normalize_text_for_match(value: str) -> str:
        text = str(value or "").strip().lower()
        if not text:
            return ""
        text = re.sub(r"\s+", "", text)
        text = re.sub(r"[^\w\u4e00-\u9fff]", "", text)
        return text

    def _is_confirm_action_text(self, selector: str) -> bool:
        normalized = self._normalize_text_for_match(selector)
        if not normalized:
            return False
        confirm_tokens = {
            "确定",
            "确认",
            "允许",
            "同意",
            "知道了",
            "我知道了",
            "好的",
            "好",
            "ok",
            "yes",
            "allow",
            "confirm",
        }
        return normalized in confirm_tokens

    def _tap_by_ocr_text(
        self,
        selector: str,
        errors: Optional[List[str]] = None,
        require_page_change: bool = False,
        step_context: Optional[Dict[str, Any]] = None,
        screenshot_timeout: Optional[float] = None,
        crop_bounds: Optional[Tuple[int, int, int, int]] = None,
        timing_metrics: Optional[Dict[str, Any]] = None,
        prefer_popup_crop: bool = False,
    ) -> bool:
        target_raw = str(selector or "").strip()
        target = self._normalize_text_for_match(target_raw)
        if not target:
            return False

        def _resolve_center_from_payload(payload: Dict[str, Any]) -> Tuple[Optional[Tuple[float, float]], Dict[str, Any]]:
            result = payload.get("result")
            offset_x, offset_y = payload.get("offset") or (0.0, 0.0)
            payload_metrics = dict(payload.get("metrics") or {})
            parse_started = time.time()
            items = iter_ocr_text_items(result)
            best_score = -1.0
            best_center: Optional[Tuple[float, float]] = None

            for item in items:
                try:
                    text = str(item.get("text") or "").strip()
                    if not text:
                        continue
                    normalized = self._normalize_text_for_match(text)
                    if not normalized:
                        continue
                    if len(target) <= 2:
                        matched = normalized == target
                    else:
                        matched = (target in normalized) or (normalized == target)
                    if not matched:
                        continue

                    score = item.get("score")
                    if score is None:
                        score = 0.8
                    else:
                        score = float(score)
                    if score < 0.6:
                        continue

                    points = item.get("box") or []
                    if not points:
                        continue
                    xs: List[float] = []
                    ys: List[float] = []
                    for point in points:
                        if not isinstance(point, (list, tuple)) or len(point) < 2:
                            continue
                        xs.append(float(point[0]) + float(offset_x))
                        ys.append(float(point[1]) + float(offset_y))
                    if not xs or not ys:
                        continue
                    center = (sum(xs) / len(xs), sum(ys) / len(ys))

                    bonus = 1.0 if normalized == target else 0.0
                    rank = score + bonus
                    if rank > best_score:
                        best_score = rank
                        best_center = center
                except Exception:
                    continue

            payload_metrics["ocr_parse_ms"] = self._ms(time.time() - parse_started)
            payload_metrics["ocr_matches"] = 1 if best_center else 0
            return best_center, payload_metrics

        payload_metrics_total: Dict[str, Any] = {}
        best_center: Optional[Tuple[float, float]] = None
        try:
            first_payload = self._get_step_ocr_result(
                step_context=step_context,
                timeout=screenshot_timeout,
                crop_bounds=crop_bounds,
                prefer_popup_crop=prefer_popup_crop,
            )
            best_center, first_metrics = _resolve_center_from_payload(first_payload)
            self._merge_timing_metrics(payload_metrics_total, first_metrics)

            should_retry_full = (
                best_center is None
                and prefer_popup_crop
                and crop_bounds is None
                and str(first_metrics.get("ocr_scope") or "").startswith("popup")
            )
            if should_retry_full:
                full_payload = self._get_step_ocr_result(
                    step_context=step_context,
                    timeout=screenshot_timeout,
                    crop_bounds=None,
                    prefer_popup_crop=False,
                )
                best_center, full_metrics = _resolve_center_from_payload(full_payload)
                self._merge_timing_metrics(payload_metrics_total, full_metrics)
        except Exception as exc:
            if errors is not None:
                errors.append(f"ocr:init:{exc}")
            return False

        if isinstance(step_context, dict):
            step_context.setdefault("artifacts", {})["ocr_timing"] = dict(payload_metrics_total)
        if isinstance(timing_metrics, dict):
            self._merge_timing_metrics(timing_metrics, payload_metrics_total)

        if not best_center:
            return False

        try:
            effect_metrics: Dict[str, Any] = {}
            before_signature = ""
            had_alert_before = False
            if require_page_change:
                sig_started = time.time()
                had_alert_before = self._has_alert_or_sheet(timeout=0.15, reuse_window=0.0)
                before_signature = self._capture_page_signature(
                    mode="quick",
                    screenshot_timeout=1.0,
                )
                effect_metrics["effect_sig_ms"] = self._ms(time.time() - sig_started)
            self.click_by_coordinates(best_center[0], best_center[1])
            if require_page_change:
                wait_started = time.time()
                changed = self._wait_confirm_click_effect(
                    selector=target_raw,
                    before_signature=before_signature,
                    timeout=0.8,
                    interval=0.12,
                    mode="quick",
                    screenshot_timeout=1.0,
                    selector_by="label",
                    had_alert_before=had_alert_before,
                )
                effect_metrics["effect_wait_ms"] = self._ms(time.time() - wait_started)
                if isinstance(timing_metrics, dict):
                    self._merge_timing_metrics(timing_metrics, effect_metrics)
                if not changed:
                    if errors is not None:
                        errors.append("ocr:tap-no-effect")
                    return False
            elif isinstance(timing_metrics, dict) and effect_metrics:
                self._merge_timing_metrics(timing_metrics, effect_metrics)
            return True
        except Exception as exc:
            if errors is not None:
                errors.append(f"ocr:tap:{exc}")
            return False

    @staticmethod
    def _apply_extract_rule(raw_text: str, options: Dict[str, Any]) -> str:
        rule = str(options.get("extract_rule") or "preset").lower()

        if rule == "regex":
            pattern = options.get("custom_regex")
            if not pattern:
                raise ValueError("extract_rule=regex 时必须提供 custom_regex")
            match = re.search(str(pattern), raw_text, re.S)
            if not match:
                raise RuntimeError(f"正则未匹配到内容: {pattern}")
            if match.groups():
                for group in match.groups():
                    if group is not None:
                        return str(group).strip()
            return match.group(0).strip()

        if rule == "boundary":
            left = str(options.get("left_bound") or "")
            right = str(options.get("right_bound") or "")
            start = raw_text.find(left) + len(left) if left else 0
            if left and raw_text.find(left) < 0:
                raise RuntimeError(f"未找到左边界: {left}")
            end = raw_text.find(right, start) if right else len(raw_text)
            if right and end < 0:
                raise RuntimeError(f"未找到右边界: {right}")
            text = raw_text[start:end].strip()
            if not text:
                raise RuntimeError("边界提取结果为空")
            return text

        preset = str(options.get("preset_type") or "number_only").lower()
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
            raise RuntimeError(f"内置模板未匹配到内容: {preset}")
        text = match.group(0).strip()
        if preset == "price":
            text = re.sub(r"[¥￥$\s]", "", text)
        return text

    @staticmethod
    def _parse_region(selector: str) -> Tuple[float, float, float, float]:
        nums = re.findall(r"-?\d+(?:\.\d+)?", str(selector))
        if len(nums) != 4:
            raise ValueError(f"区域格式非法，应为 [x1, y1, x2, y2]，当前: {selector}")
        x1, y1, x2, y2 = map(float, nums)
        if x2 <= x1 or y2 <= y1:
            raise ValueError(f"区域坐标非法，需满足 x2>x1 且 y2>y1，当前: {selector}")
        return x1, y1, x2, y2
