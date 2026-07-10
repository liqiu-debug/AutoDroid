"""
IOSDriver 日志与诊断辅助 Mixin

从 backend.drivers.ios_driver 拆出：动作成功/失败日志、耗时指标合并、
异常分类与 click_plan 计时输出。
"""
import logging
import time
from typing import Any, Dict, Optional

# 保持与拆分前一致的 logger 名称（backend.drivers.ios_driver），日志过滤与测试 assertLogs 兼容。
logger = logging.getLogger("backend.drivers.ios_driver")


class IOSDriverSupportMixin:
    """日志、诊断与耗时指标辅助方法（IOSDriver 组成部分）。"""

    @staticmethod
    def _truncate_log_value(value: Any, max_len: int = 96) -> str:
        if value is None:
            return ""
        text = str(value).strip()
        if len(text) <= max_len:
            return text
        return f"{text[: max_len - 3]}..."

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
