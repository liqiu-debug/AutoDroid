"""
IOSDriver 定位与候选回退 Mixin

从 backend.drivers.ios_driver 拆出：selector 构建/归一化/等待、候选回退，
弹窗探测与弹窗按钮点击、点击生效确认、click_with_fallback_plan 点击编排，
以及页面文本候选收集。
"""
import logging
import re
import time
import xml.etree.ElementTree as ET
from typing import Any, Dict, List, Optional, Tuple

import wda

from backend.execution_errors import (
    E2001_ELEMENT_NOT_FOUND,
    E2009_CLICK_NO_EFFECT,
    ExecutionStepRuntimeError,
)

# 保持与拆分前一致的 logger 名称（backend.drivers.ios_driver），日志过滤与测试 assertLogs 兼容。
logger = logging.getLogger("backend.drivers.ios_driver")


class IOSLocatorMixin:
    """定位、候选回退与弹窗点击链路（IOSDriver 组成部分）。"""

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
                raise ExecutionStepRuntimeError(
                    E2009_CLICK_NO_EFFECT,
                    "tap-no-effect",
                    context={"selector": selector_text, "by": by_text},
                )

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
        first_attempt = direct_attempts[0]
        final_exc = ExecutionStepRuntimeError(
            E2001_ELEMENT_NOT_FOUND,
            f"iOS.click_plan 失败: {detail}",
            context={
                "selector": first_attempt.get("selector"),
                "by": first_attempt.get("source_by") or first_attempt.get("by"),
                "timeout": round(total_timeout, 2),
                "attempts": len(errors),
            },
        )
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

        raise ExecutionStepRuntimeError(
            E2001_ELEMENT_NOT_FOUND,
            f"元素未找到: selector={selector!r}, by={by!r}, timeout={timeout}, attempts={attempts}",
            context={"selector": selector, "by": by, "timeout": timeout},
        )

    def _get_element(self, selector: str, by: str, timeout: int = 5) -> wda.Element:
        """
        获取元素（带等待）。
        """
        selector_obj, resolved_by = self._resolve_selector(selector, by, timeout=timeout)
        try:
            return selector_obj.get(timeout=timeout, raise_error=True)
        except Exception as exc:
            raise ExecutionStepRuntimeError(
                E2001_ELEMENT_NOT_FOUND,
                f"iOS 元素获取失败: selector={selector!r}, by={by!r}, resolved_by={resolved_by!r}, error={exc}",
                context={"selector": selector, "by": by, "resolved_by": resolved_by},
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
