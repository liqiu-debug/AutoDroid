"""上报协议与 App 直跑结果构造：skip_reason 透传与结论口径（error_strategy 容错改造）。

覆盖：
- output.skip_reason -> 步骤结果 skip_reason（协议模型不改 schema 的透传约定）；
- 旧客户端缺该键安全回退（skip_reason=None，不报错、error 文本不动）；
- ignored-skip（skip_reason="失败已忽略：…"）不拉低用例/场景结论 -> PASS；
- 全 SKIP 例外（含全部被忽略）仍归 WARNING/skipped；
- client-runs 聚合 _aggregate_status 与平台口径统一。
"""
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any, Dict, List, Optional
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient
from jose import jwt
from sqlalchemy.pool import StaticPool
from sqlmodel import SQLModel, Session, create_engine, select

from backend.api import autojs as autojs_api
from backend.api import client_runs as client_runs_api
from backend.api.autojs import (
    DeviceReportCase,
    DeviceReportStep,
    _aggregate_status,
    _build_case_result,
)
from backend.core.security import ALGORITHM, SECRET_KEY
from backend.database import get_session
from backend.models import TestCase, TestCaseStep, TestResult, User

IGNORED = "失败已忽略：元素未找到: 提交"


def _report_step(
    *,
    order: int,
    status: str = "PASS",
    action: str = "click",
    error: Optional[str] = None,
    error_code: Optional[str] = None,
    output: Optional[Dict[str, Any]] = None,
    duration: float = 0.1,
) -> DeviceReportStep:
    return DeviceReportStep(
        order=order,
        action=action,
        status=status,
        duration=duration,
        error=error,
        error_code=error_code,
        output=output,
    )


