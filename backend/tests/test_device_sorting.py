import unittest

from backend.device_sorting import sort_devices_for_display


class DeviceSortingTests(unittest.TestCase):
    def test_sort_devices_for_display_orders_runtime_statuses_first(self):
        devices = [
            {"serial": "offline-1", "model": "iPhone 14", "status": "OFFLINE"},
            {"serial": "wda-1", "model": "iPhone 15", "status": "WDA_DOWN"},
            {"serial": "idle-1", "model": "iPhone 16", "status": "IDLE"},
            {"serial": "busy-1", "model": "Pixel 9", "status": "BUSY"},
            {"serial": "fastbot-1", "model": "Pixel 8", "status": "FASTBOT_RUNNING"},
        ]

        ordered = sort_devices_for_display(devices)

        self.assertEqual(
            [device["serial"] for device in ordered],
            ["fastbot-1", "busy-1", "idle-1", "wda-1", "offline-1"],
        )


if __name__ == "__main__":
    unittest.main()
