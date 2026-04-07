import unittest
from unittest.mock import Mock

from backend.drivers.ios_driver import IOSDriver


class IOSDriverScaleTests(unittest.TestCase):
    def _new_driver(self) -> IOSDriver:
        driver = IOSDriver.__new__(IOSDriver)
        driver.device_id = "ios-1"
        driver.client = Mock()
        driver.scale = 3.0
        return driver

    def test_click_by_coordinates_converts_physical_pixels_to_logical_points(self):
        driver = self._new_driver()
        session = Mock()
        driver.client.session.return_value = session

        IOSDriver.click_by_coordinates(driver, 300.0, 600.0)

        session.tap.assert_called_once_with(100.0, 200.0)

    def test_edge_back_swipe_uses_absolute_edge_coordinates(self):
        driver = self._new_driver()
        session = Mock()
        session.window_size.return_value = {"width": 390, "height": 844}
        driver.client.session.return_value = session
        driver.client.window_size.return_value = {"width": 390, "height": 844}

        ok = IOSDriver._try_edge_back_swipe(driver, y_ratio=0.5)

        self.assertTrue(ok)
        session.swipe.assert_called_once_with(1, 422, 370, 422, duration=0.06)


if __name__ == "__main__":
    unittest.main()
