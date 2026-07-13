import unittest
from datetime import datetime, timedelta

from fastapi import HTTPException
from sqlmodel import Session, SQLModel, create_engine

from backend.api.reports import compare_executions, get_flaky_report
from backend.flaky_analysis import (
    ExecutionCompareError,
    build_execution_compare,
    compute_flaky_report,
)
from backend.models import TestExecution, TestResult, TestScenario


def _make_execution(scenario, status, start_time, **kwargs):
    return TestExecution(
        scenario_id=scenario.id,
        scenario_name=scenario.name,
        status=status,
        start_time=start_time,
        end_time=kwargs.pop("end_time", start_time + timedelta(minutes=2)),
        **kwargs,
    )


class FlakyAnalysisTestBase(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
        SQLModel.metadata.create_all(self.engine)
        self.session = Session(self.engine)
        self.now = datetime(2026, 7, 10, 12, 0, 0)

    def tearDown(self) -> None:
        self.session.close()

    def _add_scenario(self, name):
        scenario = TestScenario(name=name)
        self.session.add(scenario)
        self.session.commit()
        self.session.refresh(scenario)
        return scenario

    def _add_status_sequence(self, scenario, statuses, *, start=None, gap_minutes=10):
        start = start or (self.now - timedelta(days=1))
        executions = []
        for index, status in enumerate(statuses):
            execution = _make_execution(scenario, status, start + timedelta(minutes=index * gap_minutes))
            self.session.add(execution)
            executions.append(execution)
        self.session.commit()
        for execution in executions:
            self.session.refresh(execution)
        return executions


class FlakyScoringTests(FlakyAnalysisTestBase):
    def test_flip_count_and_score(self):
        scenario = self._add_scenario("翻转场景")
        # PASS FAIL PASS FAIL PASS -> 4 flips, pass_rate 60%, fail_rate 0.4
        self._add_status_sequence(scenario, ["PASS", "FAIL", "PASS", "FAIL", "PASS"])

        report = compute_flaky_report(self.session, days=30, now=self.now)
        self.assertEqual(len(report["items"]), 1)
        item = report["items"][0]
        self.assertEqual(item["scenario_id"], scenario.id)
        self.assertEqual(item["total"], 5)
        self.assertEqual(item["pass_count"], 3)
        self.assertEqual(item["fail_count"], 2)
        self.assertEqual(item["flip_count"], 4)
        self.assertEqual(item["pass_rate"], 60.0)
        self.assertEqual(item["flip_rate"], 100.0)
        self.assertEqual(item["last_status"], "PASS")
        # score = 100 * (0.6 * 4/4 + 0.4 * (1 - |0.4 - 0.5| * 2)) = 60 + 0.4*0.8*100 = 92.0
        self.assertEqual(item["score"], 92.0)

    def test_warning_and_error_count_as_fail_like_flips(self):
        scenario = self._add_scenario("告警翻转")
        # PASS WARNING PASS ERROR PASS -> WARNING/ERROR are fail-like -> 4 flips
        self._add_status_sequence(scenario, ["PASS", "WARNING", "PASS", "ERROR", "PASS"])
        report = compute_flaky_report(self.session, days=30, now=self.now)
        self.assertEqual(report["items"][0]["flip_count"], 4)

    def test_min_samples_threshold_excludes_new_scenarios(self):
        scenario = self._add_scenario("新场景")
        self._add_status_sequence(scenario, ["PASS", "FAIL", "PASS", "FAIL"])  # 4 < 5

        report = compute_flaky_report(self.session, days=30, now=self.now)
        self.assertEqual(report["items"], [])

        # 降低阈值后应进入榜单
        report = compute_flaky_report(self.session, days=30, min_samples=4, now=self.now)
        self.assertEqual(len(report["items"]), 1)

    def test_stable_scenarios_are_excluded(self):
        all_pass = self._add_scenario("全过场景")
        self._add_status_sequence(all_pass, ["PASS"] * 6)
        all_fail = self._add_scenario("持续失败场景")
        self._add_status_sequence(all_fail, ["FAIL"] * 6)

        report = compute_flaky_report(self.session, days=30, now=self.now)
        self.assertEqual(report["items"], [])
        self.assertEqual(report["total_scenarios"], 2)

    def test_aborted_and_running_do_not_participate(self):
        scenario = self._add_scenario("含终止场景")
        self._add_status_sequence(
            scenario,
            ["PASS", "ABORTED", "FAIL", "RUNNING", "PASS", "FAIL", "PASS", "ABORTED"],
        )
        report = compute_flaky_report(self.session, days=30, now=self.now)
        item = report["items"][0]
        # 仅 PASS/FAIL 参与：PASS FAIL PASS FAIL PASS -> total 5, 4 flips
        self.assertEqual(item["total"], 5)
        self.assertEqual(item["flip_count"], 4)

    def test_window_days_excludes_old_executions(self):
        scenario = self._add_scenario("窗口过滤")
        # 窗口外的翻转执行
        self._add_status_sequence(
            scenario,
            ["PASS", "FAIL", "PASS", "FAIL", "PASS", "FAIL"],
            start=self.now - timedelta(days=40),
        )
        report = compute_flaky_report(self.session, days=30, now=self.now)
        self.assertEqual(report["items"], [])

    def test_ranking_and_limit(self):
        low = self._add_scenario("低分场景")
        # 1 flip / 5 -> 分数较低
        self._add_status_sequence(low, ["PASS", "PASS", "PASS", "PASS", "FAIL"])
        high = self._add_scenario("高分场景")
        self._add_status_sequence(high, ["PASS", "FAIL", "PASS", "FAIL", "PASS"])

        report = compute_flaky_report(self.session, days=30, now=self.now)
        self.assertEqual([item["scenario_id"] for item in report["items"]], [high.id, low.id])

        limited = compute_flaky_report(self.session, days=30, limit=1, now=self.now)
        self.assertEqual(len(limited["items"]), 1)
        self.assertEqual(limited["items"][0]["scenario_id"], high.id)

    def test_endpoint_returns_pydantic_model(self):
        scenario = self._add_scenario("端点场景")
        self._add_status_sequence(scenario, ["PASS", "FAIL", "PASS", "FAIL", "PASS"])
        result = get_flaky_report(days=30, limit=20, min_samples=5, include_steps=True, session=self.session)
        self.assertEqual(result.days, 30)
        self.assertEqual(len(result.items), 1)
        self.assertEqual(result.items[0].scenario_name, "端点场景")


class StepFlakyTests(FlakyAnalysisTestBase):
    def _add_step(self, execution, order, name, status, duration=100.0, **kwargs):
        result = TestResult(
            execution_id=execution.id,
            step_name=name,
            step_order=order,
            status=status,
            duration=duration,
            **kwargs,
        )
        self.session.add(result)
        return result

    def test_step_level_flaky_top(self):
        scenario = self._add_scenario("步骤级场景")
        statuses = ["PASS", "FAIL", "PASS", "FAIL", "PASS"]
        executions = self._add_status_sequence(scenario, statuses)
        for index, execution in enumerate(executions):
            # 步骤1 一直过：不 flaky
            self._add_step(execution, 1, "[登录] 打开首页", "PASS")
            # 步骤2 时好时坏：flaky
            self._add_step(execution, 2, "[登录] 点击登录", "PASS" if statuses[index] == "PASS" else "FAIL")
        self.session.commit()

        report = compute_flaky_report(self.session, days=30, now=self.now)
        self.assertEqual(len(report["step_items"]), 1)
        step = report["step_items"][0]
        self.assertEqual(step["step_order"], 2)
        self.assertEqual(step["step_name"], "[登录] 点击登录")
        self.assertEqual(step["total"], 5)
        self.assertEqual(step["flip_count"], 4)
        self.assertEqual(step["last_status"], "PASS")

    def test_step_skip_and_renamed_steps_do_not_pollute(self):
        scenario = self._add_scenario("步骤改名场景")
        statuses = ["PASS", "FAIL", "PASS", "FAIL", "PASS"]
        executions = self._add_status_sequence(scenario, statuses)
        for index, execution in enumerate(executions):
            # SKIP 出现不计入样本
            self._add_step(execution, 1, "[登录] 打开首页", "SKIP")
            # 改名步骤：同 order 不同名 -> 各自样本数不足，均不入榜
            name = "[登录] 点击登录" if index < 3 else "[登录] 点击提交"
            self._add_step(execution, 2, name, "PASS" if statuses[index] == "PASS" else "FAIL")
        self.session.commit()

        report = compute_flaky_report(self.session, days=30, now=self.now)
        self.assertEqual(report["step_items"], [])

    def test_include_steps_false_skips_step_analysis(self):
        scenario = self._add_scenario("跳过步骤分析")
        executions = self._add_status_sequence(scenario, ["PASS", "FAIL", "PASS", "FAIL", "PASS"])
        for execution in executions:
            self._add_step(execution, 1, "[登录] 点击登录", "FAIL")
        self.session.commit()

        report = compute_flaky_report(self.session, days=30, include_steps=False, now=self.now)
        self.assertEqual(report["step_items"], [])


class ExecutionCompareTests(FlakyAnalysisTestBase):
    def _add_step(self, execution, order, name, status, duration=100.0, **kwargs):
        result = TestResult(
            execution_id=execution.id,
            step_name=name,
            step_order=order,
            status=status,
            duration=duration,
            **kwargs,
        )
        self.session.add(result)
        return result

    def _seed_pair(self):
        scenario = self._add_scenario("对比场景")
        base = _make_execution(
            scenario,
            "FAIL",
            self.now - timedelta(hours=2),
            device_serial="android-001",
            end_time=self.now - timedelta(hours=2) + timedelta(seconds=60),
        )
        target = _make_execution(
            scenario,
            "FAIL",
            self.now - timedelta(hours=1),
            device_serial="android-001",
            end_time=self.now - timedelta(hours=1) + timedelta(seconds=90),
        )
        self.session.add_all([base, target])
        self.session.commit()
        self.session.refresh(base)
        self.session.refresh(target)

        # order 1: PASS -> PASS (unchanged)
        self._add_step(base, 1, "[登录] 打开首页", "PASS", duration=100)
        self._add_step(target, 1, "[登录] 打开首页", "PASS", duration=150)
        # order 2: PASS -> FAIL (regressed)
        self._add_step(base, 2, "[登录] 点击登录", "PASS", duration=200)
        self._add_step(
            target, 2, "[登录] 点击登录", "FAIL", duration=300,
            error_message="element not found",
            report_display={"error_code": "E_LOCATOR_NOT_FOUND", "suggestion": "检查控件定位"},
        )
        # order 3: FAIL -> PASS (fixed)
        self._add_step(base, 3, "[登录] 输入密码", "FAIL", duration=120, error_message="timeout")
        self._add_step(target, 3, "[登录] 输入密码", "PASS", duration=110)
        # order 4: WARNING -> FAIL (still-failing, 均为失败样)
        self._add_step(base, 4, "[登录] 断言文案", "WARNING", duration=80)
        self._add_step(target, 4, "[登录] 断言文案", "FAIL", duration=90)
        # order 5: 仅基准存在 (removed)
        self._add_step(base, 5, "[登录] 旧步骤", "PASS", duration=50)
        # order 6: 仅目标存在 (added)
        self._add_step(target, 6, "[登录] 新步骤", "PASS", duration=60)
        self.session.commit()
        return scenario, base, target

    def test_compare_classification_and_alignment(self):
        scenario, base, target = self._seed_pair()
        result = build_execution_compare(self.session, base.id, target.id)

        self.assertEqual(result["scenario_id"], scenario.id)
        self.assertEqual(result["base"]["id"], base.id)
        self.assertEqual(result["target"]["id"], target.id)
        self.assertEqual(result["base"]["duration"], 60.0)
        self.assertEqual(result["target"]["duration"], 90.0)
        self.assertEqual(result["duration_delta"], 30.0)

        changes = {row["step_order"]: row["change"] for row in result["steps"]}
        self.assertEqual(
            changes,
            {
                1: "unchanged",
                2: "regressed",
                3: "fixed",
                4: "still-failing",
                5: "removed",
                6: "added",
            },
        )
        self.assertEqual(
            result["summary"],
            {
                "regressed": 1,
                "fixed": 1,
                "still_failing": 1,
                "unchanged": 1,
                "added": 1,
                "removed": 1,
            },
        )

        rows = {row["step_order"]: row for row in result["steps"]}
        # 耗时差（毫秒）
        self.assertEqual(rows[1]["duration_delta"], 50.0)
        self.assertEqual(rows[2]["duration_delta"], 100.0)
        # removed/added 行只有单侧数据，无耗时差
        self.assertIsNone(rows[5]["duration_delta"])
        self.assertIsNone(rows[5]["target"])
        self.assertIsNone(rows[6]["base"])
        # 错误信息与 error_code 对照
        self.assertEqual(rows[2]["target"]["error_code"], "E_LOCATOR_NOT_FOUND")
        self.assertEqual(rows[2]["target"]["suggestion"], "检查控件定位")
        self.assertEqual(rows[2]["target"]["error_message"], "element not found")
        self.assertIsNone(rows[2]["base"]["error_code"])
        self.assertEqual(rows[3]["base"]["error_message"], "timeout")

    def test_compare_cross_scenario_rejected_with_400(self):
        scenario_a = self._add_scenario("场景A")
        scenario_b = self._add_scenario("场景B")
        exec_a = _make_execution(scenario_a, "PASS", self.now - timedelta(hours=2))
        exec_b = _make_execution(scenario_b, "PASS", self.now - timedelta(hours=1))
        self.session.add_all([exec_a, exec_b])
        self.session.commit()
        self.session.refresh(exec_a)
        self.session.refresh(exec_b)

        with self.assertRaises(ExecutionCompareError) as ctx:
            build_execution_compare(self.session, exec_a.id, exec_b.id)
        self.assertEqual(ctx.exception.status_code, 400)

        with self.assertRaises(HTTPException) as http_ctx:
            compare_executions(base_id=exec_a.id, target_id=exec_b.id, session=self.session)
        self.assertEqual(http_ctx.exception.status_code, 400)

    def test_compare_missing_execution_returns_404(self):
        scenario = self._add_scenario("孤儿场景")
        execution = _make_execution(scenario, "PASS", self.now - timedelta(hours=1))
        self.session.add(execution)
        self.session.commit()
        self.session.refresh(execution)

        with self.assertRaises(HTTPException) as ctx:
            compare_executions(base_id=99999, target_id=execution.id, session=self.session)
        self.assertEqual(ctx.exception.status_code, 404)

        with self.assertRaises(HTTPException) as ctx2:
            compare_executions(base_id=execution.id, target_id=99999, session=self.session)
        self.assertEqual(ctx2.exception.status_code, 404)

    def test_compare_endpoint_returns_model(self):
        _, base, target = self._seed_pair()
        result = compare_executions(base_id=base.id, target_id=target.id, session=self.session)
        self.assertEqual(result.summary.regressed, 1)
        self.assertEqual(result.summary.still_failing, 1)
        self.assertEqual(len(result.steps), 6)
        self.assertEqual(result.steps[1].change, "regressed")
        self.assertEqual(result.steps[1].target.error_code, "E_LOCATOR_NOT_FOUND")


if __name__ == "__main__":
    unittest.main()
