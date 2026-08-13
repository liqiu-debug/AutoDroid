from typing import List, Optional, Any, Dict, Union
from enum import Enum
import re
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from backend.utils.variable_render import normalize_variable_placeholders

class ActionType(str, Enum):
    CLICK = "click"
    INPUT = "input"
    WAIT_UNTIL_EXISTS = "wait_until_exists"
    ASSERT_TEXT = "assert_text"
    ASSERT_IMAGE = "assert_image"
    SWIPE = "swipe"
    CLICK_IMAGE = "click_image"  # 图像匹配点击
    START_APP = "start_app"
    STOP_APP = "stop_app"
    BACK = "back"
    HOME = "home"
    SLEEP = "sleep"  # 🟢 新增：强制等待/睡眠
    EXTRACT_BY_OCR = "extract_by_ocr"

class SelectorType(str, Enum):
    RESOURCE_ID = "resourceId"
    TEXT = "text"
    XPATH = "xpath"
    DESCRIPTION = "description"
    IMAGE = "image"  # 图像路径

class ErrorStrategy(str, Enum):
    ABORT = "ABORT"        # 🔴 立即终止（默认）
    CONTINUE = "CONTINUE"  # 🟡 失败但继续
    IGNORE = "IGNORE"      # 🟢 忽略错误

class Step(BaseModel):
    uuid: Optional[str] = None
    action: ActionType
    selector: Optional[str] = None
    selector_type: Optional[SelectorType] = None
    value: Optional[str] = None  # For input / assert_text 等兼容字段
    options: Optional[dict] = Field(default_factory=dict)
    description: Optional[str] = None
    timeout: int = 10  # Default timeout in seconds
    error_strategy: ErrorStrategy = ErrorStrategy.ABORT # Error routing strategy
    retry_count: int = 0  # 失败自动重试次数（0-3，0 表示不重试）

    @field_validator("retry_count", mode="before")
    @classmethod
    def normalize_retry_count_lenient(cls, value):
        # legacy JSON 兼容：非法值回退 0，范围收敛到 0..3
        from backend.step_contract import MAX_RETRY_COUNT

        try:
            retry_count = int(str(value).strip())
        except Exception:
            return 0
        return max(0, min(retry_count, MAX_RETRY_COUNT))

    @field_validator("selector_type", mode="before")
    @classmethod
    def normalize_blank_selector_type(cls, value):
        if value is None:
            return None
        if isinstance(value, str) and not value.strip():
            return None
        return value

    @field_validator("selector", "value", "description", mode="before")
    @classmethod
    def normalize_text_variable_placeholders(cls, value):
        return normalize_variable_placeholders(value)

    @field_validator("options", mode="before")
    @classmethod
    def normalize_option_variable_placeholders(cls, value):
        return normalize_variable_placeholders(value)

class Variable(BaseModel):
    key: str
    value: str

    @field_validator("value", mode="before")
    @classmethod
    def normalize_value_variable_placeholders(cls, value):
        return normalize_variable_placeholders(value)

class TestCaseBase(BaseModel):
    name: str
    description: Optional[str] = None
    steps: List[Step] = Field(default_factory=list)
    variables: List[Variable] = Field(default_factory=list)
    tags: List[str] = Field(default_factory=list)

class TestCaseCreate(TestCaseBase):
    folder_id: Optional[int] = None

class TestCaseRead(TestCaseBase):
    id: int
    user_id: Optional[int] = None
    folder_id: Optional[int] = None
    folder_name: Optional[str] = None
    created_at: Any # datetime
    last_run_status: Optional[str] = None
    last_run_time: Any = None # datetime
    updated_at: Any = None # datetime
    creator_name: Optional[str] = None
    updater_name: Optional[str] = None

    class Config:
        from_attributes = True

class PaginatedTestCaseRead(BaseModel):
    total: int
    items: List[TestCaseRead]

class InteractionRequest(BaseModel):
    x: int
    y: int
    operation: str = "click"  # click, swipe, back, home, etc.
    action_data: Optional[str] = None # package name or swipe direction
    xml_dump: Optional[str] = None
    device_serial: Optional[str] = None
    record_step: bool = True

# ---- Scenario Schemas ----

class ScenarioStepCreate(BaseModel):
    case_id: int
    order: int
    alias: Optional[str] = None

class ScenarioStepRead(ScenarioStepCreate):
    id: int
    scenario_id: int

    class Config:
        from_attributes = True

class TestScenarioBase(BaseModel):
    name: str
    description: Optional[str] = None

class TestScenarioCreate(TestScenarioBase):
    folder_id: Optional[int] = None

class TestScenarioRead(TestScenarioBase):
    id: int
    user_id: Optional[int] = None
    folder_id: Optional[int] = None
    created_at: Any
    updated_at: Any = None
    step_count: int = 0
    last_run_status: Optional[str] = None
    last_run_time: Any = None
    last_run_duration: Optional[int] = None
    last_report_id: Optional[str] = None
    last_execution_id: Optional[int] = None
    last_executor: Optional[str] = None
    last_failed_step: Optional[str] = None
    creator_name: Optional[str] = None
    updater_name: Optional[str] = None

    class Config:
        from_attributes = True

class PaginatedTestScenarioRead(BaseModel):
    total: int
    items: List[TestScenarioRead]

class ScenarioRunRequest(BaseModel):
    device_serials: List[str]
    env_id: Optional[int] = None

# ---- Scheduled Task Schemas ----

class TaskStrategy(str, Enum):
    DAILY = "DAILY"
    WEEKLY = "WEEKLY"
    INTERVAL = "INTERVAL"
    ONCE = "ONCE"

class ScheduledTaskCreate(BaseModel):
    name: str
    scenario_id: Optional[int] = None
    device_serials: List[str] = []
    strategy: TaskStrategy
    strategy_config: Dict[str, Any]  # e.g. {"hour": 14, "minute": 0}
    enable_notification: bool = True

class ScheduledTaskRead(BaseModel):
    id: int
    name: str
    scenario_id: Optional[int] = None
    device_serials: List[str] = []
    strategy: str
    strategy_config: Dict[str, Any] = {}
    is_active: bool = True
    enable_notification: bool = True
    next_run_time: Any = None
    created_at: Any
    updated_at: Any = None
    formatted_schedule: str = ""  # 人话描述
    scenario_name: str = ""

    class Config:
        from_attributes = True

class ScheduledTaskUpdate(BaseModel):
    name: Optional[str] = None
    scenario_id: Optional[int] = None
    device_serials: Optional[List[str]] = None
    strategy: Optional[TaskStrategy] = None
    strategy_config: Optional[Dict[str, Any]] = None
    enable_notification: Optional[bool] = None

class PaginatedScheduledTaskRead(BaseModel):
    total: int
    items: List[ScheduledTaskRead]

class UserRead(BaseModel):
    id: int
    username: str
    full_name: Optional[str] = None
    email: Optional[str] = None
    is_active: bool = True
    created_at: Any

    class Config:
        from_attributes = True


class CurrentUserRead(UserRead):
    role: str = "user"


class UserRegister(BaseModel):
    username: str = Field(min_length=1)
    password: str = Field(min_length=6)
    name: str = Field(min_length=1)


class UserCreateByAdmin(BaseModel):
    username: str = Field(min_length=1)
    initial_password: str = Field(min_length=6)
    full_name: Optional[str] = None
    email: Optional[str] = None


class UserStatusUpdate(BaseModel):
    is_active: bool


class PasswordChange(BaseModel):
    current_password: str
    new_password: str = Field(min_length=6)


class RegistrationStatus(BaseModel):
    allow_registration: bool


# ---- Case Folder Schemas ----

class CaseFolderCreate(BaseModel):
    name: str
    parent_id: Optional[int] = None

class CaseFolderUpdate(BaseModel):
    name: str

