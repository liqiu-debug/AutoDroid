import unittest
from typing import List
from unittest.mock import AsyncMock, patch

from backend.api.log_analysis import _analysis_cache, analyze_log, clean_log_for_ai
from backend.schemas import LogAnalysisRequest


PACKAGE_NAME = "com.ehaier.zgq.shop.mall"


def _build_anr_noise_lines(count: int) -> List[str]:
    return [
        f"03-27 11:06:{index:02d}.000  1977 12961 E ActivityManager: filler line {index}"
        for index in range(count)
    ]


ANR_LOG = "\n".join(
    [
        "03-27 11:06:34.992   769   769 I tombstoned: received crash request for pid 25717",
        "03-27 11:06:34.995 25717 25734 I com.oplus.nas: Wrote stack traces to tombstoned",
        "03-27 11:06:35.267  1977 12961 E ActivityManager: ANR in com.ehaier.zgq.shop.mall (com.ehaier.zgq.shop.mall/com.ehaier.mall.MainActivity)",
        "03-27 11:06:35.267  1977 12961 E ActivityManager: PID: 31955",
        "03-27 11:06:35.267  1977 12961 E ActivityManager: Reason: Input dispatching timed out (MainActivity is not responding)",
        "03-27 11:06:35.267  1977 12961 E ActivityManager: Parent: com.ehaier.zgq.shop.mall/com.ehaier.mall.MainActivity",
        "03-27 11:06:35.267  1977 12961 E ActivityManager: ----- Output from /proc/pressure/cpu -----",
        "03-27 11:06:35.267  1977 12961 E ActivityManager: some avg10=5.11 avg60=8.38 avg300=10.88 total=26580900032",
        "03-27 11:06:35.267  1977 12961 E ActivityManager: ----- End output from /proc/pressure/cpu -----",
        "03-27 11:06:35.267  1977 12961 E ActivityManager: CPU usage from 273467ms to -1ms ago",
        "03-27 11:06:35.267  1977 12961 E ActivityManager:   135% 31955/com.ehaier.zgq.shop.mall: 116% user + 19% kernel",
    ]
    + _build_anr_noise_lines(120)
    + [
        "03-27 11:06:35.275 27017 27042 E [Fastbot]: *** ERROR *** ANR in com.ehaier.zgq.shop.mall (com.ehaier.zgq.shop.mall/com.ehaier.mall.MainActivity)",
        "03-27 11:06:35.275 27017 27042 E [Fastbot]: PID: 31955",
        "03-27 11:06:35.275 27017 27042 E [Fastbot]: Reason: Input dispatching timed out (MainActivity is not responding)",
        "03-27 11:06:35.275 27017 27042 E [Fastbot]: Parent: com.ehaier.zgq.shop.mall/com.ehaier.mall.MainActivity",
        "03-27 11:06:35.275 27017 27042 E [Fastbot]: CPU usage from 57ms to 572ms later",
        "03-27 11:06:35.275 27017 27042 E [Fastbot]:   221% 31955/com.ehaier.zgq.shop.mall: 169% user + 51% kernel",
        "03-27 11:06:35.300 25571 13002 D LogKit_AnrCrashDumpRunnable: The log switch is turned on, exceptionInfo = ExceptionInfo{anrInfo=AnrInfo{reason='Input Dispatching Timeout', message='Input dispatching timed out', stackTrace='  at com.ehaier.mall.MainActivity.onKeyDown(MainActivity.kt:42)",
        "03-27 11:06:35.300 25571 13002 D LogKit_AnrCrashDumpRunnable:   at androidx.appcompat.app.AppCompatDelegateImpl.dispatchKeyEvent(AppCompatDelegateImpl.java:1)",
        "03-27 11:06:35.300 25571 13002 D LogKit_AnrCrashDumpRunnable:   at android.app.ActivityThread.main(ActivityThread.java:9964)",
        "03-27 11:06:35.306  1977 13071 D DropBoxManagerService: file :: /data/system/dropbox/data_app_anr@1774580795305.txt.gz",
    ]
)


CRASH_LOG = "\n".join(
    [
        "03-27 12:00:00.000 12345 12345 E AndroidRuntime: FATAL EXCEPTION: main",
        f"03-27 12:00:00.000 12345 12345 E AndroidRuntime: Process: {PACKAGE_NAME}, PID: 12345",
        "03-27 12:00:00.000 12345 12345 E AndroidRuntime: java.lang.NullPointerException: boom",
        "03-27 12:00:00.000 12345 12345 E AndroidRuntime:     at com.ehaier.mall.MainActivity.onCreate(MainActivity.kt:42)",
        "03-27 12:00:00.000 12345 12345 E AndroidRuntime:     at android.app.Activity.performCreate(Activity.java:1)",
    ]
)


class CleanLogForAiTests(unittest.TestCase):
    def test_clean_log_for_ai_keeps_anr_anchor_and_stack(self):
        cleaned = clean_log_for_ai(ANR_LOG, PACKAGE_NAME)

        self.assertIn("ANR in com.ehaier.zgq.shop.mall", cleaned)
        self.assertIn("Reason: Input dispatching timed out", cleaned)
        self.assertIn("LogKit_AnrCrashDumpRunnable", cleaned)
        self.assertIn("ActivityThread.main", cleaned)
        self.assertIn("data_app_anr", cleaned)
        self.assertNotIn("tombstoned: received crash request", cleaned)
        self.assertLessEqual(len(cleaned.splitlines()), 100)

    def test_clean_log_for_ai_keeps_fatal_exception_block(self):
        cleaned = clean_log_for_ai(CRASH_LOG, PACKAGE_NAME)

        self.assertIn("FATAL EXCEPTION: main", cleaned)
        self.assertIn(f"Process: {PACKAGE_NAME}", cleaned)
        self.assertIn("java.lang.NullPointerException", cleaned)
        self.assertIn("MainActivity.onCreate", cleaned)


class AnalyzeLogApiTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        _analysis_cache.clear()

    async def test_analyze_log_uses_cache_and_force_refresh(self):
        req = LogAnalysisRequest(
            log_text=CRASH_LOG,
            package_name=PACKAGE_NAME,
            device_info="device-1",
        )

        with patch(
            "backend.api.log_analysis.call_llm_service",
            new=AsyncMock(
                side_effect=[
                    {"success": True, "analysis_result": "first", "token_usage": 11},
                    {"success": True, "analysis_result": "second", "token_usage": 22},
                ]
            ),
        ) as llm_mock:
            first = await analyze_log(req, session=object())
            second = await analyze_log(req, session=object())
            third = await analyze_log(
                LogAnalysisRequest(
                    log_text=CRASH_LOG,
                    package_name=PACKAGE_NAME,
                    device_info="device-1",
                    force_refresh=True,
                ),
                session=object(),
            )

        self.assertTrue(first.success)
        self.assertFalse(first.cached)
        self.assertEqual(first.analysis_result, "first")

        self.assertTrue(second.success)
        self.assertTrue(second.cached)
        self.assertEqual(second.analysis_result, "first")

        self.assertTrue(third.success)
        self.assertFalse(third.cached)
        self.assertEqual(third.analysis_result, "second")

        self.assertEqual(llm_mock.await_count, 2)


if __name__ == "__main__":
    unittest.main()
