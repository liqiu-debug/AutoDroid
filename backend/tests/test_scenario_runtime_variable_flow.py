import unittest
from unittest.mock import patch

from sqlmodel import SQLModel, Session, create_engine

from backend.api.scenarios import _run_scenario_cross_platform
from backend.models import ScenarioStep, TestCase, TestScenario


class ScenarioRuntimeVariableFlowTests(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
        SQLModel.metadata.create_all(self.engine)
        self.session = Session(self.engine)

    def tearDown(self) -> None:
        self.session.close()

    def _create_case(self, name: str) -> TestCase:
        case = TestCase(name=name, steps=[], variables=[])
        self.session.add(case)
        self.session.commit()
        self.session.refresh(case)
        return case

    def _create_scenario(self, name: str = "scenario-1") -> TestScenario:
        scenario = TestScenario(name=name)
        self.session.add(scenario)
        self.session.commit()
        self.session.refresh(scenario)
        return scenario

    def _add_scenario_step(self, scenario_id: int, case_id: int, order: int, alias: str) -> None:
        self.session.add(
            ScenarioStep(
                scenario_id=scenario_id,
                case_id=case_id,
                order=order,
                alias=alias,
            )
        )
        self.session.commit()

    def test_runtime_variables_flow_between_cases(self):
        scenario = self._create_scenario()
        producer = self._create_case("producer")
        consumer = self._create_case("consumer")
        self._add_scenario_step(scenario.id, producer.id, order=1, alias="producer")
        self._add_scenario_step(scenario.id, consumer.id, order=2, alias="consumer")

        variables_seen = []

        def _fake_run_case(*, case, variables_map, **kwargs):
            variables_seen.append((case.name, dict(variables_map or {})))
            if case.id == producer.id:
                return {
                    "success": True,
                    "steps": [],
                    "exported_variables": {"PRICE": "99.00"},
                }
            return {
                "success": True,
                "steps": [],
                "exported_variables": dict(variables_map or {}),
            }

        with patch("backend.api.scenarios.run_case_with_standard_runner", side_effect=_fake_run_case):
            result = _run_scenario_cross_platform(
                scenario_id=scenario.id,
                session=self.session,
                device_serial="android-1",
                env_id=None,
            )

        self.assertTrue(result["success"])
        self.assertEqual(len(variables_seen), 2)
        self.assertNotIn("PRICE", variables_seen[0][1])
        self.assertEqual(variables_seen[1][1].get("PRICE"), "99.00")


if __name__ == "__main__":
    unittest.main()