class CaseFolderRead(BaseModel):
    id: int
    name: str
    parent_id: Optional[int] = None
    children: List["CaseFolderRead"] = Field(default_factory=list)

    class Config:
        from_attributes = True

CaseFolderRead.model_rebuild()


# ---- Fastbot Schemas ----

class FastbotTaskCreate(BaseModel):
    package_name: str
    duration: int = 600
    throttle: int = 500
    enable_performance_monitor: bool
    enable_jank_frame_monitor: bool
    enable_local_replay: bool = True
    ignore_crashes: bool = False
    capture_log: bool = True
    device_serial: str
    enable_custom_event_weights: bool = False
    pct_touch: int = 40
    pct_motion: int = 30
    pct_syskeys: int = 5
    pct_majornav: int = 15

class FastbotTaskRead(BaseModel):
    id: int
    package_name: str
    duration: int
    throttle: int
    ignore_crashes: bool
    capture_log: bool
    device_serial: str
    status: str
    total_crashes: int = 0
    total_anrs: int = 0
    executor_name: Optional[str] = None
    created_at: Any
    started_at: Any = None
    finished_at: Any = None

    class Config:
        from_attributes = True

class PaginatedFastbotTaskRead(BaseModel):
    total: int
    items: List[FastbotTaskRead]

class FastbotReportRead(BaseModel):
    id: int
    task_id: int
    performance_data: Optional[List[Dict[str, Any]]] = None
    jank_data: Optional[List[Dict[str, Any]]] = None
    jank_events: Optional[List[Dict[str, Any]]] = None
    trace_artifacts: Optional[List[Dict[str, Any]]] = None
    crash_events: Optional[List[Dict[str, Any]]] = None
    summary: Optional[Dict[str, Any]] = None
    created_at: Any

    class Config:
        from_attributes = True


class FluencySessionStartRequest(BaseModel):
    package_name: str
    device_serial: str
    enable_performance_monitor: bool = True
    enable_jank_frame_monitor: bool = True
    capture_log: bool = True
    auto_launch_app: bool = True


class FluencyMarkerCreate(BaseModel):
    label: str


class FluencyMarkerRead(BaseModel):
    label: str
    time: str
    activity: Optional[str] = None


class FluencySessionRead(BaseModel):
    task_id: int
    package_name: str
    device_serial: str
    status: str
    executor_name: Optional[str] = None
    created_at: Any
    started_at: Any = None
    finished_at: Any = None
    report_ready: bool = False
    marker_count: int = 0
    markers: List[FluencyMarkerRead] = Field(default_factory=list)
    summary: Optional[Dict[str, Any]] = None

    class Config:
        from_attributes = True


class StartupReadyCheck(BaseModel):
    enabled: bool = False
    locator_type: str = "text"
    locator_value: str = ""
    timeout_sec: int = 10

    @field_validator("locator_type", mode="before")
    @classmethod
    def normalize_locator_type(cls, value):
        normalized = str(value or "text").strip().lower()
        alias_map = {
            "resourceid": "resource_id",
            "resourceId": "resource_id",
            "id": "resource_id",
            "desc": "description",
            "content-desc": "description",
        }
        normalized = alias_map.get(normalized, normalized)
        if normalized not in {"text", "resource_id", "description", "xpath"}:
            raise ValueError("locator_type must be text/resource_id/description/xpath")
        return normalized

    @field_validator("timeout_sec", mode="before")
    @classmethod
    def normalize_timeout_sec(cls, value):
        try:
            timeout = int(value)
        except (TypeError, ValueError):
            timeout = 10
        return max(1, min(timeout, 60))


class StartupPerfettoSlowTrace(BaseModel):
    enabled: bool = True
    cold_threshold_ms: int = 5000
    hot_threshold_ms: int = 1500

    @field_validator("cold_threshold_ms", "hot_threshold_ms", mode="before")
    @classmethod
    def normalize_threshold(cls, value):
        try:
            threshold = int(value)
        except (TypeError, ValueError):
            threshold = 0
        return max(1, min(threshold, 120000))


class StartupRunRequest(BaseModel):
    package_name: str
    activity_name: Optional[str] = None
    device_serials: List[str]
    startup_modes: List[str] = Field(default_factory=lambda: ["cold", "hot"])
    iterations: int = 3
    cooldown_sec: int = 3
    capture_log: bool = True
    ready_check: StartupReadyCheck = Field(default_factory=StartupReadyCheck)
    perfetto_slow_trace: StartupPerfettoSlowTrace = Field(default_factory=StartupPerfettoSlowTrace)

    @field_validator("device_serials", mode="before")
    @classmethod
    def normalize_device_serials(cls, value):
        if isinstance(value, str):
            values = [value]
        else:
            values = list(value or [])
        serials = []
        seen = set()
        for item in values:
            serial = str(item or "").strip()
            if serial and serial not in seen:
                serials.append(serial)
                seen.add(serial)
        if not serials:
            raise ValueError("device_serials must not be empty")
        return serials

    @field_validator("startup_modes", mode="before")
    @classmethod
    def normalize_startup_modes(cls, value):
        values = list(value or ["cold", "hot"])
        modes = []
        for item in values:
            mode = str(item or "").strip().lower()
            if mode not in {"cold", "hot"}:
                raise ValueError("startup_modes only supports cold/hot")
            if mode not in modes:
                modes.append(mode)
        if not modes:
            raise ValueError("startup_modes must not be empty")
        return modes

    @field_validator("iterations", mode="before")
    @classmethod
    def normalize_iterations(cls, value):
        try:
            iterations = int(value)
        except (TypeError, ValueError):
            iterations = 3
        return max(1, min(iterations, 100))

    @field_validator("cooldown_sec", mode="before")
    @classmethod
    def normalize_cooldown_sec(cls, value):
        try:
            cooldown = int(value)
        except (TypeError, ValueError):
            cooldown = 3
        return max(0, min(cooldown, 60))


class StartupTaskRead(BaseModel):
    id: int
    package_name: str
    duration: int
    throttle: int
    ignore_crashes: bool
    capture_log: bool
    device_serial: str
    status: str
    total_crashes: int = 0
    total_anrs: int = 0
    executor_name: Optional[str] = None
    created_at: Any
    started_at: Any = None
    finished_at: Any = None
    report_ready: bool = False
    summary: Optional[Dict[str, Any]] = None

    class Config:
        from_attributes = True

class PaginatedStartupTaskRead(BaseModel):
    total: int
    items: List[StartupTaskRead]

class DeviceStatusRead(BaseModel):
    serial: str
    device_name: str = ""
    ready: bool = False
    status: str = "IDLE"  # IDLE, RUNNING, FASTBOT_RUNNING, WDA_DOWN


# ---- Log Analysis Schemas ----

class LogAnalysisRequest(BaseModel):
    log_text: str  # 原始 500 行日志
    package_name: str  # 用于过滤堆栈
    device_info: Optional[str] = None  # 辅助判断，如 "Xiaomi 14, Android 14"
    force_refresh: bool = False

class LogAnalysisResponse(BaseModel):
    success: bool
    analysis_result: str = ""  # Markdown 格式的分析结果
    token_usage: int = 0  # Token 消耗统计
    cached: bool = False  # 是否命中缓存


class JankAiSummaryRequest(BaseModel):
    trace_path: str
    force_refresh: bool = False


class JankAiSummaryResponse(BaseModel):
    success: bool
    analysis_result: str = ""
    token_usage: int = 0
    cached: bool = False


# ---- Device Management Schemas ----

class DeviceRead(BaseModel):
    id: int
    serial: str
    platform: str = "android"
    model: str = "Unknown"
    brand: str = ""
    android_version: str = ""
    os_version: str = ""
    resolution: str = ""
    status: str = "IDLE"
    connection_type: Optional[str] = None  # iOS: usb | network；Android 远程 USB: remote_usb
    wireless_enabled: bool = False  # iOS: 是否已配置无线直连 WDA 地址
    agent_name: Optional[str] = None  # 远程接入点名称（remote_usb 设备）
    source_serial: Optional[str] = None  # 远程设备在接入机上的真实 USB serial
    custom_name: Optional[str] = None
    market_name: Optional[str] = None
    created_at: Any
    updated_at: Any = None

    class Config:
        from_attributes = True

