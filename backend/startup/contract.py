"""Versioned request and metric contract; no device or database dependencies."""
from __future__ import annotations

import re
from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, Field, field_validator, model_validator

PROTOCOL_VERSION = 1
MEASUREMENT_VERSION = "startup-v2"
MIN_PERCENTILE_SAMPLES = 20
READY_LOCATORS = {
    "android": ["text", "resource_id", "description", "xpath"],
    "ios": ["accessibility_id", "name", "label", "predicate", "xpath"],
}
METRICS = {
    "android": ["total_time_ms", "automation_ms", "ready_ms"],
    "ios": ["automation_ms", "ready_ms"],
}


def startup_capabilities(platform: Optional[str] = None, *, trace_supported: bool = False,
                         web_dispatch: bool = False) -> Dict[str, Any]:
    platforms = [platform] if platform in METRICS else ["android", "ios"]
    metrics = list(dict.fromkeys(item for p in platforms for item in METRICS[p]))
    locators = list(dict.fromkeys(item for p in platforms for item in READY_LOCATORS[p]))
    return {
        "protocol_version": PROTOCOL_VERSION,
        "measurement_version": MEASUREMENT_VERSION,
        "platforms": platforms,
        "metrics": metrics,
        "ready_locators": locators,
        "ready_locators_by_platform": {p: READY_LOCATORS[p] for p in platforms},
        "metrics_by_platform": {p: METRICS[p] for p in platforms},
        "trace": bool(trace_supported and "android" in platforms),
        "web_dispatch": bool(web_dispatch),
    }


def _bounded_int(value: Any, default: int, low: int, high: int) -> int:
    try:
        number = int(value)
    except (ValueError, TypeError, OverflowError):
        number = default
    return max(low, min(high, number))


class StartupReadyCheck(BaseModel):
    enabled: bool = False
    locator_type: str = "text"
    locator_value: str = Field(default="", max_length=4096)
    timeout_sec: int = 10

    @field_validator("locator_type", mode="before")
    @classmethod
    def normalize_locator_type(cls, value):
        text = str(value or "text").strip().lower()
        text = {"resourceid": "resource_id", "id": "resource_id", "desc": "description",
                "content-desc": "description", "accessibilityid": "accessibility_id"}.get(text, text)
        if text not in set(READY_LOCATORS["android"] + READY_LOCATORS["ios"]):
            raise ValueError("不支持的首页就绪定位方式")
        return text

    @field_validator("locator_value", mode="before")
    @classmethod
    def normalize_locator_value(cls, value):
        return str(value or "").strip()

    @field_validator("timeout_sec", mode="before")
    @classmethod
    def normalize_timeout_sec(cls, value):
        return _bounded_int(value, 10, 1, 60)

    @model_validator(mode="after")
    def require_locator(self):
        if self.enabled and not self.locator_value:
            raise ValueError("开启首页就绪检查时需要填写 locator")
        return self


class StartupPerfettoSlowTrace(BaseModel):
    enabled: bool = True
    # Legacy requests supplied performance limits inside this object. These
    # values remain accepted, but never determine whether limits are enabled.
    cold_threshold_ms: int = 5000
    hot_threshold_ms: int = 1500

    @field_validator("cold_threshold_ms", "hot_threshold_ms", mode="before")
    @classmethod
    def normalize_threshold(cls, value):
        return _bounded_int(value, 1, 1, 120000)


class StartupThresholds(BaseModel):
    metric: Optional[Literal["total_time_ms", "automation_ms", "ready_ms"]] = None
    cold_ms: int = 5000
    hot_ms: int = 1500
    ready_cold_ms: Optional[int] = None
    ready_hot_ms: Optional[int] = None

    @field_validator("cold_ms", "hot_ms", mode="before")
    @classmethod
    def normalize_limit(cls, value):
        return _bounded_int(value, 1, 1, 120000)

    @field_validator("ready_cold_ms", "ready_hot_ms", mode="before")
    @classmethod
    def normalize_optional_limit(cls, value):
        return None if value is None or value == "" else _bounded_int(value, 1, 1, 120000)


