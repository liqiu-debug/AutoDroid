import unittest
from unittest.mock import Mock

from backend.drivers.ios_driver import IOSDriver


class IOSDriverBackTests(unittest.TestCase):
    def _new_driver(self) -> IOSDriver:
        driver = IOSDriver.__new__(IOSDriver)
        driver.device_id = "ios-1"
        driver.client = Mock()
        driver.scale = 3.0
        return driver

    def test_back_prefers_first_edge_swipe_lane_when_page_changes(self):
        driver = self._new_driver()
        driver._capture_page_signature = Mock(return_value="sig-a")
        driver._try_edge_back_swipe = Mock(return_value=True)
        driver._try_press_back = Mock(return_value=True)
        driver._tap_common_back_buttons = Mock(return_value=True)
        driver._wait_page_changed = Mock(return_value=True)

        IOSDriver.back(driver)

        driver._try_edge_back_swipe.assert_called_once_with(y_ratio=0.2)
        driver._wait_page_changed.assert_called_once_with("sig-a", timeout=0.9)
        driver._try_press_back.assert_not_called()
        driver._tap_common_back_buttons.assert_not_called()

    def test_back_fallbacks_to_button_after_press_unchanged(self):
        driver = self._new_driver()
        driver._capture_page_signature = Mock(return_value="sig-a")
        driver._try_edge_back_swipe = Mock(return_value=False)
        driver._try_press_back = Mock(return_value=True)
        driver._tap_common_back_buttons = Mock(return_value=True)
        driver._wait_page_changed = Mock(side_effect=[False, True])

        IOSDriver.back(driver)

        self.assertEqual(driver._try_edge_back_swipe.call_count, 3)
        driver._try_press_back.assert_called_once()
        driver._tap_common_back_buttons.assert_called_once()
        self.assertEqual(driver._wait_page_changed.call_count, 2)

    def test_back_raises_when_page_never_changes(self):
        driver = self._new_driver()
        driver._capture_page_signature = Mock(return_value="sig-a")
        driver._try_edge_back_swipe = Mock(return_value=False)
        driver._try_press_back = Mock(return_value=True)
        driver._tap_common_back_buttons = Mock(return_value=True)
        driver._wait_page_changed = Mock(return_value=False)

        with self.assertRaises(RuntimeError) as context:
            IOSDriver.back(driver)

        self.assertIn("页面未变化", str(context.exception))
        self.assertEqual(driver._wait_page_changed.call_count, 2)

    def test_back_retries_edge_swipe_lanes_before_fallback(self):
        driver = self._new_driver()
        driver._capture_page_signature = Mock(return_value="sig-a")
        driver._try_edge_back_swipe = Mock(return_value=True)
        driver._try_press_back = Mock(return_value=True)
        driver._tap_common_back_buttons = Mock(return_value=True)
        driver._wait_page_changed = Mock(side_effect=[False, False, True])

        IOSDriver.back(driver)

        self.assertEqual(driver._try_edge_back_swipe.call_count, 3)
        driver._try_press_back.assert_not_called()
        driver._tap_common_back_buttons.assert_not_called()
        self.assertEqual(driver._wait_page_changed.call_count, 3)

    def test_edge_swipe_uses_integer_absolute_coordinates(self):
        driver = self._new_driver()
        session = Mock()
        session.window_size.return_value = (393, 852)
        session.swipe.return_value = None
        driver.client.session.return_value = session
        driver.client.window_size = Mock(side_effect=RuntimeError("not-used"))

        ok = IOSDriver._try_edge_back_swipe(driver)

        self.assertTrue(ok)
        self.assertTrue(session.swipe.called)
        args, kwargs = session.swipe.call_args
        for idx in range(4):
            self.assertIsInstance(args[idx], int)
        self.assertLessEqual(args[0], 2)
        self.assertGreaterEqual(args[2], int(393 * 0.90))
        self.assertIn("duration", kwargs)


if __name__ == "__main__":
    unittest.main()
