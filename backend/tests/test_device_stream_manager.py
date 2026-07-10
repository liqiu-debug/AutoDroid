import os
import unittest
from queue import Empty
from unittest.mock import patch

from backend.device_stream.manager import (
    ANDROID_MOTION_EVENT_ACTION_DOWN,
    DEFAULT_SCRCPY_BITRATE,
    DEFAULT_SCRCPY_GOP,
    DEFAULT_SCRCPY_MAX_FPS,
    DEFAULT_SCRCPY_MAX_SIZE,
    SCRCPY_BITRATE_ENV,
    SCRCPY_CONTROL_MSG_TYPE_INJECT_TOUCH_EVENT,
    SCRCPY_GOP_ENV,
    SCRCPY_MAX_FPS_ENV,
    SCRCPY_MAX_SIZE_ENV,
    SCRCPY_POINTER_ID_GENERIC_FINGER,
    ClientStreamQueue,
    DeviceInfo,
    ScrcpyDeviceManager,
    _broadcast_video_packet,
    _build_scrcpy_server_command,
    _build_touch_control_packet,
    _collect_h264_nal_types,
    _get_h264_init_packets,
    _update_h264_init_cache,
    get_scrcpy_stream_params,
)

SCRCPY_ENV_NAMES = (
    SCRCPY_MAX_SIZE_ENV,
    SCRCPY_BITRATE_ENV,
    SCRCPY_MAX_FPS_ENV,
    SCRCPY_GOP_ENV,
)

SPS = b"\x00\x00\x00\x01\x67\x64\x00\x1f"
PPS = b"\x00\x00\x00\x01\x68\xee\x3c\x80"
IDR = b"\x00\x00\x00\x01\x65\x88\x84"


def _drain(client_queue: ClientStreamQueue) -> list:
    items = []
    while True:
        try:
            items.append(client_queue.get_nowait())
        except Empty:
            return items


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


