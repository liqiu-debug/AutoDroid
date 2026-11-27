"""报告批量删除与 env_id 透传（用于重跑）单元测试。"""
from __future__ import annotations

import os
import tempfile
import unittest
from datetime import datetime
from unittest.mock import patch

from fastapi import HTTPException
from sqlmodel import Session, SQLModel, create_engine, select

from backend.api.reports import (
    BatchDeleteReportsRequest,
    batch_delete_reports,
    get_report_detail,
    get_reports,
)
from backend.models import TestExecution, TestResult


class ReportsBatchAndRerunTests(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
        SQLModel.metadata.create_all(self.engine)
        self.session = Session(self.engine)

    def tearDown(self) -> None:
        self.session.close()

    def test_reports_expose_env_id_for_rerun(self):
        execution = TestExecution(
            scenario_id=12,
            scenario_name="登录回归",
            status="FAIL",
            device_serial="dev-01",
            env_id=5,
            start_time=datetime(2026, 11, 27, 10, 0, 0),
        )
        self.session.add(execution)
        self.session.commit()
        self.session.refresh(execution)

        listing = get_reports(session=self.session)
        self.assertEqual(listing.total, 1)
        self.assertEqual(listing.items[0].env_id, 5)
        self.assertEqual(listing.items[0].scenario_id, 12)

        detail = get_report_detail(execution.id, session=self.session)
        self.assertEqual(detail.env_id, 5)

    def test_batch_delete_removes_executions_results_and_skips_running(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            report_file = os.path.join(tmpdir, "rep-1.html")
            with open(report_file, "w", encoding="utf-8") as fh:
                fh.write("<html></html>")

            e1 = TestExecution(
                scenario_id=10,
                scenario_name="S1",
                status="FAIL",
                batch_id="batch-1",
                report_id="rep-1.html",
                start_time=datetime(2026, 11, 27, 9, 0, 0),
            )
            e2 = TestExecution(
                scenario_id=10,
                scenario_name="S1",
                status="PASS",
                batch_id="batch-1",
                start_time=datetime(2026, 11, 27, 9, 1, 0),
            )
            e_single = TestExecution(
                scenario_id=11,
                scenario_name="S2",
                status="PASS",
                start_time=datetime(2026, 11, 27, 9, 2, 0),
            )
            e_running = TestExecution(
                scenario_id=12,
                scenario_name="S3",
                status="RUNNING",
                batch_id="batch-running",
                start_time=datetime(2026, 11, 27, 9, 3, 0),
            )
            self.session.add_all([e1, e2, e_single, e_running])
            self.session.commit()
            self.session.refresh(e1)
            self.session.refresh(e_single)

            self.session.add(
                TestResult(
                    execution_id=e1.id,
                    step_name="步骤 1",
                    step_order=1,
                    status="FAIL",
                )
            )
            self.session.commit()

            with patch("backend.api.reports._get_reports_dir", return_value=tmpdir):
                resp = batch_delete_reports(
                    BatchDeleteReportsRequest(
                        batch_ids=["batch-1", "batch-running"],
                        execution_ids=[e_single.id],
                    ),
                    session=self.session,
                )
            self.assertTrue(resp["success"])
            self.assertEqual(resp["deleted_count"], 3)
            self.assertEqual(resp["skipped_running_count"], 1)
            self.assertFalse(os.path.exists(report_file))

            remaining = self.session.exec(select(TestExecution)).all()
            self.assertEqual([item.batch_id for item in remaining], ["batch-running"])
            self.assertEqual(self.session.exec(select(TestResult)).all(), [])

    def test_batch_delete_rejects_empty_or_all_running(self):
        with self.assertRaises(HTTPException) as empty_ctx:
            batch_delete_reports(BatchDeleteReportsRequest(), session=self.session)
        self.assertEqual(empty_ctx.exception.status_code, 400)

        running = TestExecution(
            scenario_id=1,
            scenario_name="RunningOnly",
            status="RUNNING",
            batch_id="b-run",
            start_time=datetime(2026, 11, 27, 9, 4, 0),
        )
        self.session.add(running)
        self.session.commit()

        with self.assertRaises(HTTPException) as running_ctx:
            batch_delete_reports(
                BatchDeleteReportsRequest(batch_ids=["b-run"]),
                session=self.session,
            )
        self.assertEqual(running_ctx.exception.status_code, 400)
