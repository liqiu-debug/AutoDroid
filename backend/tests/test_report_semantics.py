"""结论口径与报告展示语义（error_strategy=IGNORE 失败 -> SKIP / 用例与场景结论）。

覆盖：
- IGNORE 失败（skip_reason 以"失败已忽略"开头）不计入 has_warning / has_fail，用例结论 PASS；
- 全 SKIP（含全部被忽略）例外：用例结论仍归 WARNING/skipped；
- CONTINUE / ABORT 失败：用例结论 FAIL，场景层按最后一个失败步骤的 error_strategy 路由；
- 场景层 IGNORE 步骤不中断、不判失败，场景结论 PASS；
- skip_reason 进入 report_display（纯增量 JSON），失败详情留痕保留；
- global_step_order 打平与步骤顺序一一对应。
"""
import shutil
import tempfile
import threading
import unittest
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import patch

from sqlmodel import Session, SQLModel, create_engine

from backend.api.cases import _summarize_case_result
from backend.report_display import build_report_display
from backend.scenario_execution import _run_scenario_cross_platform
from backend.scenario_results import (
    _build_cases_results_from_raw_results,
    _convert_cross_result_to_legacy_case_result,
    _determine_case_status,
    _summarize_cases_results,
    _summarize_scenario_raw_results,
    is_tolerated_failure,
)
from backend.models import ScenarioStep, TestCase, TestExecution, TestScenario

IGNORED = "失败已忽略：元素未找到: 提交"


def _cross_step(
    *,
    status,
    action="click",
    description="点提交",
    error_strategy="ABORT",
    error=None,
    skip_reason=None,
    error_code=None,
    duration=0.1,
):
    step = {
        "action": action,
        "status": status,
        "platform": "ios",
        "device_id": "ios-1",
        "error_strategy": error_strategy,
        "duration": duration,
        "attempts": 1,
        "error": error,
        "error_code": error_code,
        "error_context": {"action": action} if error else None,
        "suggestion": "请检查定位器" if error_code else None,
        "output": None,
        "artifacts": None,
        "step": {
            "action": action,
            "description": description,
            "error_strategy": error_strategy,
        },
    }
    if skip_reason:
        step["skip_reason"] = skip_reason
    return step


