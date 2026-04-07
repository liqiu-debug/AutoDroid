import unittest
from unittest.mock import Mock, patch

from backend.drivers.ios_driver import IOSDriver


class IOSDriverAppControlTests(unittest.TestCase):
    def _new_driver(self) -> IOSDriver:
        driver = IOSDriver.__new__(IOSDriver)
        driver.device_id = "ios-1"
        driver.client = Mock()
        driver.scale = 3.0
        return driver

    @patch("backend.drivers.ios_driver.time.sleep", return_value=None)
    def test_start_app_fallback_to_app_launch(self, _):
        driver = self._new_driver()
        driver.client.app_activate.side_effect = RuntimeError("activate failed")
        driver.client.app_launch.return_value = None
        driver.client.app_state.return_value = {"value": 4}

        IOSDriver.start_app(driver, "com.demo.mall.ios")

        driver.client.app_activate.assert_called_once_with("com.demo.mall.ios")
        driver.client.app_launch.assert_called_once_with(
            "com.demo.mall.ios",
            wait_for_quiescence=False,
        )

    @patch("backend.drivers.ios_driver.time.sleep", return_value=None)
    def test_stop_app_treats_not_running_as_success(self, _):
        driver = self._new_driver()
        session = Mock()
        session.app_terminate.side_effect = RuntimeError("no active app")
        driver.client.session.return_value = session
        driver.client.app_terminate.side_effect = RuntimeError("no active app")
        driver.client.app_state.return_value = {"value": 1}

        IOSDriver.stop_app(driver, "com.demo.mall.ios")

        driver.client.app_terminate.assert_called_once_with("com.demo.mall.ios")
        session.app_terminate.assert_not_called()

    @patch("backend.drivers.ios_driver.time.sleep", return_value=None)
    def test_stop_app_raises_when_still_running(self, _):
        driver = self._new_driver()
        session = Mock()
        session.app_terminate.side_effect = RuntimeError("terminate failed")
        driver.client.session.return_value = session
        driver.client.app_terminate.side_effect = RuntimeError("terminate failed")
        driver.client.app_state.return_value = {"value": 3}

        with self.assertRaises(RuntimeError) as context:
            IOSDriver.stop_app(driver, "com.demo.mall.ios")

        self.assertIn("iOS.stop_app 执行失败", str(context.exception))


if __name__ == "__main__":
    unittest.main()
