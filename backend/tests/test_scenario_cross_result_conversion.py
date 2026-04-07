import unittest
from types import SimpleNamespace

from backend.api.scenarios import _convert_cross_result_to_legacy_case_result


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


if __name__ == "__main__":
    unittest.main()
