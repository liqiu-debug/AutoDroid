import unittest
from datetime import datetime, timedelta
from unittest.mock import patch

from sqlmodel import Session, SQLModel, create_engine, select

from backend import retention_service
from backend.models import (
    CompatibilityCell,
    CompatibilityPageResult,
    CompatibilityRun,
    FastbotReport,
    FastbotTask,
    SystemSetting,
    TestExecution,
    TestResult,
)
from backend.retention_service import (
    cleanup_expired_reports,
    get_retention_days,
    run_retention_cleanup,
)


class RetentionDaysParsingTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
        SQLModel.metadata.create_all(self.engine)

    def _set(self, value):
        with Session(self.engine) as session:
            session.add(SystemSetting(key=retention_service.RETENTION_SETTING_KEY, value=value))
            session.commit()

    def test_missing_setting_means_disabled(self):
        with Session(self.engine) as session:
            self.assertEqual(get_retention_days(session), 0)

    def test_valid_setting(self):
        self._set("30")
        with Session(self.engine) as session:
            self.assertEqual(get_retention_days(session), 30)

    def test_invalid_or_non_positive_settings_mean_disabled(self):
        for raw in ("abc", "-5", "0"):
            engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
            SQLModel.metadata.create_all(engine)
            with Session(engine) as session:
                session.add(SystemSetting(key=retention_service.RETENTION_SETTING_KEY, value=raw))
                session.commit()
                self.assertEqual(get_retention_days(session), 0, raw)


class RetentionCleanupTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
        SQLModel.metadata.create_all(self.engine)
        self.now = datetime(2026, 7, 10, 12, 0, 0)
        self.old = self.now - timedelta(days=40)
        self.recent = self.now - timedelta(days=1)

        with Session(self.engine) as session:
            # UI 执行：过期已结束 / 过期运行中 / 新近
            session.add(TestExecution(id=1, scenario_id=1, scenario_name="old-pass", start_time=self.old, status="PASS"))
            session.add(TestResult(execution_id=1, step_name="s1", step_order=1, status="PASS"))
            session.add(TestExecution(id=2, scenario_id=1, scenario_name="old-running", start_time=self.old, status="RUNNING"))
            session.add(TestExecution(id=3, scenario_id=1, scenario_name="recent", start_time=self.recent, status="FAIL"))

            # Fastbot：过期已完成 / 过期运行中
            session.add(FastbotTask(id=1, package_name="com.a", device_serial="d1", created_at=self.old, status="COMPLETED"))
            session.add(FastbotReport(task_id=1))
            session.add(FastbotTask(id=2, package_name="com.b", device_serial="d2", created_at=self.old, status="RUNNING"))

            # 兼容性：过期已完成（含 cell 与页面结果）/ 过期 PENDING
            session.add(CompatibilityRun(id=1, name="old-run", new_package_id=1, created_at=self.old, status="COMPLETED"))
            session.add(CompatibilityCell(id=1, run_id=1, device_serial="d1"))
            session.add(CompatibilityPageResult(run_id=1, cell_id=1, page_key="home"))
            session.add(CompatibilityRun(id=2, name="old-pending", new_package_id=1, created_at=self.old, status="PENDING"))
            session.commit()

    def _cleanup(self, days=30):
        with patch.object(retention_service, "engine", self.engine), \
             patch("backend.api.reports._delete_execution_artifacts") as exec_artifacts, \
             patch("backend.api.fastbot._delete_fastbot_artifacts_dir") as fastbot_artifacts, \
             patch("backend.api.compatibility._delete_run_artifacts") as compat_artifacts:
            summary = cleanup_expired_reports(days, now=self.now)
        return summary, exec_artifacts, fastbot_artifacts, compat_artifacts

    def test_cleanup_deletes_only_expired_finished_records(self):
        summary, exec_artifacts, fastbot_artifacts, compat_artifacts = self._cleanup()

        self.assertEqual(summary["executions"], 1)
        self.assertEqual(summary["fastbot_tasks"], 1)
        self.assertEqual(summary["compatibility_runs"], 1)
        exec_artifacts.assert_called_once()
        fastbot_artifacts.assert_called_once_with(1)
        compat_artifacts.assert_called_once_with(1)

        with Session(self.engine) as session:
            executions = session.exec(select(TestExecution)).all()
            self.assertEqual({e.id for e in executions}, {2, 3})
            self.assertEqual(session.exec(select(TestResult)).all(), [])

            tasks = session.exec(select(FastbotTask)).all()
            self.assertEqual({t.id for t in tasks}, {2})
            self.assertEqual(session.exec(select(FastbotReport)).all(), [])

            runs = session.exec(select(CompatibilityRun)).all()
            self.assertEqual({r.id for r in runs}, {2})
            self.assertEqual(session.exec(select(CompatibilityCell)).all(), [])
            self.assertEqual(session.exec(select(CompatibilityPageResult)).all(), [])

    def test_cleanup_disabled_when_days_not_positive(self):
        summary, exec_artifacts, _, _ = self._cleanup(days=0)

        self.assertFalse(summary["enabled"])
        exec_artifacts.assert_not_called()
        with Session(self.engine) as session:
            self.assertEqual(len(session.exec(select(TestExecution)).all()), 3)

    def test_run_retention_cleanup_respects_setting(self):
        with patch.object(retention_service, "engine", self.engine):
            # 未配置：直接关闭
            self.assertEqual(run_retention_cleanup(), {"enabled": False})

        with Session(self.engine) as session:
            session.add(SystemSetting(key=retention_service.RETENTION_SETTING_KEY, value="30"))
            session.commit()

        with patch.object(retention_service, "engine", self.engine), \
             patch("backend.api.reports._delete_execution_artifacts"), \
             patch("backend.api.fastbot._delete_fastbot_artifacts_dir"), \
             patch("backend.api.compatibility._delete_run_artifacts"):
            summary = run_retention_cleanup()

        self.assertTrue(summary["enabled"])
        self.assertEqual(summary["executions"], 1)


if __name__ == "__main__":
    unittest.main()
