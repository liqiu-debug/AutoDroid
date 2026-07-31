from typing import Any, Dict, List, Optional
from datetime import datetime
from sqlmodel import SQLModel, Field, Column
from sqlalchemy import Index, Integer, JSON, UniqueConstraint
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
    # 新执行链路使用 owner-safe 设备租约；历史调用方可继续留空。
    lease_task_id: Optional[str] = Field(default=None, index=True)
    lease_kind: Optional[str] = None
    lease_acquired_at: Optional[datetime] = None
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
    platform: str = Field(default="android", index=True)  # "android" | "ios"
    app_name: str = Field(default="Unknown")         # 应用名称
    package_name: str = Field(default="", index=True) # Android 包名 / iOS Bundle ID
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
    source_type: str = Field(default="page_set", index=True)
    inspection_run_id: Optional[int] = Field(
        default=None,
        foreign_key="inspectionrun.id",
        index=True,
    )
    inspection_state_ids: List[int] = Field(
        default=[],
        sa_column=Column(PydanticListType(int)),
    )
    inspection_observation_ids: List[int] = Field(
        default=[],
        sa_column=Column(PydanticListType(int)),
    )
    source_coverage_snapshot: Dict[str, Any] = Field(
        default={}, sa_column=Column(JSON, default={})
    )
    old_package_id: Optional[int] = Field(default=None, foreign_key="apppackage.id", index=True)
    new_package_id: Optional[int] = Field(
        default=None,
        foreign_key="apppackage.id",
        index=True,
    )
    package_name: str = Field(default="", index=True)
    execution_mode: str = Field(default="COMPARISON", index=True)
    replay_branch_key: Optional[str] = Field(default=None, index=True)
    replay_plan_version: Optional[int] = None
    replay_plan_digest: Optional[str] = Field(default=None, index=True)
    replay_duration_seconds: int = Field(default=3600)
    source_package_snapshot: Dict[str, Any] = Field(
        default={},
        sa_column=Column(JSON, default={}),
    )
    target_package_snapshot: Dict[str, Any] = Field(
        default={},
        sa_column=Column(JSON, default={}),
    )
    manual_install_confirmed_at: Optional[datetime] = None
    compare_mode: Optional[str] = Field(default=None, index=True)  # snapshot | version | device
    baseline_device_serial: Optional[str] = Field(default=None)  # device 模式下的基准设备
    mode: Optional[str] = Field(default=None)  # upgrade | clean
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
    preflight_at: Optional[datetime] = None
    installed_package_snapshot: Dict[str, Any] = Field(
        default={},
        sa_column=Column(JSON, default={}),
    )
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
    path_key: Optional[str] = Field(default=None, index=True)
    source_state_id: Optional[int] = Field(
        default=None,
        foreign_key="inspectionstate.id",
        index=True,
    )
    source_observation_id: Optional[int] = Field(
        default=None,
        foreign_key="inspectionobservation.id",
        index=True,
    )
    evidence_level: Optional[str] = Field(default=None, index=True)
    failure_type: Optional[str] = Field(default=None, index=True)
    failed_step_index: Optional[int] = None
    replay_trace: List[Dict[str, Any]] = Field(
        default=[],
        sa_column=Column(JSON, default=[]),
    )
    case_id: Optional[int] = Field(default=None, foreign_key="testcase.id")
    status: str = Field(default="PENDING", index=True)
    reason: Optional[str] = None
    required_text: Optional[str] = None
    baseline_screenshot_path: Optional[str] = None
    candidate_screenshot_path: Optional[str] = None
    diff_screenshot_path: Optional[str] = None
    baseline_xml_path: Optional[str] = None
    candidate_xml_path: Optional[str] = None
    baseline_screenshot_asset_id: Optional[str] = Field(
        default=None,
        foreign_key="storedasset.id",
        index=True,
    )
    candidate_screenshot_asset_id: Optional[str] = Field(
        default=None,
        foreign_key="storedasset.id",
        index=True,
    )
    diff_screenshot_asset_id: Optional[str] = Field(
        default=None,
        foreign_key="storedasset.id",
        index=True,
    )
    baseline_xml_asset_id: Optional[str] = Field(
        default=None,
        foreign_key="storedasset.id",
        index=True,
    )
    candidate_xml_asset_id: Optional[str] = Field(
        default=None,
        foreign_key="storedasset.id",
        index=True,
    )
    baseline_ocr_text: Optional[str] = None
    candidate_ocr_text: Optional[str] = None
    baseline_activity: Optional[str] = None
    candidate_activity: Optional[str] = None
    metrics: Dict[str, Any] = Field(default={}, sa_column=Column(JSON, default={}))
    created_at: datetime = Field(default_factory=datetime.now)
    updated_at: Optional[datetime] = None


