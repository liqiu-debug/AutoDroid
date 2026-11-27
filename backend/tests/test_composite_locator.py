import json
import os
import sys
import unittest
from types import SimpleNamespace
from unittest.mock import patch

BACKEND_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
ROOT_DIR = os.path.abspath(os.path.join(BACKEND_DIR, ".."))
if ROOT_DIR not in sys.path:
    sys.path.insert(0, ROOT_DIR)

from backend.cross_platform_execution import precheck_steps_for_platform, render_with_variables
from backend.drivers.android_driver import AndroidDriver
from backend.drivers.ios.locator import IOSLocatorMixin
from backend.feature_flags import (
    FLAG_RECORDING_ENABLE_CLASS_NAME,
    FLAG_RECORDING_ENABLE_RESOURCE_ID,
    RECORDING_PRIORITY_ID_FIRST,
    RECORDING_PRIORITY_SEMANTIC_FIRST,
    _FLAG_DEFAULTS,
    normalize_recording_locator_priority,
    parse_recording_locator_config,
)
from backend.utils.composite_locator import (
    ALLOWED_COMPOSITE_KEYS,
    build_ios_predicate_from_composite,
    extract_semantic_fallback,
    normalize_composite_conditions,
    normalize_composite_selector,
    parse_composite_selector,
    render_composite_selector,
    serialize_composite_selector,
)


class _DummyIOSDriver(IOSLocatorMixin):
    def __init__(self, session_fn):
        self.client = SimpleNamespace(session=lambda: session_fn)
        self.platform = "ios"


class CompositeLocatorUtilsTests(unittest.TestCase):
    def test_allowed_keys_and_aliases_canonicalize_sorted_json(self):
        raw = json.dumps({"resourceId": "com.demo:id/btn", "text": "确定"})
        parsed = parse_composite_selector(raw)
        self.assertEqual(parsed, {"id": "com.demo:id/btn", "text": "确定"})
        self.assertEqual(
            normalize_composite_selector(raw),
            '{"id":"com.demo:id/btn","text":"确定"}',
        )
        self.assertIn("id", ALLOWED_COMPOSITE_KEYS)
        self.assertIn("class_name", ALLOWED_COMPOSITE_KEYS)

    def test_normalize_rejects_empty_unknown_keys_and_invalid_json(self):
        with self.assertRaises(ValueError):
            parse_composite_selector("")
        with self.assertRaises(ValueError):
            parse_composite_selector("{not-json")
        with self.assertRaises(ValueError):
            parse_composite_selector("{}")
        with self.assertRaises(ValueError):
            normalize_composite_conditions({"unknown_attr": "x"})
        with self.assertRaises(ValueError):
            serialize_composite_selector({"text": "   "})

    def test_render_composite_selector_escapes_quotes_and_backslashes(self):
        template = '{"id":"com.demo:id/btn","text":"{{LABEL}}"}'
        render_fn = lambda s: str(s).replace("{{LABEL}}", '确认 "A\\B"')
        rendered = render_composite_selector(template, render_fn)
        parsed = json.loads(rendered)
        self.assertEqual(parsed, {"id": "com.demo:id/btn", "text": '确认 "A\\B"'})

        # render_with_variables 对 {selector, by: "composite"} 字典应安全替换
        step_dict = {
            "platform_overrides": {
                "android": {"by": "composite", "selector": template}
            }
        }
        rendered_dict = render_with_variables(step_dict, {"LABEL": '含"引号'})
        parsed_step_selector = json.loads(
            rendered_dict["platform_overrides"]["android"]["selector"]
        )
        self.assertEqual(parsed_step_selector["text"], '含"引号')

    def test_extract_semantic_fallback_and_ios_predicate(self):
        self.assertEqual(
            extract_semantic_fallback('{"id":"com.demo:id/btn","text":"提交"}'),
            ("text", "提交"),
        )
        self.assertEqual(
            extract_semantic_fallback('{"id":"com.demo:id/icon","desc":"更多"}'),
            ("description", "更多"),
        )
        self.assertIsNone(
            extract_semantic_fallback('{"id":"com.demo:id/icon","class_name":"android.widget.ImageView"}')
        )

        predicate = build_ios_predicate_from_composite(
            parse_composite_selector('{"id":"login_btn","text":"登录","desc":"确认登录"}')
        )
        self.assertIn("name == 'login_btn'", predicate)
        self.assertIn("label == '登录'", predicate)
        self.assertIn(" AND ", predicate)

        contains_predicate = build_ios_predicate_from_composite(
            parse_composite_selector('{"id":"row_1","text":"共 3 件"}'),
            text_contains=True,
        )
        self.assertIn("label CONTAINS[c] '共 3 件'", contains_predicate)
        self.assertIn("name == 'row_1'", contains_predicate)