class DeviceWirelessEnableRequest(BaseModel):
    """启用 iOS 无线模式的可选参数：手动指定 IP / 设备端 WDA 端口。"""
    ip: Optional[str] = None
    port: int = 8100

class DeviceRenameRequest(BaseModel):
    custom_name: str

class DeviceSyncResponse(BaseModel):
    synced: int = 0
    online: int = 0
    offline: int = 0
    devices: List[DeviceRead] = []


# ---- App Package Schemas ----

class AppPackageRead(BaseModel):
    id: int
    platform: str = "android"
    app_name: str = "Unknown"
    package_name: str = ""
    version_name: str = ""
    version_code: str = ""
    file_path: str = ""
    file_size: float = 0.0
    is_latest: bool = False
    upload_time: Any
    uploader_name: Optional[str] = None

    class Config:
        from_attributes = True

class PaginatedAppPackageRead(BaseModel):
    total: int
    items: List[AppPackageRead]


# ---- Compatibility Test Schemas ----


class CompatPageDefinition(BaseModel):
    name: str
    case_id: Optional[int] = None
    settle_seconds: int = Field(default=2, ge=0, le=60)
    required_text: Optional[str] = None
    key: Optional[str] = None
    inspection_state_id: Optional[int] = None
    inspection_path: List[Dict[str, Any]] = Field(default_factory=list)
    branch_key: Optional[str] = None
    branch_config: Optional[Dict[str, Any]] = None
    input_rules: List[Dict[str, Any]] = Field(default_factory=list)
    sanitizer_rules: List[Dict[str, Any]] = Field(default_factory=list)
    dynamic_text_patterns: List[str] = Field(default_factory=list)
    stable_wait_seconds: float = 5.0
    baseline_screenshot_path: Optional[str] = None
    baseline_xml_path: Optional[str] = None
    baseline_activity: Optional[str] = None

    @field_validator("name", mode="before")
    @classmethod
    def normalize_name(cls, value):
        text = str(value or "").strip()
        if not text:
            raise ValueError("page name must not be empty")
        return text


class CompatPageSetCreate(BaseModel):
    name: str
    description: Optional[str] = None
    pages: List[CompatPageDefinition] = Field(default_factory=list)

    @field_validator("name", mode="before")
    @classmethod
    def normalize_name(cls, value):
        text = str(value or "").strip()
        if not text:
            raise ValueError("page set name must not be empty")
        return text


class CompatPageSetUpdate(CompatPageSetCreate):
    pass


class CompatPageSetRead(CompatPageSetCreate):
    id: int
    user_id: Optional[int] = None
    created_at: Any
    updated_at: Any = None

    class Config:
        from_attributes = True


class CompatibilityThresholds(BaseModel):
    pixel_diff_ratio_warn: float = Field(default=0.03, ge=0, le=1)
    ssim_warn: float = Field(default=0.96, ge=0, le=1)
    xml_diff_ratio_warn: float = Field(default=0.35, ge=0, le=1)


class CompatibilityReplayChainRead(BaseModel):
    chain_id: str
    path_key: str
    name: str
    endpoint_state_id: int
    source_observation_id: Optional[int] = None
    display_index: Optional[int] = None
    display_label: Optional[str] = None
    page_name: Optional[str] = None
    source_observation_index: Optional[int] = None
    evidence_level: str
    reachability_evidence: str = ""
    replay_eligibility: str = ""
    replay_scope: str = ""
    terminal_outcome: str = "NONE"
    boundary_evidence: str = "NOT_APPLICABLE"
    prefix_path_key: Optional[str] = None
    source_path_keys: List[str] = Field(default_factory=list)
    terminal_boundaries: List[Dict[str, Any]] = Field(default_factory=list)
    first_path: List[Dict[str, Any]] = Field(default_factory=list)
    checkpoints: List[Dict[str, Any]] = Field(default_factory=list)
    covered_roles: List[str] = Field(default_factory=list)
    covered_subtypes: List[str] = Field(default_factory=list)
    covered_family_ids: List[int] = Field(default_factory=list)
    covered_family_keys: List[str] = Field(default_factory=list)
    depth: int = 0

    @model_validator(mode="after")
    def fill_replay_v2_aliases(self):
        if not self.reachability_evidence:
            self.reachability_evidence = self.evidence_level
        if not self.replay_scope:
            self.replay_scope = {
                "FULL": "FULL_PATH",
                "SAFE_PREFIX": "PREFIX_TO_SAFETY_BOUNDARY",
            }.get(str(self.replay_eligibility or "").upper(), "FULL_PATH")
        if not self.replay_eligibility:
            self.replay_eligibility = {
                "FULL_PATH": "FULL",
                "PREFIX_TO_SAFETY_BOUNDARY": "SAFE_PREFIX",
            }.get(str(self.replay_scope or "").upper(), "NONE")
        if self.terminal_outcome == "NONE" and self.terminal_boundaries:
            outcomes = {
                str(item.get("terminal_outcome") or "NONE").upper()
                for item in self.terminal_boundaries
            }
            for outcome in (
                "APP_FAULT",
                "INFRA_FAULT",
                "AUTOMATION_FAILED",
                "LOCATOR_FAILED",
                "SAFETY_BLOCKED",
                "BUDGET_STOP",
                "CANCELLED",
            ):
                if outcome in outcomes:
                    self.terminal_outcome = outcome
                    break
        if not self.prefix_path_key:
            self.prefix_path_key = self.path_key
        return self


class CompatibilityReplayIssue(BaseModel):
    code: str
    message: str


class CompatibilityPackageSnapshot(BaseModel):
    package_name: str = ""
    version_name: Optional[str] = None
    version_code: Optional[str] = None
    first_install_time: Optional[str] = None
    last_update_time: Optional[str] = None
    signing_digest: Optional[str] = None
    installed: bool = False
    known: bool = False
    source: Optional[str] = None
    captured_at: Any = None


class CompatibilityReplayPreflightRequest(BaseModel):
    inspection_run_id: int = Field(gt=0)
    branch_key: str
    device_serial: str
    max_chains: int = Field(default=20, ge=1, le=20)

    @field_validator("branch_key", "device_serial", mode="before")
    @classmethod
    def normalize_required_text(cls, value, info):
        text = str(value or "").strip()
        if not text:
            raise ValueError(f"{info.field_name} must not be empty")
        return text


class CompatibilityReplayPreflightRead(BaseModel):
    execution_mode: str = "installed_replay"
    inspection_run_id: int
    branch_key: str
    package_name: str
    source_package: CompatibilityPackageSnapshot
    installed_package: CompatibilityPackageSnapshot
    blockers: List[CompatibilityReplayIssue] = Field(default_factory=list)
    warnings: List[CompatibilityReplayIssue] = Field(default_factory=list)
    plan_digest: str = ""
    device_snapshot_digest: str = ""
    plan_version: int = 3
    summary: Dict[str, Any] = Field(default_factory=dict)
    chains: List[CompatibilityReplayChainRead] = Field(default_factory=list)
    available_prefixes: List[str] = Field(default_factory=list)
    excluded: Any = Field(default_factory=dict)


# ---- Model-based Android Inspection Schemas ----


class InspectionReadyAssertion(BaseModel):
    selector: str
    by: str
    timeout: int = Field(default=5, ge=1, le=60)

    @field_validator("selector", mode="before")
    @classmethod
    def normalize_selector(cls, value):
        selector = str(value or "").strip()
        if not selector:
            raise ValueError("ready assertion selector must not be empty")
        return selector

    @field_validator("by", mode="before")
    @classmethod
    def normalize_by(cls, value):
        by = str(value or "").strip().lower()
        if by not in {"description", "text", "xpath"}:
            raise ValueError("ready assertion by must be description, text or xpath")
        return by


