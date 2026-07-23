"""Fastbot 报告目录管理。"""
import os
from datetime import datetime
from typing import Optional

from backend.paths import project_path

FASTBOT_REPORTS_DIR = str(project_path("reports", "fastbot"))


def _build_fastbot_report_dir(
    task_id: Optional[int] = None,
    *,
    reports_root: Optional[str] = None,
    run_id: Optional[str] = None,
) -> str:
    """Build a report directory while preserving Fastbot's default layout."""
    task_segment = (
        str(run_id)
        if run_id not in (None, "")
        else str(task_id)
        if task_id is not None
        else datetime.now().strftime("adhoc_%Y%m%d_%H%M%S")
    )
    report_dir = os.path.join(reports_root or FASTBOT_REPORTS_DIR, task_segment)
    os.makedirs(report_dir, exist_ok=True)
    return report_dir