class InspectionProfile(SQLModel, table=True):
    """模型化智能巡检的可复用配置。"""
    id: Optional[int] = Field(default=None, primary_key=True)
    name: str = Field(index=True)
    package_name: str = Field(index=True)
    branches: Dict[str, Any] = Field(default={}, sa_column=Column(JSON, default={}))
    input_rules: List[Dict[str, Any]] = Field(default=[], sa_column=Column(JSON, default=[]))
    safety_rules: List[Dict[str, Any]] = Field(default=[], sa_column=Column(JSON, default=[]))
    sanitizer_rules: List[Dict[str, Any]] = Field(default=[], sa_column=Column(JSON, default=[]))
    dynamic_text_patterns: List[str] = Field(
        default=[],
        sa_column=Column(PydanticListType(str)),
    )
    budgets: Dict[str, Any] = Field(default={}, sa_column=Column(JSON, default={}))
    monitor_options: Dict[str, Any] = Field(default={}, sa_column=Column(JSON, default={}))
    user_id: Optional[int] = Field(default=None, foreign_key="user.id", index=True)
    updater_id: Optional[int] = Field(default=None, foreign_key="user.id")
    created_at: datetime = Field(default_factory=datetime.now)
    updated_at: Optional[datetime] = None


class InspectionRun(SQLModel, table=True):
    """一次 Android 模型化巡检任务。"""
    id: Optional[int] = Field(default=None, primary_key=True)
    name: str
    profile_id: Optional[int] = Field(default=None, foreign_key="inspectionprofile.id", index=True)
    package_name: str = Field(index=True)
    package_id: Optional[int] = Field(default=None, foreign_key="apppackage.id", index=True)
    package_source: str = Field(default="installed")
    profile_snapshot: Dict[str, Any] = Field(default={}, sa_column=Column(JSON, default={}))
    device_serial: str = Field(index=True)
    selected_branches: List[str] = Field(
        default=["guest", "authenticated"],
        sa_column=Column(PydanticListType(str)),
    )
    coverage_manifest_id: Optional[str] = Field(default=None, index=True)
    coverage_manifest_version: Optional[str] = None
    coverage_manifest_hash: Optional[str] = Field(default=None, index=True)
    coverage_manifest_snapshot: Dict[str, Any] = Field(
        default={}, sa_column=Column(JSON, default={})
    )
    coverage_assessment: Dict[str, Any] = Field(
        default={}, sa_column=Column(JSON, default={})
    )
    coverage_verdict: str = Field(default="NOT_EVALUATED", index=True)
    coverage_evaluated_at: Optional[datetime] = None
    status: str = Field(default="PENDING", index=True)
    current_stage: Optional[str] = None
    stop_reason: Optional[str] = None
    total_branches: int = Field(default=0)
    total_clusters: int = Field(default=0)
    total_states: int = Field(default=0)
    total_transitions: int = Field(default=0)
    blocked_count: int = Field(default=0)
    stable_count: int = Field(default=0)
    fault_count: int = Field(default=0)
    error_message: Optional[str] = None
    executor_id: Optional[int] = Field(default=None, foreign_key="user.id")
    executor_name: Optional[str] = None
    created_at: datetime = Field(default_factory=datetime.now)
    started_at: Optional[datetime] = None
    finished_at: Optional[datetime] = None


class InspectionBranchRun(SQLModel, table=True):
    """巡检任务中的单条登录态业务线。"""
    id: Optional[int] = Field(default=None, primary_key=True)
    run_id: int = Field(foreign_key="inspectionrun.id", index=True)
    branch_key: str = Field(index=True)
    branch_name: str
    status: str = Field(default="PENDING", index=True)
    current_stage: Optional[str] = None
    root_state_id: Optional[int] = Field(default=None, index=True)
    stop_reason: Optional[str] = None
    state_count: int = Field(default=0)
    transition_count: int = Field(default=0)
    blocked_count: int = Field(default=0)
    stable_count: int = Field(default=0)
    fault_count: int = Field(default=0)
    error_message: Optional[str] = None
    started_at: Optional[datetime] = None
    finished_at: Optional[datetime] = None


