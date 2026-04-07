import unittest
from unittest.mock import patch

from sqlmodel import SQLModel, Session, create_engine

from backend.api.scenarios import precheck_scenario_execution
from backend.models import Device, ScenarioStep, SystemSetting, TestCase, TestCaseStep, TestScenario


class ScenarioPrecheckTests(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
        SQLModel.metadata.create_all(self.engine)
        self.session = Session(self.engine)

    def tearDown(self) -> None:
        self.session.close()

    def _create_case(self, name: str = "case-1") -> TestCase:
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

    def _add_scenario_step(
        self,
        scenario_id: int,
        case_id: int,
        order: int = 1,
        alias: str = "step-1",
    ) -> None:
        self.session.add(
            ScenarioStep(
                scenario_id=scenario_id,
                case_id=case_id,
                order=order,
                alias=alias,
            )
        )
        self.session.commit()

    def _add_case_step(
        self,
        case_id: int,
        order: int,
        action: str,
        execute_on=None,
        platform_overrides=None,
        args=None,
        value=None,
    ) -> None:
        self.session.add(
            TestCaseStep(
                case_id=case_id,
                order=order,
                action=action,
                execute_on=execute_on or ["android", "ios"],
                platform_overrides=platform_overrides or {},
                args=args or {},
                value=value,
                timeout=10,
                error_strategy="ABORT",
                description=f"step-{order}",
            )
        )
        self.session.commit()

    def _add_device(self, serial: str, platform: str) -> None:
        self.session.add(Device(serial=serial, platform=platform, model="mock"))
        self.session.commit()

    def _set_flag(self, key: str, value: str) -> None:
        self.session.add(SystemSetting(key=key, value=value))
        self.session.commit()

    def test_precheck_pass_on_android(self):
        scenario = self._create_scenario()
        case = self._create_case()
        self._add_scenario_step(scenario.id, case.id)
        self._add_device("android-1", "android")
        self._add_case_step(
            case_id=case.id,
            order=1,
            action="click",
            execute_on=["android"],
            platform_overrides={"android": {"selector": "com.demo:id/login", "by": "id"}},
        )

        result = precheck_scenario_execution(
            session=self.session,
            scenario_id=scenario.id,
            device_serial="android-1",
        )

        self.assertTrue(result["ok"])
        self.assertTrue(result["has_runnable_cases"])
        self.assertEqual(result["summary"]["pass_cases"], 1)
        self.assertEqual(result["summary"]["fail_cases"], 0)
        self.assertEqual(result["cases"][0]["status"], "PASS")

    def test_precheck_pass_on_input_without_selector(self):
        scenario = self._create_scenario()
        case = self._create_case()
        self._add_scenario_step(scenario.id, case.id)
        self._add_device("android-1", "android")
        self._add_case_step(
            case_id=case.id,
            order=1,
            action="input",
            execute_on=["android"],
            platform_overrides={},
            args={"text": "hello"},
        )

        result = precheck_scenario_execution(
            session=self.session,
            scenario_id=scenario.id,
            device_serial="android-1",
        )

        self.assertTrue(result["ok"])
        self.assertEqual(result["summary"]["pass_cases"], 1)
        self.assertEqual(result["summary"]["fail_cases"], 0)
        self.assertEqual(result["cases"][0]["status"], "PASS")

    def test_precheck_fail_on_unresolved_variable_template(self):
        scenario = self._create_scenario()
        case = self._create_case()
        self._add_scenario_step(scenario.id, case.id)
        self._add_device("android-1", "android")
        self._add_case_step(
            case_id=case.id,
            order=1,
            action="input",
            execute_on=["android"],
            platform_overrides={"android": {"selector": "请输入手机号码", "by": "text"}},
            args={"text": "{{ NAME }}"},
        )

        result = precheck_scenario_execution(
            session=self.session,
            scenario_id=scenario.id,
            device_serial="android-1",
        )

        self.assertFalse(result["ok"])
        self.assertEqual(result["summary"]["fail_cases"], 1)
        self.assertEqual(result["cases"][0]["status"], "FAIL")
        self.assertIn("未解析变量", result["cases"][0]["reason"])

    def test_precheck_pass_when_variable_from_previous_extract_step(self):
        scenario = self._create_scenario()
        case = self._create_case()
        self._add_scenario_step(scenario.id, case.id)
        self._add_device("android-1", "android")
        self._add_case_step(
            case_id=case.id,
            order=1,
            action="extract_by_ocr",
            execute_on=["android"],
            args={"region": "[0.1,0.1,0.2,0.2]"},
            value="PRICE",
        )
        self._add_case_step(
            case_id=case.id,
            order=2,
            action="input",
            execute_on=["android"],
            platform_overrides={"android": {"selector": "com.demo:id/price", "by": "id"}},
            args={"text": "{{ PRICE }}"},
        )

        result = precheck_scenario_execution(
            session=self.session,
            scenario_id=scenario.id,
            device_serial="android-1",
        )

        self.assertTrue(result["ok"])
        self.assertEqual(result["summary"]["fail_cases"], 0)
        self.assertEqual(result["cases"][0]["status"], "PASS")

    def test_precheck_pass_when_variable_exported_from_previous_case(self):
        scenario = self._create_scenario()
        producer = self._create_case(name="producer")
        consumer = self._create_case(name="consumer")
        self._add_scenario_step(scenario.id, producer.id, order=1, alias="producer")
        self._add_scenario_step(scenario.id, consumer.id, order=2, alias="consumer")
        self._add_device("android-1", "android")

        self._add_case_step(
            case_id=producer.id,
            order=1,
            action="extract_by_ocr",
            execute_on=["android"],
            args={"region": "[0.1,0.1,0.2,0.2]"},
            value="PRICE",
        )
        self._add_case_step(
            case_id=consumer.id,
            order=1,
            action="assert_text",
            execute_on=["android"],
            platform_overrides={"android": {"selector": "com.demo:id/price", "by": "id"}},
            args={"expected_text": "{{ PRICE }}"},
        )

        result = precheck_scenario_execution(
            session=self.session,
            scenario_id=scenario.id,
            device_serial="android-1",
        )

        self.assertTrue(result["ok"])
        self.assertEqual(result["summary"]["fail_cases"], 0)
        self.assertEqual(result["summary"]["pass_cases"], 2)
        self.assertEqual(result["cases"][0]["status"], "PASS")
        self.assertEqual(result["cases"][1]["status"], "PASS")

    def test_precheck_pass_when_variable_exported_from_previous_case_without_selector(self):
        scenario = self._create_scenario()
        producer = self._create_case(name="producer")
        consumer = self._create_case(name="consumer")
        self._add_scenario_step(scenario.id, producer.id, order=1, alias="producer")
        self._add_scenario_step(scenario.id, consumer.id, order=2, alias="consumer")
        self._add_device("android-1", "android")

        self._add_case_step(
            case_id=producer.id,
            order=1,
            action="extract_by_ocr",
            execute_on=["android"],
            args={"region": "[0.1,0.1,0.2,0.2]"},
            value="PRICE",
        )
        self._add_case_step(
            case_id=consumer.id,
            order=1,
            action="assert_text",
            execute_on=["android"],
            platform_overrides={},
            args={"expected_text": "{{ PRICE }}"},
        )

        result = precheck_scenario_execution(
            session=self.session,
            scenario_id=scenario.id,
            device_serial="android-1",
        )

        self.assertTrue(result["ok"])
        self.assertEqual(result["summary"]["fail_cases"], 0)
        self.assertEqual(result["summary"]["pass_cases"], 2)
        self.assertEqual(result["cases"][0]["status"], "PASS")
        self.assertEqual(result["cases"][1]["status"], "PASS")

    def test_precheck_all_skipped_is_not_runnable(self):
        scenario = self._create_scenario()
        case = self._create_case()
        self._add_scenario_step(scenario.id, case.id)
        self._add_device("android-1", "android")
        self._add_case_step(
            case_id=case.id,
            order=1,
            action="click",
            execute_on=["ios"],
            platform_overrides={"ios": {"selector": "登录", "by": "label"}},
        )

        result = precheck_scenario_execution(
            session=self.session,
            scenario_id=scenario.id,
            device_serial="android-1",
        )

        self.assertFalse(result["ok"])
        self.assertFalse(result["has_runnable_cases"])
        self.assertTrue(result["summary"]["all_cases_skipped"])
        self.assertEqual(result["summary"]["skip_cases"], 1)
        self.assertEqual(result["summary"]["fail_cases"], 0)
        self.assertEqual(result["cases"][0]["status"], "SKIP")

    def test_precheck_fail_when_case_missing(self):
        scenario = self._create_scenario()
        self._add_scenario_step(scenario.id, 9999)
        self._add_device("android-1", "android")

        result = precheck_scenario_execution(
            session=self.session,
            scenario_id=scenario.id,
            device_serial="android-1",
        )

        self.assertFalse(result["ok"])
        self.assertEqual(result["summary"]["fail_cases"], 1)
        self.assertEqual(result["cases"][0]["status"], "FAIL")
        self.assertIn("Case not found", result["cases"][0]["reason"])

    @patch("backend.cross_platform_execution.check_wda_health")
    @patch("backend.cross_platform_execution.resolve_ios_wda_url", return_value="http://127.0.0.1:8200")
    def test_precheck_pass_on_ios_home(self, _, check_wda_mock):
        check_wda_mock.return_value = None

        scenario = self._create_scenario()
        case = self._create_case()
        self._add_scenario_step(scenario.id, case.id)
        self._add_device("ios-1", "ios")
        self._set_flag("ios_execution", "true")
        self._add_case_step(
            case_id=case.id,
            order=1,
            action="home",
            execute_on=["ios"],
        )

        result = precheck_scenario_execution(
            session=self.session,
            scenario_id=scenario.id,
            device_serial="ios-1",
        )

        self.assertTrue(result["ok"])
        self.assertEqual(result["summary"]["fail_cases"], 0)
        self.assertEqual(result["cases"][0]["status"], "PASS")

    @patch("backend.cross_platform_execution.check_wda_health")
    @patch("backend.cross_platform_execution.resolve_ios_wda_url", return_value="http://127.0.0.1:8200")
    def test_precheck_global_fail_when_wda_unavailable(self, _, check_wda_mock):
        check_wda_mock.side_effect = RuntimeError("P1005_WDA_UNAVAILABLE: health check failed")

        scenario = self._create_scenario()
        case = self._create_case()
        self._add_scenario_step(scenario.id, case.id)
        self._add_device("ios-1", "ios")
        self._set_flag("ios_execution", "true")
        self._add_case_step(
            case_id=case.id,
            order=1,
            action="sleep",
            execute_on=["ios"],
            args={"seconds": 1},
        )

        result = precheck_scenario_execution(
            session=self.session,
            scenario_id=scenario.id,
            device_serial="ios-1",
        )

        self.assertFalse(result["ok"])
        self.assertEqual(result["summary"]["fail_cases"], 1)
        self.assertEqual(result["cases"][0]["status"], "FAIL")
        self.assertEqual(result["cases"][0]["global_checks"][0]["code"], "P1005_WDA_UNAVAILABLE")
        self.assertIn("P1005_WDA_UNAVAILABLE", result["cases"][0]["reason"])


if __name__ == "__main__":
    unittest.main()