class InspectionBranchConfig(BaseModel):
    name: str
    prepare_case_id: int = Field(gt=0)
    entry_case_id: int = Field(gt=0)
    env_id: Optional[int] = None
    ready_assertion: InspectionReadyAssertion
    # "full": BFS exploration from the entry surface (historical behavior).
    # "single_page": exhaustively operate the entry surface only; targets on
    # other surfaces are captured for the app map but never expanded.
    scope: str = "full"

    @field_validator("name", mode="before")
    @classmethod
    def normalize_name(cls, value):
        name = str(value or "").strip()
        if not name:
            raise ValueError("branch name must not be empty")
        return name

    @field_validator("scope", mode="before")
    @classmethod
    def normalize_scope(cls, value):
        scope = str(value or "full").strip().lower()
        if scope not in {"full", "single_page"}:
            raise ValueError("branch scope must be full or single_page")
        return scope


class InspectionInputRule(BaseModel):
    id: str
    content_desc_regex: Optional[str] = None
    text_regex: Optional[str] = None
    class_regex: Optional[str] = None
    ancestor_regex: Optional[str] = None
    page_subtype_regex: Optional[str] = None
    value_source: str = "literal"
    value: Optional[str] = None
    variable_key: Optional[str] = None
    allow_sensitive: bool = False

    @field_validator("id", mode="before")
    @classmethod
    def normalize_id(cls, value):
        item_id = str(value or "").strip()
        if not item_id:
            raise ValueError("input rule id must not be empty")
        return item_id

    @field_validator("value_source", mode="before")
    @classmethod
    def normalize_source(cls, value):
        source = str(value or "literal").strip().lower()
        if source not in {"literal", "environment"}:
            raise ValueError("value_source must be literal or environment")
        return source

    @model_validator(mode="after")
    def validate_matcher_and_value(self):
        patterns = [
            self.content_desc_regex,
            self.text_regex,
            self.class_regex,
            self.ancestor_regex,
            self.page_subtype_regex,
        ]
        if not any(patterns):
            raise ValueError("input rule requires at least one matcher")
        for pattern in patterns:
            if not pattern:
                continue
            try:
                re.compile(pattern)
            except re.error as exc:
                raise ValueError(f"invalid input rule regex: {pattern}: {exc}") from exc
        if self.value_source == "literal" and self.value is None:
            raise ValueError("literal input rule requires value")
        if self.value_source == "environment" and not str(self.variable_key or "").strip():
            raise ValueError("environment input rule requires variable_key")
        return self


class InspectionSafetyRule(BaseModel):
    id: str
    pattern: str
    risk_type: str = "CUSTOM"
    # Allow rules primarily admit system/permission surfaces. They can also
    # identify a specific unlabeled target that is otherwise conservatively
    # blocked by dangerous page context. Labeled destructive/payment targets
    # remain hard stops because their direct match is evaluated first.
    allow: bool = False

    @model_validator(mode="after")
    def validate_rule(self):
        self.id = str(self.id or "").strip()
        self.pattern = str(self.pattern or "").strip()
        if not self.id or not self.pattern:
            raise ValueError("safety rule id and pattern must not be empty")
        try:
            re.compile(self.pattern)
        except re.error as exc:
            raise ValueError(
                f"invalid safety rule regex: {self.pattern}: {exc}"
            ) from exc
        return self


class InspectionSanitizerRule(BaseModel):
    id: str
    content_desc_regex: Optional[str] = None
    text_regex: Optional[str] = None
    class_regex: Optional[str] = None

    @model_validator(mode="after")
    def validate_rule(self):
        self.id = str(self.id or "").strip()
        patterns = [
            self.content_desc_regex,
            self.text_regex,
            self.class_regex,
        ]
        if not self.id:
            raise ValueError("sanitizer rule id must not be empty")
        if not any(patterns):
            raise ValueError("sanitizer rule requires at least one matcher")
        for pattern in patterns:
            if not pattern:
                continue
            try:
                re.compile(pattern)
            except re.error as exc:
                raise ValueError(
                    f"invalid sanitizer rule regex: {pattern}: {exc}"
                ) from exc
        return self


class InspectionBudgets(BaseModel):
    duration_seconds: int = Field(default=1800, ge=30, le=7200)
    max_states: int = Field(default=200, ge=1, le=5000)
    max_device_actions: int = Field(default=800, ge=1, le=50000)
    max_actions: Optional[int] = Field(default=None, ge=1, le=50000)
    max_depth: int = Field(default=12, ge=1, le=100)
    max_scrolls_per_direction: int = Field(default=3, ge=0, le=20)
    # Shared across all scrollable surfaces during a coverage-scheduler run.
    # Large commerce apps need a higher ceiling than the historical hard-coded
    # value of 25, while per-container repetitions remain bounded above.
    max_coverage_scroll_actions: int = Field(default=50, ge=0, le=1000)
    max_variants_per_cluster: int = Field(default=5, ge=1, le=50)
    no_new_coverage_limit: int = Field(default=100, ge=1, le=5000)
    no_new_state_limit: Optional[int] = Field(default=None, ge=1, le=5000)
    max_observations: int = Field(default=400, ge=1, le=100000)
    max_artifact_bytes: int = Field(default=512 * 1024 * 1024, ge=1024 * 1024)
    stable_wait_seconds: float = Field(default=5.0, ge=1.0, le=30.0)

    @model_validator(mode="after")
    def normalize_legacy_names(self):
        fields_set = getattr(self, "model_fields_set", set())
        if "max_actions" in fields_set and "max_device_actions" not in fields_set:
            self.max_device_actions = int(self.max_actions)
        if "no_new_state_limit" in fields_set and "no_new_coverage_limit" not in fields_set:
            self.no_new_coverage_limit = int(self.no_new_state_limit)
        # Keep serialized profile snapshots readable by one older release.
        self.max_actions = self.max_device_actions
        self.no_new_state_limit = self.no_new_coverage_limit
        return self


class InspectionMonitorOptions(BaseModel):
    enable_performance_monitor: bool = True
    enable_jank_frame_monitor: bool = False
    enable_perfetto_trace: bool = False
    enable_local_replay: bool = True
    capture_log: bool = True


class InspectionProfileCreate(BaseModel):
    name: str
    package_name: str
    branches: Dict[str, InspectionBranchConfig]
    input_rules: List[InspectionInputRule] = Field(default_factory=list)
    safety_rules: List[InspectionSafetyRule] = Field(default_factory=list)
    sanitizer_rules: List[InspectionSanitizerRule] = Field(default_factory=list)
    dynamic_text_patterns: List[str] = Field(default_factory=list)
    budgets: InspectionBudgets = Field(default_factory=InspectionBudgets)
    monitor_options: InspectionMonitorOptions = Field(default_factory=InspectionMonitorOptions)

    @field_validator("name", "package_name", mode="before")
    @classmethod
    def normalize_required_text(cls, value):
        text = str(value or "").strip()
        if not text:
            raise ValueError("value must not be empty")
        return text

    @model_validator(mode="after")
    def validate_branches(self):
        required = {"guest", "authenticated"}
        keys = set(self.branches.keys())
        if not required.issubset(keys):
            raise ValueError("branches must contain guest and authenticated")
        # Custom branch keys become report directory names, so keep them slug-safe.
        for key in keys - required:
            if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,63}", key):
                raise ValueError(
                    f"branch key must match [a-z0-9][a-z0-9_-]{{0,63}}: {key}"
                )
        for label, rules in (
            ("input_rules", self.input_rules),
            ("safety_rules", self.safety_rules),
            ("sanitizer_rules", self.sanitizer_rules),
        ):
            ids = [str(item.id) for item in rules]
            if len(ids) != len(set(ids)):
                raise ValueError(f"{label} contains duplicate rule ids")
        return self

    @field_validator("dynamic_text_patterns", mode="before")
    @classmethod
    def normalize_dynamic_patterns(cls, value):
        result = []
        for item in list(value or []):
            pattern = str(item or "").strip()
            if not pattern:
                continue
            try:
                re.compile(pattern)
            except re.error as exc:
                raise ValueError(f"invalid dynamic text regex: {pattern}: {exc}") from exc
            if pattern not in result:
                result.append(pattern)
        return result