class InspectionPageTemplate(SQLModel, table=True):
    """Versioned, exact structural identity shared by inspection states."""

    __table_args__ = (
        UniqueConstraint(
            "package_name",
            "fingerprint_version",
            "template_key",
            name="uq_inspectionpagetemplate_identity",
        ),
        Index(
            "ix_inspectionpagetemplate_package_activity",
            "package_name",
            "activity",
        ),
    )

    id: Optional[int] = Field(default=None, primary_key=True)
    package_name: str = Field(index=True)
    activity: Optional[str] = Field(default=None, index=True)
    activity_family: Optional[str] = Field(default=None, index=True)
    page_role: str = Field(default="UNKNOWN", index=True)
    is_modal: bool = Field(default=False)
    fingerprint_version: int = Field(default=1, index=True)
    template_key: str = Field(index=True)
    structure_signature: List[str] = Field(
        default=[], sa_column=Column(JSON, default=[])
    )
    action_signature: List[str] = Field(
        default=[], sa_column=Column(JSON, default=[])
    )
    anchor_signature: List[str] = Field(
        default=[], sa_column=Column(JSON, default=[])
    )
    control_state_signature: List[str] = Field(
        default=[], sa_column=Column(JSON, default=[])
    )
    risk_signature: List[str] = Field(
        default=[], sa_column=Column(JSON, default=[])
    )
    observation_count: int = Field(default=0)
    first_seen_at: datetime = Field(default_factory=datetime.now)
    last_seen_at: Optional[datetime] = None
    created_at: datetime = Field(default_factory=datetime.now)
    updated_at: Optional[datetime] = None


class InspectionExplorationFamily(SQLModel, table=True):
    """Run-scoped logical page family used to converge repeated instances."""

    __table_args__ = (
        UniqueConstraint(
            "branch_run_id",
            "fingerprint_version",
            "family_key",
            name="uq_inspectionfamily_branch_version_key",
        ),
        Index(
            "ix_inspectionfamily_run_branch",
            "run_id",
            "branch_run_id",
        ),
    )

    id: Optional[int] = Field(default=None, primary_key=True)
    run_id: int = Field(foreign_key="inspectionrun.id", index=True)
    branch_run_id: int = Field(foreign_key="inspectionbranchrun.id", index=True)
    family_key: str = Field(index=True)
    fingerprint_version: int = Field(default=1, index=True)
    page_role: str = Field(default="UNKNOWN", index=True)
    activity_family: Optional[str] = Field(default=None, index=True)
    representative_state_id: Optional[int] = Field(
        default=None,
        foreign_key="inspectionstate.id",
        index=True,
    )
    signature: Dict[str, Any] = Field(
        default={},
        sa_column=Column(JSON, default={}),
    )
    member_count: int = Field(default=0)
    created_at: datetime = Field(default_factory=datetime.now)
    updated_at: Optional[datetime] = None


