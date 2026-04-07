"""
Cross-platform case runner.

对标准步骤执行统一编排：
- 平台拦截（execute_on）
- 动作分发（action + args）
- 容错策略（ABORT/CONTINUE/IGNORE）
"""
from __future__ import annotations

import base64
import logging
import re
import threading
import time
from typing import Any, Dict, List, Optional, Type

from backend.locator_resolution import resolve_locator_candidates
from backend.utils.variable_render import render_step_data
from backend.step_contract import (
    normalize_action,
    normalize_error_strategy,
    normalize_execute_on,
)

from .android_driver import AndroidDriver
from .base_driver import BaseDriver
from .ios_driver import IOSDriver

logger = logging.getLogger(__name__)
_UNRESOLVED_VAR_PATTERN = re.compile(r"{{\s*[A-Z0-9_]+\s*}}")
_SUPPORTED_ACTIONS_BY_PLATFORM = {
    "android": {
        "click",
        "input",
        "wait_until_exists",
        "assert_text",
        "assert_image",
        "click_image",
        "extract_by_ocr",
        "sleep",
        "swipe",
        "back",
        "home",
        "start_app",
        "stop_app",
    },
    "ios": {
        "click",
        "input",
        "wait_until_exists",
        "assert_text",
        "assert_image",
        "click_image",
        "extract_by_ocr",
        "sleep",
        "swipe",
        "back",
        "home",
        "start_app",
        "stop_app",
    },
}


def _with_error_code(code: str, message: str) -> str:
    return f"{code}: {message}"


class DriverFactory:
    """平台驱动工厂。"""

    _registry: Dict[str, Type[BaseDriver]] = {
        "android": AndroidDriver,
        "ios": IOSDriver,
    }

    @classmethod
    def register(cls, platform: str, driver_class: Type[BaseDriver]) -> None:
        cls._registry[platform.lower()] = driver_class
        logger.info("DriverFactory.register: %s -> %s", platform, driver_class.__name__)

    @classmethod
    def create(cls, platform: str, device_id: str, **kwargs: Any) -> BaseDriver:
        platform_lower = str(platform or "").strip().lower()
        driver_cls = cls._registry.get(platform_lower)
        if driver_cls is None:
            supported = ", ".join(sorted(cls._registry.keys()))
            raise ValueError(f"不支持的平台: {platform!r}，当前支持: {supported}")
        return driver_cls(device_id=device_id, **kwargs)