class RecordingLocatorConfigTests(unittest.TestCase):
    def test_defaults_keep_id_and_class_disabled(self):
        self.assertFalse(_FLAG_DEFAULTS[FLAG_RECORDING_ENABLE_RESOURCE_ID])
        self.assertFalse(_FLAG_DEFAULTS[FLAG_RECORDING_ENABLE_CLASS_NAME])
        self.assertEqual(
            normalize_recording_locator_priority(None),
            RECORDING_PRIORITY_SEMANTIC_FIRST,
        )
        self.assertEqual(
            normalize_recording_locator_priority("id_first"),
            RECORDING_PRIORITY_ID_FIRST,
        )
        cfg = parse_recording_locator_config({})
        self.assertEqual(
            cfg,
            {
                "enable_resource_id": False,
                "enable_class_name": False,
                "priority": RECORDING_PRIORITY_SEMANTIC_FIRST,
            },
        )


class PrecheckCompositeTests(unittest.TestCase):
    def test_precheck_accepts_valid_and_rejects_invalid_composite(self):
        valid_steps = [
            {
                "action": "click",
                "args": {},
                "platform_overrides": {
                    "android": {
                        "by": "composite",
                        "selector": '{"id":"com.demo:id/btn","text":"确认"}',
                    }
                },
            },
            {
                "action": "input",
                "args": {"text": "abc"},
                "platform_overrides": {
                    "android": {
                        "by": "class_name",
                        "selector": "android.widget.EditText",
                    }
                },
            },
        ]
        with patch("backend.cross_platform_execution.load_app_key_mapping", return_value={}):
            ok_res = precheck_steps_for_platform(None, valid_steps, platform="android")
            self.assertEqual([item["status"] for item in ok_res], ["PASS", "PASS"])

            bad_steps = [
                {
                    "action": "click",
                    "args": {},
                    "platform_overrides": {
                        "android": {"by": "composite", "selector": "{bad-json"}
                    },
                }
            ]
            bad_res = precheck_steps_for_platform(None, bad_steps, platform="android")
        self.assertEqual(len(bad_res), 1)
        self.assertEqual(bad_res[0]["status"], "FAIL")
        self.assertEqual(bad_res[0]["code"], "P1006_INVALID_ARGS")


class DriverCompositeExecutionTests(unittest.TestCase):
    def test_android_driver_find_element_with_class_name_and_composite_fallback(self):
        calls = []

        class _FakeNode:
            def __init__(self, ok):
                self._ok = ok

            def exists(self, timeout=2.0):
                return self._ok

        def _fake_device(**kwargs):
            calls.append(kwargs)
            if "text" in kwargs:
                return _FakeNode(False)
            if kwargs.get("resourceId") == "com.demo:id/item" and kwargs.get("textContains") == "立即购买":
                return _FakeNode(True)
            if kwargs.get("className") == "android.widget.EditText":
                return _FakeNode(True)
            return _FakeNode(False)

        driver = object.__new__(AndroidDriver)
        driver._device = _fake_device

        cls_el = driver._find_element("android.widget.EditText", by="class_name")
        self.assertIsNotNone(cls_el)
        self.assertEqual(calls[0], {"className": "android.widget.EditText"})

        calls.clear()
        comp_el = driver._find_element(
            '{"id":"com.demo:id/item","text":"立即购买"}',
            by="composite",
            text_probe_timeout=0.1,
        )
        self.assertIsNotNone(comp_el)
        self.assertEqual(calls[0], {"resourceId": "com.demo:id/item", "text": "立即购买"})
        self.assertEqual(calls[1], {"resourceId": "com.demo:id/item", "textContains": "立即购买"})

    def test_ios_driver_builds_composite_selector_and_contains_fallback(self):
        captured_calls = []

        def _fake_session(**kwargs):
            captured_calls.append(kwargs)
            return SimpleNamespace(kwargs=kwargs)

        driver = _DummyIOSDriver(_fake_session)
        driver._build_selector('{"id":"confirm_btn","text":"确认"}', "composite")
        self.assertEqual(len(captured_calls), 1)
        self.assertIn("predicate", captured_calls[0])
        self.assertIn("name == 'confirm_btn'", captured_calls[0]["predicate"])
        self.assertIn("label == '确认'", captured_calls[0]["predicate"])

        specs = driver._build_fallback_locator_specs(
            '{"id":"confirm_btn","text":"确认下单"}',
            "composite",
        )
        self.assertEqual(len(specs), 1)
        self.assertEqual(specs[0][0], "predicate")
        self.assertIn("label CONTAINS[c] '确认下单'", specs[0][1])
        self.assertIn("name == 'confirm_btn'", specs[0][1])


if __name__ == "__main__":
    unittest.main()