class InspectionState(SQLModel, table=True):
    """巡检状态图节点；截图/XML 仅保存报告相对路径。"""

    __table_args__ = (
        UniqueConstraint(
            "branch_run_id",
            "semantic_key",
            "instance_anchor",
            name="uq_inspectionstate_branch_semantic_instance",
        ),
        Index(
            "ix_inspectionstate_run_template",
            "run_id",
            "template_id",
        ),
    )

    id: Optional[int] = Field(default=None, primary_key=True)
    run_id: int = Field(foreign_key="inspectionrun.id", index=True)
    branch_run_id: int = Field(foreign_key="inspectionbranchrun.id", index=True)
    branch_key: str = Field(index=True)
    cluster_key: str = Field(index=True)
    state_key: str = Field(index=True)
    template_id: Optional[int] = Field(
        default=None,
        foreign_key="inspectionpagetemplate.id",
        index=True,
    )
    semantic_key: Optional[str] = Field(default=None, index=True)
    identity_version: int = Field(default=1, index=True)
    instance_anchor: Optional[str] = Field(default=None, index=True)
    # Content-insensitive logical screen identity; drives frontier priority and
    # the cross-run coverage denominator.  Coarser than semantic_key on purpose.
    surface_key: Optional[str] = Field(default=None, index=True)
    surface_fingerprint_version: int = Field(default=1, index=True)
    exploration_family_id: Optional[int] = Field(
        default=None,
        foreign_key="inspectionexplorationfamily.id",
        index=True,
    )
    family_match_confidence: Optional[float] = None
    family_match_evidence: Dict[str, Any] = Field(
        default={},
        sa_column=Column(JSON, default={}),
    )
    exploration_mode: str = Field(default="INDEPENDENT", index=True)
    page_subtype: str = Field(default="UNKNOWN", index=True)
    coverage_status: str = Field(default="DISCOVERED", index=True)
    frontier_priority: int = Field(default=700, index=True)
    frontier_reason: Optional[str] = None
    expansion_status: str = Field(default="DISCOVERED", index=True)
    pending_action_count: int = Field(default=0)
    last_action_cursor: Optional[int] = None
    recovery_retry_count: int = Field(default=0)
    expansion_completed_at: Optional[datetime] = None
    representative_observation_id: Optional[int] = Field(default=None, index=True)
    observation_count: int = Field(default=0)
    last_observed_at: Optional[datetime] = None
    queued_at: Optional[datetime] = None
    expanded_at: Optional[datetime] = None
    activity: Optional[str] = None
    foreground_package: Optional[str] = None
    depth: int = Field(default=0)
    parent_state_id: Optional[int] = Field(default=None, index=True)
    incoming_transition_id: Optional[int] = Field(default=None, index=True)
    screenshot_path: Optional[str] = None
    thumbnail_path: Optional[str] = None
    xml_path: Optional[str] = None
    screenshot_sha: Optional[str] = None
    perceptual_hash: Optional[str] = None
    stable_status: str = Field(default="UNVERIFIED", index=True)
    selected_for_regression: bool = Field(default=False)
    locator_quality: str = Field(default="UNKNOWN")
    is_dynamic: bool = Field(default=False)
    is_opaque: bool = Field(default=False)
    visit_count: int = Field(default=1)
    first_path: List[Dict[str, Any]] = Field(default=[], sa_column=Column(JSON, default=[]))
    created_at: datetime = Field(default_factory=datetime.now)
    updated_at: Optional[datetime] = None


class InspectionTransition(SQLModel, table=True):
    """状态图边，同时保存被安全策略拦截、无效果和执行失败动作。"""

    __table_args__ = (
        Index(
            "ix_inspectiontransition_branch_sequence",
            "branch_run_id",
            "sequence",
        ),
        Index(
            "ix_inspectiontransition_run_topology",
            "run_id",
            "topology_type",
        ),
    )

    id: Optional[int] = Field(default=None, primary_key=True)
    run_id: int = Field(foreign_key="inspectionrun.id", index=True)
    branch_run_id: int = Field(foreign_key="inspectionbranchrun.id", index=True)
    from_state_id: int = Field(foreign_key="inspectionstate.id", index=True)
    to_state_id: Optional[int] = Field(default=None, foreign_key="inspectionstate.id", index=True)
    sequence: int = Field(default=0)
    action_type: str = Field(index=True)
    action_key: str = Field(index=True)
    locator_candidates: List[Dict[str, Any]] = Field(default=[], sa_column=Column(JSON, default=[]))
    target_meta: Dict[str, Any] = Field(default={}, sa_column=Column(JSON, default={}))
    relation_type: Optional[str] = None
    relation_confidence: Optional[float] = None
    topology_type: Optional[str] = Field(default=None, index=True)
    action_role_key: Optional[str] = Field(default=None, index=True)
    action_role: Optional[str] = None
    execution_disposition: str = Field(default="EXECUTED", index=True)
    failure_type: Optional[str] = Field(default=None, index=True)
    coverage_source_transition_id: Optional[int] = Field(
        default=None,
        foreign_key="inspectiontransition.id",
        index=True,
    )
    coverage_contract_id: Optional[int] = Field(
        default=None,
        foreign_key="inspectioncoveragecontract.id",
        index=True,
    )
    action_group_key: Optional[str] = Field(default=None, index=True)
    sampling_disposition: Optional[str] = Field(default=None, index=True)
    visual_locator_evidence: Dict[str, Any] = Field(
        default={},
        sa_column=Column(JSON, default={}),
    )
    recovery_attempt_count: int = Field(default=0)
    source_observation_id: Optional[int] = Field(default=None, index=True)
    target_observation_id: Optional[int] = Field(default=None, index=True)
    traversal_count: int = Field(default=1)
    target_was_existing: bool = Field(default=False)
    status: str = Field(default="PENDING", index=True)
    risk_type: Optional[str] = None
    reason: Optional[str] = None
    coordinate_only: bool = Field(default=False)
    replayable: bool = Field(default=True)
    duration_ms: float = Field(default=0.0)
    input_rule_id: Optional[str] = None
    input_variable_key: Optional[str] = None
    input_length: Optional[int] = None
    error_message: Optional[str] = None
    created_at: datetime = Field(default_factory=datetime.now)


