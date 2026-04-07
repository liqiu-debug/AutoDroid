import unittest

from fastapi import HTTPException

from backend.main import (
    CLICK_IMAGE_REQUIRED_DETAIL,
    CLICK_TARGET_NOT_FOUND_DETAIL,
    _build_click_step_from_inspect_result,
    _build_step_from_inspect,
)


class RecordingClickGuardTests(unittest.TestCase):
    def test_build_step_accepts_semantic_text_locator(self):
        inspect_res = {
            "selector": "登录",
            "strategy": "text",
            "element": {
                "text": "登录",
                "description": "",
                "resourceId": "com.demo:id/login",
            },
        }

        step = _build_step_from_inspect(inspect_res)

        self.assertEqual(step["selector"], "登录")
        self.assertEqual(step["selector_type"], "text")
        self.assertEqual(step["description"], "Click [登录]")

    def test_build_step_rejects_xpath_fallback_for_click_recording(self):
        inspect_res = {
            "selector": "//android.view.View",
            "strategy": "xpath",
            "element": {
                "text": "",
                "description": "",
                "resourceId": "com.demo:id/icon",
            },
        }

        with self.assertRaises(HTTPException) as context:
            _build_step_from_inspect(inspect_res)

        self.assertEqual(context.exception.status_code, 400)
        self.assertEqual(context.exception.detail, CLICK_IMAGE_REQUIRED_DETAIL)

    def test_missing_click_target_rejects_coordinate_fallback(self):
        with self.assertRaises(HTTPException) as context:
            _build_click_step_from_inspect_result({"error": "在该坐标未找到任何元素"})

        self.assertEqual(context.exception.status_code, 400)
        self.assertEqual(context.exception.detail, CLICK_TARGET_NOT_FOUND_DETAIL)


if __name__ == "__main__":
    unittest.main()
