import os
import unittest
from queue import Empty
from unittest.mock import patch

from fastapi import HTTPException

from backend.device_stream import router as stream_router
from backend.device_stream.manager import (
    ANDROID_MOTION_EVENT_ACTION_DOWN,
    DEFAULT_SCRCPY_BITRATE,
    DEFAULT_SCRCPY_GOP,
    DEFAULT_SCRCPY_MAX_FPS,
    DEFAULT_SCRCPY_MAX_SIZE,
    DEFAULT_SCRCPY_REMOTE_BITRATE,
    DEFAULT_SCRCPY_REMOTE_GOP,
    DEFAULT_SCRCPY_REMOTE_MAX_FPS,
    DEFAULT_SCRCPY_REMOTE_MAX_SIZE,
    SCRCPY_BITRATE_ENV,
    SCRCPY_CONTROL_MSG_TYPE_INJECT_TOUCH_EVENT,
    SCRCPY_GOP_ENV,
    SCRCPY_MAX_FPS_ENV,
    SCRCPY_MAX_SIZE_ENV,
    SCRCPY_POINTER_ID_GENERIC_FINGER,
    SCRCPY_REMOTE_BITRATE_ENV,
    SCRCPY_REMOTE_GOP_ENV,
    SCRCPY_REMOTE_MAX_FPS_ENV,
    SCRCPY_REMOTE_MAX_SIZE_ENV,
    STREAM_PROFILE_HD,
    STREAM_PROFILE_SMOOTH,
    STREAM_PROFILE_STANDARD,
    STREAM_PROFILES,
    ClientStreamQueue,
    DeviceInfo,
    ScrcpyDeviceManager,
    _broadcast_video_packet,
    _build_scrcpy_server_command,
    _build_touch_control_packet,
    _collect_h264_nal_types,
    _get_h264_init_packets,
    _update_h264_init_cache,
    default_stream_profile,
    get_scrcpy_stream_params,
    get_stream_profile_state,
    is_remote_serial,
    is_tunnel_serial,
    set_stream_profile_override,
)