class InspectionFamilyActionCoverage(SQLModel, table=True):
    """Per-family action-role frontier and bounded execution history."""

    __table_args__ = (
        UniqueConstraint(
            "family_id",
            "action_role_key",
            name="uq_inspectionfamilycoverage_family_role",
        ),
        Index(
            "ix_inspectionfamilycoverage_family_status",
            "family_id",
            "status",
        ),
    )

    id: Optional[int] = Field(default=None, primary_key=True)
    family_id: int = Field(
        foreign_key="inspectionexplorationfamily.id",
        index=True,
    )
    action_role_key: str = Field(index=True)
    action_role: Optional[str] = None
    status: str = Field(default="PENDING", index=True)
    source_state_id: Optional[int] = Field(
        default=None,
        foreign_key="inspectionstate.id",
        index=True,
    )
    source_transition_id: Optional[int] = Field(
        default=None,
        foreign_key="inspectiontransition.id",
        index=True,
    )
    attempt_count: int = Field(default=0)
    max_attempts: int = Field(default=2)
    last_error: Optional[str] = None
    created_at: datetime = Field(default_factory=datetime.now)
    updated_at: Optional[datetime] = None


class InspectionCoverageContract(SQLModel, table=True):
    """Auditable, run-scoped evidence for representative action reuse."""

    __table_args__ = (
        UniqueConstraint(
            "branch_run_id",
            "contract_key",
            name="uq_inspectioncoveragecontract_branch_key",
        ),
        Index(
            "ix_inspectioncoveragecontract_run_status",
            "run_id",
            "status",
        ),
        Index(
            "ix_inspectioncoveragecontract_source_action",
            "source_family_id",
            "action_group_key",
        ),
    )

    id: Optional[int] = Field(default=None, primary_key=True)
    run_id: int = Field(foreign_key="inspectionrun.id", index=True)
    branch_run_id: int = Field(foreign_key="inspectionbranchrun.id", index=True)
    contract_key: str = Field(index=True)
    scope: str = Field(default="FAMILY_ACTION", index=True)
    source_family_id: Optional[int] = Field(
        default=None,
        foreign_key="inspectionexplorationfamily.id",
        index=True,
    )
    source_page_subtype: str = Field(default="UNKNOWN", index=True)
    action_group_key: str = Field(index=True)
    action_role: Optional[str] = None
    target_family_id: Optional[int] = Field(
        default=None,
        foreign_key="inspectionexplorationfamily.id",
        index=True,
    )
    target_page_role: Optional[str] = Field(default=None, index=True)
    status: str = Field(default="PENDING", index=True)
    required_samples: int = Field(default=2)
    success_count: int = Field(default=0)
    failure_count: int = Field(default=0)
    source_instance_anchors: List[str] = Field(
        default=[],
        sa_column=Column(JSON, default=[]),
    )
    sample_transition_ids: List[int] = Field(
        default=[],
        sa_column=Column(JSON, default=[]),
    )
    risk_signature: Optional[str] = None
    control_signature: Optional[str] = None
    last_error: Optional[str] = None
    created_at: datetime = Field(default_factory=datetime.now)
    updated_at: Optional[datetime] = None


class StoredAsset(SQLModel, table=True):
    """Immutable content-addressed report asset metadata."""

    __table_args__ = (
        UniqueConstraint("blob_sha256", name="uq_storedasset_blob_sha256"),
        UniqueConstraint("storage_key", name="uq_storedasset_storage_key"),
        Index(
            "ix_storedasset_logical_media_encoding",
            "logical_sha256",
            "media_type",
            "content_encoding",
        ),
    )

    id: str = Field(primary_key=True)
    logical_sha256: str = Field(index=True)
    blob_sha256: str = Field(index=True)
    media_type: str = Field(default="application/octet-stream", index=True)
    encoding: Optional[str] = Field(default=None, index=True)
    content_encoding: Optional[str] = None
    storage_key: str
    byte_size: int = Field(default=0)
    width: Optional[int] = None
    height: Optional[int] = None
    original_width: Optional[int] = None
    original_height: Optional[int] = None
    scale: float = Field(default=1.0)
    status: str = Field(default="ACTIVE", index=True)
    integrity_status: str = Field(default="VERIFIED", index=True)
    last_verified_at: Optional[datetime] = None
    orphaned_at: Optional[datetime] = Field(default=None, index=True)
    created_at: datetime = Field(default_factory=datetime.now)
    updated_at: Optional[datetime] = None