class ScrcpyStreamParamsTests(unittest.TestCase):
    def _clear_scrcpy_env(self):
        for name in SCRCPY_ENV_NAMES:
            os.environ.pop(name, None)

    def test_defaults_when_env_unset(self):
        with patch.dict(os.environ, {}, clear=False):
            self._clear_scrcpy_env()
            params = get_scrcpy_stream_params()

        self.assertEqual(
            params,
            {
                "max_size": DEFAULT_SCRCPY_MAX_SIZE,
                "video_bit_rate": DEFAULT_SCRCPY_BITRATE,
                "max_fps": DEFAULT_SCRCPY_MAX_FPS,
                "i_frame_interval": DEFAULT_SCRCPY_GOP,
            },
        )

    def test_env_overrides_stream_params(self):
        env = {
            SCRCPY_MAX_SIZE_ENV: "1600",
            SCRCPY_BITRATE_ENV: "12000000",
            SCRCPY_MAX_FPS_ENV: "30",
            SCRCPY_GOP_ENV: "2",
        }
        with patch.dict(os.environ, env):
            params = get_scrcpy_stream_params()

        self.assertEqual(
            params,
            {
                "max_size": 1600,
                "video_bit_rate": 12000000,
                "max_fps": 30,
                "i_frame_interval": 2,
            },
        )

    def test_invalid_env_values_fall_back_to_defaults(self):
        env = {
            SCRCPY_MAX_SIZE_ENV: "abc",
            SCRCPY_BITRATE_ENV: "0",
            SCRCPY_MAX_FPS_ENV: "-5",
            SCRCPY_GOP_ENV: "  ",
        }
        with patch.dict(os.environ, env):
            params = get_scrcpy_stream_params()

        self.assertEqual(
            params,
            {
                "max_size": DEFAULT_SCRCPY_MAX_SIZE,
                "video_bit_rate": DEFAULT_SCRCPY_BITRATE,
                "max_fps": DEFAULT_SCRCPY_MAX_FPS,
                "i_frame_interval": DEFAULT_SCRCPY_GOP,
            },
        )

    def test_build_scrcpy_server_command_with_defaults(self):
        with patch.dict(os.environ, {}, clear=False):
            self._clear_scrcpy_env()
            command = _build_scrcpy_server_command("serial-1")

        self.assertEqual(
            command,
            "adb -s serial-1 shell "
            "CLASSPATH=/data/local/tmp/scrcpy-server.jar "
            "app_process / com.genymobile.scrcpy.Server 3.3.4 "
            "log_level=info tunnel_forward=true video=true control=true audio=false "
            "send_frame_meta=true "
            "max_size=1920 "
            "video_bit_rate=8000000 "
            "max_fps=60 "
            "i_frame_interval=1",
        )

    def test_build_scrcpy_server_command_uses_env_overrides(self):
        env = {
            SCRCPY_MAX_SIZE_ENV: "1280",
            SCRCPY_BITRATE_ENV: "4000000",
            SCRCPY_MAX_FPS_ENV: "25",
            SCRCPY_GOP_ENV: "5",
        }
        with patch.dict(os.environ, env):
            command = _build_scrcpy_server_command("serial-2")

        self.assertIn("max_size=1280 ", command)
        self.assertIn("video_bit_rate=4000000 ", command)
        self.assertIn("max_fps=25 ", command)
        self.assertTrue(command.endswith("i_frame_interval=5"))


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
        client_queue = ClientStreamQueue(maxsize=2)
        self.assertTrue(client_queue.offer(b"\x00\x00\x00\x01\x41\x9a\x01", {1}))
        self.assertTrue(client_queue.offer(b"\x00\x00\x00\x01\x41\x9a\x02", {1}))

        offered = client_queue.offer(b"\x00\x00\x00\x01\x41\x9a\x03", {1}, init_packets=None)

        self.assertFalse(offered)
        # 已入队的旧包保持不动
        self.assertEqual(client_queue.get_nowait(), b"\x00\x00\x00\x01\x41\x9a\x01")
        self.assertEqual(client_queue.get_nowait(), b"\x00\x00\x00\x01\x41\x9a\x02")

    def test_offer_video_packet_replaces_queue_with_latest_sync_sequence(self):
        client_queue = ClientStreamQueue(maxsize=3)
        client_queue.offer(b"\x00\x00\x00\x01\x41\x9a\x01", {1})
        client_queue.offer(b"\x00\x00\x00\x01\x41\x9a\x02", {1})
        client_queue.offer(b"\x00\x00\x00\x01\x41\x9a\x03", {1})

        offered = client_queue.offer(IDR, {5}, init_packets=[SPS, PPS, IDR])

        self.assertTrue(offered)
        self.assertEqual(_drain(client_queue), [SPS, PPS, IDR])

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