SCRCPY_ENV_NAMES = (
    SCRCPY_MAX_SIZE_ENV,
    SCRCPY_BITRATE_ENV,
    SCRCPY_MAX_FPS_ENV,
    SCRCPY_GOP_ENV,
    SCRCPY_REMOTE_MAX_SIZE_ENV,
    SCRCPY_REMOTE_BITRATE_ENV,
    SCRCPY_REMOTE_MAX_FPS_ENV,
    SCRCPY_REMOTE_GOP_ENV,
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

    def test_is_remote_serial_detects_tunnel_and_wireless_forms(self):
        self.assertTrue(is_remote_serial("127.0.0.1:28100"))
        self.assertTrue(is_remote_serial("192.168.1.5:5555"))
        self.assertFalse(is_remote_serial("emulator-5554"))
        self.assertFalse(is_remote_serial("ABCD1234"))
        self.assertFalse(is_remote_serial(""))

    def test_is_tunnel_serial_only_matches_tunnel_port_range(self):
        self.assertTrue(is_tunnel_serial("127.0.0.1:28100"))
        self.assertTrue(is_tunnel_serial("127.0.0.1:28199"))
        self.assertFalse(is_tunnel_serial("127.0.0.1:28200"))
        self.assertFalse(is_tunnel_serial("192.168.1.5:5555"))
        self.assertFalse(is_tunnel_serial("ABCD1234"))
        self.assertFalse(is_tunnel_serial(""))

    def test_wireless_serial_uses_standard_profile_defaults(self):
        with patch.dict(os.environ, {}, clear=False):
            self._clear_scrcpy_env()
            params = get_scrcpy_stream_params("192.168.1.5:5555")

        self.assertEqual(
            params,
            {
                "max_size": DEFAULT_SCRCPY_REMOTE_MAX_SIZE,
                "video_bit_rate": DEFAULT_SCRCPY_REMOTE_BITRATE,
                "max_fps": DEFAULT_SCRCPY_REMOTE_MAX_FPS,
                "i_frame_interval": DEFAULT_SCRCPY_REMOTE_GOP,
            },
        )

    def test_tunnel_serial_defaults_to_smooth_profile(self):
        with patch.dict(os.environ, {}, clear=False):
            self._clear_scrcpy_env()
            params = get_scrcpy_stream_params("127.0.0.1:28100")

        self.assertEqual(params, STREAM_PROFILES[STREAM_PROFILE_SMOOTH])
        self.assertEqual(default_stream_profile("127.0.0.1:28100"), STREAM_PROFILE_SMOOTH)
        self.assertEqual(default_stream_profile("192.168.1.5:5555"), STREAM_PROFILE_STANDARD)
        self.assertEqual(default_stream_profile("ABCD1234"), STREAM_PROFILE_HD)

    def test_usb_serial_keeps_standard_profile(self):
        with patch.dict(os.environ, {}, clear=False):
            self._clear_scrcpy_env()
            params = get_scrcpy_stream_params("ABCD1234")

        self.assertEqual(params["max_size"], DEFAULT_SCRCPY_MAX_SIZE)
        self.assertEqual(params["video_bit_rate"], DEFAULT_SCRCPY_BITRATE)

    def test_remote_env_overrides_remote_profile_only(self):
        env = {
            SCRCPY_REMOTE_MAX_SIZE_ENV: "960",
            SCRCPY_REMOTE_BITRATE_ENV: "1500000",
            SCRCPY_REMOTE_MAX_FPS_ENV: "20",
            SCRCPY_REMOTE_GOP_ENV: "2",
        }
        with patch.dict(os.environ, env):
            for name in (SCRCPY_MAX_SIZE_ENV, SCRCPY_BITRATE_ENV, SCRCPY_MAX_FPS_ENV, SCRCPY_GOP_ENV):
                os.environ.pop(name, None)
            remote = get_scrcpy_stream_params("192.168.1.7:5555")
            usb = get_scrcpy_stream_params("ABCD1234")

        self.assertEqual(
            remote,
            {
                "max_size": 960,
                "video_bit_rate": 1500000,
                "max_fps": 20,
                "i_frame_interval": 2,
            },
        )
        self.assertEqual(usb["max_size"], DEFAULT_SCRCPY_MAX_SIZE)
        self.assertEqual(usb["video_bit_rate"], DEFAULT_SCRCPY_BITRATE)

    def test_build_scrcpy_server_command_applies_smooth_profile_for_tunnel_serial(self):
        with patch.dict(os.environ, {}, clear=False):
            self._clear_scrcpy_env()
            command = _build_scrcpy_server_command("127.0.0.1:28100")

        smooth = STREAM_PROFILES[STREAM_PROFILE_SMOOTH]
        self.assertIn(f"max_size={smooth['max_size']} ", command)
        self.assertIn(f"video_bit_rate={smooth['video_bit_rate']} ", command)
        self.assertIn(f"max_fps={smooth['max_fps']} ", command)


class StreamProfileOverrideTests(unittest.TestCase):
    SERIAL = "127.0.0.1:28105"

    def setUp(self):
        self.addCleanup(set_stream_profile_override, self.SERIAL, None)
        for name in SCRCPY_ENV_NAMES:
            os.environ.pop(name, None)

    def test_override_takes_precedence_over_default_and_env(self):
        with patch.dict(os.environ, {SCRCPY_REMOTE_BITRATE_ENV: "1500000"}):
            set_stream_profile_override(self.SERIAL, STREAM_PROFILE_HD)
            params = get_scrcpy_stream_params(self.SERIAL)

        self.assertEqual(params, STREAM_PROFILES[STREAM_PROFILE_HD])

    def test_clearing_override_restores_default_profile(self):
        set_stream_profile_override(self.SERIAL, STREAM_PROFILE_HD)
        set_stream_profile_override(self.SERIAL, None)

        self.assertEqual(get_scrcpy_stream_params(self.SERIAL), STREAM_PROFILES[STREAM_PROFILE_SMOOTH])

    def test_rejects_unknown_profile_and_empty_serial(self):
        with self.assertRaises(ValueError):
            set_stream_profile_override(self.SERIAL, "ultra")
        with self.assertRaises(ValueError):
            set_stream_profile_override("", STREAM_PROFILE_HD)

    def test_profile_state_reports_source(self):
        state = get_stream_profile_state(self.SERIAL)
        self.assertEqual(state["profile"], STREAM_PROFILE_SMOOTH)
        self.assertEqual(state["source"], "default")

        set_stream_profile_override(self.SERIAL, STREAM_PROFILE_STANDARD)
        state = get_stream_profile_state(self.SERIAL)
        self.assertEqual(state["profile"], STREAM_PROFILE_STANDARD)
        self.assertEqual(state["source"], "override")
        self.assertEqual(state["params"], STREAM_PROFILES[STREAM_PROFILE_STANDARD])


class StreamProfileEndpointTests(unittest.TestCase):
    SERIAL = "127.0.0.1:28106"

    def setUp(self):
        self.addCleanup(set_stream_profile_override, self.SERIAL, None)

    def test_set_profile_reconnects_managed_device(self):
        request = stream_router.StreamProfileRequest(profile=STREAM_PROFILE_HD)
        with patch.object(stream_router.device_manager, "get_device", return_value={"serial": self.SERIAL}), \
             patch.object(stream_router.device_manager, "reconnect_device") as reconnect:
            payload = stream_router.set_stream_profile(self.SERIAL, request)

        reconnect.assert_called_once_with(self.SERIAL)
        self.assertEqual(payload["status"], "reconnecting")
        self.assertEqual(payload["profile"], STREAM_PROFILE_HD)
        self.assertEqual(payload["source"], "override")

    def test_set_profile_saves_without_reconnect_when_not_managed(self):
        request = stream_router.StreamProfileRequest(profile=STREAM_PROFILE_STANDARD)
        with patch.object(stream_router.device_manager, "get_device", return_value=None), \
             patch.object(stream_router.device_manager, "reconnect_device") as reconnect:
            payload = stream_router.set_stream_profile(self.SERIAL, request)

        reconnect.assert_not_called()
        self.assertEqual(payload["status"], "saved")
        self.assertEqual(payload["profile"], STREAM_PROFILE_STANDARD)

    def test_set_profile_auto_clears_override(self):
        set_stream_profile_override(self.SERIAL, STREAM_PROFILE_HD)
        request = stream_router.StreamProfileRequest(profile="auto")
        with patch.object(stream_router.device_manager, "get_device", return_value=None):
            payload = stream_router.set_stream_profile(self.SERIAL, request)

        self.assertEqual(payload["source"], "default")
        self.assertEqual(payload["profile"], STREAM_PROFILE_SMOOTH)

    def test_set_profile_rejects_unknown_value(self):
        request = stream_router.StreamProfileRequest(profile="ultra")
        with self.assertRaises(HTTPException) as ctx:
            stream_router.set_stream_profile(self.SERIAL, request)
        self.assertEqual(ctx.exception.status_code, 400)

    def test_get_profile_returns_state(self):
        payload = stream_router.get_stream_profile(self.SERIAL)
        self.assertEqual(payload["serial"], self.SERIAL)
        self.assertEqual(payload["profile"], STREAM_PROFILE_SMOOTH)


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


class _RecorderProbe:
    """记录 ingest 调用的假录制器，用于验证录制取流点。"""

    def __init__(self):
        self.packets = []

    def ingest(self, data: bytes) -> None:
        self.packets.append(data)


class CrashReplayTapPointTests(unittest.TestCase):
    """崩溃复现录制器必须消费丢帧路径之前的原始码流。"""

    def test_recorder_receives_raw_stream_before_client_drop_path(self):
        dev_info = DeviceInfo("serial-recorder", 27183)
        recorder = _RecorderProbe()
        dev_info.recorder = recorder
        client_queue = ClientStreamQueue(maxsize=2)
        dev_info.input_queues.append(client_queue)
        p1 = b"\x00\x00\x00\x01\x41\x9a\x01"
        p2 = b"\x00\x00\x00\x01\x41\x9a\x02"

        _broadcast_video_packet(dev_info, SPS)
        _broadcast_video_packet(dev_info, PPS)  # 队列满 2/2
        _broadcast_video_packet(dev_info, p1)   # 观看端丢弃 → 等待关键帧
        _broadcast_video_packet(dev_info, p2)   # 观看端继续丢弃

        self.assertTrue(client_queue.awaiting_keyframe)
        # 观看端丢掉的 P 帧原样进入录制器，顺序完整
        self.assertEqual(recorder.packets, [SPS, PPS, p1, p2])

    def test_recorder_receives_stream_without_any_viewer(self):
        dev_info = DeviceInfo("serial-no-viewer", 27184)
        recorder = _RecorderProbe()
        dev_info.recorder = recorder
        p1 = b"\x00\x00\x00\x01\x41\x9a\x01"

        _broadcast_video_packet(dev_info, IDR)
        _broadcast_video_packet(dev_info, p1)

        self.assertEqual(recorder.packets, [IDR, p1])


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
