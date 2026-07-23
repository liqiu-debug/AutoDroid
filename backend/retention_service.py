"""报告数据保留策略：按配置的保留天数定期清理过期报告与产物。

保留天数存于 SystemSetting（key: ``report_retention_days``）：
- 未设置 / 0 / 非法值：不清理（默认关闭，避免升级后静默删除历史数据）
- N > 0：每日定时清理 N 天前产生且已结束的 UI 执行、Fastbot、兼容性和巡检任务，
  同时删除对应的 HTML 报告、失败截图与 reports/ 下的产物目录。

复用各业务 API 中已有的产物删除逻辑，保证与手动删除行为一致。
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any, Dict, Optional

from sqlmodel import Session, select

from backend.database import engine
from backend.feature_flags import get_setting_value
from backend.models import (
    CompatibilityCell,
    CompatibilityPageResult,
    CompatibilityRun,
    FastbotReport,
    FastbotTask,
    InspectionBranchRun,
    InspectionFault,
    InspectionObservation,
    InspectionRun,
    InspectionState,
    InspectionTransition,
    TestExecution,
    TestResult,
)

logger = logging.getLogger(__name__)

RETENTION_SETTING_KEY = "report_retention_days"
RETENTION_JOB_ID = "report_retention_cleanup"
ASSET_LOW_WATERMARK_KEY = "asset_storage_low_watermark_percent"
ASSET_HIGH_WATERMARK_KEY = "asset_storage_high_watermark_percent"
ASSET_CRITICAL_WATERMARK_KEY = "asset_storage_critical_watermark_percent"

# 仍在进行中的记录不允许清理
_ACTIVE_STATUSES = {"PENDING", "RUNNING"}


def get_retention_days(session: Session) -> int:
    """读取保留天数配置；未设置/非法/非正数均视为 0（关闭清理）。"""
    raw = get_setting_value(session, RETENTION_SETTING_KEY)
    if raw is None:
        return 0
    try:
        days = int(str(raw).strip())
    except (TypeError, ValueError):
        logger.warning("系统配置 %s=%r 非法，保留策略视为关闭", RETENTION_SETTING_KEY, raw)
        return 0
    return days if days > 0 else 0


def _cleanup_expired_executions(session: Session, cutoff: datetime) -> int:
    from backend.api.reports import _delete_execution_artifacts

    expired = session.exec(
        select(TestExecution).where(
            TestExecution.start_time < cutoff,
            TestExecution.status != "RUNNING",
        )
    ).all()
    deleted = 0
    for execution in expired:
        try:
            results = session.exec(
                select(TestResult).where(TestResult.execution_id == execution.id)
            ).all()
            _delete_execution_artifacts(execution, results)
            for result in results:
                session.delete(result)
            session.delete(execution)
            deleted += 1
        except Exception:
            logger.exception("清理过期执行记录失败: execution_id=%s", execution.id)
    return deleted


def _cleanup_expired_fastbot_tasks(session: Session, cutoff: datetime) -> int:
    from backend.api.fastbot import _delete_fastbot_artifacts_dir

    expired = session.exec(
        select(FastbotTask).where(FastbotTask.created_at < cutoff)
    ).all()
    deleted = 0
    for task in expired:
        if str(task.status or "").upper() in _ACTIVE_STATUSES:
            continue
        try:
            reports = session.exec(
                select(FastbotReport).where(FastbotReport.task_id == task.id)
            ).all()
            for report in reports:
                session.delete(report)
            session.delete(task)
            _delete_fastbot_artifacts_dir(task.id)
            deleted += 1
        except Exception:
            logger.exception("清理过期 Fastbot 任务失败: task_id=%s", task.id)
    return deleted


def _cleanup_expired_compatibility_runs(session: Session, cutoff: datetime) -> int:
    from backend.api.compatibility import _delete_run_artifacts
    from backend.artifact_store import release_owner_references

    expired = session.exec(
        select(CompatibilityRun).where(CompatibilityRun.created_at < cutoff)
    ).all()
    deleted = 0
    for run in expired:
        if str(run.status or "").upper() in _ACTIVE_STATUSES:
            continue
        try:
            _delete_run_artifacts(run.id)
            page_results = session.exec(
                select(CompatibilityPageResult).where(CompatibilityPageResult.run_id == run.id)
            ).all()
            cells = session.exec(
                select(CompatibilityCell).where(CompatibilityCell.run_id == run.id)
            ).all()
            for result in page_results:
                release_owner_references(
                    session,
                    owner_type="compatibility_page_result",
                    owner_id=result.id,
                    commit=False,
                )
                session.delete(result)
            for cell in cells:
                release_owner_references(
                    session,
                    owner_type="compatibility_cell",
                    owner_id=cell.id,
                    commit=False,
                )
                session.delete(cell)
            release_owner_references(
                session,
                owner_type="compatibility_run",
                owner_id=run.id,
                commit=False,
            )
            session.delete(run)
            deleted += 1
        except Exception:
            logger.exception("清理过期兼容性任务失败: run_id=%s", run.id)
    return deleted


def _cleanup_expired_inspection_runs(session: Session, cutoff: datetime) -> int:
    from backend.api.inspections import _delete_inspection_run_artifacts
    from backend.artifact_store import release_owner_references

    expired = session.exec(
        select(InspectionRun).where(InspectionRun.created_at < cutoff)
    ).all()
    deleted = 0
    for run in expired:
        if str(run.status or "").upper() in _ACTIVE_STATUSES:
            continue
        try:
            _delete_inspection_run_artifacts(run.id)
            # Compatibility reports own PINNED CAS baselines plus legacy path
            # fallbacks and remain readable after their source run expires.
            referencing_compatibility_runs = session.exec(
                select(CompatibilityRun).where(
                    CompatibilityRun.inspection_run_id == run.id
                )
            ).all()
            for compatibility_run in referencing_compatibility_runs:
                compatibility_run.inspection_run_id = None
                session.add(compatibility_run)
            faults = session.exec(
                select(InspectionFault).where(InspectionFault.run_id == run.id)
            ).all()
            transitions = session.exec(
                select(InspectionTransition).where(
                    InspectionTransition.run_id == run.id
                )
            ).all()
            states = session.exec(
                select(InspectionState).where(InspectionState.run_id == run.id)
            ).all()
            branches = session.exec(
                select(InspectionBranchRun).where(InspectionBranchRun.run_id == run.id)
            ).all()
            observations = session.exec(
                select(InspectionObservation).where(
                    InspectionObservation.run_id == run.id
                )
            ).all()
            for item in faults:
                release_owner_references(
                    session,
                    owner_type="inspection_fault",
                    owner_id=item.id,
                    commit=False,
                )
                session.delete(item)
            for item in transitions:
                session.delete(item)
            for item in observations:
                release_owner_references(
                    session,
                    owner_type="inspection_observation",
                    owner_id=item.id,
                    commit=False,
                )
                session.delete(item)
            for item in states:
                release_owner_references(
                    session,
                    owner_type="inspection_state",
                    owner_id=item.id,
                    commit=False,
                )
                release_owner_references(
                    session,
                    owner_type="inspection_regression",
                    owner_id=item.id,
                    commit=False,
                )
                session.delete(item)
            for item in branches:
                session.delete(item)
            release_owner_references(
                session,
                owner_type="inspection_run",
                owner_id=run.id,
                commit=False,
            )
            session.delete(run)
            deleted += 1
        except Exception:
            logger.exception("清理过期巡检任务失败: run_id=%s", run.id)
    return deleted


def cleanup_expired_reports(days: int, now: Optional[datetime] = None) -> Dict[str, Any]:
    """删除 days 天前且已结束的报告数据，返回清理摘要。"""
    if days <= 0:
        return {
            "enabled": False,
            "executions": 0,
            "fastbot_tasks": 0,
            "compatibility_runs": 0,
            "inspection_runs": 0,
        }

    cutoff = (now or datetime.now()) - timedelta(days=days)
    summary: Dict[str, Any] = {"enabled": True, "cutoff": cutoff.isoformat()}
    with Session(engine) as session:
        summary["executions"] = _cleanup_expired_executions(session, cutoff)
        summary["fastbot_tasks"] = _cleanup_expired_fastbot_tasks(session, cutoff)
        summary["compatibility_runs"] = _cleanup_expired_compatibility_runs(session, cutoff)
        summary["inspection_runs"] = _cleanup_expired_inspection_runs(session, cutoff)
        session.commit()
    return summary


def _watermark_percent(session: Session, key: str, default: float) -> float:
    raw = get_setting_value(session, key)
    if raw is None:
        return default
    try:
        value = float(str(raw).strip())
    except (TypeError, ValueError):
        logger.warning("系统配置 %s=%r 非法，使用默认值 %.1f", key, raw, default)
        return default
    if not 0.0 < value <= 100.0:
        logger.warning("系统配置 %s=%r 超出范围，使用默认值 %.1f", key, raw, default)
        return default
    return value


def run_retention_cleanup() -> Dict[str, Any]:
    """定时任务入口：独立执行旧报告清理与分层资产 GC。"""
    from backend.artifact_store import (
        DEFAULT_CRITICAL_WATERMARK_PERCENT,
        DEFAULT_HIGH_WATERMARK_PERCENT,
        DEFAULT_LOW_WATERMARK_PERCENT,
        cleanup_verified_legacy_files,
        gc_assets,
        materialize_warm_derivatives,
        tiered_asset_retention_enabled,
        transition_warm_observations_to_cold,
    )

    with Session(engine) as session:
        days = get_retention_days(session)
        asset_retention_enabled = tiered_asset_retention_enabled(session)
        low = _watermark_percent(
            session,
            ASSET_LOW_WATERMARK_KEY,
            DEFAULT_LOW_WATERMARK_PERCENT,
        )
        high = _watermark_percent(
            session,
            ASSET_HIGH_WATERMARK_KEY,
            DEFAULT_HIGH_WATERMARK_PERCENT,
        )
        critical = _watermark_percent(
            session,
            ASSET_CRITICAL_WATERMARK_KEY,
            DEFAULT_CRITICAL_WATERMARK_PERCENT,
        )
    if days <= 0 and not asset_retention_enabled:
        return {"enabled": False}

    if not low < high < critical:
        logger.warning(
            "资产水位必须满足 low < high < critical，回退默认 80/90/95: %s/%s/%s",
            low,
            high,
            critical,
        )
        low = DEFAULT_LOW_WATERMARK_PERCENT
        high = DEFAULT_HIGH_WATERMARK_PERCENT
        critical = DEFAULT_CRITICAL_WATERMARK_PERCENT

    summary = cleanup_expired_reports(days) if days > 0 else {"enabled": True}
    summary["report_retention_enabled"] = days > 0
    summary["asset_retention_enabled"] = asset_retention_enabled
    if asset_retention_enabled:
        with Session(engine) as session:
            summary["legacy_rollback_cleanup"] = cleanup_verified_legacy_files(
                session
            )
            summary["warm_derivatives"] = materialize_warm_derivatives(session)
            summary["cold_transitions"] = transition_warm_observations_to_cold(
                session
            )
            summary["assets"] = gc_assets(
                session,
                low_watermark_percent=low,
                high_watermark_percent=high,
                critical_watermark_percent=critical,
            )
    if any(
        summary.get(key)
        for key in (
            "executions",
            "fastbot_tasks",
            "compatibility_runs",
            "inspection_runs",
        )
    ):
        logger.info("报告保留清理完成: %s", summary)
    return summary


def register_retention_job() -> None:
    """向全局调度器注册每日清理任务（每天 03:17，避开整点高峰）。"""
    from apscheduler.triggers.cron import CronTrigger

    from backend.scheduler_service import get_scheduler

    get_scheduler().scheduler.add_job(
        run_retention_cleanup,
        trigger=CronTrigger(hour=3, minute=17),
        id=RETENTION_JOB_ID,
        name="报告保留策略清理",
        replace_existing=True,
    )
    logger.info("报告保留清理任务已注册（每日 03:17，保留天数由 %s 控制）", RETENTION_SETTING_KEY)