class InspectionProfileUpdate(InspectionProfileCreate):
    pass


class InspectionProfileRead(InspectionProfileCreate):
    id: int
    user_id: Optional[int] = None
    created_at: Any
    updated_at: Any = None

    class Config:
        from_attributes = True


class InspectionRunCreate(BaseModel):
    profile_id: int = Field(gt=0)
    name: Optional[str] = None
    device_serial: str
    package_id: Optional[int] = None
    branches: List[str] = Field(default_factory=lambda: ["guest", "authenticated"])
    duration_seconds: Optional[int] = Field(default=None, ge=300, le=7200)

    @field_validator("device_serial", mode="before")
    @classmethod
    def normalize_serial(cls, value):
        serial = str(value or "").strip()
        if not serial:
            raise ValueError("device_serial must not be empty")
        return serial

    @field_validator("branches", mode="before")
    @classmethod
    def normalize_branches(cls, value):
        source = list(value or ["guest", "authenticated"])
        result = []
        for item in source:
            key = str(item or "").strip().lower()
            # Membership in the profile's configured branches is enforced by
            # the run-create endpoint; here only keep keys path-safe.
            if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,63}", key):
                raise ValueError(f"unsupported inspection branch: {key}")
            if key not in result:
                result.append(key)
        if not result:
            raise ValueError("branches must not be empty")
        return result


class InspectionSelectionUpdate(BaseModel):
    state_ids: List[int] = Field(default_factory=list)
    observation_ids: List[int] = Field(default_factory=list)

    @field_validator("state_ids", "observation_ids", mode="before")
    @classmethod
    def normalize_ids(cls, value, info):
        result = []
        for item in list(value or []):
            item_id = int(item)
            if item_id <= 0:
                raise ValueError(f"{info.field_name} must contain positive ids")
            if item_id not in result:
                result.append(item_id)
        return result


class InspectionRepresentativeUpdate(BaseModel):
    observation_id: int = Field(gt=0)


class InspectionPageTemplateRead(BaseModel):
    id: int
    package_name: str
    activity: Optional[str] = None
    activity_family: Optional[str] = None
    page_role: str = "UNKNOWN"
    is_modal: bool = False
    fingerprint_version: int = 1
    template_key: str
    structure_signature: List[str] = Field(default_factory=list)
    action_signature: List[str] = Field(default_factory=list)
    anchor_signature: List[str] = Field(default_factory=list)
    control_state_signature: List[str] = Field(default_factory=list)
    risk_signature: List[str] = Field(default_factory=list)
    observation_count: int = 0
    first_seen_at: Any
    last_seen_at: Any = None
    created_at: Any
    updated_at: Any = None

    class Config:
        from_attributes = True


class StoredAssetRead(BaseModel):
    id: str
    logical_sha256: str
    blob_sha256: str
    media_type: str = "application/octet-stream"
    encoding: Optional[str] = None
    content_encoding: Optional[str] = None
    storage_key: str
    byte_size: int = 0
    width: Optional[int] = None
    height: Optional[int] = None
    original_width: Optional[int] = None
    original_height: Optional[int] = None
    scale: float = 1.0
    status: str = "ACTIVE"
    integrity_status: str = "VERIFIED"
    last_verified_at: Any = None
    orphaned_at: Any = None
    created_at: Any
    updated_at: Any = None

    class Config:
        from_attributes = True


class AssetReferenceRead(BaseModel):
    id: int
    asset_id: str
    owner_type: str
    owner_id: int
    role: str
    retention_class: str = "HOT"
    expires_at: Any = None
    pinned_reason: Optional[str] = None
    released_at: Any = None
    grace_until: Any = None
    created_at: Any

    class Config:
        from_attributes = True


class InspectionObservationRead(BaseModel):
    id: int
    run_id: int
    branch_run_id: int
    state_id: int
    template_id: Optional[int] = None
    transition_id: Optional[int] = None
    sequence: int = 0
    capture_kind: str = "DISCOVERY"
    package_name: Optional[str] = None
    activity: Optional[str] = None
    exact_cluster_key: str = ""
    exact_replay_key: str = ""
    exact_state_key: str = ""
    screenshot_sha: Optional[str] = None
    screenshot_phash: Optional[str] = None
    perceptual_hash: Optional[str] = None
    stable_by: Optional[str] = None
    screenshot_asset_id: Optional[str] = None
    xml_asset_id: Optional[str] = None
    thumbnail_asset_id: Optional[str] = None
    action_map_asset_id: Optional[str] = None
    asset_status: str = "AVAILABLE"
    is_representative: bool = False
    retention_class: str = "HOT"
    retained_until: Any = None
    original_width: Optional[int] = None
    original_height: Optional[int] = None
    match_confidence: Optional[float] = None
    match_evidence: Dict[str, Any] = Field(default_factory=dict)
    metadata_only: bool = False
    captured_at: Any
    created_at: Any

    class Config:
        from_attributes = True


class PaginatedInspectionObservationRead(BaseModel):
    total: int
    page: int
    page_size: int
    items: List[InspectionObservationRead] = Field(default_factory=list)


class InspectionReplayPathRead(CompatibilityReplayChainRead):
    branch_key: str
    branch_name: Optional[str] = None


class PaginatedInspectionReplayPathRead(BaseModel):
    schema_version: int = 3
    run_id: int
    branch_key: Optional[str] = None
    total: int
    page: int
    page_size: int
    summary: Dict[str, Any] = Field(default_factory=dict)
    items: List[InspectionReplayPathRead] = Field(default_factory=list)


class InspectionStateRead(BaseModel):
    id: int
    display_index: Optional[int] = None
    display_label: Optional[str] = None
    page_title: Optional[str] = None
    run_id: int
    branch_run_id: int
    branch_key: str
    cluster_key: str
    state_key: str
    template_id: Optional[int] = None
    semantic_key: Optional[str] = None
    identity_version: int = 1
    instance_anchor: Optional[str] = None
    exploration_family_id: Optional[int] = None
    family_match_confidence: Optional[float] = None
    family_match_evidence: Dict[str, Any] = Field(default_factory=dict)
    exploration_mode: str = "INDEPENDENT"
    page_subtype: str = "UNKNOWN"
    coverage_status: str = "DISCOVERED"
    frontier_priority: int = 700
    frontier_reason: Optional[str] = None
    expansion_status: str = "DISCOVERED"
    pending_action_count: int = 0
    last_action_cursor: Optional[int] = None
    recovery_retry_count: int = 0
    expansion_completed_at: Any = None
    representative_observation_id: Optional[int] = None
    observation_count: int = 0
    last_observed_at: Any = None
    queued_at: Any = None
    expanded_at: Any = None
    activity: Optional[str] = None
    foreground_package: Optional[str] = None
    depth: int = 0
    parent_state_id: Optional[int] = None
    incoming_transition_id: Optional[int] = None
    screenshot_path: Optional[str] = None
    thumbnail_path: Optional[str] = None
    xml_path: Optional[str] = None
    screenshot_sha: Optional[str] = None
    perceptual_hash: Optional[str] = None
    stable_status: str = "UNVERIFIED"
    reachability_evidence: str = "UNKNOWN"
    replay_scope: str = "NONE"
    replay_eligibility: str = "NONE"
    terminal_outcome: str = "NONE"
    boundary_evidence: List[str] = Field(default_factory=list)
    terminal_boundaries: List[Dict[str, Any]] = Field(default_factory=list)
    action_summary: Dict[str, Any] = Field(default_factory=dict)
    selected_for_regression: bool = False
    locator_quality: str = "UNKNOWN"
    is_dynamic: bool = False
    is_opaque: bool = False
    visit_count: int = 1
    first_path: List[Dict[str, Any]] = Field(default_factory=list)
    created_at: Any
    updated_at: Any = None

    class Config:
        from_attributes = True