class AssetReference(SQLModel, table=True):
    """Owner-scoped lifecycle reference to an immutable stored asset."""

    __table_args__ = (
        UniqueConstraint(
            "owner_type",
            "owner_id",
            "role",
            name="uq_assetreference_owner_role",
        ),
        Index(
            "ix_assetreference_retention_expiry",
            "retention_class",
            "expires_at",
        ),
    )

    id: Optional[int] = Field(default=None, primary_key=True)
    asset_id: str = Field(foreign_key="storedasset.id", index=True)
    owner_type: str = Field(index=True)
    owner_id: int = Field(index=True)
    role: str = Field(index=True)
    retention_class: str = Field(default="HOT", index=True)
    expires_at: Optional[datetime] = Field(default=None, index=True)
    pinned_reason: Optional[str] = None
    released_at: Optional[datetime] = Field(default=None, index=True)
    grace_until: Optional[datetime] = Field(default=None, index=True)
    created_at: datetime = Field(default_factory=datetime.now)


class InspectionObservation(SQLModel, table=True):
    """One concrete capture mapped to a logical inspection state/template."""

    __table_args__ = (
        Index(
            "ix_inspectionobservation_run_sequence",
            "run_id",
            "sequence",
        ),
        Index(
            "ix_inspectionobservation_state_captured",
            "state_id",
            "captured_at",
        ),
        Index(
            "ix_inspectionobservation_template_match",
            "template_id",
            "match_confidence",
        ),
    )

    id: Optional[int] = Field(default=None, primary_key=True)
    run_id: int = Field(foreign_key="inspectionrun.id", index=True)
    branch_run_id: int = Field(foreign_key="inspectionbranchrun.id", index=True)
    state_id: int = Field(foreign_key="inspectionstate.id", index=True)
    template_id: Optional[int] = Field(
        default=None,
        foreign_key="inspectionpagetemplate.id",
        index=True,
    )
    transition_id: Optional[int] = Field(default=None, index=True)
    sequence: int = Field(default=0)
    capture_kind: str = Field(default="DISCOVERY", index=True)
    package_name: Optional[str] = None
    activity: Optional[str] = None
    exact_cluster_key: str = Field(default="", index=True)
    exact_replay_key: str = Field(default="", index=True)
    exact_state_key: str = Field(default="", index=True)
    screenshot_sha: Optional[str] = None
    screenshot_phash: Optional[str] = None
    perceptual_hash: Optional[str] = None
    stable_by: Optional[str] = None
    screenshot_asset_id: Optional[str] = Field(
        default=None,
        foreign_key="storedasset.id",
        index=True,
    )
    xml_asset_id: Optional[str] = Field(
        default=None,
        foreign_key="storedasset.id",
        index=True,
    )
    thumbnail_asset_id: Optional[str] = Field(
        default=None,
        foreign_key="storedasset.id",
        index=True,
    )
    action_map_asset_id: Optional[str] = Field(
        default=None,
        foreign_key="storedasset.id",
        index=True,
    )
    asset_status: str = Field(default="AVAILABLE", index=True)
    is_representative: bool = Field(default=False, index=True)
    retention_class: str = Field(default="HOT", index=True)
    retained_until: Optional[datetime] = Field(default=None, index=True)
    original_width: Optional[int] = None
    original_height: Optional[int] = None
    match_confidence: Optional[float] = None
    match_evidence: Dict[str, Any] = Field(
        default={},
        sa_column=Column(JSON, default={}),
    )
    metadata_only: bool = Field(default=False)
    captured_at: datetime = Field(default_factory=datetime.now)
    created_at: datetime = Field(default_factory=datetime.now)


