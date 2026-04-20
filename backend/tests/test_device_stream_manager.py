import unittest
from queue import Queue

from backend.device_stream.manager import (
    ANDROID_MOTION_EVENT_ACTION_DOWN,
    SCRCPY_CONTROL_MSG_TYPE_INJECT_TOUCH_EVENT,
    SCRCPY_POINTER_ID_GENERIC_FINGER,
    DeviceInfo,
    _build_touch_control_packet,
    _collect_h264_nal_types,
    _get_h264_init_packets,
    _offer_video_packet,
    _update_h264_init_cache,
)


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


if __name__ == "__main__":
    unittest.main()