class StartupConfig(BaseModel):
    package_name: str = Field(min_length=1, max_length=255)
    activity_name: Optional[str] = Field(default=None, max_length=512)
    platform: Optional[Literal["android", "ios"]] = None
    device_serials: List[str] = Field(min_length=1, max_length=16)
    request_id: Optional[str] = Field(default=None, min_length=1, max_length=128)
    startup_modes: List[str] = Field(default_factory=lambda: ["cold", "hot"])
    iterations: int = 3
    cooldown_sec: int = 3
    capture_log: bool = True
    ready_check: StartupReadyCheck = Field(default_factory=StartupReadyCheck)
    perfetto_slow_trace: StartupPerfettoSlowTrace = Field(default_factory=StartupPerfettoSlowTrace)
    thresholds: StartupThresholds = Field(default_factory=StartupThresholds)

    @model_validator(mode="before")
    @classmethod
    def legacy_limits(cls, raw):
        if not isinstance(raw, dict):
            return raw
        value = dict(raw)
        if "thresholds" not in value:
            legacy = value.get("perfetto_slow_trace") or {}
            if isinstance(legacy, BaseModel):
                legacy = legacy.model_dump()
            value["thresholds"] = {
                "cold_ms": legacy.get("cold_threshold_ms", 5000),
                "hot_ms": legacy.get("hot_threshold_ms", 1500),
            }
        if str(value.get("platform") or "").lower() == "ios" and "perfetto_slow_trace" not in value:
            value["perfetto_slow_trace"] = {"enabled": False}
        return value

    @field_validator("package_name", mode="before")
    @classmethod
    def normalize_package(cls, value):
        text = str(value or "").strip()
        if not re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_.-]*", text):
            raise ValueError("应用标识必须是有效的包名或 Bundle ID")
        return text

    @field_validator("activity_name", mode="before")
    @classmethod
    def normalize_activity(cls, value):
        text = str(value or "").strip()
        if text and not re.fullmatch(r"[A-Za-z0-9_.$/-]+", text):
            raise ValueError("Activity 名称包含非法字符")
        return text or None

    @field_validator("platform", mode="before")
    @classmethod
    def normalize_platform(cls, value):
        return str(value).strip().lower() if value else None

    @field_validator("device_serials", mode="before")
    @classmethod
    def normalize_device_serials(cls, value):
        values = [value] if isinstance(value, str) else list(value or [])
        result = []
        for item in values:
            serial = str(item or "").strip()
            if not serial:
                continue
            if not re.fullmatch(r"[A-Za-z0-9_.:-]{1,255}", serial):
                raise ValueError("设备序列号包含非法字符")
            if serial not in result:
                result.append(serial)
        return result

    @field_validator("startup_modes", mode="before")
    @classmethod
    def normalize_startup_modes(cls, value):
        values = [value] if isinstance(value, str) else list(value if value is not None else ["cold", "hot"])
        modes = []
        for item in values:
            mode = str(item).strip().lower()
            if mode not in ("cold", "hot"):
                raise ValueError("startup_modes 仅支持 cold/hot")
            if mode not in modes:
                modes.append(mode)
        if not modes:
            raise ValueError("至少选择一种启动模式")
        return modes

    @field_validator("iterations", mode="before")
    @classmethod
    def normalize_iterations(cls, value):
        return _bounded_int(value, 3, 1, 100)

    @field_validator("cooldown_sec", mode="before")
    @classmethod
    def normalize_cooldown_sec(cls, value):
        return _bounded_int(value, 3, 0, 60)

    @model_validator(mode="after")
    def platform_rules(self):
        if self.platform:
            if self.ready_check.enabled and self.ready_check.locator_type not in READY_LOCATORS[self.platform]:
                raise ValueError(f"{self.platform} 不支持此首页就绪定位方式")
            if self.thresholds.metric and self.thresholds.metric not in METRICS[self.platform]:
                raise ValueError(f"{self.platform} 不提供 {self.thresholds.metric} 指标")
        if self.platform == "ios":
            if self.activity_name:
                raise ValueError("iOS 使用 Bundle ID，不接受 Android Activity")
            if self.perfetto_slow_trace.enabled:
                raise ValueError("iOS 不支持 Perfetto，请关闭慢启动取证")
        if self.thresholds.metric == "ready_ms" and not self.ready_check.enabled:
            raise ValueError("选择业务就绪耗时阈值时必须开启首页就绪检查")
        return self


def normalize_config(config: Any, *, platform: str, device_serial: str) -> Dict[str, Any]:
    data = config.model_dump() if isinstance(config, BaseModel) else dict(config or {})
    data.setdefault("device_serials", [device_serial])
    requested_platform = data.get("platform")
    if requested_platform and requested_platform != platform:
        raise ValueError("所选平台与实际设备平台不一致")
    # Old Android requests omit platform; infer it from the execution node.
    data["platform"] = platform
    if platform == "ios" and "perfetto_slow_trace" not in data:
        data["perfetto_slow_trace"] = {"enabled": False}
    validated = StartupConfig.model_validate(data).model_dump()
    validated["thresholds"]["metric"] = validated["thresholds"]["metric"] or (
        "total_time_ms" if platform == "android" else "automation_ms"
    )
    validated["measurement_version"] = MEASUREMENT_VERSION
    return validated