class _SkipReasonBase(unittest.TestCase):
    """内存库 + client-runs 路由的最小执行环境（对齐 test_client_runs 的隔离口径）。"""

    def setUp(self) -> None:
        self.engine = create_engine(
            "sqlite://",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        SQLModel.metadata.create_all(self.engine)
        self.session = Session(self.engine)
        self.user = User(username="alice", hashed_password="x", role="user", full_name="alice")
        self.session.add(self.user)
        self.session.commit()
        self.session.refresh(self.user)

        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        tmp_path = Path(self.tmp.name)
        self.state_patch = patch.object(
            client_runs_api,
            "_run_state_path",
            lambda execution_id: tmp_path / "state" / f"exec_{execution_id}.json",
        )
        self.state_patch.start()
        self.addCleanup(self.state_patch.stop)

        import backend.scenario_results as scenario_results

        self.shots_patch = patch.object(
            scenario_results,
            "_get_reports_root_dir",
            return_value=str(tmp_path / "reports"),
        )
        self.shots_patch.start()
        self.addCleanup(self.shots_patch.stop)

        app = FastAPI()
        app.include_router(client_runs_api.router, prefix="/api/client-runs")
        app.include_router(autojs_api.device_router, prefix="/api/device-runs")
        app.dependency_overrides[get_session] = lambda: self.session
        self.client = TestClient(app)

    def tearDown(self) -> None:
        self.session.close()
        self.engine.dispose()

    @staticmethod
    def _headers() -> dict:
        return {"Authorization": f"Bearer {jwt.encode({'sub': 'alice'}, SECRET_KEY, algorithm=ALGORITHM)}"}

    def _create_case(self, error_strategy: str = "IGNORE") -> TestCase:
        case = TestCase(name="登录用例", steps=[], variables=[])
        self.session.add(case)
        self.session.commit()
        self.session.refresh(case)
        for order in range(1, 3):
            self.session.add(
                TestCaseStep(
                    case_id=case.id,
                    order=order,
                    action="click" if order == 1 else "assert_text",
                    execute_on=["android", "ios"],
                    platform_overrides={},
                    args={},
                    timeout=10,
                    error_strategy=error_strategy if order == 2 else "ABORT",
                    retry_count=0,
                    description=f"step-{order}",
                )
            )
        self.session.commit()
        return case

    def _start_run(self, case: TestCase) -> dict:
        resp = self.client.post(
            "/api/client-runs/",
            json={
                "run_id": "run-skip-1",
                "kind": "case",
                "case_id": case.id,
                "device_serial": "emulator-5554",
                "platform": "android",
            },
            headers=self._headers(),
        )
        self.assertEqual(resp.status_code, 200, resp.text)
        return resp.json()

    def _post_steps(self, execution_id: Any, case: TestCase, steps: List[dict], **extra) -> Any:
        payload = {"case_id": case.id, "name": case.name, "steps": steps}
        payload.update(extra)
        return self.client.post(
            f"/api/client-runs/{execution_id}/steps", json=payload, headers=self._headers()
        )

    def _finish(self, execution_id: Any) -> dict:
        with patch.object(client_runs_api.report_generator, "generate_report", return_value="r.html"), \
                patch.object(client_runs_api.NotificationService, "send_report_card"):
            resp = self.client.post(
                f"/api/client-runs/{execution_id}/finish",
                json={"notify": False},
                headers=self._headers(),
            )
        self.assertEqual(resp.status_code, 200, resp.text)
        return resp.json()

    def _state_steps(self, execution_id: Any) -> List[dict]:
        state = client_runs_api._load_run_state(execution_id)
        return list(state["cases"][0]["steps"]) if state.get("cases") else []


class BuildCaseResultSkipReasonTests(_SkipReasonBase):
    """App 直跑结果构造：_build_case_result 的 output.skip_reason 解析。"""

    def test_output_skip_reason_hoisted_to_step_result(self):
        case = self._create_case()
        reported = DeviceReportCase(
            case_id=case.id,
            name=case.name,
            success=True,
            steps=[
                _report_step(order=1),
                _report_step(
                    order=2,
                    status="SKIP",
                    error="元素未找到: 提交",
                    error_code="E2001_ELEMENT_NOT_FOUND",
                    output={"skip_reason": IGNORED},
                ),
            ],
        )

        result = _build_case_result(self.session, case, reported)
        skipped = result["steps"][1]

        self.assertEqual(skipped.get("skip_reason"), IGNORED)
        # 透传的 output 原样保留，报告层仍可从 output.skip_reason 兜底读取
        self.assertEqual(skipped["output"]["skip_reason"], IGNORED)
        # 详情留痕不因状态改为 SKIP 丢失
        self.assertTrue(skipped["is_skipped"])
        self.assertEqual(skipped["error"], "元素未找到: 提交")
        self.assertEqual(skipped["error_code"], "E2001_ELEMENT_NOT_FOUND")
        self.assertNotIn("skip_reason", result["steps"][0])

    def test_missing_skip_reason_falls_back_without_error(self):
        """旧客户端没有 output.skip_reason：安全回退，不报错。"""
        case = self._create_case()
        reported = DeviceReportCase(
            case_id=case.id,
            name=case.name,
            success=True,
            steps=[
                _report_step(order=1),
                _report_step(order=2, status="SKIP", error_code="P1001_PLATFORM_NOT_ALLOWED"),
                _report_step(order=2, status="FAIL", error="boom", output={}),
            ],
        )

        result = _build_case_result(self.session, case, reported)

        for entry in result["steps"]:
            self.assertIsNone(entry.get("skip_reason"))
        self.assertTrue(result["steps"][1]["is_skipped"])

    def test_blank_skip_reason_ignored(self):
        case = self._create_case()
        reported = DeviceReportCase(
            case_id=case.id,
            name=case.name,
            success=True,
            steps=[_report_step(order=1, status="SKIP", output={"skip_reason": "   "})],
        )

        result = _build_case_result(self.session, case, reported)

        self.assertIsNone(result["steps"][0].get("skip_reason"))

    def test_non_string_skip_reason_is_normalized(self):
        case = self._create_case()
        reported = DeviceReportCase(
            case_id=case.id,
            name=case.name,
            success=True,
            steps=[_report_step(order=1, status="SKIP", output={"skip_reason": 123})],
        )

        result = _build_case_result(self.session, case, reported)

        self.assertEqual(result["steps"][0].get("skip_reason"), "123")


class AggregateStatusSemanticsTests(unittest.TestCase):
    """执行结论聚合：ignored-skip 不拉低结论；全 SKIP -> WARNING 例外保留。"""

    def test_ignored_skip_does_not_lower_conclusion(self):
        entries = [{"status": "success"}, {"status": "skipped"}]
        self.assertEqual(_aggregate_status(entries, False), "PASS")

    def test_all_skipped_stays_warning(self):
        entries = [{"status": "skipped"}, {"status": "skipped"}]
        self.assertEqual(_aggregate_status(entries, False), "WARNING")

    def test_warning_and_failed_keep_their_conclusions(self):
        self.assertEqual(_aggregate_status([{"status": "success"}, {"status": "warning"}], False), "WARNING")
        self.assertEqual(_aggregate_status([{"status": "success"}, {"status": "failed"}], False), "FAIL")
        self.assertEqual(_aggregate_status([{"status": "skipped"}, {"status": "failed"}], False), "FAIL")
        self.assertEqual(_aggregate_status([], False), "PASS")

    def test_aborted_wins(self):
        self.assertEqual(_aggregate_status([{"status": "success"}], True), "ABORTED")


class ClientRunReportPayloadTests(_SkipReasonBase):
    """client-runs 链路：上报 -> 报告步骤结果 -> 用例/场景结论。"""

    def test_skip_reason_reaches_report_step_entries(self):
        case = self._create_case()
        run = self._start_run(case)
        resp = self._post_steps(
            run["execution_id"],
            case,
            [
                {"order": 1, "action": "click", "status": "PASS", "duration": 0.1},
                {
                    "order": 2,
                    "action": "assert_text",
                    "status": "SKIP",
                    "duration": 0.2,
                    "error": "元素未找到: 提交",
                    "error_code": "E2001_ELEMENT_NOT_FOUND",
                    "output": {"skip_reason": IGNORED},
                },
            ],
        )
        self.assertEqual(resp.status_code, 200, resp.text)

        steps = self._state_steps(run["execution_id"])
        self.assertIsNone(steps[0].get("skip_reason"))
        self.assertEqual(steps[1]["skip_reason"], IGNORED)
        # 详情留痕：error / error_code / suggestion 仍在，截图列由报告层渲染
        self.assertEqual(steps[1]["error"], "元素未找到: 提交")
        self.assertEqual(steps[1]["report_display"]["error_code"], "E2001_ELEMENT_NOT_FOUND")
        self.assertEqual(steps[1]["report_display"]["skip_reason"], IGNORED)
        self.assertEqual(steps[1]["status"], "skipped")
        self.assertEqual(steps[0]["status"], "success")

    def test_ignored_skip_case_conclusion_is_pass(self):
        """IGNORE 失败 -> 步骤 SKIP，用例/场景结论 PASS（即使客户端把用例 success 报成 False）。"""
        case = self._create_case()
        run = self._start_run(case)
        self._post_steps(
            run["execution_id"],
            case,
            [
                {"order": 1, "action": "click", "status": "PASS", "duration": 0.1},
                {
                    "order": 2,
                    "action": "assert_text",
                    "status": "SKIP",
                    "duration": 0.2,
                    "error": "元素未找到: 提交",
                    "output": {"skip_reason": IGNORED},
                },
            ],
            success=False,
        )

        body = self._finish(run["execution_id"])

        self.assertEqual(body["status"], "PASS")
        entries = client_runs_api._build_case_entries(client_runs_api._load_run_state(run["execution_id"]))
        self.assertEqual(entries[0]["status"], "success")

    def test_all_ignored_case_stays_warning(self):
        """全 SKIP 例外：用例所有步骤都是 SKIP（含全被忽略）-> 仍归 WARNING。"""
        case = self._create_case()
        run = self._start_run(case)
        self._post_steps(
            run["execution_id"],
            case,
            [
                {
                    "order": 1,
                    "action": "click",
                    "status": "SKIP",
                    "duration": 0.1,
                    "error": "元素未找到: 提交",
                    "output": {"skip_reason": IGNORED},
                },
                {
                    "order": 2,
                    "action": "assert_text",
                    "status": "SKIP",
                    "duration": 0.1,
                    "error": "输入失败",
                    "output": {"skip_reason": "失败已忽略：输入失败"},
                },
            ],
            success=True,
        )

        body = self._finish(run["execution_id"])

        self.assertEqual(body["status"], "WARNING")
        entries = client_runs_api._build_case_entries(client_runs_api._load_run_state(run["execution_id"]))
        self.assertEqual(entries[0]["status"], "skipped")

    def test_un_tolerated_failure_still_fails(self):
        case = self._create_case(error_strategy="ABORT")
        run = self._start_run(case)
        self._post_steps(
            run["execution_id"],
            case,
            [
                {"order": 1, "action": "click", "status": "PASS", "duration": 0.1},
                {"order": 2, "action": "assert_text", "status": "FAIL", "duration": 0.1, "error": "boom"},
            ],
        )

        body = self._finish(run["execution_id"])

        self.assertEqual(body["status"], "FAIL")

    def test_legacy_client_without_skip_reason_is_safe(self):
        """旧客户端：无 output.skip_reason 也不报错，平台跳过沿用现状（不计入失败）。"""
        case = self._create_case()
        run = self._start_run(case)
        resp = self._post_steps(
            run["execution_id"],
            case,
            [
                {"order": 1, "action": "click", "status": "PASS", "duration": 0.1},
                {
                    "order": 2,
                    "action": "assert_text",
                    "status": "SKIP",
                    "duration": 0.0,
                    "error_code": "P1001_PLATFORM_NOT_ALLOWED",
                },
            ],
        )
        self.assertEqual(resp.status_code, 200, resp.text)

        steps = self._state_steps(run["execution_id"])
        self.assertIsNone(steps[1].get("skip_reason"))
        self.assertEqual(steps[1]["status"], "skipped")
        self.assertEqual(self._finish(run["execution_id"])["status"], "PASS")

    def test_report_step_entries_normalize_output_only_skip_reason(self):
        """状态文件里只有 output.skip_reason 的旧步骤，转报告条目时补到顶层字段。"""
        step = {"status": "skipped", "error": "boom", "output": {"skip_reason": IGNORED}}
        merged = client_runs_api._report_step_with_skip_reason(step)
        self.assertEqual(merged["skip_reason"], IGNORED)
        self.assertNotIn("skip_reason", step, "纯增量：不得改动入参")

        # 缺 output.skip_reason：原样返回，不报错
        plain = {"status": "success"}
        self.assertIs(client_runs_api._report_step_with_skip_reason(plain), plain)

    def test_report_step_entries_keep_existing_skip_reason(self):
        step = {"status": "skipped", "skip_reason": IGNORED, "output": {"skip_reason": "其他"}}
        self.assertIs(client_runs_api._report_step_with_skip_reason(step), step)


class TestResultRowsSkipReasonTests(_SkipReasonBase):
    """落库 TestResult 的 report_display 同步带 skip_reason（供报告/Flaky 统计读取）。"""

    def test_test_result_report_display_carries_skip_reason(self):
        case = self._create_case()
        run = self._start_run(case)
        self._post_steps(
            run["execution_id"],
            case,
            [
                {
                    "order": 1,
                    "action": "click",
                    "status": "SKIP",
                    "duration": 0.1,
                    "error": "元素未找到: 提交",
                    "output": {"skip_reason": IGNORED},
                }
            ],
            success=True,
        )

        rows = self.session.exec(select(TestResult)).all()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].status, "SKIP")
        self.assertEqual(rows[0].report_display["skip_reason"], IGNORED)


