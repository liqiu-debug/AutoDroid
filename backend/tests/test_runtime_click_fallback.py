# -*- coding: utf-8 -*-
"""设备端点击兜底链回归测试（三星 One UI 场景）。

真机表现为：等待元素/文本断言都命中「首页」，点击却报
「click 点击返回失败: 首页」——文本节点 clickable=false 时
ACTION_CLICK 返回 false。runtime.js 需退到可点击父级容器或元素中心坐标
（镜像 PC 侧 u2「取可见区域中心点发坐标点击」语义）。
"""
import json
import os
import shutil
import subprocess
import unittest
from pathlib import Path

HARNESS = Path(__file__).resolve().parent / "support" / "android_click_harness.mjs"
RUNTIME = Path(__file__).resolve().parents[1] / "autojs_export" / "runtime.js"
NODE = shutil.which("node")


@unittest.skipUnless(NODE, "需要 node 运行设备端 JS 链路试验台")
class RuntimeClickFallbackTests(unittest.TestCase):
    def run_mode(self, mode: str) -> dict:
        proc = subprocess.run(
            [NODE, str(HARNESS), str(RUNTIME)],
            env={**os.environ, "MODE": mode},
            capture_output=True,
            text=True,
            timeout=30,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr or proc.stdout)
        return json.loads(proc.stdout.strip().splitlines()[-1])

    def test_direct_click_still_passes(self):
        result = self.run_mode("direct")
        self.assertEqual(result["status"], "PASS", result)
        self.assertEqual(result["clicks"], [])

    def test_falls_back_to_clickable_parent(self):
        result = self.run_mode("parent")
        self.assertEqual(result["status"], "PASS", result)
        self.assertEqual(result["clicks"], [])  # 父级 ACTION_CLICK 成功，不发坐标

    def test_falls_back_to_center_coordinates(self):
        result = self.run_mode("coords")
        self.assertEqual(result["status"], "PASS", result)
        self.assertEqual(result["clicks"], [{"x": 111, "y": 222, "via": "global.click"}])

    def test_center_supports_android_rect_methods(self):
        result = self.run_mode("coords-method")
        self.assertEqual(result["status"], "PASS", result)
        self.assertEqual(result["clicks"], [{"x": 333, "y": 444, "via": "global.click"}])

    def test_all_fallbacks_fail_reports_click_failure(self):
        result = self.run_mode("fail")
        self.assertEqual(result["status"], "FAIL", result)
        self.assertIn("click 点击返回失败", result["error"] or "")


if __name__ == "__main__":
    unittest.main()
