"""_get_device_props_batch 批量属性解析测试。"""
import asyncio
import unittest
from unittest.mock import patch

from backend.api.devices import (
    _BASE_DEVICE_PROPS,
    _MARKET_NAME_PROPS,
    _PROP_BATCH_SEPARATOR,
    _get_device_props_batch,
)


def _batch_output(values, size_line="Physical size: 1080x2340"):
    """按批量命令的输出格式拼接：各属性值之间夹分隔符行。"""
    segments = [f"{value}\n" for value in values] + [f"{size_line}\n"]
    return f"{_PROP_BATCH_SEPARATOR}\n".join(segments).encode("utf-8")


class DevicePropsBatchTests(unittest.TestCase):
    def test_parses_all_props_and_resolution_in_single_round_trip(self):
        calls = []

        async def fake_run(*args, timeout=15):
            calls.append(args)
            return _batch_output(
                ["SM-G9980", "samsung", "14", "Galaxy S21 Ultra", "", "null"]
            )

        with patch("backend.api.devices._run_adb_command", side_effect=fake_run):
            props = asyncio.run(_get_device_props_batch("serial-1"))

        self.assertEqual(len(calls), 1)  # 全部属性一次往返
        self.assertEqual(props["ro.product.model"], "SM-G9980")
        self.assertEqual(props["ro.product.brand"], "samsung")
        self.assertEqual(props["ro.build.version.release"], "14")
        self.assertEqual(props["ro.product.marketname"], "Galaxy S21 Ultra")
        self.assertEqual(props["ro.vendor.oplus.market.name"], "")
        self.assertEqual(props["ro.vivo.market.name"], "null")
        self.assertEqual(props["resolution"], "1080x2340")

    def test_command_failure_returns_empty_values(self):
        async def fake_run(*args, timeout=15):
            raise RuntimeError("device offline")

        with patch("backend.api.devices._run_adb_command", side_effect=fake_run):
            props = asyncio.run(_get_device_props_batch("serial-1"))

        for prop in _BASE_DEVICE_PROPS + _MARKET_NAME_PROPS:
            self.assertEqual(props[prop], "")
        self.assertEqual(props["resolution"], "")

    def test_malformed_output_returns_empty_values(self):
        async def fake_run(*args, timeout=15):
            return b"garbage without separators"

        with patch("backend.api.devices._run_adb_command", side_effect=fake_run):
            props = asyncio.run(_get_device_props_batch("serial-1"))

        self.assertEqual(props["ro.product.model"], "")
        self.assertEqual(props["resolution"], "")

    def test_resolution_without_colon_kept_as_is(self):
        async def fake_run(*args, timeout=15):
            return _batch_output(["m", "b", "14", "", "", ""], size_line="1080x2340")

        with patch("backend.api.devices._run_adb_command", side_effect=fake_run):
            props = asyncio.run(_get_device_props_batch("serial-1"))

        self.assertEqual(props["resolution"], "1080x2340")


if __name__ == "__main__":
    unittest.main()