class DeviceReportSkipReasonTests(_SkipReasonBase):
    """App 直跑（AutoJs6 设备端）结果构造：/api/device-runs/report 链路。"""

    def test_device_report_carries_skip_reason_and_keeps_pass(self):
        case = self._create_case()
        with patch.object(autojs_api.report_generator, "generate_report", return_value="r.html"), \
                patch.object(autojs_api.NotificationService, "send_report_card"):
            resp = self.client.post(
                "/api/device-runs/report",
                json={
                    "run_id": "run-app-1",
                    "case_id": case.id,
                    "platform": "android",
                    "device": {"model": "Pixel 7", "android_id": "device-1"},
                    "started_at": 0,
                    "finished_at": 1000,
                    "success": True,
                    "cases": [
                        {
                            "case_id": case.id,
                            "name": case.name,
                            "success": True,
                            "steps": [
                                {"order": 1, "action": "click", "status": "PASS", "duration": 0.1},
                                {
                                    "order": 2,
                                    "action": "assert_text",
                                    "status": "SKIP",
                                    "duration": 0.2,
                                    "error": "元素未找到: 提交",
                                    "error_code": "E2001_ELEMENT_NOT_FOUND",
                                    "output": {"skip_reason": IGNORED},
                                },
                            ],
                        }
                    ],
                },
                headers=self._headers(),
            )
        self.assertEqual(resp.status_code, 200, resp.text)
        # ignored-skip 不拉低结论
        self.assertEqual(resp.json()["status"], "PASS")

        rows = self.session.exec(select(TestResult).order_by(TestResult.step_order)).all()
        self.assertEqual([row.status for row in rows], ["PASS", "SKIP"])
        self.assertEqual(rows[1].report_display["skip_reason"], IGNORED)
        self.assertEqual(rows[1].error_message, "元素未找到: 提交")

    def test_device_report_without_skip_reason_is_safe(self):
        """旧客户端（无 output.skip_reason）：不报错，skip_reason 回退为空。"""
        case = self._create_case()
        with patch.object(autojs_api.report_generator, "generate_report", return_value="r.html"), \
                patch.object(autojs_api.NotificationService, "send_report_card"):
            resp = self.client.post(
                "/api/device-runs/report",
                json={
                    "run_id": "run-app-legacy",
                    "case_id": case.id,
                    "success": True,
                    "cases": [
                        {
                            "case_id": case.id,
                            "name": case.name,
                            "success": True,
                            "steps": [
                                {"order": 1, "action": "click", "status": "SKIP", "duration": 0.0,
                                 "error_code": "P1001_PLATFORM_NOT_ALLOWED"},
                            ],
                        }
                    ],
                },
                headers=self._headers(),
            )
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertEqual(resp.json()["status"], "WARNING")  # 全 SKIP 例外

        rows = self.session.exec(select(TestResult)).all()
        self.assertEqual(len(rows), 1)
        self.assertNotIn("skip_reason", rows[0].report_display or {})


if __name__ == "__main__":
    unittest.main()