class InspectionFault(SQLModel, table=True):
    """巡检期间捕获并聚类后的应用/设备故障。"""
    id: Optional[int] = Field(default=None, primary_key=True)
    run_id: int = Field(foreign_key="inspectionrun.id", index=True)
    branch_run_id: Optional[int] = Field(default=None, foreign_key="inspectionbranchrun.id", index=True)
    state_id: Optional[int] = Field(default=None, foreign_key="inspectionstate.id", index=True)
    transition_id: Optional[int] = Field(default=None, foreign_key="inspectiontransition.id", index=True)
    fault_type: str = Field(index=True)
    signature: str = Field(index=True)
    summary: Optional[str] = None
    full_log_path: Optional[str] = None
    screenshot_path: Optional[str] = None
    xml_path: Optional[str] = None
    replay_path: Optional[str] = None
    trace_path: Optional[str] = None
    details: Dict[str, Any] = Field(default={}, sa_column=Column(JSON, default={}))
    occurrence_count: int = Field(default=1)
    created_at: datetime = Field(default_factory=datetime.now)
    updated_at: Optional[datetime] = None


class InspectionAppSurface(SQLModel, table=True):
    """A logical application screen, accumulated across runs.

    Every other inspection table is scoped to one run, so a run can only measure
    coverage against what it happened to discover itself.  This table is scoped
    to the package instead: it is the denominator the report needs in order to
    say "we checked 61 of 70 screens" rather than "we checked everything we
    found".  Rows accumulate; a run updates it and never owns it.
    """

    __table_args__ = (
        UniqueConstraint(
            "package_name",
            "surface_fingerprint_version",
            "surface_key",
            name="uq_inspectionappsurface_package_version_key",
        ),
        Index(
            "ix_inspectionappsurface_package_subtype",
            "package_name",
            "page_subtype",
        ),
    )

    id: Optional[int] = Field(default=None, primary_key=True)
    package_name: str = Field(index=True)
    surface_key: str = Field(index=True)
    # Identity is only comparable within one skeleton rule.  Queries filter on
    # this so a rule change cannot silently corrupt the accumulated denominator.
    surface_fingerprint_version: int = Field(default=1, index=True)
    page_subtype: str = Field(default="UNKNOWN", index=True)
    role: str = Field(default="UNKNOWN")
    # Optional human name, shown in the report instead of the hash.
    label: Optional[str] = None
    first_seen_run_id: Optional[int] = Field(default=None, index=True)
    last_seen_run_id: Optional[int] = Field(default=None, index=True)
    last_seen_at: Optional[datetime] = Field(default=None, index=True)
    seen_run_count: int = Field(default=0)
    representative_state_id: Optional[int] = Field(default=None, index=True)
    representative_screenshot_path: Optional[str] = None
    # Set by hand when a screen is retired from the app, to drop it from the
    # denominator without deleting its history.
    is_retired: bool = Field(default=False, index=True)
    created_at: datetime = Field(default_factory=datetime.now)
    updated_at: Optional[datetime] = None


class InspectionAppAction(SQLModel, table=True):
    """One action slot on a surface, with its cross-run coverage history.

    ``action_role_key`` is already stable across runs (82 of 92 shared between
    two runs of the Haier mall, versus 47 of 75 for page templates), so it can
    carry the per-slot record that drives frontier priority: never covered
    first, then longest un-covered, then last failed.
    """

    __table_args__ = (
        UniqueConstraint(
            "surface_id",
            "action_role_key",
            name="uq_inspectionappaction_surface_role",
        ),
        Index(
            "ix_inspectionappaction_package_covered",
            "package_name",
            "last_covered_at",
        ),
    )

    id: Optional[int] = Field(default=None, primary_key=True)
    surface_id: int = Field(foreign_key="inspectionappsurface.id", index=True)
    package_name: str = Field(index=True)
    action_role_key: str = Field(index=True)
    action_role: Optional[str] = None
    action_type: Optional[str] = None
    last_covered_run_id: Optional[int] = Field(default=None, index=True)
    last_covered_at: Optional[datetime] = Field(default=None, index=True)
    coverage_count: int = Field(default=0)
    # NEVER until an execution produces a verdict; then the transition status.
    last_status: str = Field(default="NEVER", index=True)
    # Distinct runs whose outcome for this slot was a failure.  Derived, like
    # coverage_count, so re-folding a run cannot inflate it.
    failed_run_count: int = Field(default=0)
    first_seen_run_id: Optional[int] = Field(default=None, index=True)
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
