import unittest
from unittest.mock import patch

from sqlmodel import Session, SQLModel, create_engine

from backend.cross_platform_execution import precheck_case_execution
from backend.models import Device, SystemSetting, TestCase, TestCaseStep


class CasePrecheckTests(unittest.TestCase):
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

    def _add_device(self, serial: str, platform: str) -> None:
        self.session.add(Device(serial=serial, platform=platform, model="mock"))
        self.session.commit()

    def _add_step(
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

    def _set_flag(self, key: str, value: str) -> None:
        self.session.add(SystemSetting(key=key, value=value))
        self.session.commit()

    def test_precheck_pass_on_android_click(self):
        case = self._create_case()
        self._add_device("android-1", "android")
        self._add_step(
            case_id=case.id,
            order=1,
            action="click",
            execute_on=["android"],
            platform_overrides={"android": {"selector": "com.demo:id/login", "by": "id"}},
        )

        result = precheck_case_execution(
            session=self.session,
            case=case,
            device_serial="android-1",
        )

        self.assertTrue(result["ok"])
        self.assertEqual(result["summary"]["pass"], 1)
        self.assertEqual(result["summary"]["fail"], 0)
        self.assertEqual(result["steps"][0]["status"], "PASS")

    def test_precheck_all_skipped_is_not_runnable(self):
        case = self._create_case()
        self._add_device("android-1", "android")
        self._add_step(
            case_id=case.id,
            order=1,
            action="click",
            execute_on=["ios"],
            platform_overrides={"ios": {"selector": "登录", "by": "label"}},
        )

        result = precheck_case_execution(
            session=self.session,
            case=case,
            device_serial="android-1",
        )

        self.assertFalse(result["ok"])
        self.assertFalse(result["has_runnable_steps"])
        self.assertEqual(result["summary"]["skip"], 1)
        self.assertEqual(result["steps"][0]["status"], "SKIP")
        self.assertEqual(result["steps"][0]["code"], "P1001_PLATFORM_NOT_ALLOWED")

    def test_precheck_fail_on_missing_selector(self):
        case = self._create_case()
        self._add_device("android-1", "android")
        self._add_step(
            case_id=case.id,
            order=1,
            action="click",
            execute_on=["android"],
            platform_overrides={},
        )

        result = precheck_case_execution(
            session=self.session,
            case=case,
            device_serial="android-1",
        )

        self.assertFalse(result["ok"])
        self.assertEqual(result["summary"]["fail"], 1)
        self.assertEqual(result["steps"][0]["code"], "P1003_SELECTOR_MISSING")

    def test_precheck_pass_on_input_without_selector(self):
        case = self._create_case()
        self._add_device("android-1", "android")
        self._add_step(
            case_id=case.id,
            order=1,
            action="input",
            execute_on=["android"],
            platform_overrides={},
            args={"text": "hello"},
        )

        result = precheck_case_execution(
            session=self.session,
            case=case,
            device_serial="android-1",
        )

        self.assertTrue(result["ok"])
        self.assertEqual(result["summary"]["pass"], 1)
        self.assertEqual(result["summary"]["fail"], 0)
        self.assertEqual(result["steps"][0]["status"], "PASS")

    def test_precheck_pass_on_assert_text_without_selector(self):
        case = self._create_case()
        self._add_device("android-1", "android")
        self._add_step(
            case_id=case.id,
            order=1,
            action="assert_text",
            execute_on=["android"],
            platform_overrides={},
            args={"expected_text": "99.00"},
        )

        result = precheck_case_execution(
            session=self.session,
            case=case,
            device_serial="android-1",
        )

        self.assertTrue(result["ok"])
        self.assertEqual(result["summary"]["pass"], 1)
        self.assertEqual(result["summary"]["fail"], 0)
        self.assertEqual(result["steps"][0]["status"], "PASS")

    def test_precheck_fail_on_unresolved_variable_template(self):
        case = self._create_case()
        self._add_device("android-1", "android")
        self._add_step(
            case_id=case.id,
            order=1,
            action="input",
            execute_on=["android"],
            platform_overrides={"android": {"selector": "请输入手机号码", "by": "text"}},
            args={"text": "{{ NAME }}"},
        )

        result = precheck_case_execution(
            session=self.session,
            case=case,
            device_serial="android-1",
        )

        self.assertFalse(result["ok"])
        self.assertEqual(result["summary"]["fail"], 1)
        self.assertEqual(result["steps"][0]["code"], "P1006_INVALID_ARGS")
        self.assertIn("未解析变量", result["steps"][0]["message"])

    def test_precheck_pass_when_variable_comes_from_previous_extract_step(self):
        case = self._create_case()
        self._add_device("android-1", "android")
        self._add_step(
            case_id=case.id,
            order=1,
            action="extract_by_ocr",
            execute_on=["android"],
            args={"region": "[0.1,0.1,0.2,0.2]"},
            value="PRICE",
        )
        self._add_step(
            case_id=case.id,
            order=2,
            action="input",
            execute_on=["android"],
            platform_overrides={"android": {"selector": "com.demo:id/price", "by": "id"}},
            args={"text": "{{ PRICE }}"},
        )

        result = precheck_case_execution(
            session=self.session,
            case=case,
            device_serial="android-1",
        )

        self.assertTrue(result["ok"])
        self.assertEqual(result["summary"]["pass"], 2)
        self.assertEqual(result["summary"]["fail"], 0)
        self.assertEqual(result["steps"][0]["status"], "PASS")
        self.assertEqual(result["steps"][1]["status"], "PASS")
        self.assertIn("PRICE", result.get("exported_variables", {}))

    def test_precheck_pass_on_android_click_image(self):
        case = self._create_case()
        self._add_device("android-1", "android")
        self._add_step(
            case_id=case.id,
            order=1,
            action="click_image",
            execute_on=["android"],
            platform_overrides={"android": {"selector": "static/images/a.png", "by": "image"}},
        )

        result = precheck_case_execution(
            session=self.session,
            case=case,
            device_serial="android-1",
        )

        self.assertTrue(result["ok"])
        self.assertEqual(result["summary"]["pass"], 1)
        self.assertEqual(result["summary"]["fail"], 0)

    def test_precheck_pass_on_android_assert_image(self):
        case = self._create_case()
        self._add_device("android-1", "android")
        self._add_step(
            case_id=case.id,
            order=1,
            action="assert_image",
            execute_on=["android"],
            platform_overrides={"android": {"selector": "static/images/a.png", "by": "image"}},
            args={"image_path": "static/images/a.png", "match_mode": "exists"},
        )

        result = precheck_case_execution(
            session=self.session,
            case=case,
            device_serial="android-1",
        )

        self.assertTrue(result["ok"])
        self.assertEqual(result["summary"]["pass"], 1)
        self.assertEqual(result["summary"]["fail"], 0)

    def test_precheck_pass_on_android_extract_by_ocr(self):
        case = self._create_case()
        self._add_device("android-1", "android")
        self._add_step(
            case_id=case.id,
            order=1,
            action="extract_by_ocr",
            execute_on=["android"],
            args={"region": "[0.1,0.1,0.2,0.2]", "extract_rule": {"preset_type": "price"}},
            value="PRICE",
        )

        result = precheck_case_execution(
            session=self.session,
            case=case,
            device_serial="android-1",
        )

        self.assertTrue(result["ok"])
        self.assertEqual(result["summary"]["pass"], 1)
        self.assertEqual(result["summary"]["fail"], 0)

    @patch("backend.cross_platform_execution.check_wda_health")
    @patch("backend.cross_platform_execution.resolve_ios_wda_url", return_value="http://127.0.0.1:8200")
    def test_precheck_ios_pass_with_inferred_locator_from_android(self, _, check_wda_mock):
        check_wda_mock.return_value = None

        case = self._create_case()
        self._add_device("ios-1", "ios")
        self._set_flag("ios_execution", "true")
        self._add_step(
            case_id=case.id,
            order=1,
            action="click",
            execute_on=["ios"],
            platform_overrides={"android": {"selector": "登录", "by": "text"}},
        )

        result = precheck_case_execution(
            session=self.session,
            case=case,
            device_serial="ios-1",
        )

        self.assertTrue(result["ok"])
        self.assertEqual(result["summary"]["pass"], 1)
        self.assertEqual(result["summary"]["fail"], 0)

    @patch("backend.cross_platform_execution.check_wda_health")
    @patch("backend.cross_platform_execution.resolve_ios_wda_url", return_value="http://127.0.0.1:8200")
    def test_precheck_pass_on_ios_home(self, _, check_wda_mock):
        check_wda_mock.return_value = None

        case = self._create_case()
        self._add_device("ios-1", "ios")
        self._set_flag("ios_execution", "true")
        self._add_step(
            case_id=case.id,
            order=1,
            action="home",
            execute_on=["ios"],
        )

        result = precheck_case_execution(
            session=self.session,
            case=case,
            device_serial="ios-1",
        )

        self.assertTrue(result["ok"])
        self.assertEqual(result["steps"][0]["status"], "PASS")

    @patch("backend.cross_platform_execution.check_wda_health")
    @patch("backend.cross_platform_execution.resolve_ios_wda_url", return_value="http://127.0.0.1:8200")
    def test_precheck_auto_upgrades_legacy_android_only_execute_on(self, _, check_wda_mock):
        check_wda_mock.return_value = None

        case = self._create_case()
        self._add_device("ios-1", "ios")
        self._set_flag("ios_execution", "true")
        self._add_step(
            case_id=case.id,
            order=1,
            action="home",
            execute_on=["android"],  # 历史旧数据
        )
        self._add_step(
            case_id=case.id,
            order=2,
            action="click_image",
            execute_on=["android"],  # 历史旧数据
            platform_overrides={"android": {"selector": "static/images/a.png", "by": "image"}},
        )
        self._add_step(
            case_id=case.id,
            order=3,
            action="extract_by_ocr",
            execute_on=["android"],  # 历史旧数据
            args={"region": "[0.1,0.1,0.2,0.2]"},
        )

        result = precheck_case_execution(
            session=self.session,
            case=case,
            device_serial="ios-1",
        )

        self.assertTrue(result["ok"])
        self.assertTrue(result["has_runnable_steps"])
        self.assertEqual(result["summary"]["pass"], 3)
        self.assertEqual(result["summary"]["fail"], 0)
        self.assertEqual(result["steps"][0]["status"], "PASS")
        self.assertEqual(result["steps"][1]["status"], "PASS")
        self.assertEqual(result["steps"][2]["status"], "PASS")

    @patch("backend.cross_platform_execution.check_wda_health")
    @patch("backend.cross_platform_execution.resolve_ios_wda_url", return_value="http://127.0.0.1:8200")
    def test_precheck_global_fail_when_wda_unavailable(self, _, check_wda_mock):
        check_wda_mock.side_effect = RuntimeError("P1005_WDA_UNAVAILABLE: health check failed")

        case = self._create_case()
        self._add_device("ios-1", "ios")
        self._set_flag("ios_execution", "true")
        self._add_step(
            case_id=case.id,
            order=1,
            action="sleep",
            execute_on=["ios"],
            args={"seconds": 1},
        )

        result = precheck_case_execution(
            session=self.session,
            case=case,
            device_serial="ios-1",
        )

        self.assertFalse(result["ok"])
        self.assertEqual(result["summary"]["global_fail"], 1)
        self.assertEqual(result["global_checks"][0]["code"], "P1005_WDA_UNAVAILABLE")

    @patch("backend.cross_platform_execution.check_wda_health")
    @patch("backend.cross_platform_execution.resolve_ios_wda_url", return_value="http://127.0.0.1:8200")
    def test_precheck_ios_execution_disabled(self, _, check_wda_mock):
        check_wda_mock.return_value = None

        case = self._create_case()
        self._add_device("ios-1", "ios")
        self._add_step(
            case_id=case.id,
            order=1,
            action="sleep",
            execute_on=["ios"],
            args={"seconds": 1},
        )

        result = precheck_case_execution(
            session=self.session,
            case=case,
            device_serial="ios-1",
        )

        self.assertFalse(result["ok"])
        self.assertEqual(result["summary"]["global_fail"], 1)
        self.assertEqual(result["global_checks"][0]["code"], "IOS_EXECUTION_DISABLED")


if __name__ == "__main__":
    unittest.main()
