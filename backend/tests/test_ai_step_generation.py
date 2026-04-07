import unittest

from sqlmodel import SQLModel, Session, create_engine

from backend.api.ai import (
    GenerateStepsRequest,
    _normalize_generated_steps,
    generate_steps,
)


class AiStepGenerationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
        SQLModel.metadata.create_all(self.engine)
        self.session = Session(self.engine)

    def tearDown(self) -> None:
        self.session.close()

    def test_normalize_generated_steps_converts_legacy_locator_step_to_standard(self):
        steps = _normalize_generated_steps(
            [
                {
                    "action": "input",
                    "selector": "用户名输入框",
                    "selector_type": "text",
                    "value": "admin",
                    "description": "输入用户名",
                }
            ]
        )

        self.assertEqual(len(steps), 1)
        step = steps[0]
        self.assertEqual(step["action"], "input")
        self.assertEqual(step["args"], {"text": "admin"})
        self.assertEqual(step["value"], "admin")
        self.assertEqual(step["execute_on"], ["android", "ios"])
        self.assertEqual(
            step["platform_overrides"]["android"],
            {"selector": "用户名输入框", "by": "text"},
        )
        self.assertEqual(step["error_strategy"], "ABORT")

    def test_normalize_generated_steps_preserves_standard_extract_by_ocr_shape(self):
        steps = _normalize_generated_steps(
            [
                {
                    "action": "extract_by_ocr",
                    "args": {
                        "region": "[0.1,0.2,0.3,0.4]",
                        "extract_rule": {"mode": "contains"},
                        "output_var": "PRICE",
                    },
                    "execute_on": ["android", "ios"],
                    "description": "提取价格",
                }
            ]
        )

        self.assertEqual(len(steps), 1)
        step = steps[0]
        self.assertEqual(step["action"], "extract_by_ocr")
        self.assertEqual(step["args"]["region"], "[0.1,0.2,0.3,0.4]")
        self.assertEqual(step["args"]["output_var"], "PRICE")
        self.assertEqual(step["value"], "PRICE")
        self.assertEqual(
            step["platform_overrides"]["android"],
            {"selector": "[0.1,0.2,0.3,0.4]", "by": "text"},
        )

    def test_normalize_generated_steps_assert_text_uses_global_match_mode(self):
        steps = _normalize_generated_steps(
            [
                {
                    "action": "assert_text",
                    "selector": "状态提示",
                    "selector_type": "text",
                    "value": "登录成功",
                    "args": {"match_mode": "不包含"},
                    "platform_overrides": {
                        "android": {"selector": "状态提示", "by": "text"},
                    },
                    "description": "校验文案",
                }
            ]
        )

        self.assertEqual(len(steps), 1)
        step = steps[0]
        self.assertEqual(step["action"], "assert_text")
        self.assertEqual(
            step["args"],
            {"expected_text": "登录成功", "match_mode": "not_contains"},
        )
        self.assertEqual(step["value"], "登录成功")
        self.assertEqual(step["platform_overrides"], {})

    def test_normalize_generated_steps_assert_image_accepts_chinese_match_mode(self):
        steps = _normalize_generated_steps(
            [
                {
                    "action": "assert_image",
                    "args": {
                        "image_path": "static/images/sample.png",
                        "match_mode": "不存在",
                    },
                    "description": "校验图片不存在",
                }
            ]
        )

        self.assertEqual(len(steps), 1)
        step = steps[0]
        self.assertEqual(step["action"], "assert_image")
        self.assertEqual(
            step["args"],
            {"image_path": "static/images/sample.png", "match_mode": "not_exists"},
        )
        self.assertEqual(
            step["platform_overrides"]["android"],
            {"selector": "static/images/sample.png", "by": "image"},
        )

    async def test_generate_steps_mock_returns_standard_step_payloads(self):
        response = await generate_steps(
            GenerateStepsRequest(text="点击登录按钮，输入用户名 admin 和密码 123456，然后点击提交"),
            session=self.session,
        )

        self.assertTrue(response.success)
        self.assertGreater(len(response.data), 0)
        first = response.data[0]
        self.assertIn("args", first)
        self.assertIn("execute_on", first)
        self.assertIn("platform_overrides", first)
        self.assertNotIn("selector", first)
        self.assertEqual(first["execute_on"], ["android", "ios"])
        self.assertEqual(first["platform_overrides"]["android"]["by"], "text")


if __name__ == "__main__":
    unittest.main()