class TestCaseRunner:
    """标准步骤执行器（单设备）。"""

    def __init__(
        self,
        platform: str,
        device_id: str,
        abort_event: Optional[threading.Event] = None,
        **driver_kwargs: Any,
    ) -> None:
        self.platform = str(platform or "").strip().lower()
        self.device_id = device_id
        self.abort_event = abort_event
        self.driver = DriverFactory.create(self.platform, device_id, **driver_kwargs)
        self.runtime_variables: Dict[str, str] = {}
        logger.info(
            "Cross-platform runner ready: platform=%s device_id=%s driver=%s",
            self.platform,
            self.device_id,
            self.driver.__class__.__name__,
        )

    def run_step(self, step_data: Dict[str, Any]) -> Dict[str, Any]:
        """执行单步并返回结构化结果。"""
        started_at = time.time()
        step_context: Dict[str, Any] = {}

        raw_action = step_data.get("action")
        action = str(raw_action or "").strip().lower() or "unknown"
        raw_strategy = step_data.get("error_strategy", "ABORT")
        strategy = "ABORT"
        timeout = _parse_timeout(step_data.get("timeout"), default=10)

        if self.abort_event and self.abort_event.is_set():
            return self._build_abort_result(
                step_data=step_data,
                action=action,
                error_strategy=normalize_error_strategy(raw_strategy),
                duration=time.time() - started_at,
            )

        try:
            try:
                action = normalize_action(raw_action)
            except Exception as exc:
                raise NotImplementedError(
                    _with_error_code("P1002_ACTION_NOT_SUPPORTED", str(exc))
                ) from exc
            strategy = normalize_error_strategy(raw_strategy)
            execute_on = normalize_execute_on(step_data.get("execute_on"))

            if self.platform not in execute_on:
                return self._result(
                    step_data=step_data,
                    action=action,
                    status="SKIP",
                    error_strategy=strategy,
                    error=_with_error_code(
                        "P1001_PLATFORM_NOT_ALLOWED",
                        f"execute_on={execute_on}, current={self.platform}",
                    ),
                    output=None,
                    artifacts=None,
                    duration=time.time() - started_at,
                )

            args = step_data.get("args") or {}
            if not isinstance(args, dict):
                raise ValueError(
                    _with_error_code("P1006_INVALID_ARGS", "args must be an object")
                )
            value = step_data.get("value")
            args = _render_runtime_value(args, self.runtime_variables)
            value = _render_runtime_value(value, self.runtime_variables)
            locator_candidates = [
                {
                    "selector": _render_runtime_value(item.get("selector"), self.runtime_variables),
                    "by": _render_runtime_value(item.get("by"), self.runtime_variables),
                }
                for item in resolve_locator_candidates(step_data, platform=self.platform)
            ]
            unresolved = _collect_unresolved_templates(
                {
                    "args": args,
                    "value": value,
                    "locators": locator_candidates,
                }
            )
            if unresolved:
                preview = ", ".join(unresolved[:3])
                raise ValueError(
                    _with_error_code(
                        "P1006_INVALID_ARGS",
                        f"存在未解析变量占位符: {preview}",
                    )
                )

            dispatch_output = self._dispatch(
                step_data=step_data,
                action=action,
                locator_candidates=locator_candidates,
                args=args,
                value=value,
                timeout=timeout,
                step_context=step_context,
            )

            if isinstance(dispatch_output, dict):
                export_var = str(dispatch_output.get("export_var") or "").strip()
                if export_var:
                    export_value = dispatch_output.get("export_value")
                    self.runtime_variables[export_var] = (
                        "" if export_value is None else str(export_value)
                    )

            return self._result(
                step_data=step_data,
                action=action,
                status="PASS",
                error_strategy=strategy,
                error=None,
                output=dispatch_output,
                artifacts=None,
                duration=time.time() - started_at,
            )
        except Exception as exc:
            return self._result(
                step_data=step_data,
                action=action,
                status="FAIL",
                error_strategy=strategy,
                error=str(exc),
                output=None,
                artifacts=self._extract_step_artifacts(step_context),
                duration=time.time() - started_at,
            )

    def run_all(self, steps: List[Dict[str, Any]]) -> Dict[str, Any]:
        """
        批量执行标准步骤，应用 ABORT/CONTINUE/IGNORE 容错策略。
        """
        results: List[Dict[str, Any]] = []
        overall_success = True
        total_steps = len(steps or [])

        for index, step_data in enumerate(steps or [], start=1):
            step_action = str((step_data or {}).get("action") or "").strip().lower() or "unknown"
            step_desc = str((step_data or {}).get("description") or "").strip()

            if self.abort_event and self.abort_event.is_set():
                result = self._build_abort_result(
                    step_data=step_data,
                    action=step_action,
                    error_strategy=normalize_error_strategy(
                        (step_data or {}).get("error_strategy", "ABORT")
                    ),
                )
                result["order"] = index
                results.append(result)
                overall_success = False
                logger.warning(
                    "runner aborted before step: %s/%s action=%s desc=%s",
                    index,
                    total_steps,
                    step_action,
                    step_desc or "-",
                )
                break

            logger.info(
                "runner step start: %s/%s action=%s desc=%s",
                index,
                total_steps,
                step_action,
                step_desc or "-",
            )
            result = self.run_step(step_data)
            result["order"] = index
            results.append(result)

            status = result.get("status")
            strategy = normalize_error_strategy(result.get("error_strategy", "ABORT"))

            if status in ("PASS", "SKIP"):
                logger.info(
                    "runner step end: %s/%s action=%s status=%s duration=%.3fs",
                    index,
                    total_steps,
                    step_action,
                    result.get("status"),
                    float(result.get("duration") or 0.0),
                )
                continue

            if status in ("FAIL", "WARNING"):
                screenshot = self._result_screenshot_base64(result)
                if not screenshot:
                    screenshot = self._capture_screenshot_base64()
                if screenshot:
                    result["screenshot"] = screenshot

            if status == "FAIL" and strategy == "IGNORE":
                result["status"] = "WARNING"
                result["warning"] = "step failed but ignored by error_strategy=IGNORE"
                logger.warning(
                    "runner step end: %s/%s action=%s status=%s duration=%.3fs error=%s",
                    index,
                    total_steps,
                    step_action,
                    result.get("status"),
                    float(result.get("duration") or 0.0),
                    result.get("error") or "-",
                )
                continue

            if status == "FAIL" and strategy == "CONTINUE":
                overall_success = False
                logger.warning(
                    "runner step end: %s/%s action=%s status=%s duration=%.3fs error=%s",
                    index,
                    total_steps,
                    step_action,
                    result.get("status"),
                    float(result.get("duration") or 0.0),
                    result.get("error") or "-",
                )
                continue

            if status == "FAIL" and strategy == "ABORT":
                overall_success = False
                logger.warning(
                    "runner step end: %s/%s action=%s status=%s duration=%.3fs error=%s",
                    index,
                    total_steps,
                    step_action,
                    result.get("status"),
                    float(result.get("duration") or 0.0),
                    result.get("error") or "-",
                )
                break

            logger.info(
                "runner step end: %s/%s action=%s status=%s duration=%.3fs",
                index,
                total_steps,
                step_action,
                result.get("status"),
                float(result.get("duration") or 0.0),
            )

        return {
            "success": overall_success,
            "platform": self.platform,
            "device_id": self.device_id,
            "steps": results,
            "runtime_variables": dict(self.runtime_variables),
        }

    def disconnect(self) -> None:
        self.driver.disconnect()

    def _capture_screenshot_base64(self) -> Optional[str]:
        try:
            raw_png = self.driver.screenshot()
            if not raw_png:
                return None
            return base64.b64encode(raw_png).decode("utf-8")
        except Exception:
            return None

    def _build_abort_result(
        self,
        step_data: Dict[str, Any],
        action: str,
        error_strategy: str,
        duration: float = 0.0,
    ) -> Dict[str, Any]:
        return self._result(
            step_data=step_data,
            action=action,
            status="FAIL",
            error_strategy=error_strategy,
            error="执行已被用户中止",
            output=None,
            artifacts=None,
            duration=duration,
        )

    @staticmethod
    def _extract_step_artifacts(step_context: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
        if not isinstance(step_context, dict):
            return None
        artifacts = step_context.get("artifacts")
        if not isinstance(artifacts, dict) or not artifacts:
            return None
        return dict(artifacts)

    @staticmethod
    def _result_screenshot_base64(result: Dict[str, Any]) -> Optional[str]:
        if not isinstance(result, dict):
            return None
        if result.get("screenshot"):
            return str(result.get("screenshot"))
        artifacts = result.get("artifacts")
        if not isinstance(artifacts, dict):
            return None
        screenshot = artifacts.get("screenshot_base64")
        if not screenshot:
            return None
        return str(screenshot)

    def _dispatch(
        self,
        step_data: Dict[str, Any],
        action: str,
        locator_candidates: List[Dict[str, Any]],
        args: Dict[str, Any],
        value: Optional[Any],
        timeout: int,
        step_context: Optional[Dict[str, Any]] = None,
    ) -> Optional[Dict[str, Any]]:
        if action not in _SUPPORTED_ACTIONS_BY_PLATFORM.get(self.platform, set()):
            raise NotImplementedError(
                _with_error_code(
                    "P1002_ACTION_NOT_SUPPORTED",
                    f"platform={self.platform}, action={action}",
                )
            )

        if action == "click":
            planned_click = getattr(self.driver, "click_with_fallback_plan", None)
            if self.platform == "ios" and callable(planned_click):
                planned_click(
                    locator_candidates=locator_candidates,
                    timeout=timeout,
                    step_context=step_context,
                )
                return None
            return self._dispatch_with_locator_fallback(
                action=action,
                locator_candidates=locator_candidates,
                dispatch=lambda selector, by: self.driver.click(selector=selector, by=by),
            )

        if action == "input":
            text = args.get("text", value)
            if text is None:
                raise ValueError(
                    _with_error_code(
                        "P1006_INVALID_ARGS",
                        "input 动作缺少 args.text/value",
                    )
                )
            text_value = str(text)
            if not _has_valid_locator(locator_candidates):
                self.driver.input_focused(text=text_value)
                return None
            return self._dispatch_with_locator_fallback(
                action=action,
                locator_candidates=locator_candidates,
                dispatch=lambda selector, by: self.driver.input(
                    selector=selector,
                    by=by,
                    text=text_value,
                ),
            )

        if action == "wait_until_exists":
            return self._dispatch_with_locator_fallback(
                action=action,
                locator_candidates=locator_candidates,
                dispatch=lambda selector, by: self.driver.wait_until_exists(
                    selector=selector,
                    by=by,
                    timeout=timeout,
                ),
            )

        if action == "assert_text":
            expected_text = args.get("expected_text", value)
            if expected_text is None:
                raise ValueError(
                    _with_error_code(
                        "P1006_INVALID_ARGS",
                        "assert_text 动作缺少 args.expected_text/value",
                    )
                )
            expected_text_value = str(expected_text)
            if not expected_text_value.strip():
                raise ValueError(
                    _with_error_code(
                        "P1006_INVALID_ARGS",
                        "assert_text 的 expected_text 不能为空",
                    )
                )
            match_mode = str(args.get("match_mode") or "contains").strip().lower()
            if match_mode not in {"contains", "not_contains"}:
                raise ValueError(
                    _with_error_code(
                        "P1006_INVALID_ARGS",
                        f"assert_text 不支持的 match_mode: {match_mode}",
                    )
                )
            self.driver.assert_text(
                expected_text=expected_text_value,
                match_mode=match_mode,
            )
            return None

        if action == "click_image":
            image_path = _resolve_image_path(
                step_data=step_data,
                args=args,
                locator_candidates=locator_candidates,
                platform=self.platform,
                value=value,
            )
            if image_path is None:
                raise ValueError(
                    _with_error_code(
                        "P1006_INVALID_ARGS",
                        "click_image 动作缺少 image_path/selector",
                    )
                )
            path = str(image_path).strip()
            if not path:
                raise ValueError(
                    _with_error_code(
                        "P1006_INVALID_ARGS",
                        "click_image 动作 image_path 不能为空",
                    )
                )
            self.driver.click_image(path)
            return None

        if action == "assert_image":
            image_path = _resolve_image_path(
                step_data=step_data,
                args=args,
                locator_candidates=locator_candidates,
                platform=self.platform,
                value=value,
            )
            if image_path is None:
                raise ValueError(
                    _with_error_code(
                        "P1006_INVALID_ARGS",
                        "assert_image 动作缺少 args.image_path/selector",
                    )
                )
            path = str(image_path).strip()
            if not path:
                raise ValueError(
                    _with_error_code(
                        "P1006_INVALID_ARGS",
                        "assert_image 动作 image_path 不能为空",
                    )
                )
            match_mode = str(args.get("match_mode") or "exists").strip().lower()
            if match_mode not in {"exists", "not_exists"}:
                raise ValueError(
                    _with_error_code(
                        "P1006_INVALID_ARGS",
                        f"assert_image 不支持的 match_mode: {match_mode}",
                    )
                )
            self.driver.assert_image(path, match_mode=match_mode)
            return None

        if action == "extract_by_ocr":
            region = _resolve_extract_region(
                step_data=step_data,
                args=args,
                locator_candidates=locator_candidates,
                platform=self.platform,
            )
            if region is None:
                raise ValueError(
                    _with_error_code(
                        "P1006_INVALID_ARGS",
                        "extract_by_ocr 动作缺少 args.region/selector",
                    )
                )
            region_text = str(region).strip()
            if not region_text:
                raise ValueError(
                    _with_error_code(
                        "P1006_INVALID_ARGS",
                        "extract_by_ocr 动作 region 不能为空",
                    )
                )

            extract_rule = args.get("extract_rule") or {}
            if isinstance(extract_rule, dict):
                options = dict(extract_rule)
            elif extract_rule is None:
                options = {}
            else:
                options = {"extract_rule": str(extract_rule)}

            extracted = self._extract_by_ocr_with_retry(
                region=region_text,
                extract_rule=options,
                timeout=timeout,
            )
            export_var = str(args.get("output_var", value) or "").strip()
            if export_var:
                return {
                    "export_var": export_var,
                    "export_value": extracted,
                }
            return {"ocr_value": extracted}

        if action == "sleep":
            seconds = args.get("seconds", value if value is not None else 1)
            sleep_seconds = _parse_seconds(seconds, default=1.0)
            time.sleep(sleep_seconds)
            return None

        if action == "swipe":
            legacy_direction = _first_locator_selector(locator_candidates)
            if not legacy_direction and value is not None:
                legacy_direction = str(value).strip()
            direction = str(args.get("direction", legacy_direction or "up")).strip().lower()
            self.driver.swipe(direction=direction)
            return None

        if action == "back":
            self.driver.back()
            return None

        if action == "home":
            self.driver.home()
            return None

        if action in ("start_app", "stop_app"):
            selector = _first_locator_selector(locator_candidates)
            app_id = args.get("app_key")
            if app_id is None:
                app_id = selector if selector else value
            if app_id is None:
                raise ValueError(
                    _with_error_code(
                        "P1006_INVALID_ARGS",
                        f"{action} 动作缺少 args.app_key 或 selector",
                    )
                )
            app_id = str(app_id).strip()
            if not app_id:
                raise ValueError(
                    _with_error_code(
                        "P1006_INVALID_ARGS",
                        f"{action} 动作 app_id 不能为空",
                    )
                )
            if action == "start_app":
                self.driver.start_app(app_id=app_id)
            else:
                self.driver.stop_app(app_id=app_id)
            return None

        raise NotImplementedError(
            _with_error_code(
                "P1002_ACTION_NOT_SUPPORTED",
                f"platform={self.platform}, action={action}",
            )
        )

    def _extract_by_ocr_with_retry(
        self,
        *,
        region: str,
        extract_rule: Dict[str, Any],
        timeout: int,
    ) -> str:
        """
        extract_by_ocr 按步骤 timeout 重试，减少页面加载抖动导致的偶发失败。
        """
        wait_seconds = max(1, int(timeout))
        started_at = time.time()
        deadline = started_at + wait_seconds
        attempt = 0
        last_error: Optional[Exception] = None

        while True:
            attempt += 1
            remaining_before = max(0.0, deadline - time.time())
            logger.info(
                "extract_by_ocr attempt %s start: region=%s, remaining=%.2fs",
                attempt,
                region,
                remaining_before,
            )
            try:
                text = self.driver.extract_by_ocr(region=region, extract_rule=extract_rule)
                if str(text or "").strip():
                    if attempt > 1:
                        logger.info(
                            "extract_by_ocr retry success: attempts=%s waited=%.2fs region=%s",
                            attempt,
                            time.time() - started_at,
                            region,
                        )
                    return str(text)
                last_error = RuntimeError("extract_by_ocr 未识别到文本")
            except Exception as exc:
                last_error = exc
                logger.warning(
                    "extract_by_ocr attempt %s failed: %s",
                    attempt,
                    exc,
                )
                if "未识别到文本" not in str(exc or ""):
                    raise

            now = time.time()
            if now >= deadline:
                break
            time.sleep(min(0.4, max(0.0, deadline - now)))

        elapsed = time.time() - started_at
        message = (
            f"extract_by_ocr 未识别到文本（重试 {attempt} 次，耗时 {elapsed:.2f}s，region={region}）"
        )
        if last_error is not None:
            raise RuntimeError(message) from last_error
        raise RuntimeError(message)

    def _dispatch_with_locator_fallback(
        self,
        action: str,
        locator_candidates: List[Dict[str, Any]],
        dispatch,
    ) -> Optional[Dict[str, Any]]:
        valid_candidates = []
        for item in locator_candidates or []:
            selector = str(item.get("selector") or "").strip()
            by = str(item.get("by") or "").strip()
            if selector and by:
                valid_candidates.append({"selector": selector, "by": by})

        if not valid_candidates:
            raise ValueError(
                _with_error_code(
                    "P1003_SELECTOR_MISSING",
                    f"{action} 动作缺少 selector/by",
                )
            )

        errors = []
        for candidate in valid_candidates:
            selector = candidate["selector"]
            by = candidate["by"]
            try:
                dispatch(selector, by)
                return None
            except Exception as exc:
                errors.append(f"selector={selector!r}, by={by!r}, error={exc}")

        if errors:
            raise RuntimeError("; ".join(errors))
        return None

    def _result(
        self,
        step_data: Dict[str, Any],
        action: str,
        status: str,
        error_strategy: str,
        error: Optional[str],
        output: Optional[Dict[str, Any]],
        artifacts: Optional[Dict[str, Any]],
        duration: float,
    ) -> Dict[str, Any]:
        return {
            "action": action,
            "status": status,
            "platform": self.platform,
            "device_id": self.device_id,
            "error_strategy": error_strategy,
            "duration": round(duration, 3),
            "error": error,
            "output": output,
            "artifacts": artifacts,
            "step": step_data,
        }


def _parse_timeout(value: Any, default: int = 10) -> int:
    try:
        timeout = int(value)
        if timeout > 0:
            return timeout
    except Exception:
        pass
    return default


def _parse_seconds(value: Any, default: float = 1.0) -> float:
    try:
        seconds = float(value)
        if seconds >= 0:
            return seconds
    except Exception:
        pass
    return default


def _render_runtime_value(value: Any, variables: Dict[str, str]) -> Any:
    if not variables:
        return value
    if isinstance(value, str):
        return render_step_data(value, variables)
    if isinstance(value, list):
        return [_render_runtime_value(item, variables) for item in value]
    if isinstance(value, dict):
        return {k: _render_runtime_value(v, variables) for k, v in value.items()}
    return value


def _collect_unresolved_templates(value: Any) -> List[str]:
    found: List[str] = []

    def _walk(node: Any) -> None:
        if isinstance(node, str):
            match = _UNRESOLVED_VAR_PATTERN.search(node)
            if match:
                found.append(match.group(0))
            return
        if isinstance(node, list):
            for item in node:
                _walk(item)
            return
        if isinstance(node, dict):
            for item in node.values():
                _walk(item)

    _walk(value)
    return found


def _first_locator_selector(locator_candidates: List[Dict[str, Any]]) -> Optional[str]:
    for item in locator_candidates or []:
        selector = str(item.get("selector") or "").strip()
        if selector:
            return selector
    return None


def _extract_override_selector(step_data: Dict[str, Any], platform: str) -> Optional[str]:
    overrides = step_data.get("platform_overrides")
    if not isinstance(overrides, dict):
        return None
    candidate = overrides.get(platform)
    if not isinstance(candidate, dict):
        return None
    selector = str(candidate.get("selector") or "").strip()
    return selector or None


def _resolve_image_path(
    step_data: Dict[str, Any],
    args: Dict[str, Any],
    locator_candidates: List[Dict[str, Any]],
    platform: str,
    value: Optional[Any],
) -> Optional[Any]:
    selector = _first_locator_selector(locator_candidates)
    options = step_data.get("options")
    option_image_path = None
    if isinstance(options, dict):
        option_image_path = options.get("image_path") or options.get("path")
    return (
        args.get("image_path")
        or args.get("path")
        or selector
        or step_data.get("selector")
        or _extract_override_selector(step_data, platform)
        or _extract_override_selector(step_data, "android")
        or option_image_path
        or value
    )


def _resolve_extract_region(
    step_data: Dict[str, Any],
    args: Dict[str, Any],
    locator_candidates: List[Dict[str, Any]],
    platform: str,
) -> Optional[Any]:
    selector = _first_locator_selector(locator_candidates)
    return (
        args.get("region")
        or selector
        or step_data.get("selector")
        or _extract_override_selector(step_data, platform)
        or _extract_override_selector(step_data, "android")
    )


def _has_valid_locator(locator_candidates: List[Dict[str, Any]]) -> bool:
    for item in locator_candidates or []:
        selector = str(item.get("selector") or "").strip()
        by = str(item.get("by") or "").strip()
        if selector and by:
            return True
    return False
