import unittest
from unittest.mock import patch

from backend.execution_errors import suggestion_for
from backend.report_display import build_report_display, storage_report_display, with_report_display


class ReportDisplayTests(unittest.TestCase):
    def test_custom_description_wins_and_hides_preview(self):
        display = build_report_display(
            {
                "action": "click_image",
                "selector": "static/images/sample.png",
                "selector_type": "image",
                "description": "点头像",
            },
            include_preview_base64=True,
        )

        self.assertEqual(display["display_text"], "点头像")
        self.assertTrue(display["has_custom_description"])
        self.assertNotIn("preview_type", display)

    def test_action_specific_text_without_description(self):
        cases = [
            (
                {"action": "assert_text", "value": "登录成功", "options": {"match_mode": "not_contains"}},
                "文本断言 不包含 登录成功",
            ),
            (
                {"action": "click", "selector": "登录"},
                "点击 登录",
            ),
            (
                {"action": "wait_until_exists", "selector": "首页"},
                "等待元素 首页",
            ),
            (
                {"action": "input", "value": "admin"},
                "输入 admin",
            ),
            (
                {"action": "swipe", "selector": "down"},
                "滑动 下滑",
            ),
            (
                {"action": "sleep", "value": "5"},
                "强制等待 5s",
            ),
            (
                {"action": "start_app", "selector": "mall_app"},
                "启动应用 mall_app",
            ),
            (
                {"action": "stop_app", "selector": "mall_app"},
                "停止应用 mall_app",
            ),
            (
                {"action": "back"},
                "返回",
            ),
            (
                {"action": "home"},
                "主页",
            ),
        ]

        for step, expected in cases:
            with self.subTest(step=step):
                self.assertEqual(build_report_display(step)["display_text"], expected)

    def test_image_actions_include_template_preview_when_available(self):
        with patch("backend.report_display._read_image_base64", return_value="ZmFrZQ=="):
            click_display = build_report_display(
                {"action": "click_image", "selector": "static/images/element_1.png"},
                include_preview_base64=True,
            )
            assert_display = build_report_display(
                {
                    "action": "assert_image",
                    "args": {
                        "image_path": "static/images/element_2.png",
                        "match_mode": "not_exists",
                    },
                },
                include_preview_base64=True,
            )

        self.assertEqual(click_display["display_text"], "图像点击")
        self.assertEqual(click_display["preview_type"], "template_image")
        self.assertEqual(click_display["preview_base64"], "ZmFrZQ==")
        self.assertEqual(assert_display["display_text"], "图像断言 不存在")
        self.assertEqual(assert_display["preview_path"], "static/images/element_2.png")

    def test_image_preview_keeps_path_when_file_missing(self):
        display = build_report_display(
            {"action": "click_image", "selector": "static/images/missing.png"},
            include_preview_base64=True,
        )

        self.assertEqual(display["preview_type"], "template_image")
        self.assertEqual(display["preview_path"], "static/images/missing.png")
        self.assertNotIn("preview_base64", display)

    def test_ocr_uses_variable_and_extracted_result_without_preview(self):
        display = build_report_display(
            {
                "action": "extract_by_ocr",
                "value": "ORDER_ID",
                "output": {"export_var": "ORDER_ID", "export_value": "A-1001"},
            },
            screenshot_base64="data:image/png;base64,ZmFrZQ==",
            screenshot_path="screenshots/exec_1_step_1.png",
            include_preview_base64=True,
        )

        self.assertEqual(display["display_text"], "OCR提取变量 ORDER_ID A-1001")
        self.assertNotIn("preview_type", display)
        self.assertNotIn("preview_path", display)
        self.assertNotIn("preview_base64", display)

    def test_storage_report_display_removes_base64(self):
        stored = storage_report_display(
            {
                "display_text": "OCR提取变量 ORDER_ID",
                "preview_type": "screenshot",
                "preview_base64": "large",
            }
        )

        self.assertEqual(stored, {"display_text": "OCR提取变量 ORDER_ID", "preview_type": "screenshot"})

    def test_with_report_display_preserves_existing_display(self):
        step = with_report_display(
            {
                "action": "step",
                "report_display": {"display_text": "已有展示", "preview_type": "screenshot"},
                "screenshot": "ZmFrZQ==",
            },
            include_preview_base64=True,
        )

        self.assertEqual(step["report_display"]["display_text"], "已有展示")
        self.assertEqual(step["report_display"]["preview_base64"], "ZmFrZQ==")

    def test_error_code_and_suggestion_merged_into_display(self):
        display = build_report_display(
            {
                "action": "click",
                "selector": "登录",
                "error_code": "E2001_ELEMENT_NOT_FOUND",
                "suggestion": "请检查定位器",
            }
        )

        self.assertEqual(display["display_text"], "点击 登录")
        self.assertEqual(display["error_code"], "E2001_ELEMENT_NOT_FOUND")
        self.assertEqual(display["suggestion"], "请检查定位器")

    def test_error_fields_kept_with_custom_description(self):
        display = build_report_display(
            {
                "action": "click",
                "description": "点头像",
                "error_code": "E2002_WAIT_TIMEOUT",
                "suggestion": "适当增大 timeout",
            }
        )

        self.assertEqual(display["display_text"], "点头像")
        self.assertEqual(display["error_code"], "E2002_WAIT_TIMEOUT")
        self.assertEqual(display["suggestion"], "适当增大 timeout")

    def test_error_code_without_suggestion_falls_back_to_default(self):
        display = build_report_display(
            {"action": "click", "error_code": "E2001_ELEMENT_NOT_FOUND"}
        )

        self.assertEqual(display["error_code"], "E2001_ELEMENT_NOT_FOUND")
        self.assertEqual(display["suggestion"], suggestion_for("E2001_ELEMENT_NOT_FOUND"))

        unknown = build_report_display({"action": "click", "error_code": "E9999_UNKNOWN"})
        self.assertEqual(unknown["error_code"], "E9999_UNKNOWN")
        self.assertNotIn("suggestion", unknown)

    def test_display_without_error_fields_stays_clean(self):
        display = build_report_display({"action": "click", "selector": "登录"})

        self.assertNotIn("error_code", display)
        self.assertNotIn("suggestion", display)

    def test_attempts_merged_into_display_when_retried(self):
        display = build_report_display(
            {"action": "click", "selector": "登录", "attempts": 3}
        )

        self.assertEqual(display["attempts"], 3)

    def test_attempts_kept_with_custom_description(self):
        display = build_report_display(
            {"action": "click", "description": "点头像", "attempts": 2}
        )

        self.assertEqual(display["display_text"], "点头像")
        self.assertEqual(display["attempts"], 2)

    def test_attempts_omitted_without_retry(self):
        for payload in (
            {"action": "click", "selector": "登录"},
            {"action": "click", "selector": "登录", "attempts": 1},
            {"action": "click", "selector": "登录", "attempts": "oops"},
        ):
            display = build_report_display(payload)
            self.assertNotIn("attempts", display)

    def test_storage_report_display_keeps_error_fields(self):
        stored = storage_report_display(
            {
                "display_text": "点击 登录",
                "error_code": "E2001_ELEMENT_NOT_FOUND",
                "suggestion": "请检查定位器",
                "preview_base64": "large",
            }
        )

        self.assertEqual(
            stored,
            {
                "display_text": "点击 登录",
                "error_code": "E2001_ELEMENT_NOT_FOUND",
                "suggestion": "请检查定位器",
            },
        )


if __name__ == "__main__":
    unittest.main()