class InspectionTransitionRead(BaseModel):
    id: int
    run_id: int
    branch_run_id: int
    from_state_id: int
    to_state_id: Optional[int] = None
    sequence: int = 0
    action_type: str
    action_key: str
    locator_candidates: List[Dict[str, Any]] = Field(default_factory=list)
    target_meta: Dict[str, Any] = Field(default_factory=dict)
    relation_type: Optional[str] = None
    relation_confidence: Optional[float] = None
    topology_type: Optional[str] = None
    action_role_key: Optional[str] = None
    action_role: Optional[str] = None
    execution_disposition: str = "EXECUTED"
    failure_type: Optional[str] = None
    coverage_source_transition_id: Optional[int] = None
    coverage_contract_id: Optional[int] = None
    action_group_key: Optional[str] = None
    sampling_disposition: Optional[str] = None
    visual_locator_evidence: Dict[str, Any] = Field(default_factory=dict)
    recovery_attempt_count: int = 0
    source_observation_id: Optional[int] = None
    target_observation_id: Optional[int] = None
    traversal_count: int = 1
    target_was_existing: bool = False
    status: str
    risk_type: Optional[str] = None
    reason: Optional[str] = None
    coordinate_only: bool = False
    replayable: bool = True
    duration_ms: float = 0.0
    input_rule_id: Optional[str] = None
    input_variable_key: Optional[str] = None
    input_length: Optional[int] = None
    error_message: Optional[str] = None
    created_at: Any

    class Config:
        from_attributes = True


class InspectionFaultRead(BaseModel):
    id: int
    run_id: int
    branch_run_id: Optional[int] = None
    state_id: Optional[int] = None
    transition_id: Optional[int] = None
    fault_type: str
    signature: str
    summary: Optional[str] = None
    full_log_path: Optional[str] = None
    screenshot_path: Optional[str] = None
    xml_path: Optional[str] = None
    replay_path: Optional[str] = None
    trace_path: Optional[str] = None
    full_log_asset_id: Optional[str] = None
    screenshot_asset_id: Optional[str] = None
    xml_asset_id: Optional[str] = None
    replay_asset_id: Optional[str] = None
    trace_asset_id: Optional[str] = None
    details: Dict[str, Any] = Field(default_factory=dict)
    occurrence_count: int = 1
    created_at: Any
    updated_at: Any = None

    class Config:
        from_attributes = True


class InspectionBranchRunRead(BaseModel):
    id: int
    run_id: int
    branch_key: str
    branch_name: str
    status: str
    current_stage: Optional[str] = None
    phase: Optional[str] = None
    frontier: Dict[str, int] = Field(default_factory=dict)
    root_state_id: Optional[int] = None
    stop_reason: Optional[str] = None
    state_count: int = 0
    transition_count: int = 0
    blocked_count: int = 0
    stable_count: int = 0
    fault_count: int = 0
    error_message: Optional[str] = None
    started_at: Any = None
    finished_at: Any = None

    class Config:
        from_attributes = True


class InspectionRunRead(BaseModel):
    id: int
    name: str
    profile_id: Optional[int] = None
    package_name: str
    package_id: Optional[int] = None
    package_source: str = "installed"
    profile_snapshot: Dict[str, Any] = Field(default_factory=dict)
    device_serial: str
    selected_branches: List[str] = Field(default_factory=list)
    coverage_manifest_id: Optional[str] = None
    coverage_manifest_version: Optional[str] = None
    coverage_manifest_hash: Optional[str] = None
    coverage_manifest_snapshot: Dict[str, Any] = Field(default_factory=dict)
    coverage_assessment: Dict[str, Any] = Field(default_factory=dict)
    coverage_verdict: str = "NOT_EVALUATED"
    coverage_evaluated_at: Any = None
    status: str
    current_stage: Optional[str] = None
    phase: Optional[str] = None
    frontier: Dict[str, int] = Field(default_factory=dict)
    effective_features: Dict[str, bool] = Field(default_factory=dict)
    stop_reason: Optional[str] = None
    total_branches: int = 0
    total_clusters: int = 0
    total_states: int = 0
    total_transitions: int = 0
    blocked_count: int = 0
    stable_count: int = 0
    fault_count: int = 0
    summary: Dict[str, Any] = Field(default_factory=dict)
    summary_available: bool = False
    summary_unavailable_reason: Optional[str] = None
    replay_source_eligible: bool = False
    replay_source_reason: Optional[str] = None
    replay_evidence_available: bool = False
    replay_default_eligible: bool = False
    last_active_state_id: Optional[int] = None
    last_observation_id: Optional[int] = None
    error_message: Optional[str] = None
    executor_name: Optional[str] = None
    created_at: Any
    started_at: Any = None
    finished_at: Any = None
    branches: List[InspectionBranchRunRead] = Field(default_factory=list)
    faults: List[InspectionFaultRead] = Field(default_factory=list)

    class Config:
        from_attributes = True


class PaginatedInspectionRunRead(BaseModel):
    total: int
    items: List[InspectionRunRead]


class InspectionFamilyActionCoverageRead(BaseModel):
    id: int
    family_id: int
    action_role_key: str
    action_role: Optional[str] = None
    status: str = "PENDING"
    source_state_id: Optional[int] = None
    source_transition_id: Optional[int] = None
    attempt_count: int = 0
    max_attempts: int = 2
    last_error: Optional[str] = None
    created_at: Any
    updated_at: Any = None

    class Config:
        from_attributes = True


class InspectionCoverageContractRead(BaseModel):
    id: int
    run_id: int
    branch_run_id: int
    contract_key: str
    scope: str = "FAMILY_ACTION"
    source_family_id: Optional[int] = None
    source_page_subtype: str = "UNKNOWN"
    action_group_key: str
    action_role: Optional[str] = None
    target_family_id: Optional[int] = None
    target_page_role: Optional[str] = None
    status: str = "PENDING"
    required_samples: int = 2
    success_count: int = 0
    failure_count: int = 0
    source_instance_anchors: List[str] = Field(default_factory=list)
    sample_transition_ids: List[int] = Field(default_factory=list)
    risk_signature: Optional[str] = None
    control_signature: Optional[str] = None
    last_error: Optional[str] = None
    created_at: Any
    updated_at: Any = None

    class Config:
        from_attributes = True


class InspectionExplorationFamilyRead(BaseModel):
    id: int
    run_id: int
    branch_run_id: int
    family_key: str
    fingerprint_version: int = 1
    page_role: str = "UNKNOWN"
    activity_family: Optional[str] = None
    representative_state_id: Optional[int] = None
    signature: Dict[str, Any] = Field(default_factory=dict)
    member_count: int = 0
    frontier: Dict[str, int] = Field(default_factory=dict)
    action_coverage: List[InspectionFamilyActionCoverageRead] = Field(
        default_factory=list
    )
    coverage_contracts: List[InspectionCoverageContractRead] = Field(
        default_factory=list
    )
    created_at: Any
    updated_at: Any = None

    class Config:
        from_attributes = True


class InspectionExplorationFamilyListRead(BaseModel):
    schema_version: int = 8
    run_id: int
    phase: Optional[str] = None
    frontier: Dict[str, int] = Field(default_factory=dict)
    effective_features: Dict[str, bool] = Field(default_factory=dict)
    items: List[InspectionExplorationFamilyRead] = Field(default_factory=list)


