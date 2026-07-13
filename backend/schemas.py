from typing import List, Optional, Any, Dict
from enum import Enum
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
    custom_name: Optional[str] = None
    market_name: Optional[str] = None
    created_at: Any
    updated_at: Any = None

    class Config:
        from_attributes = True

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
    case_id: int
    settle_seconds: int = Field(default=2, ge=0, le=60)
    required_text: Optional[str] = None
    key: Optional[str] = None

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


class CompatibilityRunCreate(BaseModel):
    name: str
    old_package_id: Optional[int] = None
    new_package_id: int
    page_set_id: int
    device_serials: List[str]
    compare_mode: str = "version"
    baseline_device_serial: Optional[str] = None
    mode: str = "upgrade"
    env_id: Optional[int] = None
    thresholds: CompatibilityThresholds = Field(default_factory=CompatibilityThresholds)

    @field_validator("name", mode="before")
    @classmethod
    def normalize_name(cls, value):
        text = str(value or "").strip()
        if not text:
            raise ValueError("run name must not be empty")
        return text

    @field_validator("mode", mode="before")
    @classmethod
    def normalize_mode(cls, value):
        mode = str(value or "upgrade").strip().lower()
        if mode not in {"upgrade", "clean"}:
            raise ValueError("mode must be upgrade or clean")
        return mode

    @field_validator("compare_mode", mode="before")
    @classmethod
    def normalize_compare_mode(cls, value):
        mode = str(value or "version").strip().lower()
        if mode not in {"version", "device"}:
            raise ValueError("compare_mode must be version or device")
        return mode

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
    case_id: Optional[int] = None
    status: str = "PENDING"
    reason: Optional[str] = None
    required_text: Optional[str] = None
    baseline_screenshot_path: Optional[str] = None
    candidate_screenshot_path: Optional[str] = None
    diff_screenshot_path: Optional[str] = None
    baseline_xml_path: Optional[str] = None
    candidate_xml_path: Optional[str] = None
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
    page_set_snapshot: List[CompatPageDefinition] = Field(default_factory=list)
    old_package_id: Optional[int] = None
    new_package_id: int
    package_name: str = ""
    compare_mode: str = "version"
    baseline_device_serial: Optional[str] = None
    mode: str = "upgrade"
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
