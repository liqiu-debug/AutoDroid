import unittest

from backend.api.scenarios import (
    _build_scenario_summary_message,
    _determine_case_status,
    _find_last_failed_step_name,
    _summarize_cases_results,
    _summarize_scenario_raw_results,
)


class ScenarioStatusSummaryTests(unittest.TestCase):
    def test_all_skipped_is_warning(self):
        raw_results = [
            {
                "result": {
                    "success": True,
                    "steps": [
                        {"success": True, "is_skipped": True},
                        {"success": True, "is_skipped": True},
                    ],
                }
            }
        ]
        summary = _summarize_scenario_raw_results(raw_results)
        self.assertEqual(summary["status"], "WARNING")
        self.assertTrue(summary["all_skipped"])

    def test_warning_beats_pass(self):
        raw_results = [
            {
                "result": {
                    "success": True,
                    "steps": [
                        {"success": True},
                        {"success": False, "is_warning": True},
                    ],
                }
            }
        ]
        summary = _summarize_scenario_raw_results(raw_results)
        self.assertEqual(summary["status"], "WARNING")
        self.assertFalse(summary["all_skipped"])

    def test_fail_beats_warning(self):
        raw_results = [
            {
                "result": {
                    "success": False,
                    "steps": [
                        {"success": False, "is_warning": True},
                        {"success": False},
                    ],
                }
            }
        ]
        summary = _summarize_scenario_raw_results(raw_results)
        self.assertEqual(summary["status"], "FAIL")

    def test_pass_with_mix_of_pass_and_skip(self):
        raw_results = [
            {
                "result": {
                    "success": True,
                    "steps": [
                        {"success": True},
                        {"success": True, "is_skipped": True},
                    ],
                }
            }
        ]
        summary = _summarize_scenario_raw_results(raw_results)
        self.assertEqual(summary["status"], "PASS")
        self.assertFalse(summary["all_skipped"])

    def test_determine_case_status_prefers_failed_then_skipped_then_warning_then_success(self):
        self.assertEqual(
            _determine_case_status(
                [{"status": "failed"}, {"status": "success"}],
                case_success=True,
                case_is_warning=False,
            ),
            "failed",
        )
        self.assertEqual(
            _determine_case_status(
                [{"status": "skipped"}, {"status": "skipped"}],
                case_success=True,
                case_is_warning=False,
            ),
            "skipped",
        )
        self.assertEqual(
            _determine_case_status(
                [{"status": "success"}],
                case_success=True,
                case_is_warning=True,
            ),
            "warning",
        )
        self.assertEqual(
            _determine_case_status(
                [{"status": "success"}],
                case_success=True,
                case_is_warning=False,
            ),
            "success",
        )
        self.assertEqual(
            _determine_case_status(
                [],
                case_success=False,
                case_is_warning=False,
            ),
            "failed",
        )

    def test_find_last_failed_step_name_prefers_first_failed_case_and_step(self):
        cases_results = [
            {
                "alias": "case-a",
                "case_name": "case-a",
                "status": "warning",
                "steps": [{"status": "warning", "description": "ignored"}],
            },
            {
                "alias": "case-b",
                "case_name": "case-b",
                "status": "failed",
                "steps": [
                    {"status": "success", "description": "ok"},
                    {"status": "failed", "description": "点击登录"},
                ],
            },
        ]

        self.assertEqual(
            _find_last_failed_step_name(cases_results),
            "[case-b] 点击登录",
        )

    def test_find_last_failed_step_name_returns_all_skipped_message(self):
        self.assertEqual(
            _find_last_failed_step_name([], all_skipped=True),
            "全部步骤均跳过（平台不匹配或未配置）",
        )

    def test_summarize_cases_results_uses_case_statuses(self):
        cases_results = [
            {
                "alias": "case-a",
                "case_name": "case-a",
                "status": "success",
                "steps": [{"status": "success", "description": "ok"}],
            },
            {
                "alias": "case-b",
                "case_name": "case-b",
                "status": "failed",
                "steps": [{"status": "failed", "description": "点击登录"}],
            },
        ]

        summary = _summarize_cases_results(cases_results)
        self.assertEqual(summary["scenario_status"], "FAIL")
        self.assertEqual(summary["success_count"], 1)
        self.assertEqual(summary["fail_count"], 1)
        self.assertFalse(summary["all_skipped"])
        self.assertEqual(summary["last_failed_step_name"], "[case-b] 点击登录")

    def test_build_scenario_summary_message_uses_unified_format(self):
        self.assertEqual(
            _build_scenario_summary_message(
                total_duration=12.345,
                success_count=3,
                warning_count=1,
                skipped_count=2,
                fail_count=4,
            ),
            "🏁 执行结束: 总耗时 12.35s | 通过 3 | 警告 1 | 跳过 2 | 失败 4",
        )


if __name__ == "__main__":
    unittest.main()
