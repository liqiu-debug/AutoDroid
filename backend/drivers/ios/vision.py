"""
IOSDriver 截图 / OCR / 图像匹配 Mixin

从 backend.drivers.ios_driver 拆出：截图抓取与步内缓存、裁剪坐标归一化、
OpenCV 模板匹配、OCR 引擎与步内 OCR 结果缓存、OCR 文本点击回退，
以及 OCR 提取规则与区域解析。
"""
import base64
import logging
import os
import re
import time
from typing import Any, Dict, List, Optional, Tuple

from backend.execution_errors import (
    E2004_ASSERT_IMAGE_FAILED,
    E2005_IMAGE_NOT_MATCHED,
    E2102_WDA_SESSION_ERROR,
    ExecutionStepRuntimeError,
)
from backend.ocr_service import get_ocr_engine
from backend.utils.ocr_compat import extract_ocr_text, iter_ocr_text_items, run_paddle_ocr

# 保持与拆分前一致的 logger 名称（backend.drivers.ios_driver），日志过滤与测试 assertLogs 兼容。
logger = logging.getLogger("backend.drivers.ios_driver")


class IOSVisionMixin:
    """截图、OCR 与图像匹配能力（IOSDriver 组成部分）。"""

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
            final_exc = ExecutionStepRuntimeError(
                E2102_WDA_SESSION_ERROR,
                f"iOS.screenshot 请求失败或超时(>{effective_timeout:.1f}s): {exc}",
                context={"timeout": effective_timeout},
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
            final_exc = ExecutionStepRuntimeError(
                E2102_WDA_SESSION_ERROR,
                "iOS.screenshot 返回内容不是有效 PNG",
                context={"timeout": effective_timeout},
            )
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
        """获取 OCR 引擎（使用全局单例服务）"""
        return get_ocr_engine(use_angle_cls=False, lang="ch")

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
            code = (
                E2004_ASSERT_IMAGE_FAILED
                if str(action_name or "").strip().lower() == "assert_image"
                else E2005_IMAGE_NOT_MATCHED
            )
            raise ExecutionStepRuntimeError(
                code,
                f"{action_name} 未匹配到足够置信度目标: score={best_score:.4f}, threshold={threshold_value:.2f}",
                context={
                    "score": round(best_score, 4),
                    "threshold": round(threshold_value, 2),
                },
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