class ClientStreamQueueTests(unittest.TestCase):
    @staticmethod
    def _p_frame(marker: int) -> bytes:
        return b"\x00\x00\x00\x01\x41\x9a" + bytes([marker])

    def test_delivery_without_drops_matches_legacy_order(self):
        client_queue = ClientStreamQueue(maxsize=8)
        p1, p2 = self._p_frame(1), self._p_frame(2)

        self.assertTrue(client_queue.offer(SPS, {7}, init_packets=[SPS]))
        self.assertTrue(client_queue.offer(PPS, {8}, init_packets=[SPS, PPS]))
        self.assertTrue(client_queue.offer(IDR, {5}, init_packets=[SPS, PPS, IDR]))
        self.assertTrue(client_queue.offer(p1, {1}))
        self.assertTrue(client_queue.offer(p2, {1}))

        self.assertFalse(client_queue.awaiting_keyframe)
        self.assertEqual(_drain(client_queue), [SPS, PPS, IDR, p1, p2])

    def test_p_frames_after_drop_are_rejected_even_with_room(self):
        client_queue = ClientStreamQueue(maxsize=2)
        client_queue.offer(self._p_frame(1), {1})
        client_queue.offer(self._p_frame(2), {1})
        # 队列满，首次丢弃 → 进入等待关键帧
        self.assertFalse(client_queue.offer(self._p_frame(3), {1}))
        self.assertTrue(client_queue.awaiting_keyframe)

        # 消费端取走积压后队列有空位，但参考链已断裂，后续 P 帧仍需丢弃
        _drain(client_queue)

        self.assertFalse(client_queue.offer(self._p_frame(4), {1}))
        self.assertTrue(client_queue.awaiting_keyframe)
        self.assertEqual(_drain(client_queue), [])

    def test_idr_after_drop_restores_delivery_with_init_sequence(self):
        client_queue = ClientStreamQueue(maxsize=3)
        for marker in (1, 2, 3):
            self.assertTrue(client_queue.offer(self._p_frame(marker), {1}))
        self.assertFalse(client_queue.offer(self._p_frame(4), {1}))
        self.assertTrue(client_queue.awaiting_keyframe)

        offered = client_queue.offer(IDR, {5}, init_packets=[SPS, PPS, IDR])

        self.assertTrue(offered)
        self.assertFalse(client_queue.awaiting_keyframe)
        # 积压被清空，恢复投递以完整 init 序列开头
        self.assertEqual(_drain(client_queue), [SPS, PPS, IDR])

        p5 = self._p_frame(5)
        self.assertTrue(client_queue.offer(p5, {1}))
        self.assertEqual(_drain(client_queue), [p5])

    def test_config_only_reseed_keeps_waiting_until_idr(self):
        client_queue = ClientStreamQueue(maxsize=3)
        for marker in (1, 2, 3):
            client_queue.offer(self._p_frame(marker), {1})
        self.assertFalse(client_queue.offer(self._p_frame(4), {1}))

        # 仅 SPS/PPS（尚无 IDR）：投递成功但继续等待关键帧
        self.assertTrue(client_queue.offer(PPS, {8}, init_packets=[SPS, PPS]))
        self.assertTrue(client_queue.awaiting_keyframe)
        self.assertFalse(client_queue.offer(self._p_frame(5), {1}))

        # IDR 到来后恢复投递
        self.assertTrue(client_queue.offer(IDR, {5}, init_packets=[SPS, PPS, IDR]))
        self.assertFalse(client_queue.awaiting_keyframe)
        self.assertEqual(_drain(client_queue), [SPS, PPS, IDR])

    def test_seed_prefills_init_sequence_for_new_client(self):
        client_queue = ClientStreamQueue(maxsize=30)
        client_queue.seed([SPS, PPS, IDR])

        self.assertFalse(client_queue.awaiting_keyframe)
        p1 = self._p_frame(1)
        self.assertTrue(client_queue.offer(p1, {1}))
        self.assertEqual(_drain(client_queue), [SPS, PPS, IDR, p1])

    def test_broadcast_reseeds_congested_client_from_device_cache(self):
        dev_info = DeviceInfo("serial-broadcast", 27183)
        client_queue = ClientStreamQueue(maxsize=3)
        dev_info.input_queues.append(client_queue)
        p1 = self._p_frame(1)
        p2 = self._p_frame(2)
        fresh_idr = b"\x00\x00\x00\x01\x65\x88\x99"

        _broadcast_video_packet(dev_info, SPS)
        _broadcast_video_packet(dev_info, PPS)
        _broadcast_video_packet(dev_info, p1)  # 队列满 3/3
        _broadcast_video_packet(dev_info, p2)  # 丢弃 → 等待关键帧
        self.assertTrue(client_queue.awaiting_keyframe)

        _broadcast_video_packet(dev_info, fresh_idr)

        self.assertFalse(client_queue.awaiting_keyframe)
        self.assertEqual(_drain(client_queue), [SPS, PPS, fresh_idr])


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
