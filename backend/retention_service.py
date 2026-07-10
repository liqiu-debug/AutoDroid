"""报告数据保留策略：按配置的保留天数定期清理过期报告与产物。

保留天数存于 SystemSetting（key: ``report_retention_days``）：
- 未设置 / 0 / 非法值：不清理（默认关闭，避免升级后静默删除历史数据）
- N > 0：每日定时清理 N 天前产生且已结束的 UI 执行、Fastbot 任务、兼容性任务，
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
    TestExecution,
    TestResult,
)

logger = logging.getLogger(__name__)

RETENTION_SETTING_KEY = "report_retention_days"
RETENTION_JOB_ID = "report_retention_cleanup"

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
                session.delete(result)
            for cell in cells:
                session.delete(cell)
            session.delete(run)
            deleted += 1
        except Exception:
            logger.exception("清理过期兼容性任务失败: run_id=%s", run.id)
    return deleted


def cleanup_expired_reports(days: int, now: Optional[datetime] = None) -> Dict[str, Any]:
    """删除 days 天前且已结束的报告数据，返回清理摘要。"""
    if days <= 0:
        return {"enabled": False, "executions": 0, "fastbot_tasks": 0, "compatibility_runs": 0}

    cutoff = (now or datetime.now()) - timedelta(days=days)
    summary: Dict[str, Any] = {"enabled": True, "cutoff": cutoff.isoformat()}
    with Session(engine) as session:
        summary["executions"] = _cleanup_expired_executions(session, cutoff)
        summary["fastbot_tasks"] = _cleanup_expired_fastbot_tasks(session, cutoff)
        summary["compatibility_runs"] = _cleanup_expired_compatibility_runs(session, cutoff)
        session.commit()
    return summary


def run_retention_cleanup() -> Dict[str, Any]:
    """定时任务入口：读取配置并执行清理。"""
    with Session(engine) as session:
        days = get_retention_days(session)
    if days <= 0:
        return {"enabled": False}
    summary = cleanup_expired_reports(days)
    if summary.get("executions") or summary.get("fastbot_tasks") or summary.get("compatibility_runs"):
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
