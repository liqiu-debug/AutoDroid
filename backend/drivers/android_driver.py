"""
AndroidDriver — 基于 uiautomator2 的 Android 端驱动

继承 BaseDriver 抽象基类，封装 uiautomator2 的 API 调用。
"""
import io
import hashlib
import logging
import os
import re
import threading
import time
import xml.etree.ElementTree as ET
from typing import Any, Dict, List, Optional, Tuple

import uiautomator2 as u2

from .base_driver import BaseDriver
from backend.utils.ocr_compat import create_paddle_ocr_engine, extract_ocr_text, run_paddle_ocr
from backend.utils.template_match import find_template_match, image_to_bgr, load_image_bgr

logger = logging.getLogger(__name__)


class AndroidDriver(BaseDriver):
    """
    Android 设备驱动，使用 uiautomator2 库操控设备。

    支持的定位策略 (by):
        - "id" / "resourceId" : 通过 resourceId 定位
        - "text"              : 通过文本定位（精确匹配）
        - "xpath"             : 通过 XPath 表达式定位
        - "description"       : 通过 content-desc 定位
    """
    _ocr_engine: Any = None
    _GET_TEXT_TIMEOUT_SECONDS = 0.8
    _CONFIRM_HINT_TOKENS = (
        "我知道了",
        "知道了",
        "确定",
        "确认",
        "允许",
        "同意",
        "好的",
        "ok",
        "yes",
        "allow",
        "confirm",
    )
    _IMAGE_MATCH_THRESHOLD = 0.9
    _IMAGE_CLICK_TIMEOUT_SECONDS = 5.0
    _IMAGE_ASSERT_TIMEOUT_SECONDS = 1.2
    _IMAGE_ASSERT_RECHECK_DELAY_SECONDS = 0.25
    _ASSERT_IMAGE_TEMPLATE_THRESHOLD = 0.95
    _ASSERT_IMAGE_SSIM_THRESHOLD = 0.9
    _ASSERT_IMAGE_FAST_FAIL_SIMILARITY = 0.98
    _ASSERT_IMAGE_FAST_FAIL_SSIM = 0.97

    def __init__(self, device_id: str) -> None:
        super().__init__(device_id)
        try:
            self._device: u2.Device = u2.connect(device_id)
            info = self._device.info
            logger.info(
                "Android 设备已连接: %s (SDK=%s)",
                device_id,
                info.get("sdkInt", "?"),
            )
        except Exception as exc:
            logger.error("Android 设备连接失败 [%s]: %s", device_id, exc)
            raise ConnectionError(f"Android 设备连接失败: {exc}") from exc

    @staticmethod
    def _truncate_log_value(value: Any, max_len: int = 96) -> str:
        text = str(value or "").strip()
        if len(text) <= max_len:
            return text
        return f"{text[: max_len - 3]}..."

    def _diag_common(self) -> Dict[str, Any]:
        return {
            "serial": getattr(self, "device_id", "?"),
        }

    @staticmethod
    def _normalize_text_for_match(value: Any) -> str:
        text = str(value or "").strip().lower()
        if not text:
            return ""
        text = re.sub(r"\s+", "", text)
        text = re.sub(r"[^\w\u4e00-\u9fff]", "", text)
        return text

    def _capture_page_signature_quick(self) -> str:
        try:
            image = self._device.screenshot(format="pillow")
            if image is None:
                return ""
            buf = io.BytesIO()
            image.save(buf, format="PNG")
            raw = buf.getvalue()
            if not raw:
                return ""
            return hashlib.sha1(raw).hexdigest()[:16]
        except Exception:
            return ""

    def _template_confirm_text_hint(self, image_path: str) -> str:
        cache = getattr(self, "_image_confirm_hint_cache", None)
        if not isinstance(cache, dict):
            cache = {}
            setattr(self, "_image_confirm_hint_cache", cache)

        target = str(image_path or "").strip()
        if target in cache:
            return str(cache.get(target) or "")

        hint = ""
        try:
            from PIL import Image
            import cv2
            import numpy as np

            image = Image.open(target).convert("RGB")
            image_arr = cv2.cvtColor(np.array(image), cv2.COLOR_RGB2BGR)
            if image_arr.size > 0:
                ocr_engine = self._get_ocr_engine()
                result = run_paddle_ocr(ocr_engine, image_arr, use_cls=False)
                raw_text = extract_ocr_text(result)
                normalized = self._normalize_text_for_match(raw_text)
                if normalized:
                    for token in self._CONFIRM_HINT_TOKENS:
                        normalized_token = self._normalize_text_for_match(token)
                        if normalized_token and normalized_token in normalized:
                            hint = token
                            break
        except Exception:
            hint = ""

        cache[target] = hint
        return hint

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

    def _find_image_match(
        self,
        image_path: str,
        timeout: float,
        threshold: Optional[float] = None,
    ) -> Optional[Dict[str, Any]]:
        threshold_value = float(threshold or self._IMAGE_MATCH_THRESHOLD)
        match = self._device.image.wait(image_path, timeout=timeout, threshold=threshold_value)
        if isinstance(match, dict):
            return match
        return None

    def _find_image_match_strict(self, image_path: str) -> Optional[Dict[str, Any]]:
        screen_image = self._device.screenshot(format="opencv")
        screen_bgr = image_to_bgr(screen_image, source="screenshot")
        template_bgr = load_image_bgr(image_path)
        return find_template_match(
            screen_bgr=screen_bgr,
            template_bgr=template_bgr,
            threshold=self._ASSERT_IMAGE_TEMPLATE_THRESHOLD,
            ssim_threshold=self._ASSERT_IMAGE_SSIM_THRESHOLD,
        )

    @staticmethod
    def _extract_image_match_meta(match: Optional[Dict[str, Any]]) -> Dict[str, Any]:
        if not isinstance(match, dict):
            return {}
        meta: Dict[str, Any] = {}
        point = match.get("point")
        if isinstance(point, (list, tuple)) and len(point) >= 2:
            meta["x"] = round(float(point[0]), 1)
            meta["y"] = round(float(point[1]), 1)
        if match.get("similarity") is not None:
            meta["similarity"] = round(float(match.get("similarity") or 0.0), 4)
        if match.get("ssim") is not None:
            meta["ssim"] = round(float(match.get("ssim") or 0.0), 4)
        return meta

    def _is_strong_image_match(self, match: Optional[Dict[str, Any]]) -> bool:
        if not isinstance(match, dict):
            return False
        similarity = float(match.get("similarity") or 0.0)
        ssim = match.get("ssim")
        if ssim is None:
            return similarity >= 0.995
        return (
            similarity >= self._ASSERT_IMAGE_FAST_FAIL_SIMILARITY
            and float(ssim) >= self._ASSERT_IMAGE_FAST_FAIL_SSIM
        )

    def _classify_exception(self, exc: Exception) -> str:
        text = str(exc or "").strip().lower()
        exc_name = exc.__class__.__name__.lower()

        if any(token in text for token in ("timeout", "timed out", "等待超时")):
            return "TIMEOUT"
        if any(token in text for token in (
            "connection refused",
            "connection reset",
            "device offline",
            "adb",
            "uiautomator",
            "rpc error",
        )):
            return "CONNECTION"
        if any(token in text for token in ("元素未找到", "not found", "no such element", "no object")):
            return "ELEMENT_NOT_FOUND"
        if any(token in text for token in ("verify-failed", "unverifiable", "actual-empty")):
            return "INPUT_VALIDATION"
        if "assert" in exc_name or "断言失败" in text:
            return "ASSERTION"
        if "ocr" in text or "未识别到文本" in text:
            return "OCR"
        if any(token in text for token in ("app_start", "app_stop", "app_id", "package")):
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
            "Android.%s success: duration=%.3fs %s",
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
            "Android.%s failed: duration=%.3fs category=%s %s error=%s",
            action,
            max(time.time() - started_at, 0.0),
            self._classify_exception(exc),
            field_text,
            self._truncate_log_value(exc),
        )

    # ------------------------------------------------------------------ #
    #  内部：根据 by 策略定位元素
    # ------------------------------------------------------------------ #

    def _find_element(self, selector: str, by: str) -> Any:
        """
        根据 by 策略查找 UI 元素。

        Args:
            selector: 定位值。
            by: 定位策略名称。

        Returns:
            uiautomator2 UiObject 或 XPathSelector。

        Raises:
            ValueError: 不支持的 by 策略。
        """
        by_lower = by.lower()

        if by_lower in ("id", "resourceid", "resource_id"):
            return self._device(resourceId=selector)

        if by_lower == "text":
            el = self._device(text=selector)
            if not el.exists(timeout=2):
                # 降级：模糊匹配
                el = self._device(textContains=selector)
            return el

        if by_lower in ("label", "name"):
            # 兼容跨端步骤在 Android 上的回退语义
            el = self._device(text=selector)
            if not el.exists(timeout=2):
                el = self._device(textContains=selector)
            return el

        if by_lower == "xpath":
            return self._device.xpath(selector)

        if by_lower in ("description", "desc", "content-desc"):
            return self._device(description=selector)

        raise ValueError(f"Android 不支持的定位策略: by={by!r}")

    def _collect_page_text_candidates(self) -> List[str]:
        values: List[str] = []
        xml_text = ""

        try:
            xml_text = str(self._device.dump_hierarchy() or "")
        except Exception as exc:
            logger.warning("Android.dump_hierarchy failed: %s", exc)

        if xml_text:
            try:
                root = ET.fromstring(xml_text)
                for node in root.iter():
                    for attr_name in ("text", "content-desc", "contentDescription"):
                        value = str(node.attrib.get(attr_name) or "").strip()
                        if value:
                            values.append(value)
            except Exception as exc:
                logger.warning("Android.dump_hierarchy parse failed: %s", exc)
                values.extend(
                    match.strip()
                    for match in re.findall(r'(?:text|content-desc|contentDescription)="([^"]+)"', xml_text)
                    if str(match).strip()
                )

        deduped: List[str] = []
        seen = set()
        for item in values:
            if item in seen:
                continue
            seen.add(item)
            deduped.append(item)
        return deduped

    # ------------------------------------------------------------------ #
    #  BaseDriver 接口实现
    # ------------------------------------------------------------------ #

    def click(self, selector: str, by: str) -> None:
        """点击元素。"""
        started_at = time.time()
        logger.info("Android.click start: selector=%s by=%s", selector, by)
        try:
            el = self._find_element(selector, by)
            if not el.exists(timeout=5):
                raise RuntimeError(f"元素未找到: selector={selector!r}, by={by!r}")
            el.click()
            self._log_action_success(
                "click",
                started_at,
                selector=selector,
                by=by,
            )
        except Exception as exc:
            self._log_action_failure(
                "click",
                started_at,
                exc,
                selector=selector,
                by=by,
            )
            raise

    def _safe_element_info(self, el: Any) -> Dict[str, Any]:
        try:
            info = el.info or {}
            if isinstance(info, dict):
                return info
        except Exception:
            pass
        return {}

    def _resolve_input_target(self, el: Any) -> Any:
        info = self._safe_element_info(el)
        class_name = str(info.get("className") or "")
        if class_name == "android.widget.EditText":
            return el

        child_getter = getattr(el, "child", None)
        if callable(child_getter):
            try:
                child = child_getter(className="android.widget.EditText")
                if child is not None and child.exists(timeout=1):
                    return child
            except Exception:
                pass
        return el

    def _collect_input_candidates(self, el: Any) -> Tuple[List[str], bool]:
        info = self._safe_element_info(el)
        candidates: List[str] = []
        class_name = str(info.get("className") or "")
        is_edit_text = class_name == "android.widget.EditText"

        for key in ("text", "contentDescription", "hint"):
            value = info.get(key)
            if value is None:
                continue
            candidates.append(str(value))

        # get_text 在部分控件/ROM 上可能出现长阻塞，仅在 EditText 上做限时调用。
        if is_edit_text:
            value = self._safe_get_text(el, timeout_seconds=self._GET_TEXT_TIMEOUT_SECONDS)
            if value is not None:
                candidates.append(str(value))

        unique_candidates: List[str] = []
        seen = set()
        for item in candidates:
            normalized = str(item or "")
            if normalized in seen:
                continue
            seen.add(normalized)
            unique_candidates.append(normalized)

        is_password = bool(info.get("password"))
        return unique_candidates, is_password

    def _safe_get_text(self, el: Any, timeout_seconds: float) -> Optional[str]:
        getter = getattr(el, "get_text", None)
        if not callable(getter):
            return None

        result_box: Dict[str, Any] = {}
        error_box: Dict[str, Exception] = {}

        def _worker() -> None:
            try:
                result_box["value"] = getter()
            except Exception as exc:  # pragma: no cover - defensive
                error_box["error"] = exc

        thread = threading.Thread(target=_worker, daemon=True)
        thread.start()
        thread.join(max(timeout_seconds, 0.1))

        if thread.is_alive():
            logger.warning("Android.input get_text timeout: %.2fs", timeout_seconds)
            return None
        if "error" in error_box:
            return None

        value = result_box.get("value")
        if value is None:
            return None
        return str(value)

    @staticmethod
    def _is_masked_password_text(text: str) -> bool:
        raw = str(text or "").strip()
        if not raw:
            return False
        masked_chars = {"*", "•", "●", "·", "﹡"}
        return all(ch in masked_chars for ch in raw)

    def _verify_input_result(
        self,
        el: Any,
        expected_text: str,
        password_hint: bool = False,
    ) -> Tuple[Optional[bool], str]:
        expected = "" if expected_text is None else str(expected_text)
        candidates, is_password = self._collect_input_candidates(el)
        info = self._safe_element_info(el)
        class_name = str(info.get("className") or "").lower()
        if "secure" in class_name or "password" in class_name:
            is_password = True
        if password_hint:
            is_password = True

        if expected == "":
            if not candidates:
                return True, "empty-expected"
            if any(item.strip() == "" for item in candidates):
                return True, "empty-matched"
            if is_password:
                return None, f"password-empty-unverifiable candidates={candidates!r}"
            return False, f"expected-empty-but-actual={candidates!r}"

        for actual in candidates:
            if actual == expected or (expected and expected in actual) or (actual and actual in expected):
                return True, f"matched actual={actual!r}"

        if is_password:
            for actual in candidates:
                if self._is_masked_password_text(actual) and len(actual) == len(expected):
                    return None, f"masked-length-matched actual={actual!r}"
            if not candidates:
                return None, "password-unreadable"
            return None, f"password-unverifiable actual={candidates!r}"

        if not candidates:
            return False, "actual-empty"
        return False, f"actual={candidates!r}"

    def _get_focused_element(self, timeout: int = 1) -> Optional[Any]:
        try:
            focused = self._device(focused=True)
            if focused is not None and focused.exists(timeout=timeout):
                return focused
        except Exception:
            return None
        return None

    def _verify_input_result_with_focused(
        self,
        el: Any,
        expected_text: str,
        password_hint: bool = False,
    ) -> Tuple[Optional[bool], str]:
        primary_status, primary_detail = self._verify_input_result(
            el,
            expected_text,
            password_hint=password_hint,
        )
        details = [f"target:{primary_detail}"]
        if primary_status is True:
            return True, "; ".join(details)

        focused = self._get_focused_element(timeout=1)
        if focused is not None:
            focused_status, focused_detail = self._verify_input_result(
                focused,
                expected_text,
                password_hint=password_hint,
            )
            details.append(f"focused:{focused_detail}")
            if focused_status is True:
                return True, "; ".join(details)
            if primary_status is None or focused_status is None:
                return None, "; ".join(details)

        if primary_status is None:
            return None, "; ".join(details)
        return False, "; ".join(details)

    def _capture_input_state(self, el: Any) -> Dict[str, List[str]]:
        target_candidates, _ = self._collect_input_candidates(el)
        focused_candidates: List[str] = []
        focused = self._get_focused_element(timeout=0)
        if focused is not None:
            focused_candidates, _ = self._collect_input_candidates(focused)
        return {
            "target": target_candidates,
            "focused": focused_candidates,
        }

    @staticmethod
    def _input_state_changed(before: Dict[str, List[str]], after: Dict[str, List[str]]) -> bool:
        return (
            list(before.get("target") or []) != list(after.get("target") or [])
            or list(before.get("focused") or []) != list(after.get("focused") or [])
        )

    def _has_masked_value(self, state: Dict[str, List[str]]) -> bool:
        for candidate in list(state.get("target") or []) + list(state.get("focused") or []):
            if self._is_masked_password_text(candidate):
                return True
        return False

    def _is_password_intent(self, selector: str, target: Any) -> bool:
        normalized_selector = str(selector or "").strip().lower()
        if "password" in normalized_selector or "密码" in normalized_selector:
            return True

        def _is_password_by_info(element: Any) -> bool:
            info = self._safe_element_info(element)
            if bool(info.get("password")):
                return True
            class_name = str(info.get("className") or "").lower()
            if "secure" in class_name or "password" in class_name:
                return True
            return False

        if _is_password_by_info(target):
            return True

        focused = self._get_focused_element(timeout=0)
        if focused is not None and _is_password_by_info(focused):
            return True
        return False

    @staticmethod
    def _to_adb_input_text(text: str) -> str:
        return str(text or "").replace(" ", "%s")

    @staticmethod
    def _is_ascii_text(text: str) -> bool:
        try:
            str(text or "").encode("ascii")
            return True
        except Exception:
            return False

    def input(self, selector: str, by: str, text: str) -> None:
        """
        向元素输入文本。

        流程：定位 → 聚焦 → 输入 → 结果校验（必要时多策略回退）。
        """
        started_at = time.time()
        text_value = "" if text is None else str(text)
        text_len = len(text_value)
        errors: List[str] = []
        detail: Optional[str] = None
        is_password_intent = False

        logger.info("Android.input start: selector=%s by=%s text_len=%s", selector, by, text_len)
        try:
            el = self._find_element(selector, by)
            if not el.exists(timeout=5):
                raise RuntimeError(f"元素未找到: selector={selector!r}, by={by!r}")

            target = self._resolve_input_target(el)
            is_password_intent = self._is_password_intent(selector=selector, target=target)

            strategies = []
            strategies.append(("set_text", lambda: target.set_text(text_value)))

            def _focused_set_text():
                focused = self._get_focused_element(timeout=1)
                if focused is None:
                    raise RuntimeError("focused element not found")
                try:
                    focused.clear_text()
                except Exception:
                    pass
                focused.set_text(text_value)

            strategies.append(("focused.set_text", _focused_set_text))

            if self._is_ascii_text(text_value) and not is_password_intent:
                strategies.append(
                    (
                        "device.shell(input text)",
                        lambda: self._device.shell(f"input text {self._to_adb_input_text(text_value)}"),
                    )
                )

            for strategy_name, strategy in strategies:
                try:
                    try:
                        target.click()
                    except Exception:
                        pass
                    try:
                        target.clear_text()
                    except Exception:
                        pass

                    state_before = self._capture_input_state(target)
                    strategy()
                    verified, verify_detail = self._verify_input_result_with_focused(
                        target,
                        text_value,
                        password_hint=is_password_intent,
                    )
                    state_after = self._capture_input_state(target)
                    if verified is True:
                        self._log_action_success(
                            "input",
                            started_at,
                            selector=selector,
                            by=by,
                            text_len=text_len,
                            strategy=strategy_name,
                            attempts=len(errors) + 1,
                            verification="matched",
                            detail=verify_detail,
                            password=is_password_intent,
                        )
                        return
                    if verified is None:
                        if is_password_intent:
                            if self._input_state_changed(state_before, state_after) and self._has_masked_value(state_after):
                                self._log_action_success(
                                    "input",
                                    started_at,
                                    selector=selector,
                                    by=by,
                                    text_len=text_len,
                                    strategy=strategy_name,
                                    attempts=len(errors) + 1,
                                    verification="masked-state-change",
                                    detail=verify_detail,
                                    password=is_password_intent,
                                )
                                return
                            errors.append(
                                f"{strategy_name}: unverifiable-no-state-change ({verify_detail}) before={state_before} after={state_after}"
                            )
                            continue

                        self._log_action_success(
                            "input",
                            started_at,
                            selector=selector,
                            by=by,
                            text_len=text_len,
                            strategy=strategy_name,
                            attempts=len(errors) + 1,
                            verification="unverifiable",
                            detail=verify_detail,
                            password=is_password_intent,
                        )
                        return

                    errors.append(f"{strategy_name}: verify-failed ({verify_detail})")
                except Exception as exc:
                    errors.append(f"{strategy_name}: {exc}")

            detail = "; ".join(errors) if errors else "unknown"
            raise RuntimeError(
                f"Android.input 执行失败: selector={selector!r}, by={by!r}, text_len={text_len}, detail={detail}"
            )
        except Exception as exc:
            self._log_action_failure(
                "input",
                started_at,
                exc,
                selector=selector,
                by=by,
                text_len=text_len,
                attempts=len(errors) if errors else None,
                detail=detail,
                password=is_password_intent,
            )
            raise

    def input_focused(self, text: str) -> None:
        """
        向当前焦点输入框输入文本（无定位器模式）。
        """
        started_at = time.time()
        text_value = "" if text is None else str(text)
        text_len = len(text_value)
        logger.info("Android.input_focused start: text_len=%s", text_len)
        errors = []
        detail: Optional[str] = None

        try:
            try:
                focused = self._device(focused=True)
                if focused.exists(timeout=1):
                    try:
                        focused.clear_text()
                    except Exception:
                        pass
                    focused.set_text(text_value)
                    self._log_action_success(
                        "input_focused",
                        started_at,
                        text_len=text_len,
                        strategy="focused.set_text",
                        attempts=1,
                    )
                    return
                errors.append("focused element not found")
            except Exception as exc:
                errors.append(f"focused element failed: {exc}")

            try:
                self._device.send_keys(text_value, clear=True)
                self._log_action_success(
                    "input_focused",
                    started_at,
                    text_len=text_len,
                    strategy="send_keys(clear=True)",
                    attempts=len(errors) + 1,
                )
                return
            except Exception as exc:
                errors.append(f"send_keys(clear=True) failed: {exc}")

            try:
                self._device.send_keys(text_value)
                self._log_action_success(
                    "input_focused",
                    started_at,
                    text_len=text_len,
                    strategy="send_keys",
                    attempts=len(errors) + 1,
                )
                return
            except Exception as exc:
                errors.append(f"send_keys failed: {exc}")

            detail = "; ".join(errors) if errors else "unknown"
            raise RuntimeError(f"Android.input_focused 执行失败: {detail}")
        except Exception as exc:
            self._log_action_failure(
                "input_focused",
                started_at,
                exc,
                text_len=text_len,
                attempts=len(errors) if errors else None,
                detail=detail,
            )
            raise

    def screenshot(self) -> bytes:
        """截取当前屏幕，返回 PNG 字节流。"""
        started_at = time.time()
        try:
            image = self._device.screenshot(format="pillow")
            buf = io.BytesIO()
            image.save(buf, format="PNG")
            raw = buf.getvalue()
            self._log_action_success(
                "screenshot",
                started_at,
                bytes=len(raw),
            )
            return raw
        except Exception as exc:
            self._log_action_failure("screenshot", started_at, exc)
            raise

    def click_by_coordinates(self, x: float, y: float) -> None:
        """按物理坐标点击屏幕（uiautomator2 直接使用像素坐标）。"""
        started_at = time.time()
        logger.info("Android.click_by_coordinates start: x=%.1f y=%.1f", x, y)
        try:
            self._device.click(int(x), int(y))
            self._log_action_success(
                "click_by_coordinates",
                started_at,
                x=round(x, 1),
                y=round(y, 1),
            )
        except Exception as exc:
            self._log_action_failure(
                "click_by_coordinates",
                started_at,
                exc,
                x=round(x, 1),
                y=round(y, 1),
            )
            raise

    def wait_until_exists(self, selector: str, by: str, timeout: int = 10) -> None:
        started_at = time.time()
        logger.info(
            "Android.wait_until_exists start: selector=%s by=%s timeout=%s",
            selector, by, timeout,
        )
        try:
            el = self._find_element(selector, by)
            if not el.exists(timeout=timeout):
                raise RuntimeError(
                    f"等待超时，元素未出现: selector={selector!r}, by={by!r}, timeout={timeout}"
                )
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
            "Android.assert_text start: expected=%s match_mode=%s",
            expected_text,
            normalized_mode,
        )
        try:
            expected = str(expected_text or "")
            if not expected.strip():
                raise ValueError("assert_text expected_text 不能为空")

            candidates = self._collect_page_text_candidates()
            matched = [candidate for candidate in candidates if expected in candidate]

            if normalized_mode == "contains" and matched:
                self._log_action_success(
                    "assert_text",
                    started_at,
                    expected_text=expected,
                    match_mode=normalized_mode,
                    candidates=len(candidates),
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

            preview = matched[:5] if matched else candidates[:5]
            if normalized_mode == "not_contains":
                raise AssertionError(
                    f"断言失败: 期望页面不包含 {expected!r}, 实际命中={preview!r}"
                )
            raise AssertionError(
                f"断言失败: 期望页面包含 {expected!r}, 实际候选={preview!r}"
            )
        except Exception as exc:
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
        try:
            if direction not in {"up", "down", "left", "right"}:
                raise ValueError(f"不支持的滑动方向: {direction}")
            self._device.swipe_ext(direction, scale=0.8)
            self._log_action_success("swipe", started_at, direction=direction)
        except Exception as exc:
            self._log_action_failure("swipe", started_at, exc, direction=direction)
            raise

    def back(self) -> None:
        started_at = time.time()
        try:
            self._device.press("back")
            self._log_action_success("back", started_at)
        except Exception as exc:
            self._log_action_failure("back", started_at, exc)
            raise

    def home(self) -> None:
        started_at = time.time()
        try:
            self._device.press("home")
            self._log_action_success("home", started_at)
        except Exception as exc:
            self._log_action_failure("home", started_at, exc)
            raise

    def start_app(self, app_id: str) -> None:
        started_at = time.time()
        try:
            if not app_id:
                raise ValueError("start_app 需要 app_id（Android package）")
            self._device.app_start(app_id)
            self._log_action_success("start_app", started_at, app_id=app_id)
        except Exception as exc:
            self._log_action_failure("start_app", started_at, exc, app_id=app_id)
            raise

    def stop_app(self, app_id: str) -> None:
        started_at = time.time()
        try:
            if not app_id:
                raise ValueError("stop_app 需要 app_id（Android package）")
            self._device.app_stop(app_id)
            self._log_action_success("stop_app", started_at, app_id=app_id)
        except Exception as exc:
            self._log_action_failure("stop_app", started_at, exc, app_id=app_id)
            raise

    def click_image(self, image_path: str) -> None:
        started_at = time.time()
        target = ""
        text_hint = ""
        try:
            target = self._resolve_template_image_path(image_path, action_name="click_image")

            text_hint = self._template_confirm_text_hint(target)
            if text_hint:
                try:
                    self.click(selector=text_hint, by="text")
                    self._log_action_success(
                        "click_image",
                        started_at,
                        image_path=target,
                        strategy="text-hint",
                        hint=text_hint,
                    )
                    return
                except Exception as hint_exc:
                    logger.info(
                        "Android.click_image text-hint fallback failed, fallback to template match: hint=%s error=%s",
                        text_hint,
                        hint_exc,
                    )

            wait_started = time.time()
            match = self._find_image_match(
                target,
                timeout=self._IMAGE_CLICK_TIMEOUT_SECONDS,
                threshold=self._IMAGE_MATCH_THRESHOLD,
            )
            match_elapsed = time.time() - wait_started
            if not isinstance(match, dict):
                raise RuntimeError(f"图像模板匹配失败: 未在屏幕上找到匹配的图像区域 ({target})")
            point = match.get("point")
            if not isinstance(point, (list, tuple)) or len(point) < 2:
                raise RuntimeError(f"图像模板匹配失败: 匹配结果缺少坐标 point ({target})")
            x = float(point[0])
            y = float(point[1])
            similarity = float(match.get("similarity") or 0.0)

            before_signature = self._capture_page_signature_quick()
            self._device.click(int(x), int(y))
            time.sleep(0.12)
            after_signature = self._capture_page_signature_quick()

            # 当匹配耗时很长且点击后页面无变化时，进行一次同位重试，减少“看起来成功但没触发”的误报。
            retried = False
            if (
                before_signature
                and after_signature
                and before_signature == after_signature
                and match_elapsed >= 3.0
            ):
                retried = True
                self._device.click(int(x), int(y))
                time.sleep(0.12)
                retry_signature = self._capture_page_signature_quick()
                if retry_signature and retry_signature == before_signature:
                    raise RuntimeError("图像模板匹配点击后页面未变化（tap-no-effect）")

            self._log_action_success(
                "click_image",
                started_at,
                image_path=target,
                timeout=self._IMAGE_CLICK_TIMEOUT_SECONDS,
                strategy="template-match",
                similarity=round(similarity, 4),
                x=round(x, 1),
                y=round(y, 1),
                retried=retried,
                hint=text_hint,
            )
        except Exception as exc:
            self._log_action_failure(
                "click_image",
                started_at,
                exc,
                image_path=target,
                hint=text_hint,
            )
            raise

    def assert_image(self, image_path: str, match_mode: str = "exists") -> None:
        started_at = time.time()
        target = ""
        normalized_mode = "not_exists" if str(match_mode or "").strip().lower() == "not_exists" else "exists"
        logger.info(
            "Android.assert_image start: path=%s match_mode=%s",
            image_path,
            normalized_mode,
        )
        try:
            target = self._resolve_template_image_path(image_path, action_name="assert_image")

            if normalized_mode == "exists":
                match = self._find_image_match_strict(target)
                if not isinstance(match, dict):
                    raise AssertionError(f"断言失败: 期望页面存在图像 {target!r}，但未匹配到")
                self._log_action_success(
                    "assert_image",
                    started_at,
                    image_path=target,
                    match_mode=normalized_mode,
                    **self._extract_image_match_meta(match),
                )
                return

            first_match = self._find_image_match_strict(target)
            if first_match is None:
                self._log_action_success(
                    "assert_image",
                    started_at,
                    image_path=target,
                    match_mode=normalized_mode,
                )
                return

            if self._is_strong_image_match(first_match):
                meta = self._extract_image_match_meta(first_match)
                raise AssertionError(
                    f"断言失败: 期望页面不存在图像 {target!r}，但已高置信度匹配到目标"
                    + (
                        f" (similarity={meta.get('similarity')}, ssim={meta.get('ssim')}, x={meta.get('x')}, y={meta.get('y')})"
                        if meta else ""
                    )
                )

            time.sleep(self._IMAGE_ASSERT_RECHECK_DELAY_SECONDS)
            second_match = self._find_image_match_strict(target)
            confirmed_match = second_match if second_match is not None else None
            if confirmed_match is not None:
                meta = self._extract_image_match_meta(confirmed_match)
                raise AssertionError(
                    f"断言失败: 期望页面不存在图像 {target!r}，但仍匹配到目标"
                    + (f" (similarity={meta.get('similarity')}, x={meta.get('x')}, y={meta.get('y')})" if meta else "")
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
        crop_desc: Optional[str] = None
        try:
            if not region:
                raise ValueError("extract_by_ocr 需要 region")

            x1, y1, x2, y2 = self._parse_region(region)
            image = self._device.screenshot(format="pillow")
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
                raise ValueError(f"extract_by_ocr 区域无效: [{rx1},{ry1},{rx2},{ry2}]")

            crop_desc = f"[{rx1},{ry1},{rx2},{ry2}]"
            raw_text = self._extract_text_from_screenshot(image, rx1, ry1, rx2, ry2)
            if not raw_text:
                raise RuntimeError("extract_by_ocr 未识别到文本")
            extracted = self._apply_extract_rule(raw_text, extract_rule or {})
            self._log_action_success(
                "extract_by_ocr",
                started_at,
                region=region,
                crop=crop_desc,
                raw_len=len(raw_text),
                extracted_len=len(str(extracted or "")),
            )
            return extracted
        except Exception as exc:
            self._log_action_failure(
                "extract_by_ocr",
                started_at,
                exc,
                region=region,
                crop=crop_desc,
            )
            raise

    def disconnect(self) -> None:
        """断开设备连接（uiautomator2 无显式断开，仅做日志记录）。"""
        started_at = time.time()
        try:
            self._log_action_success("disconnect", started_at)
        finally:
            super().disconnect()

    @classmethod
    def _get_ocr_engine(cls) -> Any:
        if cls._ocr_engine is None:
            try:
                logger.debug("Android OCR engine loading")
                cls._ocr_engine = create_paddle_ocr_engine(use_angle_cls=False, lang="ch")
                logger.debug("Android OCR engine ready")
            except Exception as exc:
                raise RuntimeError(
                    "extract_by_ocr 依赖缺失: 请安装 paddleocr 及其依赖"
                ) from exc
        return cls._ocr_engine

    def _extract_text_from_screenshot(
        self,
        image: Any,
        x1: int,
        y1: int,
        x2: int,
        y2: int,
    ) -> str:
        try:
            import cv2
            import numpy as np
        except Exception as exc:
            raise RuntimeError(
                "extract_by_ocr 依赖缺失: 请安装 opencv-python 与 numpy"
            ) from exc

        crop = image.crop((x1, y1, x2, y2))
        img_arr = cv2.cvtColor(np.array(crop), cv2.COLOR_RGB2BGR)
        if img_arr.size == 0:
            return ""

        ocr_engine = self._get_ocr_engine()
        result = run_paddle_ocr(ocr_engine, img_arr, use_cls=False)
        return extract_ocr_text(result)

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
