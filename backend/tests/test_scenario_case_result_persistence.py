import unittest
import base64
import tempfile
from unittest.mock import patch
from pathlib import Path

from PIL import Image
from sqlmodel import Session, SQLModel, create_engine, select

from backend.api.scenarios import (
    _build_cases_results_from_raw_results,
    _persist_case_result_and_build_case_report,
    _persist_step_screenshot,
)
from backend.models import TestExecution, TestResult, TestScenario


class ScenarioCaseResultPersistenceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
        SQLModel.metadata.create_all(self.engine)
        self.session = Session(self.engine)

        scenario = TestScenario(name="scenario-1")
        self.session.add(scenario)
        self.session.commit()
        self.session.refresh(scenario)
        self.scenario_id = scenario.id

        execution = TestExecution(
            scenario_id=self.scenario_id,
            scenario_name="scenario-1",
            status="RUNNING",
            device_serial="ios-1",
            platform="ios",
            device_info="iPhone 15",
            executor_name="tester",
        )
        self.session.add(execution)
        self.session.commit()
        self.session.refresh(execution)
        self.execution_id = execution.id

    def tearDown(self) -> None:
        self.session.close()

    def test_persist_case_result_builds_case_entry_and_rows(self):
        item = {"alias": "case-a", "case_name": "Case A"}
        case_result = {
            "case_id": 101,
            "success": True,
            "steps": [
                {
                    "step": {
                        "action": "click",
                        "description": "点击登录",
                        "selector": "登录",
                        "selector_type": "text",
                    },
                    "success": True,
                    "duration": 0.25,
                    "screenshot": "ZmFrZQ==",
                },
                {
                    "step": {
                        "action": "wait_until_exists",
                        "description": "等待首页",
                    },
                    "success": False,
                    "is_warning": True,
                    "duration": 0.5,
                    "error": "ignored",
                },
            ],
            "is_warning": True,
        }

        with patch("backend.api.scenarios._persist_step_screenshot", return_value="screenshots/fake.png"):
            case_entry, next_order, case_duration = _persist_case_result_and_build_case_report(
                session=self.session,
                execution_id=self.execution_id,
                item=item,
                case_result=case_result,
                global_step_order=1,
                step_name_prefix="case-a",
                include_case_duration=True,
            )
            self.session.commit()

        self.assertEqual(case_entry["status"], "warning")
        self.assertEqual(case_entry["duration"], 0.75)
        self.assertEqual(case_duration, 0.75)
        self.assertEqual(next_order, 3)
        self.assertEqual(len(case_entry["steps"]), 2)
        self.assertEqual(case_entry["steps"][0]["status"], "success")
        self.assertEqual(case_entry["steps"][1]["status"], "warning")

        rows = self.session.exec(select(TestResult).order_by(TestResult.step_order)).all()
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0].status, "PASS")
        self.assertEqual(rows[0].step_name, "[case-a] 点击登录")
        self.assertEqual(rows[0].report_display["display_text"], "点击登录")
        self.assertEqual(rows[0].screenshot_path, "screenshots/fake.png")
        self.assertEqual(rows[1].status, "WARNING")
        self.assertEqual(rows[1].report_display["display_text"], "等待首页")
        self.assertEqual(rows[1].error_message, "ignored")

    def test_persist_error_code_and_suggestion_into_report_display(self):
        item = {"alias": "case-e", "case_name": "Case E"}
        case_result = {
            "case_id": 103,
            "success": False,
            "steps": [
                {
                    "step": {"action": "click", "selector": "登录", "selector_type": "text"},
                    "success": True,
                    "duration": 0.1,
                },
                {
                    "step": {"action": "click", "selector": "提交", "selector_type": "text"},
                    "success": False,
                    "duration": 0.2,
                    "error": "元素未找到: 提交",
                    "error_code": "E2001_ELEMENT_NOT_FOUND",
                    "error_context": {"action": "click", "selector": "提交"},
                    "suggestion": "请检查定位器是否正确",
                },
            ],
        }

        case_entry, _, _ = _persist_case_result_and_build_case_report(
            session=self.session,
            execution_id=self.execution_id,
            item=item,
            case_result=case_result,
            global_step_order=1,
            step_name_prefix="case-e",
        )
        self.session.commit()

        rows = self.session.exec(select(TestResult).order_by(TestResult.step_order)).all()
        self.assertEqual(len(rows), 2)
        # 成功步骤不携带结构化错误字段
        self.assertNotIn("error_code", rows[0].report_display)
        self.assertNotIn("suggestion", rows[0].report_display)
        # 失败步骤的错误码/建议随 report_display 持久化
        self.assertEqual(rows[1].status, "FAIL")
        self.assertEqual(rows[1].error_message, "元素未找到: 提交")
        self.assertEqual(rows[1].report_display["error_code"], "E2001_ELEMENT_NOT_FOUND")
        self.assertEqual(rows[1].report_display["suggestion"], "请检查定位器是否正确")

        # 场景报告使用的 case_entry 同步携带
        failed_display = case_entry["steps"][1]["report_display"]
        self.assertEqual(failed_display["error_code"], "E2001_ELEMENT_NOT_FOUND")
        self.assertEqual(failed_display["suggestion"], "请检查定位器是否正确")

    def test_case_level_error_screenshot_fills_first_failed_step(self):
        item = {"alias": "case-b", "case_name": "Case B"}
        case_result = {
            "case_id": 102,
            "success": False,
            "steps": [
                {
                    "step": {
                        "action": "click",
                        "description": "点确定",
                    },
                    "success": False,
                    "duration": 0.1,
                    "error": "boom",
                }
            ],
        }
        image = Image.new("RGB", (1, 1), color=(255, 0, 0))

        with patch("backend.api.scenarios._persist_step_screenshot", return_value="screenshots/fallback.png") as persist_mock:
            case_entry, next_order, _ = _persist_case_result_and_build_case_report(
                session=self.session,
                execution_id=self.execution_id,
                item=item,
                case_result=case_result,
                global_step_order=5,
                step_name_prefix="case-b",
                case_level_error_screenshot=image,
            )
            self.session.commit()

        self.assertEqual(case_entry["status"], "failed")
        self.assertEqual(next_order, 6)
        self.assertIn("screenshot", case_entry["steps"][0])
        persist_mock.assert_called_once()

    def test_build_cases_results_normalizes_legacy_error_items(self):
        raw_results = [
            {
                "step_order": 1,
                "case_id": 404,
                "alias": "missing-case",
                "error": "Case not found: 404",
            }
        ]

        cases_results = _build_cases_results_from_raw_results(
            session=self.session,
            execution_id=self.execution_id,
            raw_results=raw_results,
            include_case_duration=True,
        )
        self.session.commit()

        self.assertEqual(len(cases_results), 1)
        self.assertEqual(cases_results[0]["status"], "failed")
        self.assertEqual(cases_results[0]["case_id"], 404)
        self.assertEqual(cases_results[0]["case_name"], "Unknown")
        self.assertEqual(cases_results[0]["duration"], 0.0)
        self.assertEqual(cases_results[0]["steps"][0]["description"], "Case not found: 404")

        rows = self.session.exec(select(TestResult).order_by(TestResult.step_order)).all()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].status, "FAIL")
        self.assertEqual(rows[0].step_name, "[missing-case] Case not found: 404")
        self.assertEqual(rows[0].error_message, "Case not found: 404")

    def test_persist_step_screenshot_accepts_data_uri(self):
        png_payload = base64.b64encode(b"\x89PNG\r\n\x1a\nfake").decode("utf-8")

        with tempfile.TemporaryDirectory() as tmpdir:
            with patch("backend.api.scenarios._get_reports_root_dir", return_value=tmpdir):
                stored_path = _persist_step_screenshot(
                    execution_id=42,
                    step_order=7,
                    screenshot_b64=f"data:image/png;base64,{png_payload}",
                )

            self.assertEqual(stored_path, "screenshots/exec_42_step_7.png")
            saved_file = Path(tmpdir) / stored_path
            self.assertTrue(saved_file.exists())
            self.assertEqual(saved_file.read_bytes(), b"\x89PNG\r\n\x1a\nfake")


if __name__ == "__main__":
    unittest.main()
