import unittest

from fastapi import HTTPException

from backend.api.recording import (
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
        self.assertEqual(step["description"], "")

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

    def test_ambiguous_click_target_raises_400_with_dedicated_detail(self):
        from backend.api.recording import CLICK_AMBIGUOUS_LOCATOR_DETAIL

        with self.assertRaises(HTTPException) as context:
            _build_click_step_from_inspect_result(
                {"strategy": "ambiguous", "selector": "", "element": {"bounds": "[0,0][100,100]"}}
            )

        self.assertEqual(context.exception.status_code, 400)
        self.assertEqual(context.exception.detail, CLICK_AMBIGUOUS_LOCATOR_DETAIL)

    def test_resource_id_class_name_and_composite_allowed_only_when_configured(self):
        id_result = {"strategy": "resourceId", "selector": "com.demo:id/icon", "element": {}}
        comp_result = {
            "strategy": "composite",
            "selector": '{"id":"com.demo:id/btn","text":"确认"}',
            "element": {},
        }
        cls_result = {"strategy": "className", "selector": "android.widget.EditText", "element": {}}

        with self.assertRaises(HTTPException):
            _build_click_step_from_inspect_result(id_result)
        with self.assertRaises(HTTPException):
            _build_click_step_from_inspect_result(cls_result)
        with self.assertRaises(HTTPException):
            _build_click_step_from_inspect_result(comp_result)

        id_step = _build_click_step_from_inspect_result(
            id_result,
            locator_config={"enable_resource_id": True},
        )
        self.assertEqual(id_step["selector_type"], "resourceId")
        self.assertEqual(id_step["selector"], "com.demo:id/icon")

        cls_step = _build_click_step_from_inspect_result(
            cls_result,
            locator_config={"enable_class_name": True},
        )
        self.assertEqual(cls_step["selector_type"], "className")
        self.assertEqual(cls_step["selector"], "android.widget.EditText")

        comp_step = _build_click_step_from_inspect_result(
            comp_result,
            locator_config={"enable_resource_id": True},
        )
        self.assertEqual(comp_step["selector_type"], "composite")
        self.assertEqual(comp_step["selector"], '{"id":"com.demo:id/btn","text":"确认"}')


if __name__ == "__main__":
    unittest.main()
