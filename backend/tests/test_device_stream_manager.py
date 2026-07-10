import unittest
from queue import Queue
from unittest.mock import patch

from backend.device_stream.manager import (
    ANDROID_MOTION_EVENT_ACTION_DOWN,
    SCRCPY_CONTROL_MSG_TYPE_INJECT_TOUCH_EVENT,
    SCRCPY_POINTER_ID_GENERIC_FINGER,
    DeviceInfo,
    ScrcpyDeviceManager,
    _build_touch_control_packet,
    _collect_h264_nal_types,
    _get_h264_init_packets,
    _offer_video_packet,
    _update_h264_init_cache,
)


class _FakeAdbDevice:
    def __init__(self):
        self.shell_calls = []

    def shell(self, command):
        self.shell_calls.append(command)
        return ""


class _FakeAdbClient:
    def __init__(self, device):
        self._device = device

    def device(self, serial):
        return self._device


class DeviceStreamManagerCacheTests(unittest.TestCase):
    def test_collect_h264_nal_types_supports_mixed_start_codes(self):
        packet = (
            b"\x00\x00\x00\x01\x67\x64\x00\x1f"
            b"\x00\x00\x01\x68\xee\x3c\x80"
            b"\x00\x00\x00\x01\x65\x88\x84"
        )

        nal_types = _collect_h264_nal_types(packet)

        self.assertEqual(nal_types, {5, 7, 8})

    def test_init_cache_includes_recent_keyframe_for_new_client(self):
        dev_info = DeviceInfo("serial-1", 27183)
        sps = b"\x00\x00\x00\x01\x67\x64\x00\x1f"
        pps = b"\x00\x00\x00\x01\x68\xee\x3c\x80"
        idr = b"\x00\x00\x00\x01\x65\x88\x84"

        _update_h264_init_cache(dev_info, sps)
        _update_h264_init_cache(dev_info, pps)
        _update_h264_init_cache(dev_info, idr)

        self.assertEqual(_get_h264_init_packets(dev_info), [sps, pps, idr])

    def test_new_sps_invalidates_stale_keyframe(self):
        dev_info = DeviceInfo("serial-2", 27184)
        old_idr = b"\x00\x00\x00\x01\x65\x88\x84"
        new_sps = b"\x00\x00\x00\x01\x67\x64\x00\x28"

        _update_h264_init_cache(dev_info, old_idr)
        self.assertEqual(dev_info.last_keyframe_packet, old_idr)

        _update_h264_init_cache(dev_info, new_sps)

        self.assertIsNone(dev_info.last_keyframe_packet)
        self.assertEqual(_get_h264_init_packets(dev_info), [new_sps])

    def test_offer_video_packet_drops_non_sync_packet_when_queue_is_full(self):
        client_queue = Queue(maxsize=2)
        client_queue.put(b"old-1")
        client_queue.put(b"old-2")

        offered = _offer_video_packet(client_queue, b"p-frame", {1}, init_packets=None)

        self.assertFalse(offered)
        self.assertEqual(client_queue.get_nowait(), b"old-1")
        self.assertEqual(client_queue.get_nowait(), b"old-2")

    def test_offer_video_packet_replaces_queue_with_latest_sync_sequence(self):
        client_queue = Queue(maxsize=3)
        client_queue.put(b"stale-1")
        client_queue.put(b"stale-2")
        client_queue.put(b"stale-3")
        sps = b"\x00\x00\x00\x01\x67\x64\x00\x1f"
        pps = b"\x00\x00\x00\x01\x68\xee\x3c\x80"
        idr = b"\x00\x00\x00\x01\x65\x88\x84"

        offered = _offer_video_packet(client_queue, idr, {5}, init_packets=[sps, pps, idr])

        self.assertTrue(offered)
        self.assertEqual(
            [client_queue.get_nowait(), client_queue.get_nowait(), client_queue.get_nowait()],
            [sps, pps, idr],
        )

    def test_build_touch_control_packet_matches_scrcpy_wire_format(self):
        packet = _build_touch_control_packet(
            ANDROID_MOTION_EVENT_ACTION_DOWN,
            100,
            200,
            1080,
            1920,
        )

        self.assertEqual(len(packet), 32)
        self.assertEqual(packet[0], SCRCPY_CONTROL_MSG_TYPE_INJECT_TOUCH_EVENT)
        self.assertEqual(packet[1], ANDROID_MOTION_EVENT_ACTION_DOWN)
        self.assertEqual(int.from_bytes(packet[2:10], "big", signed=True), SCRCPY_POINTER_ID_GENERIC_FINGER)
        self.assertEqual(int.from_bytes(packet[10:14], "big"), 100)
        self.assertEqual(int.from_bytes(packet[14:18], "big"), 200)
        self.assertEqual(int.from_bytes(packet[18:20], "big"), 1080)
        self.assertEqual(int.from_bytes(packet[20:22], "big"), 1920)
        self.assertEqual(int.from_bytes(packet[22:24], "big"), 0xFFFF)


class DeviceStreamManagerInitializationTests(unittest.TestCase):
    def test_connection_exception_is_visible_in_device_status(self):
        manager = ScrcpyDeviceManager()

        with patch(
            "backend.device_stream.manager.adbutils.AdbClient",
            side_effect=RuntimeError("adb unavailable"),
        ):
            manager._on_device_connected("serial-1")

        devices = manager.get_devices_list()
        self.assertEqual(len(devices), 1)
        self.assertEqual(devices[0]["serial"], "serial-1")
        self.assertFalse(devices[0]["ready"])
        self.assertIn("adb unavailable", devices[0]["error"])

    def test_reconnect_does_not_clear_in_progress_status(self):
        manager = ScrcpyDeviceManager()
        # 模拟 _on_device_connected 进行中的完整状态：_connecting 标记 + 初始化中的 _devices 条目
        in_progress = DeviceInfo("serial-1", 0)
        in_progress.initializing = True
        manager._connecting.add("serial-1")
        manager._devices["serial-1"] = in_progress

        manager.reconnect_device("serial-1")

        devices = manager.get_devices_list()
        self.assertEqual(len(devices), 1)
        self.assertEqual(devices[0]["serial"], "serial-1")
        self.assertFalse(devices[0]["ready"])
        self.assertTrue(devices[0]["initializing"])
        self.assertIsNone(devices[0]["error"])
        self.assertIn("serial-1", manager._connecting)

    def test_adb_touch_method_sends_clamped_tap(self):
        manager = ScrcpyDeviceManager()
        dev_info = DeviceInfo("serial-1", 27183)
        dev_info.ready = True
        dev_info.screen_width = 100
        dev_info.screen_height = 200
        manager._devices["serial-1"] = dev_info
        adb_device = _FakeAdbDevice()

        with patch(
            "backend.device_stream.manager.adbutils.AdbClient",
            return_value=_FakeAdbClient(adb_device),
        ):
            manager.send_touch_event("serial-1", ANDROID_MOTION_EVENT_ACTION_DOWN, 150, -20, method="adb")

        self.assertEqual(adb_device.shell_calls, ["input tap 99 0"])


if __name__ == "__main__":
    unittest.main()
