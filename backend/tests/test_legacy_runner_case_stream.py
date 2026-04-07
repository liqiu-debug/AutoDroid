import unittest
from types import SimpleNamespace

from sqlmodel import Session, SQLModel, create_engine

from backend.models import Environment, GlobalVariable, ScenarioStep, TestCase, TestScenario
from backend.runner import ScenarioRunner, TestRunner
from backend.schemas import ActionType, ErrorStrategy, SelectorType, Step, Variable


class _FakeStreamRunner(TestRunner):
    def __init__(self, step_results):
        super().__init__(device_serial="android-1")
        self.d = object()
        self._step_results = list(step_results)

    def connect(self):
        self.d = object()

    def execute_step(self, step, variables):
        result = dict(self._step_results.pop(0))
        if step.action == ActionType.INPUT:
            variables["TYPED"] = step.value or ""
        return result


class LegacyRunnerCaseStreamTests(unittest.TestCase):
    def test_iter_case_execution_marks_ignore_as_warning_and_preserves_case_variables(self):
        case = SimpleNamespace(
            id=11,
            name="case-1",
            variables=[Variable(key="TOKEN", value="case-token")],
            steps=[
                Step(
                    action=ActionType.CLICK,
                    selector="登录",
                    selector_type=SelectorType.TEXT,
                    description="点击登录",
                    error_strategy=ErrorStrategy.IGNORE,
                ),
                Step(
                    action=ActionType.INPUT,
                    selector="手机号",
                    selector_type=SelectorType.TEXT,
                    value="18800001111",
                    description="输入手机号",
                ),
            ],
        )
        runner = _FakeStreamRunner([
            {"step": case.steps[0].model_dump(), "success": False, "error": "boom", "duration": 0.1},
            {"step": case.steps[1].model_dump(), "success": True, "duration": 0.2},
        ])

        events = []
        case_iter = runner.iter_case_execution(case, extra_variables={"TOKEN": "env-token", "ENV_ONLY": "1"})
        while True:
            try:
                events.append(next(case_iter))
            except StopIteration as stop:
                case_result = stop.value
                break

        self.assertEqual(len(events), 2)
        self.assertTrue(events[0]["step_result"]["is_warning"])
        self.assertEqual(case_result["case_id"], 11)
        self.assertTrue(case_result["success"])
        self.assertTrue(case_result["is_warning"])
        self.assertEqual(case_result["exported_variables"]["TOKEN"], "case-token")
        self.assertEqual(case_result["exported_variables"]["ENV_ONLY"], "1")
        self.assertEqual(case_result["exported_variables"]["TYPED"], "18800001111")

    def test_run_case_continues_after_continue_failure_and_returns_failed_case(self):
        case = SimpleNamespace(
            id=12,
            name="case-2",
            variables=[],
            steps=[
                Step(
                    action=ActionType.CLICK,
                    selector="下一步",
                    selector_type=SelectorType.TEXT,
                    description="点下一步",
                    error_strategy=ErrorStrategy.CONTINUE,
                ),
                Step(
                    action=ActionType.CLICK,
                    selector="完成",
                    selector_type=SelectorType.TEXT,
                    description="点完成",
                ),
            ],
        )
        runner = _FakeStreamRunner([
            {"step": case.steps[0].model_dump(), "success": False, "error": "fail-1", "duration": 0.1},
            {"step": case.steps[1].model_dump(), "success": True, "duration": 0.2},
        ])

        result = runner.run_case(case, extra_variables={"FLOW": "A"})

        self.assertFalse(result["success"])
        self.assertEqual(len(result["steps"]), 2)
        self.assertEqual(result["steps"][0]["error"], "fail-1")
        self.assertEqual(result["exported_variables"]["FLOW"], "A")


class LegacyScenarioRunnerStreamTests(unittest.TestCase):
    def test_iter_scenario_execution_streams_case_events_and_bridges_variables(self):
        engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
        SQLModel.metadata.create_all(engine)

        with Session(engine) as session:
            env = Environment(name="env-1")
            session.add(env)
            session.commit()
            session.refresh(env)
            session.add(GlobalVariable(env_id=env.id, key="ENV_ONLY", value="env-value"))

            case_one = TestCase(
                name="case-1",
                variables=[Variable(key="TOKEN", value="case-token")],
                steps=[
                    Step(
                        action=ActionType.INPUT,
                        selector="手机号",
                        selector_type=SelectorType.TEXT,
                        value="18800001111",
                        description="输入手机号",
                    )
                ],
            )
            case_two = TestCase(
                name="case-2",
                variables=[],
                steps=[
                    Step(
                        action=ActionType.CLICK,
                        selector="完成",
                        selector_type=SelectorType.TEXT,
                        description="点击完成",
                    )
                ],
            )
            scenario = TestScenario(name="scenario-1")
            session.add(case_one)
            session.add(case_two)
            session.add(scenario)
            session.commit()
            session.refresh(case_one)
            session.refresh(case_two)
            session.refresh(scenario)

            session.add(ScenarioStep(scenario_id=scenario.id, case_id=case_one.id, order=1, alias="first"))
            session.add(ScenarioStep(scenario_id=scenario.id, case_id=case_two.id, order=2, alias="second"))
            session.commit()

            runner = ScenarioRunner(device_serial="android-1")
            runner.runner = _FakeStreamRunner([
                {"step": case_one.steps[0].model_dump(), "success": True, "duration": 0.1},
                {"step": case_two.steps[0].model_dump(), "success": True, "duration": 0.2},
            ])

            events = []
            scenario_iter = runner.iter_scenario_execution(scenario.id, session, env_id=env.id)
            while True:
                try:
                    events.append(next(scenario_iter))
                except StopIteration as stop:
                    result = stop.value
                    break

        self.assertEqual(
            [event["type"] for event in events],
            [
                "case_start",
                "step_result",
                "case_complete",
                "case_start",
                "step_result",
                "case_complete",
            ],
        )
        self.assertEqual(events[2]["scenario_context"]["ENV_ONLY"], "env-value")
        self.assertEqual(events[2]["scenario_context"]["TOKEN"], "case-token")
        self.assertEqual(events[2]["scenario_context"]["TYPED"], "18800001111")
        self.assertEqual(events[4]["variables_map"]["ENV_ONLY"], "env-value")
        self.assertEqual(events[4]["variables_map"]["TOKEN"], "case-token")
        self.assertEqual(events[4]["variables_map"]["TYPED"], "18800001111")
        self.assertTrue(result["success"])
        self.assertEqual(len(result["results"]), 2)
        self.assertEqual(result["results"][0]["result"]["exported_variables"]["TYPED"], "18800001111")


if __name__ == "__main__":
    unittest.main()