class CompatibilityRunCreate(BaseModel):
    name: str
    execution_mode: str = "comparison"
    old_package_id: Optional[int] = None
    new_package_id: Optional[int] = None
    page_set_id: Optional[int] = None
    source_type: str = "page_set"
    inspection_run_id: Optional[int] = None
    inspection_state_ids: List[int] = Field(default_factory=list)
    inspection_observation_ids: List[int] = Field(default_factory=list)
    replay_branch_key: Optional[str] = None
    selected_chain_ids: List[str] = Field(default_factory=list)
    selected_path_ids: List[str] = Field(default_factory=list)
    plan_digest: Optional[str] = None
    device_snapshot_digest: Optional[str] = None
    manual_install_confirmed: bool = False
    duration_seconds: Optional[int] = Field(default=None, ge=300, le=3600)
    device_serials: List[str]
    compare_mode: Optional[str] = None
    baseline_device_serial: Optional[str] = None
    mode: Optional[str] = None
    env_id: Optional[int] = None
    thresholds: Optional[CompatibilityThresholds] = None

    @field_validator("name", mode="before")
    @classmethod
    def normalize_name(cls, value):
        text = str(value or "").strip()
        if not text:
            raise ValueError("run name must not be empty")
        return text

    @field_validator("execution_mode", mode="before")
    @classmethod
    def normalize_execution_mode(cls, value):
        mode = str(value or "comparison").strip().lower()
        if mode not in {"comparison", "installed_replay"}:
            raise ValueError("execution_mode must be comparison or installed_replay")
        return mode

    @field_validator("mode", mode="before")
    @classmethod
    def normalize_mode(cls, value):
        if value is None:
            return None
        mode = str(value).strip().lower()
        if mode not in {"upgrade", "clean"}:
            raise ValueError("mode must be upgrade or clean")
        return mode

    @field_validator("compare_mode", mode="before")
    @classmethod
    def normalize_compare_mode(cls, value):
        if value is None:
            return None
        mode = str(value).strip().lower()
        if mode not in {"snapshot", "version", "device"}:
            raise ValueError("compare_mode must be snapshot, version or device")
        return mode

    @field_validator("source_type", mode="before")
    @classmethod
    def normalize_source_type(cls, value):
        source_type = str(value or "page_set").strip().lower()
        if source_type not in {"page_set", "inspection"}:
            raise ValueError("source_type must be page_set or inspection")
        return source_type

    @field_validator(
        "inspection_state_ids",
        "inspection_observation_ids",
        mode="before",
    )
    @classmethod
    def normalize_inspection_ids(cls, value, info):
        result = []
        for item in list(value or []):
            item_id = int(item)
            if item_id <= 0:
                raise ValueError(f"{info.field_name} must contain positive ids")
            if item_id not in result:
                result.append(item_id)
        return result

    @field_validator("selected_chain_ids", "selected_path_ids", mode="before")
    @classmethod
    def normalize_chain_ids(cls, value):
        result = []
        for item in list(value or []):
            chain_id = str(item or "").strip()
            if not chain_id:
                raise ValueError("selected_chain_ids must not contain blanks")
            if chain_id not in result:
                result.append(chain_id)
        return result

    @field_validator(
        "replay_branch_key",
        "plan_digest",
        "device_snapshot_digest",
        mode="before",
    )
    @classmethod
    def normalize_optional_text(cls, value):
        text = str(value or "").strip()
        return text or None

    @field_validator("baseline_device_serial", mode="before")
    @classmethod
    def normalize_baseline_serial(cls, value):
        serial = str(value or "").strip()
        return serial or None

    @field_validator("device_serials", mode="before")
    @classmethod
    def normalize_serials(cls, value):
        values = [value] if isinstance(value, str) else list(value or [])
        serials = []
        seen = set()
        for item in values:
            serial = str(item or "").strip()
            if serial and serial not in seen:
                serials.append(serial)
                seen.add(serial)
        if not serials:
            raise ValueError("device_serials must not be empty")
        return serials

    @model_validator(mode="after")
    def validate_device_compare_mode(self):
        if self.execution_mode == "installed_replay":
            self.source_type = "inspection"
            self.page_set_id = None
            if self.inspection_run_id is None:
                raise ValueError("installed replay requires inspection_run_id")
            if not self.replay_branch_key:
                raise ValueError("installed replay requires replay_branch_key")
            if len(self.device_serials) != 1:
                raise ValueError("installed replay requires exactly one device")
            if not self.selected_chain_ids and not self.selected_path_ids:
                raise ValueError(
                    "installed replay requires selected_path_ids or selected_chain_ids"
                )
            if not self.plan_digest:
                raise ValueError("installed replay requires plan_digest")
            if not self.device_snapshot_digest:
                raise ValueError("installed replay requires device_snapshot_digest")
            if not self.manual_install_confirmed:
                raise ValueError("installed replay requires manual_install_confirmed=true")
            self.duration_seconds = self.duration_seconds or 3600
            if self.old_package_id is not None or self.new_package_id is not None:
                raise ValueError("installed replay does not accept APK package ids")
            if self.compare_mode is not None or self.mode is not None:
                raise ValueError("installed replay does not accept compare_mode or mode")
            if self.thresholds is not None:
                raise ValueError("installed replay does not accept comparison thresholds")
            if self.inspection_state_ids or self.inspection_observation_ids:
                raise ValueError("installed replay selects frozen chains, not states")
            self.baseline_device_serial = None
            return self

        self.compare_mode = self.compare_mode or "version"
        self.mode = self.mode or "upgrade"
        self.thresholds = self.thresholds or CompatibilityThresholds()
        if self.selected_chain_ids or self.selected_path_ids:
            raise ValueError("comparison does not accept replay path ids")
        if self.duration_seconds is not None:
            raise ValueError("comparison does not accept duration_seconds")
        if self.new_package_id is None:
            raise ValueError("comparison requires new_package_id")
        if self.source_type == "page_set":
            if self.page_set_id is None:
                raise ValueError("page_set source requires page_set_id")
            if (
                self.inspection_run_id is not None
                or self.inspection_state_ids
                or self.inspection_observation_ids
            ):
                raise ValueError("page_set source cannot include inspection ids")
            if self.compare_mode == "snapshot":
                raise ValueError("snapshot compare_mode requires inspection source")
        else:
            if self.inspection_run_id is None:
                raise ValueError("inspection source requires inspection_run_id")
            self.page_set_id = None
            if self.compare_mode == "version":
                if self.old_package_id is None:
                    raise ValueError("巡检 Version 模式必须显式选择旧版 APK")
                if self.mode != "upgrade":
                    raise ValueError("巡检 Version 模式固定使用覆盖升级，不支持 clean")
        if self.compare_mode == "snapshot":
            if self.old_package_id is not None:
                raise ValueError("snapshot 模式使用巡检快照作为基线，old_package_id 必须为空")
            self.baseline_device_serial = None
        if self.compare_mode == "device":
            if self.old_package_id is not None:
                raise ValueError("机型对比模式只需一个测试包，old_package_id 必须为空")
            if len(self.device_serials) < 2:
                raise ValueError("机型对比模式至少需要 2 台设备")
            if self.baseline_device_serial is None:
                self.baseline_device_serial = self.device_serials[0]
            elif self.baseline_device_serial not in self.device_serials:
                raise ValueError("基准设备必须在所选设备列表中")
        else:
            self.baseline_device_serial = None
        return self