class CaseConclusionSemanticsTests(unittest.TestCase):
    def test_ignored_failure_case_is_pass(self):
        result = {
            "success": False,
            "steps": [
                {"status": "PASS", "step": {"error_strategy": "ABORT"}},
                _cross_step(
                    status="SKIP",
                    error="元素未找到: 提交",
                    error_code="E2001_ELEMENT_NOT_FOUND",
                    error_strategy="IGNORE",
                    skip_reason=IGNORED,
                ),
            ],
        }
        summary = _summarize_case_result(result)
        self.assertEqual(summary["status"], "PASS")
        self.assertFalse(summary["all_skipped"])

    def test_ignored_failure_legacy_warning_case_is_pass(self):
        # 兼容旧表示（IGNORE 失败曾上报为 WARNING）：结论仍按新口径归 PASS
        result = {
            "success": False,
            "steps": [
                {"status": "PASS", "step": {"error_strategy": "ABORT"}},
                {
                    "success": False,
                    "is_warning": True,
                    "error": "元素未找到: 提交",
                    "step": {"error_strategy": "IGNORE"},
                },
            ],
        }
        summary = _summarize_case_result(result)
        self.assertEqual(summary["status"], "PASS")
        self.assertTrue(is_tolerated_failure(result["steps"][1]))

    def test_all_ignored_case_is_warning(self):
        result = {
            "success": False,
            "steps": [
                _cross_step(
                    status="SKIP",
                    error="元素未找到: 提交",
                    error_strategy="IGNORE",
                    skip_reason=IGNORED,
                ),
                _cross_step(
                    status="SKIP",
                    action="input",
                    error="输入失败",
                    error_strategy="IGNORE",
                    skip_reason="失败已忽略：输入失败",
                ),
            ],
        }
        summary = _summarize_case_result(result)
        self.assertEqual(summary["status"], "WARNING")
        self.assertTrue(summary["all_skipped"])

    def test_abort_and_continue_failures_are_fail(self):
        for strategy in ("ABORT", "CONTINUE"):
            with self.subTest(strategy=strategy):
                result = {
                    "success": False,
                    "steps": [
                        {"status": "PASS", "step": {"error_strategy": "ABORT"}},
                        _cross_step(
                            status="FAIL",
                            error="元素未找到: 提交",
                            error_strategy=strategy,
                        ),
                    ],
                }
                summary = _summarize_case_result(result)
                self.assertEqual(summary["status"], "FAIL")
                self.assertFalse(summary["all_skipped"])

    def test_determine_case_status_pass_with_ignored_failure(self):
        steps = [
            {"status": "success"},
            {"status": "skipped", "skip_reason": IGNORED, "error": "元素未找到: 提交"},
        ]
        self.assertEqual(
            _determine_case_status(steps, case_success=False, case_is_warning=False),
            "success",
        )
        self.assertEqual(
            _determine_case_status(steps, case_success=True, case_is_warning=True),
            "success",
        )

    def test_determine_case_status_all_ignored_is_skipped(self):
        steps = [
            {"status": "skipped", "skip_reason": IGNORED, "error": "boom"},
            {"status": "skipped", "skip_reason": "失败已忽略：输入失败", "error": "输入失败"},
        ]
        self.assertEqual(
            _determine_case_status(steps, case_success=False, case_is_warning=False),
            "skipped",
        )

    def test_determine_case_status_failures_still_fail(self):
        for strategy in ("ABORT", "CONTINUE"):
            with self.subTest(strategy=strategy):
                steps = [
                    {"status": "success"},
                    {
                        "status": "failed",
                        "error": "boom",
                        "error_strategy": strategy,
                    },
                ]
                self.assertEqual(
                    _determine_case_status(steps, case_success=False),
                    "failed",
                )

    def test_summarize_scenario_raw_results_ignores_tolerated_failures(self):
        raw_results = [
            {
                "result": {
                    "success": False,
                    "is_warning": True,
                    "steps": [
                        {"success": True},
                        {
                            "success": False,
                            "is_warning": True,
                            "error": "元素未找到: 提交",
                            "skip_reason": IGNORED,
                            "step": {"error_strategy": "IGNORE"},
                        },
                    ],
                }
            }
        ]
        summary = _summarize_scenario_raw_results(raw_results)
        self.assertEqual(summary["status"], "PASS")
        self.assertFalse(summary["has_warning"])
        self.assertFalse(summary["has_fail"])
        self.assertFalse(summary["all_skipped"])


class ReportDisplaySkipReasonTests(unittest.TestCase):
    def test_skip_reason_merged_into_report_display(self):
        display = build_report_display(
            {
                "action": "click",
                "description": "点提交",
                "error_strategy": "IGNORE",
                "skip_reason": IGNORED,
            }
        )
        self.assertEqual(display["skip_reason"], IGNORED)

    def test_skip_reason_read_from_output_passthrough(self):
        display = build_report_display(
            {
                "action": "click",
                "description": "点提交",
                "output": {"skip_reason": IGNORED},
            }
        )
        self.assertEqual(display["skip_reason"], IGNORED)

    def test_no_skip_reason_when_absent(self):
        display = build_report_display({"action": "click", "description": "点提交"})
        self.assertNotIn("skip_reason", display)


class ScenarioConclusionSemanticsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
        SQLModel.metadata.create_all(self.engine)
        self.session = Session(self.engine)

        self.case_ids = []
        for name in ("case-1", "case-2"):
            case = TestCase(name=name, steps=[], variables=[])
            self.session.add(case)
            self.session.commit()
            self.session.refresh(case)
            self.case_ids.append(case.id)

        scenario = TestScenario(name="scenario-1")
        self.session.add(scenario)
        self.session.commit()
        self.session.refresh(scenario)
        self.scenario_id = scenario.id

        for order, case_id in enumerate(self.case_ids, start=1):
            self.session.add(
                ScenarioStep(
                    scenario_id=self.scenario_id,
                    case_id=case_id,
                    order=order,
                    alias=f"case-{order}",
                )
            )

        self.execution = TestExecution(
            scenario_id=self.scenario_id,
            scenario_name="scenario-1",
            status="RUNNING",
            device_serial="ios-1",
            platform="ios",
            device_info="iPhone 15",
            executor_name="tester",
        )
        self.session.add(self.execution)
        self.session.commit()
        self.session.refresh(self.execution)

    def tearDown(self) -> None:
        self.session.close()

    def _case_result(self, *, case_id, steps, success):
        return {
            "case_id": case_id,
            "success": success,
            "steps": steps,
            "exported_variables": {},
        }

    def _run_with_results(self, case_results):
        calls = []

        def fake_run(*, session, case, device_serial, env_id, variables_map, abort_event):
            calls.append(case.id)
            return case_results[len(calls) - 1]

        with patch(
            "backend.scenario_execution.run_case_with_standard_runner",
            side_effect=fake_run,
        ):
            outcome = _run_scenario_cross_platform(
                self.scenario_id,
                self.session,
                "ios-1",
                None,
                threading.Event(),
            )
        return outcome, calls

    def _legacy_case_result(self, *, case_id, steps, success):
        """跨端步骤结果 -> legacy 用例结果（与执行链路同一转换）。"""
        return _convert_cross_result_to_legacy_case_result(
            case=SimpleNamespace(id=case_id),
            cross_result={"success": success, "steps": steps, "exported_variables": {}},
            variables_map={},
        )

    def _test_result_rows(self):
        from sqlmodel import select

        from backend.models import TestResult

        return self.session.exec(select(TestResult).order_by(TestResult.step_order)).all()

    def _summarize(self, raw_results):
        cases_results = _build_cases_results_from_raw_results(
            session=self.session,
            execution_id=self.execution.id,
            raw_results=raw_results,
            include_case_duration=True,
        )
        self.session.commit()
        return cases_results, _summarize_cases_results(cases_results)

    def test_ignored_failure_does_not_break_and_scenario_passes(self):
        outcome, calls = self._run_with_results(
            [
                self._case_result(
                    case_id=self.case_ids[0],
                    # 用例侧即使上报 success=False，IGNORE 容忍的失败也不判失败
                    success=False,
                    steps=[
                        _cross_step(status="PASS", description="打开首页"),
                        _cross_step(
                            status="SKIP",
                            error="元素未找到: 提交",
                            error_code="E2001_ELEMENT_NOT_FOUND",
                            error_strategy="IGNORE",
                            skip_reason=IGNORED,
                        ),
                    ],
                ),
                self._case_result(
                    case_id=self.case_ids[1],
                    success=True,
                    steps=[_cross_step(status="PASS", description="下单成功")],
                ),
            ]
        )

        self.assertEqual(len(calls), 2, "IGNORE 失败不应中断后续用例")
        self.assertTrue(outcome["success"])
        self.assertEqual(len(outcome["results"]), 2)

        cases_results, summary = self._summarize(outcome["results"])
        self.assertEqual(cases_results[0]["status"], "success")
        self.assertEqual(summary["scenario_status"], "PASS")
        self.assertEqual(summary["fail_count"], 0)
        self.assertEqual(summary["warning_count"], 0)
        self.assertEqual(summary["success_count"], 2)

    def test_abort_failure_still_breaks_and_continue_failure_keeps_running(self):
        outcome, calls = self._run_with_results(
            [
                self._case_result(
                    case_id=self.case_ids[0],
                    success=False,
                    steps=[_cross_step(status="FAIL", error="boom", error_strategy="ABORT")],
                ),
                self._case_result(
                    case_id=self.case_ids[1],
                    success=True,
                    steps=[_cross_step(status="PASS")],
                ),
            ]
        )
        self.assertEqual(len(calls), 1, "ABORT 失败应中断后续用例")
        self.assertEqual(len(outcome["results"]), 1)

        _, summary = self._summarize(outcome["results"])
        self.assertEqual(summary["scenario_status"], "FAIL")

        outcome, calls = self._run_with_results(
            [
                self._case_result(
                    case_id=self.case_ids[0],
                    success=False,
                    steps=[_cross_step(status="FAIL", error="boom", error_strategy="CONTINUE")],
                ),
                self._case_result(
                    case_id=self.case_ids[1],
                    success=True,
                    steps=[_cross_step(status="PASS")],
                ),
            ]
        )
        self.assertEqual(len(calls), 2, "CONTINUE 失败应跑完剩余用例")
        self.assertEqual(len(outcome["results"]), 2)

        _, summary = self._summarize(outcome["results"])
        self.assertEqual(summary["scenario_status"], "FAIL")

    def test_report_keeps_step_order_and_skip_reason_details(self):
        outcome, _ = self._run_with_results(
            [
                self._case_result(
                    case_id=self.case_ids[0],
                    success=False,
                    steps=[
                        _cross_step(status="PASS", description="打开首页"),
                        _cross_step(
                            status="SKIP",
                            error="元素未找到: 提交",
                            error_code="E2001_ELEMENT_NOT_FOUND",
                            error_strategy="IGNORE",
                            skip_reason=IGNORED,
                        ),
                    ],
                ),
                self._case_result(
                    case_id=self.case_ids[1],
                    success=True,
                    steps=[_cross_step(status="PASS", description="下单成功")],
                ),
            ]
        )

        cases_results, _ = self._summarize(outcome["results"])
        flat = [step for case in cases_results for step in case["steps"]]
        self.assertEqual(len(flat), 3)

        ignored = flat[1]
        self.assertEqual(ignored["status"], "skipped")
        self.assertEqual(ignored["skip_reason"], IGNORED)
        # 详情留痕：原始 error / error_code / suggestion 不因状态变为 SKIP 而丢失
        self.assertEqual(ignored["error"], "元素未找到: 提交")
        self.assertEqual(ignored["report_display"]["error_code"], "E2001_ELEMENT_NOT_FOUND")
        self.assertEqual(ignored["report_display"]["suggestion"], "请检查定位器")
        self.assertEqual(ignored["report_display"]["skip_reason"], IGNORED)

    def test_global_step_order_matches_step_order(self):
        raw_results = [
            {
                "alias": "case-a",
                "case_name": "Case A",
                "result": self._legacy_case_result(
                    case_id=self.case_ids[0],
                    success=True,
                    steps=[
                        _cross_step(status="PASS", description="步骤一"),
                        _cross_step(
                            status="SKIP",
                            error="boom",
                            error_strategy="IGNORE",
                            skip_reason="失败已忽略：boom",
                        ),
                    ],
                ),
            },
            {
                "alias": "case-b",
                "case_name": "Case B",
                "result": self._legacy_case_result(
                    case_id=self.case_ids[1],
                    success=True,
                    steps=[
                        _cross_step(status="PASS", description="步骤三"),
                        _cross_step(status="PASS", description="步骤四", duration=0),
                    ],
                ),
            },
        ]

        cases_results = _build_cases_results_from_raw_results(
            session=self.session,
            execution_id=self.execution.id,
            raw_results=raw_results,
            include_case_duration=True,
        )
        self.session.commit()

        rows = self._test_result_rows()
        self.assertEqual([row.step_order for row in rows], [1, 2, 3, 4])
        self.assertEqual(len(rows), 4, "结果条数恒等于步骤条数")
        self.assertEqual(rows[1].status, "SKIP")
        self.assertEqual(rows[1].report_display["skip_reason"], "失败已忽略：boom")

        flat = [step for case in cases_results for step in case["steps"]]
        self.assertEqual(len(flat), 4)
        self.assertEqual(
            [step["status"] for step in flat],
            ["success", "skipped", "success", "success"],
        )

    def test_summary_counts_after_supplemented_skip_records(self):
        # 上游步骤失败/用户中止后补记的 SKIP 记录：条数补齐、顺序不变、不额外判失败
        raw_results = [
            {
                "alias": "case-a",
                "case_name": "Case A",
                "result": self._case_result(
                    case_id=self.case_ids[0],
                    success=False,
                    steps=[
                        {
                            "step": {
                                "action": "click",
                                "description": "步骤一",
                                "error_strategy": "ABORT",
                            },
                            "success": False,
                            "error": "boom",
                            "duration": 0.2,
                        },
                        {
                            "step": {
                                "action": "click",
                                "description": "步骤二",
                                "error_strategy": "ABORT",
                            },
                            "success": True,
                            "is_skipped": True,
                            "skip_reason": "上游步骤失败，中止执行",
                            "duration": 0,
                            "error": None,
                        },
                        {
                            "step": {
                                "action": "click",
                                "description": "步骤三",
                                "error_strategy": "ABORT",
                            },
                            "success": True,
                            "is_skipped": True,
                            "skip_reason": "用户中止执行",
                            "duration": 0,
                            "error": None,
                        },
                    ],
                ),
            },
            {
                "alias": "case-b",
                "case_name": "Case B",
                "result": self._legacy_case_result(
                    case_id=self.case_ids[1],
                    success=True,
                    steps=[_cross_step(status="PASS", description="步骤四")],
                ),
            },
        ]

        cases_results, summary = self._summarize(raw_results)
        self.assertEqual(len(cases_results[0]["steps"]), 3)
        self.assertEqual(
            [step["status"] for step in cases_results[0]["steps"]],
            ["failed", "skipped", "skipped"],
        )
        self.assertEqual(
            cases_results[0]["steps"][1]["skip_reason"],
            "上游步骤失败，中止执行",
        )
        self.assertEqual(cases_results[0]["steps"][2]["skip_reason"], "用户中止执行")
        # 补记的 SKIP 不改变结论：失败仍由未被容忍的失败决定
        self.assertEqual(cases_results[0]["status"], "failed")
        self.assertEqual(summary["scenario_status"], "FAIL")
        self.assertEqual(summary["fail_count"], 1)
        self.assertEqual(summary["warning_count"], 0)

        rows = self._test_result_rows()
        self.assertEqual([row.step_order for row in rows], [1, 2, 3, 4])
        self.assertEqual([row.status for row in rows], ["FAIL", "SKIP", "SKIP", "PASS"])
        self.assertEqual(rows[1].duration, 0)

    def test_report_generator_counts_close_after_supplemented_skips(self):
        from backend.report_generator import ReportGenerator

        generator = ReportGenerator()
        steps = [
            {"status": "success", "action": "click"},
            {"status": "skipped", "action": "click", "skip_reason": IGNORED},
            {"status": "skipped", "action": "click", "skip_reason": "上游步骤失败，中止执行"},
            {"status": "failed", "action": "click", "error": "boom"},
        ]

        rendered = {}

        class _Template:
            def render(self, **kwargs):
                rendered.update(kwargs)
                return "html"

        generator.env.get_template = lambda name: _Template()
        generator.reports_dir = self._tmp_reports_dir()

        generator.generate_report(
            case_id=1,
            case_name="case",
            steps_results=steps,
            start_time=datetime(2026, 1, 1, 10, 0, 0),
            end_time=datetime(2026, 1, 1, 10, 0, 1),
        )

        self.assertEqual(rendered["total_steps"], 4)
        self.assertEqual(rendered["passed"], 1)
        self.assertEqual(rendered["failed"], 1)
        self.assertEqual(rendered["skipped"], 2)
        self.assertEqual(rendered["warning"], 0)

    def _tmp_reports_dir(self):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, True)
        return tmp


class ToleratedFailureDetectionTests(unittest.TestCase):
    """纯函数语义：不需要数据库。"""

    def test_tolerated_failure_detection(self):
        self.assertTrue(
            is_tolerated_failure(
                {
                    "success": False,
                    "error": "boom",
                    "step": {"error_strategy": "IGNORE"},
                }
            )
        )
        self.assertFalse(
            is_tolerated_failure(
                {
                    "success": False,
                    "error": "boom",
                    "step": {"error_strategy": "ABORT"},
                }
            )
        )
        # 平台跳过（P1001）沿用现状：不是被容忍的失败
        self.assertFalse(
            is_tolerated_failure(
                {
                    "success": True,
                    "is_skipped": True,
                    "error": "P1001_PLATFORM_NOT_ALLOWED",
                    "skip_reason": "平台不匹配",
                    "step": {"error_strategy": "ABORT"},
                }
            )
        )


if __name__ == "__main__":
    unittest.main()
