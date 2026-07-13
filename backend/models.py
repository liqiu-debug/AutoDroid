from typing import Any, Dict, List, Optional
from datetime import datetime
from sqlmodel import SQLModel, Field, Column
from sqlalchemy import Integer, JSON
from .schemas import TestCaseBase, Step, Variable
from .json_type import PydanticListType


class CaseFolder(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    name: str
    parent_id: Optional[int] = Field(default=None, foreign_key="casefolder.id")
    created_at: datetime = Field(default_factory=datetime.now)


class ScenarioFolder(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    name: str
    parent_id: Optional[int] = Field(default=None, foreign_key="scenariofolder.id")
    created_at: datetime = Field(default_factory=datetime.now)


class User(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    username: str = Field(index=True, unique=True)
    hashed_password: str
    full_name: Optional[str] = None
    email: Optional[str] = None
    role: str = Field(default="user")
    is_active: bool = Field(default=True)
    created_at: datetime = Field(default_factory=datetime.now)


class ApiToken(SQLModel, table=True):
    """长效 API Token（机器凭证），供外部 CI 系统调用业务接口。

    仅存储 sha256 哈希，明文只在创建时返回一次。
    """

    id: Optional[int] = Field(default=None, primary_key=True)
    name: str
    token_hash: str = Field(index=True, unique=True)
    token_prefix: str
    user_id: int = Field(foreign_key="user.id")
    created_at: datetime = Field(default_factory=datetime.now)
    last_used_at: Optional[datetime] = None
    is_active: bool = Field(default=True)


class TestCase(TestCaseBase, SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    created_at: datetime = Field(default_factory=datetime.now)
    user_id: Optional[int] = Field(default=None, foreign_key="user.id")
    updater_id: Optional[int] = Field(default=None, foreign_key="user.id")
    updated_at: Optional[datetime] = None
    folder_id: Optional[int] = Field(default=None, foreign_key="casefolder.id")

    # Use the custom PydanticListType
    steps: List[Step] = Field(default=[], sa_column=Column(PydanticListType(Step)))
    variables: List[Variable] = Field(default=[], sa_column=Column(PydanticListType(Variable)))
    tags: List[str] = Field(default=[], sa_column=Column(PydanticListType(str)))
    last_run_status: Optional[str] = None
    last_run_time: Optional[datetime] = None


class ScenarioStep(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    scenario_id: int = Field(foreign_key="testscenario.id")
    case_id: int = Field(foreign_key="testcase.id")
    order: int
    alias: Optional[str] = None


class TestScenario(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    name: str
    description: Optional[str] = None
    user_id: Optional[int] = Field(default=None, foreign_key="user.id")
    updater_id: Optional[int] = Field(default=None, foreign_key="user.id")
    created_at: datetime = Field(default_factory=datetime.now)
    updated_at: Optional[datetime] = None
    folder_id: Optional[int] = Field(default=None, foreign_key="scenariofolder.id")
    
    # Statistics
    step_count: int = Field(default=0)
    last_run_status: Optional[str] = None # PASS, FAIL
    last_run_time: Optional[datetime] = None
    last_run_duration: Optional[int] = None # seconds
    last_report_id: Optional[str] = None # Filename of the report
    last_execution_id: Optional[int] = None # ID of the last TestExecution
    last_executor: Optional[str] = None # Executor of the last run
    last_failed_step: Optional[str] = None # Name of the last failed step


class TestExecution(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    scenario_id: int = Field(foreign_key="testscenario.id")
    executor_id: Optional[int] = Field(default=None, foreign_key="user.id") # Nullable for compatibility
    start_time: datetime = Field(default_factory=datetime.now)
    end_time: Optional[datetime] = None
    status: str = Field(default="RUNNING") # RUNNING, PASS, FAIL, WARNING, ERROR, ABORTED
    device_serial: Optional[str] = None
    platform: Optional[str] = None  # android | ios
    device_info: Optional[str] = None
    scenario_name: str # Snapshot of scenario name at time of execution
    executor_name: Optional[str] = None # Snapshot of user name
    duration: float = 0.0 # seconds
    report_id: Optional[str] = None # Filename of the generated HTML report
    batch_id: Optional[str] = Field(default=None, index=True) # UUID correlating multiple device runs
    batch_name: Optional[str] = None # Display name for the batch


class TestResult(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    execution_id: int = Field(foreign_key="testexecution.id")
    step_name: str
    step_order: int
    status: str # PASS, FAIL, SKIP, WARNING
    error_message: Optional[str] = None
    screenshot_path: Optional[str] = None
    ui_hierarchy: Optional[str] = None # Store XML content
    report_display: Optional[Dict[str, Any]] = Field(default=None, sa_column=Column(JSON))
    duration: float = 0.0 # milliseconds


class ScheduledTask(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    name: str
    scenario_id: Optional[int] = Field(default=None, foreign_key="testscenario.id")
    device_serial: Optional[str] = None
    strategy: str  # DAILY, WEEKLY, INTERVAL, ONCE
    strategy_config: Optional[str] = None  # JSON string
    is_active: bool = Field(default=True)
    enable_notification: bool = Field(default=True)  # 执行后是否发送飞书通知
    next_run_time: Optional[datetime] = None
    user_id: Optional[int] = Field(default=None, foreign_key="user.id")
    created_at: datetime = Field(default_factory=datetime.now)
    updated_at: Optional[datetime] = None


class SystemSetting(SQLModel, table=True):
    """全局系统配置 (Key-Value 存储)"""
    id: Optional[int] = Field(default=None, primary_key=True)
    key: str = Field(unique=True, index=True)
    value: str = Field(default="")
    description: Optional[str] = None


class FastbotTask(SQLModel, table=True):
    """Fastbot 智能探索任务"""
    id: Optional[int] = Field(default=None, primary_key=True)
    package_name: str
    duration: int = 600  # 探索时长(秒)
    throttle: int = 500  # 操作频率(ms)
    ignore_crashes: bool = Field(default=False)
    capture_log: bool = Field(default=True)
    device_serial: str
    status: str = Field(default="PENDING")  # PENDING, RUNNING, COMPLETED, FAILED
    total_crashes: int = Field(default=0)
    total_anrs: int = Field(default=0)
    executor_id: Optional[int] = Field(default=None, foreign_key="user.id")
    executor_name: Optional[str] = None
    created_at: datetime = Field(default_factory=datetime.now)
    started_at: Optional[datetime] = None
    finished_at: Optional[datetime] = None


class FastbotReport(SQLModel, table=True):
    """Fastbot 性能报告数据"""
    id: Optional[int] = Field(default=None, primary_key=True)
    task_id: int = Field(foreign_key="fastbottask.id")
    performance_data: Optional[str] = None   # JSON: [{time, cpu, mem}, ...]
    jank_data: Optional[str] = None          # JSON: [{time, fps, jank_rate, ...}, ...]
    jank_events: Optional[str] = None        # JSON: [{time, severity, reason, ...}, ...]
    trace_artifacts: Optional[str] = None    # JSON: [{path, trigger_time, ...}, ...]
    crash_events: Optional[str] = None       # JSON: [{time, type, full_log}, ...]
    summary: Optional[str] = None            # JSON: {avg_cpu, max_cpu, avg_mem, max_mem, ...}
    created_at: datetime = Field(default_factory=datetime.now)


class Device(SQLModel, table=True):
    """设备管理表 - 记录由 ADB / tidevice 同步的物理设备"""
    id: Optional[int] = Field(default=None, primary_key=True)
    serial: str = Field(unique=True, index=True)
    platform: str = Field(default="android")  # "android" | "ios"
    model: str = Field(default="Unknown")
    brand: str = Field(default="")
    android_version: str = Field(default="")
    os_version: str = Field(default="")        # 跨平台统一版本号
    resolution: str = Field(default="")
    status: str = Field(default="IDLE")  # IDLE, BUSY, OFFLINE, WDA_DOWN
    connection_type: Optional[str] = Field(default=None)  # iOS: 最近一次同步的 usbmux 连接方式 usb | network
    custom_name: Optional[str] = Field(default=None)  # 用户自定义设备名称
    market_name: Optional[str] = Field(default=None)  # 设备市场型号
    created_at: datetime = Field(default_factory=datetime.now)
    updated_at: Optional[datetime] = None


class Environment(SQLModel, table=True):
    """全局变量-环境"""
    id: Optional[int] = Field(default=None, primary_key=True)
    name: str = Field(index=True)
    description: Optional[str] = None
    created_at: datetime = Field(default_factory=datetime.now)


class GlobalVariable(SQLModel, table=True):
    """全局变量"""
    id: Optional[int] = Field(default=None, primary_key=True)
    env_id: int = Field(foreign_key="environment.id", index=True)
    key: str
    value: str = Field(default="")
    is_secret: bool = Field(default=False)
    description: Optional[str] = None
    created_at: datetime = Field(default_factory=datetime.now)
    updated_at: Optional[datetime] = None


class AppPackage(SQLModel, table=True):
    """APP 安装包管理"""
    id: Optional[int] = Field(default=None, primary_key=True)
    app_name: str = Field(default="Unknown")         # 应用名称
    package_name: str = Field(default="", index=True) # 包名
    version_name: str = Field(default="")             # 版本号
    version_code: str = Field(default="")             # 构建号
    file_path: str = Field(default="")                # 项目内相对存储路径
    file_size: float = Field(default=0.0)             # 文件大小 (MB)
    is_latest: bool = Field(default=True)             # 是否为最新包
    upload_time: datetime = Field(default_factory=datetime.now)
    uploader_id: Optional[int] = Field(default=None, foreign_key="user.id")
    uploader_name: Optional[str] = None


class CompatPageSet(SQLModel, table=True):
    """兼容性测试页面集合：用已有用例进入预设页面并采集快照。"""
    id: Optional[int] = Field(default=None, primary_key=True)
    name: str
    description: Optional[str] = None
    pages: List[Dict[str, Any]] = Field(default=[], sa_column=Column(JSON, default=[]))
    user_id: Optional[int] = Field(default=None, foreign_key="user.id")
    updater_id: Optional[int] = Field(default=None, foreign_key="user.id")
    created_at: datetime = Field(default_factory=datetime.now)
    updated_at: Optional[datetime] = None


class CompatibilityRun(SQLModel, table=True):
    """生产包视觉兼容性测试任务。"""
    id: Optional[int] = Field(default=None, primary_key=True)
    name: str
    page_set_id: Optional[int] = Field(default=None, foreign_key="compatpageset.id", index=True)
    page_set_name: Optional[str] = None
    page_set_snapshot: List[Dict[str, Any]] = Field(default=[], sa_column=Column(JSON, default=[]))
    old_package_id: Optional[int] = Field(default=None, foreign_key="apppackage.id", index=True)
    new_package_id: int = Field(foreign_key="apppackage.id", index=True)
    package_name: str = Field(default="", index=True)
    compare_mode: str = Field(default="version", index=True)  # version（同设备新旧对比）| device（跨设备横向对比）
    baseline_device_serial: Optional[str] = Field(default=None)  # device 模式下的基准设备
    mode: str = Field(default="upgrade")  # upgrade | clean
    env_id: Optional[int] = Field(default=None, foreign_key="environment.id")
    device_serials: List[str] = Field(default=[], sa_column=Column(PydanticListType(str)))
    thresholds: Dict[str, Any] = Field(default={}, sa_column=Column(JSON, default={}))
    status: str = Field(default="PENDING", index=True)
    total_cells: int = Field(default=0)
    total_pages: int = Field(default=0)
    pass_count: int = Field(default=0)
    warning_count: int = Field(default=0)
    fail_count: int = Field(default=0)
    error_message: Optional[str] = None
    user_id: Optional[int] = Field(default=None, foreign_key="user.id")
    executor_name: Optional[str] = None
    created_at: datetime = Field(default_factory=datetime.now)
    started_at: Optional[datetime] = None
    finished_at: Optional[datetime] = None


class CompatibilityCell(SQLModel, table=True):
    """兼容性任务中单台设备的执行单元。"""
    id: Optional[int] = Field(default=None, primary_key=True)
    run_id: int = Field(foreign_key="compatibilityrun.id", index=True)
    device_serial: str = Field(index=True)
    device_info: Optional[str] = None
    os_version: Optional[str] = None
    resolution: Optional[str] = None
    is_baseline: bool = Field(default=False)  # device 模式下该设备是否为横向对比基准
    status: str = Field(default="PENDING", index=True)
    current_stage: Optional[str] = None
    old_install_status: Optional[str] = None
    new_install_status: Optional[str] = None
    error_message: Optional[str] = None
    started_at: Optional[datetime] = None
    finished_at: Optional[datetime] = None


class CompatibilityPageResult(SQLModel, table=True):
    """兼容性任务中单页面的旧/新版对比结果。"""
    id: Optional[int] = Field(default=None, primary_key=True)
    run_id: int = Field(foreign_key="compatibilityrun.id", index=True)
    cell_id: int = Field(foreign_key="compatibilitycell.id", index=True)
    page_key: str = Field(default="")
    page_name: str = Field(default="")
    case_id: Optional[int] = Field(default=None, foreign_key="testcase.id")
    status: str = Field(default="PENDING", index=True)
    reason: Optional[str] = None
    required_text: Optional[str] = None
    baseline_screenshot_path: Optional[str] = None
    candidate_screenshot_path: Optional[str] = None
    diff_screenshot_path: Optional[str] = None
    baseline_xml_path: Optional[str] = None
    candidate_xml_path: Optional[str] = None
    baseline_ocr_text: Optional[str] = None
    candidate_ocr_text: Optional[str] = None
    baseline_activity: Optional[str] = None
    candidate_activity: Optional[str] = None
    metrics: Dict[str, Any] = Field(default={}, sa_column=Column(JSON, default={}))
    created_at: datetime = Field(default_factory=datetime.now)
    updated_at: Optional[datetime] = None


class TestCaseStep(SQLModel, table=True):
    """
    跨端测试步骤表

    支持"一套 JSON 数据，双端分发执行"：
    - execute_on: 标记该步骤允许在哪些平台运行
    - platform_overrides: 存储各平台的选择器覆盖配置
    """
    id: Optional[int] = Field(default=None, primary_key=True)
    case_id: int = Field(foreign_key="testcase.id", index=True)
    order: int = Field(
        default=0,
        sa_column=Column("step_order", Integer, default=0),
    )
    action: str = Field(default="click")
    args: dict = Field(
        default={},
        sa_column=Column(JSON, default={}),
    )
    value: Optional[str] = None
    timeout: int = Field(default=10)
    error_strategy: str = Field(default="ABORT")
    # 失败自动重试次数（0-3，0 表示不重试；总尝试 = 1 + retry_count）
    retry_count: int = Field(default=0)
    description: Optional[str] = None

    # 核心字段 1：允许执行的平台列表，默认双端
    execute_on: List[str] = Field(
        default=["android", "ios"],
        sa_column=Column(PydanticListType(str)),
    )

    # 核心字段 2：各平台的选择器覆盖
    # 结构示例: {"android": {"selector": "id/login", "by": "id"}, "ios": {"selector": "登录", "by": "label"}}
    platform_overrides: dict = Field(
        default={},
        sa_column=Column(JSON, default={}),
    )
