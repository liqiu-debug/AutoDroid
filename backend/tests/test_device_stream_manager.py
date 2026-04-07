import unittest

from backend.device_stream.manager import (
    DeviceInfo,
    _collect_h264_nal_types,
    _get_h264_init_packets,
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


if __name__ == "__main__":
    unittest.main()