class CompatibilityPageResultRead(BaseModel):
    id: int
    run_id: int
    cell_id: int
    page_key: str = ""
    page_name: str = ""
    path_key: Optional[str] = None
    source_state_id: Optional[int] = None
    source_observation_id: Optional[int] = None
    evidence_level: Optional[str] = None
    failure_type: Optional[str] = None
    failed_step_index: Optional[int] = None
    replay_trace: List[Dict[str, Any]] = Field(default_factory=list)
    case_id: Optional[int] = None
    status: str = "PENDING"
    reason: Optional[str] = None
    required_text: Optional[str] = None
    baseline_screenshot_path: Optional[str] = None
    candidate_screenshot_path: Optional[str] = None
    diff_screenshot_path: Optional[str] = None
    baseline_xml_path: Optional[str] = None
    candidate_xml_path: Optional[str] = None
    baseline_screenshot_asset_id: Optional[str] = None
    candidate_screenshot_asset_id: Optional[str] = None
    diff_screenshot_asset_id: Optional[str] = None
    baseline_xml_asset_id: Optional[str] = None
    candidate_xml_asset_id: Optional[str] = None
    baseline_activity: Optional[str] = None
    candidate_activity: Optional[str] = None
    metrics: Dict[str, Any] = Field(default_factory=dict)
    created_at: Any
    updated_at: Any = None

    class Config:
        from_attributes = True


class CompatibilityCellRead(BaseModel):
    id: int
    run_id: int
    device_serial: str
    device_info: Optional[str] = None
    os_version: Optional[str] = None
    resolution: Optional[str] = None
    is_baseline: bool = False
    status: str = "PENDING"
    current_stage: Optional[str] = None
    old_install_status: Optional[str] = None
    new_install_status: Optional[str] = None
    preflight_at: Any = None
    installed_package_snapshot: Dict[str, Any] = Field(default_factory=dict)
    error_message: Optional[str] = None
    started_at: Any = None
    finished_at: Any = None
    pages: List[CompatibilityPageResultRead] = Field(default_factory=list)

    class Config:
        from_attributes = True


class CompatibilityRunRead(BaseModel):
    id: int
    name: str
    page_set_id: Optional[int] = None
    page_set_name: Optional[str] = None
    page_set_snapshot: List[
        Union[CompatPageDefinition, CompatibilityReplayChainRead]
    ] = Field(default_factory=list)
    source_type: str = "page_set"
    inspection_run_id: Optional[int] = None
    inspection_state_ids: List[int] = Field(default_factory=list)
    inspection_observation_ids: List[int] = Field(default_factory=list)
    source_coverage_snapshot: Dict[str, Any] = Field(default_factory=dict)
    old_package_id: Optional[int] = None
    new_package_id: Optional[int] = None
    package_name: str = ""
    execution_mode: str = "comparison"
    replay_branch_key: Optional[str] = None
    replay_plan_version: Optional[int] = None
    replay_plan_digest: Optional[str] = None
    duration_seconds: int = 3600
    source_package_snapshot: Dict[str, Any] = Field(default_factory=dict)
    target_package_snapshot: Dict[str, Any] = Field(default_factory=dict)
    manual_install_confirmed_at: Any = None
    compare_mode: Optional[str] = "version"
    baseline_device_serial: Optional[str] = None
    mode: Optional[str] = "upgrade"
    env_id: Optional[int] = None
    device_serials: List[str] = Field(default_factory=list)
    thresholds: Dict[str, Any] = Field(default_factory=dict)
    status: str = "PENDING"
    total_cells: int = 0
    total_pages: int = 0
    pass_count: int = 0
    warning_count: int = 0
    fail_count: int = 0
    error_message: Optional[str] = None
    executor_name: Optional[str] = None
    created_at: Any
    started_at: Any = None
    finished_at: Any = None
    page_set: Optional[CompatPageSetRead] = None
    cells: List[CompatibilityCellRead] = Field(default_factory=list)

    class Config:
        from_attributes = True


class PaginatedCompatibilityRunRead(BaseModel):
    total: int
    items: List[CompatibilityRunRead]


# ---- Global Variable / Environment Schemas ----

class EnvironmentCreate(BaseModel):
    name: str
    description: Optional[str] = None

class EnvironmentRead(BaseModel):
    id: int
    name: str
    description: Optional[str] = None
    created_at: Any

    class Config:
        from_attributes = True

class GlobalVariableCreate(BaseModel):
    key: str = Field(..., pattern=r"^[A-Z0-9_]+$")
    value: str = ""
    is_secret: bool = False
    description: Optional[str] = None

    @field_validator("value", "description", mode="before")
    @classmethod
    def normalize_variable_placeholders_in_fields(cls, value):
        return normalize_variable_placeholders(value)

class GlobalVariableRead(BaseModel):
    id: int
    env_id: int
    key: str
    value: str = ""
    is_secret: bool = False
    description: Optional[str] = None
    created_at: Any
    updated_at: Any = None

    @field_validator("value", "description", mode="before")
    @classmethod
    def normalize_variable_placeholders_in_fields(cls, value):
        return normalize_variable_placeholders(value)

    class Config:
        from_attributes = True

class GlobalVariableUpdate(BaseModel):
    key: Optional[str] = Field(default=None, pattern=r"^[A-Z0-9_]+$")
    value: Optional[str] = None
    is_secret: Optional[bool] = None
    description: Optional[str] = None

    @field_validator("value", "description", mode="before")
    @classmethod
    def normalize_variable_placeholders_in_fields(cls, value):
        return normalize_variable_placeholders(value)


# ---- Cross-Platform Step Schemas (跨端步骤) ----

class PlatformSelector(BaseModel):
    """单端选择器配置"""
    model_config = ConfigDict(extra="forbid")

    selector: str = Field(..., description="定位值，如 resourceId / label / xpath")
    by: str = Field(..., description="定位策略，如 id / text / xpath / label / name")

    @field_validator("selector", mode="before")
    @classmethod
    def normalize_selector_variable_placeholders(cls, value):
        return normalize_variable_placeholders(value)

class PlatformOverrides(BaseModel):
    model_config = ConfigDict(extra="forbid")

    """
    双端选择器覆盖配置。

    结构示例::

        {
            "android": {"selector": "id/login_btn", "by": "id"},
            "ios": {"selector": "登录", "by": "label"}
        }
    """
    android: Optional[PlatformSelector] = None
    ios: Optional[PlatformSelector] = None

class TestCaseStepWrite(BaseModel):
    """跨端测试步骤 — 写入模型（不含 case_id）"""
    order: int = Field(default=0, ge=0, description="步骤顺序，值越小越先执行")
    action: str = Field(..., description="标准动作名（建议小写）")
    args: Dict[str, Any] = Field(default_factory=dict, description="动作参数")
    value: Optional[str] = Field(default=None, description="兼容旧模型保留字段")
    execute_on: List[str] = Field(default_factory=lambda: ["android", "ios"])
    platform_overrides: PlatformOverrides = Field(default_factory=PlatformOverrides)
    timeout: int = Field(default=10, ge=1)
    error_strategy: str = Field(default="ABORT", description="ABORT | CONTINUE | IGNORE")
    retry_count: int = Field(default=0, ge=0, le=3, description="失败自动重试次数（0-3）")
    description: Optional[str] = None

    @field_validator("args", "value", "description", mode="before")
    @classmethod
    def normalize_variable_placeholders_in_fields(cls, value):
        return normalize_variable_placeholders(value)

class TestCaseStepCreate(TestCaseStepWrite):
    """跨端测试步骤 — 创建入参"""
    case_id: int = Field(..., description="所属用例 ID")

class TestCaseStepUpdate(BaseModel):
    """跨端测试步骤 — 单步更新入参（可选字段）"""
    order: Optional[int] = Field(default=None, ge=0)
    action: Optional[str] = None
    args: Optional[Dict[str, Any]] = None
    value: Optional[str] = None
    execute_on: Optional[List[str]] = None
    platform_overrides: Optional[PlatformOverrides] = None
    timeout: Optional[int] = Field(default=None, ge=1)
    error_strategy: Optional[str] = None
    description: Optional[str] = None

    @field_validator("args", "value", "description", mode="before")
    @classmethod
    def normalize_variable_placeholders_in_fields(cls, value):
        return normalize_variable_placeholders(value)

class TestCaseStepRead(TestCaseStepWrite):
    """跨端测试步骤 — 读取响应（包含 id/case_id）"""
    id: int
    case_id: int

    class Config:
        from_attributes = True
