"""飞书通知报告链接回归测试。

背景：system_base_url 被误配成带报表路径的地址（如
https://autodroid.cc.cd/execution/reports）时，拼接出的链接路径重复：
…/execution/reports/execution/reports/66。修复：拼接前归一化
（去尾斜杠与误配的报表路径后缀），配置成什么样都能出对链接。
"""
import unittest
from unittest.mock import patch

from backend.notification_service import NotificationService, _normalize_base_url


class NormalizeBaseUrlTests(unittest.TestCase):
    def test_strips_trailing_report_path(self):
        self.assertEqual(
            _normalize_base_url("https://autodroid.cc.cd/execution/reports"),
            "https://autodroid.cc.cd",
        )
        self.assertEqual(
            _normalize_base_url("https://autodroid.cc.cd/execution"),
            "https://autodroid.cc.cd",
        )
        self.assertEqual(
            _normalize_base_url("https://autodroid.cc.cd/fastbot/report/"),
            "https://autodroid.cc.cd",
        )

    def test_keeps_plain_root_and_subpath_prefix(self):
        self.assertEqual(_normalize_base_url("https://autodroid.cc.cd"), "https://autodroid.cc.cd")
        self.assertEqual(_normalize_base_url("https://autodroid.cc.cd/"), "https://autodroid.cc.cd")
        # 反代子路径前缀是合法配置，不能误剥
        self.assertEqual(
            _normalize_base_url("https://host/autodroid"),
            "https://host/autodroid",
        )

    def test_empty_falls_through(self):
        self.assertEqual(_normalize_base_url(""), "")
        self.assertEqual(_normalize_base_url(None), "")


class FeishuReportUrlTests(unittest.TestCase):
    def _send_card(self, base):
        captured = {}

        class _FakeClient:
            def __init__(self, *args, **kwargs):
                pass

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def post(self, url, json=None):
                captured["url"] = url
                captured["card"] = json

                class _Resp:
                    status_code = 200

                    def json(self):
                        return {"code": 0}

                return _Resp()

        settings = {"feishu_webhook": "https://open.feishu.cn/hook", "system_base_url": base}
        with patch.object(
            NotificationService, "_get_setting", classmethod(lambda cls, key: settings.get(key))
        ), patch("backend.notification_service.httpx.Client", _FakeClient):
            NotificationService.send_report_card(
                task_name="回归任务",
                execution_id=66,
                status="PASS",
                total=3,
                passed=3,
                failed=0,
                duration_seconds=12,
                errors=[],
                device_count=1,
            )
        return captured

    def test_misconfigured_base_with_report_path_still_links_correctly(self):
        """主场景：base 误配成 …/execution/reports，链接不得重复路径。"""
        captured = self._send_card("https://autodroid.cc.cd/execution/reports")
        card_text = str(captured["card"])
        self.assertIn("https://autodroid.cc.cd/execution/reports/66", card_text)
        self.assertNotIn("execution/reports/execution/reports", card_text)

    def test_plain_base_unchanged(self):
        captured = self._send_card("https://autodroid.cc.cd")
        self.assertIn("https://autodroid.cc.cd/execution/reports/66", str(captured["card"]))


if __name__ == "__main__":
    unittest.main()
