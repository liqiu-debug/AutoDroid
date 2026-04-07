import unittest

from backend.api.cases import _summarize_case_result


class CaseStatusSummaryTests(unittest.TestCase):
    def test_all_skipped_is_warning(self):
        result = {
            "success": True,
            "steps": [
                {"status": "SKIP"},
                {"status": "SKIP"},
            ],
        }
        summary = _summarize_case_result(result)
        self.assertEqual(summary["status"], "WARNING")
        self.assertTrue(summary["all_skipped"])

    def test_warning_beats_pass(self):
        result = {
            "success": True,
            "steps": [
                {"status": "PASS"},
                {"status": "WARNING"},
            ],
        }
        summary = _summarize_case_result(result)
        self.assertEqual(summary["status"], "WARNING")
        self.assertFalse(summary["all_skipped"])

    def test_fail_beats_warning(self):
        result = {
            "success": False,
            "steps": [
                {"status": "WARNING"},
                {"status": "FAIL"},
            ],
        }
        summary = _summarize_case_result(result)
        self.assertEqual(summary["status"], "FAIL")

    def test_legacy_ignore_failure_maps_to_warning(self):
        result = {
            "success": True,
            "steps": [
                {"success": True},
                {"success": False, "is_warning": True},
            ],
        }
        summary = _summarize_case_result(result)
        self.assertEqual(summary["status"], "WARNING")


if __name__ == "__main__":
    unittest.main()
