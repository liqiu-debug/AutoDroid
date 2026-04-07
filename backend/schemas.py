from typing import List, Optional, Any, Dict
from enum import Enum
from pydantic import BaseModel, ConfigDict, Field

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

class Variable(BaseModel):
    key: str
    value: str

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
    pass

class TestScenarioRead(TestScenarioBase):
    id: int
    user_id: Optional[int] = None
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

class UserRegister(BaseModel):
    username: str
    password: str
    name: str


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

class GlobalVariableRead(BaseModel):
    id: int
    env_id: int
    key: str
    value: str = ""
    is_secret: bool = False
    description: Optional[str] = None
    created_at: Any
    updated_at: Any = None

    class Config:
        from_attributes = True

class GlobalVariableUpdate(BaseModel):
    key: Optional[str] = Field(default=None, pattern=r"^[A-Z0-9_]+$")
    value: Optional[str] = None
    is_secret: Optional[bool] = None
    description: Optional[str] = None


# ---- Cross-Platform Step Schemas (跨端步骤) ----

class PlatformSelector(BaseModel):
    """单端选择器配置"""
    model_config = ConfigDict(extra="forbid")

    selector: str = Field(..., description="定位值，如 resourceId / label / xpath")
    by: str = Field(..., description="定位策略，如 id / text / xpath / label / name")

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
    description: Optional[str] = None

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

class TestCaseStepRead(TestCaseStepWrite):
    """跨端测试步骤 — 读取响应（包含 id/case_id）"""
    id: int
    case_id: int

    class Config:
        from_attributes = True
