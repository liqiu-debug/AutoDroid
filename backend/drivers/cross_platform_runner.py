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

from backend.execution_errors import (
    E2006_OCR_NO_RESULT,
    ExecutionStepError,
    ExecutionStepRuntimeError,
    classify_exception,
    suggestion_for,
)
from backend.locator_resolution import resolve_locator_candidates
from backend.utils.variable_render import format_variable_placeholder, render_step_data
from backend.step_contract import (
    MAX_RETRY_COUNT,
    normalize_action,
    normalize_error_strategy,
    normalize_execute_on,
)

from .android_driver import AndroidDriver
from .base_driver import BaseDriver
from .driver_pool import get_execution_driver_pool, is_driver_pool_enabled
from .ios_driver import IOSDriver

logger = logging.getLogger(__name__)
_UNRESOLVED_VAR_PATTERN = re.compile(r"{{\s*([A-Z0-9_]+)\s*}}")
# 步骤失败自动重试的间隔（短退避，配合 _sleep_or_abort 支持即时中止）
RETRY_BACKOFF_SECONDS = 1.0
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
        # AUTODROID_DRIVER_POOL=1 时按设备复用驱动连接（团队服务器推荐开启）
        self._driver_pool = get_execution_driver_pool() if is_driver_pool_enabled() else None
        if self._driver_pool is not None:
            self.driver = self._driver_pool.acquire(self.platform, device_id, **driver_kwargs)
        else:
            self.driver = DriverFactory.create(self.platform, device_id, **driver_kwargs)
        self.runtime_variables: Dict[str, str] = {}
        logger.info(
            "Cross-platform runner ready: platform=%s device_id=%s driver=%s",
            self.platform,
            self.device_id,
            self.driver.__class__.__name__,
        )

    def run_step(self, step_data: Dict[str, Any]) -> Dict[str, Any]:
        """执行单步并返回结构化结果。

        重试语义（retry_count，0-3）：
        - 仅 FAIL 触发重试（SKIP/PASS 不重试），最多再试 retry_count 次（总尝试 = 1 + retry_count）；
        - 每次重试前短退避 RETRY_BACKOFF_SECONDS，abort 触发时立即停止重试并走中止路径；
        - 重试耗尽仍失败时，error/error_code 取最后一次尝试的结果；
        - error_strategy 只在最终结果上生效（由 run_all 判断，重试期间不触发）；
        - 结果 dict 的 attempts 记录总尝试次数（1 表示无重试，纯增量字段）。
        """
        retry_count = _parse_retry_count((step_data or {}).get("retry_count"))
        started_at = time.time()

        result = self._run_step_attempt(step_data)
        attempts = 1
        while attempts <= retry_count and result.get("status") == "FAIL":
            # 短退避后重试；abort 触发时立即停止重试并走中止路径。
            if self._sleep_or_abort(RETRY_BACKOFF_SECONDS):
                result = self._build_abort_result(
                    step_data=step_data,
                    action=str(result.get("action") or "unknown"),
                    error_strategy=str(result.get("error_strategy") or "ABORT"),
                    duration=time.time() - started_at,
                )
                break
            attempts += 1
            logger.info(
                "runner step retry: attempt %s/%s action=%s last_error=%s",
                attempts,
                1 + retry_count,
                result.get("action"),
                result.get("error") or "-",
            )
            result = self._run_step_attempt(step_data)

        result["attempts"] = attempts
        if attempts > 1:
            # 汇总首次尝试开始至今的总耗时（含退避间隔）。
            result["duration"] = round(time.time() - started_at, 3)
        return result

    def _run_step_attempt(self, step_data: Dict[str, Any]) -> Dict[str, Any]:
        """执行单次尝试并返回结构化结果（不含重试语义）。"""
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
                    error_code="P1001_PLATFORM_NOT_ALLOWED",
                    error_context={
                        "action": action,
                        "platform": self.platform,
                        "device_id": self.device_id,
                        "execute_on": list(execute_on),
                    },
                    suggestion=suggestion_for("P1001_PLATFORM_NOT_ALLOWED") or None,
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
            # 供失败时构建 error_context 使用（纯错误路径辅助信息）。
            step_context["locator_candidates"] = locator_candidates
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

            result = self._result(
                step_data=step_data,
                action=action,
                status="PASS",
                error_strategy=strategy,
                error=None,
                output=dispatch_output,
                artifacts=self._extract_step_artifacts(step_context),
                duration=time.time() - started_at,
            )
            return result
        except Exception as exc:
            error_code, suggestion = classify_exception(
                exc,
                platform=self.platform,
                action=action,
            )
            return self._result(
                step_data=step_data,
                action=action,
                status="FAIL",
                error_strategy=strategy,
                error=str(exc),
                output=None,
                artifacts=self._extract_step_artifacts(step_context),
                duration=time.time() - started_at,
                error_code=error_code or None,
                error_context=self._build_error_context(
                    action=action,
                    exc=exc,
                    step_context=step_context,
                ),
                suggestion=suggestion or None,
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
        if self._driver_pool is not None:
            # 池化驱动归还复用，不真正断开
            self._driver_pool.release(self.platform, self.device_id, self.driver)
            return
        self.driver.disconnect()

    def _sleep_or_abort(self, seconds: float) -> bool:
        if seconds <= 0:
            return bool(self.abort_event and self.abort_event.is_set())
        if self.abort_event:
            return self.abort_event.wait(seconds)
        time.sleep(seconds)
        return False

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

    def _build_error_context(
        self,
        *,
        action: str,
        exc: Exception,
        step_context: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """构建失败上下文（至少含 action/platform/device_id，按需附 selector/by 等）。"""
        context: Dict[str, Any] = {
            "action": action,
            "platform": self.platform,
            "device_id": self.device_id,
        }
        candidates = (step_context or {}).get("locator_candidates")
        if isinstance(candidates, list):
            for item in candidates:
                if not isinstance(item, dict):
                    continue
                selector = str(item.get("selector") or "").strip()
                by = str(item.get("by") or "").strip()
                if not selector and not by:
                    continue
                if selector:
                    context["selector"] = selector
                if by:
                    context["by"] = by
                break
        if isinstance(exc, ExecutionStepError):
            for key, value in exc.context.items():
                if value not in (None, ""):
                    context[key] = value
        return context

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
            if self._sleep_or_abort(sleep_seconds):
                raise RuntimeError("执行已被用户中止")
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
            if self._sleep_or_abort(min(0.4, max(0.0, deadline - now))):
                raise RuntimeError("执行已被用户中止")

        elapsed = time.time() - started_at
        message = (
            f"extract_by_ocr 未识别到文本（重试 {attempt} 次，耗时 {elapsed:.2f}s，region={region}）"
        )
        final_exc = ExecutionStepRuntimeError(
            E2006_OCR_NO_RESULT,
            message,
            context={
                "action": "extract_by_ocr",
                "region": region,
                "timeout": wait_seconds,
                "attempts": attempt,
            },
        )
        if last_error is not None:
            raise final_exc from last_error
        raise final_exc

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
        first_exc: Optional[Exception] = None
        for candidate in valid_candidates:
            selector = candidate["selector"]
            by = candidate["by"]
            try:
                dispatch(selector, by)
                return None
            except Exception as exc:
                if first_exc is None:
                    first_exc = exc
                errors.append(f"selector={selector!r}, by={by!r}, error={exc}")

        if errors:
            # 以首个（主定位器）异常归类，聚合消息格式保持不变。
            code, suggestion = classify_exception(
                first_exc,
                platform=self.platform,
                action=action,
            )
            primary = valid_candidates[0]
            context: Dict[str, Any] = {
                "action": action,
                "selector": primary["selector"],
                "by": primary["by"],
                "attempts": len(valid_candidates),
            }
            if isinstance(first_exc, ExecutionStepError):
                for key, value in first_exc.context.items():
                    if value not in (None, ""):
                        context.setdefault(key, value)
            raise ExecutionStepRuntimeError(
                code,
                "; ".join(errors),
                context=context,
                suggestion=suggestion or None,
            ) from first_exc
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
        error_code: Optional[str] = None,
        error_context: Optional[Dict[str, Any]] = None,
        suggestion: Optional[str] = None,
    ) -> Dict[str, Any]:
        return {
            "action": action,
            "status": status,
            "platform": self.platform,
            "device_id": self.device_id,
            "error_strategy": error_strategy,
            "duration": round(duration, 3),
            # 总尝试次数（纯增量字段，1 表示无重试；见 run_step 重试语义）。
            "attempts": 1,
            "error": error,
            # 结构化错误信息（纯增量字段，原 error 字符串格式保持不变）。
            "error_code": error_code,
            "error_context": error_context,
            "suggestion": suggestion,
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


def _parse_retry_count(value: Any, default: int = 0) -> int:
    """执行期防御性解析 retry_count：非法取默认，收敛到 0..MAX_RETRY_COUNT。"""
    try:
        retry_count = int(str(value).strip())
    except Exception:
        return default
    return max(0, min(retry_count, MAX_RETRY_COUNT))


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
                found.append(format_variable_placeholder(match.group(1)))
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
