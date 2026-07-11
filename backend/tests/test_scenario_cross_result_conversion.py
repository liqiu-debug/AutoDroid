import unittest
from types import SimpleNamespace

from backend.scenario_results import _convert_cross_result_to_legacy_case_result


class ScenarioCrossResultConversionTests(unittest.TestCase):
    def test_preserve_step_screenshot_from_cross_result(self):
        case = SimpleNamespace(id=1)
        cross_result = {
            "success": False,
            "steps": [
                {
                    "status": "FAIL",
                    "duration": 0.5,
                    "error": "boom",
                    "screenshot": "ZmFrZS1wbmc=",
                    "step": {
                        "action": "click",
                        "description": "点按钮",
                        "error_strategy": "ABORT",
                    },
                }
            ],
        }

        result = _convert_cross_result_to_legacy_case_result(
            case=case,
            cross_result=cross_result,
            variables_map={},
        )

        self.assertFalse(result["success"])
        self.assertEqual(result["steps"][0].get("screenshot"), "ZmFrZS1wbmc=")

    def test_preserve_attempts_when_step_was_retried(self):
        case = SimpleNamespace(id=1)
        cross_result = {
            "success": True,
            "steps": [
                {
                    "status": "PASS",
                    "duration": 1.2,
                    "attempts": 3,
                    "step": {"action": "click", "error_strategy": "ABORT"},
                },
                {
                    "status": "PASS",
                    "duration": 0.3,
                    "attempts": 1,
                    "step": {"action": "back", "error_strategy": "ABORT"},
                },
            ],
        }

        result = _convert_cross_result_to_legacy_case_result(
            case=case,
            cross_result=cross_result,
            variables_map={},
        )

        self.assertEqual(result["steps"][0].get("attempts"), 3)
        # 无重试（attempts=1）不冗余携带
        self.assertNotIn("attempts", result["steps"][1])


if __name__ == "__main__":
    unittest.main()
