import json
import unittest
from typing import List
from unittest.mock import AsyncMock, patch

from backend.api.log_analysis import (
    _analysis_cache,
    analyze_log,
    call_llm_service,
    clean_log_for_ai,
)
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

    def test_clean_log_for_ai_keeps_business_context_around_the_anchor(self):
        """崩溃正文之外要带上「被测 App 当时在干什么」，不管它排在锚点前还是后。

        只取锚点之后的话这些行全丢，LLM 拿到的就是一个孤立的栈，指不出触发页面。
        """
        log = "\n".join(
            [
                "03-27 11:59:58.000  1974  2100 I ActivityManager: Start proc 12345:com.ehaier.zgq.shop.mall/u0a123 for activity com.ehaier.mall.MainActivity",
                "03-27 11:59:59.000 2100 2100 D SomeVendorNoise: transport layer chatter one",
                "03-27 11:59:59.100 2100 2100 D SomeVendorNoise: transport layer chatter two",
                "03-27 11:59:59.200 2100 2100 D ViewRootImpl: dispatchKeyEvent KEYCODE_DPAD_RIGHT",
            ]
            + CRASH_LOG.splitlines()
        )

        cleaned = clean_log_for_ai(log, PACKAGE_NAME)

        self.assertIn("Start proc 12345:com.ehaier.zgq.shop.mall", cleaned)
        self.assertIn("dispatchKeyEvent KEYCODE_DPAD_RIGHT", cleaned)
        # 厂商噪音既不带包名也不带生命周期事件，不该占预算
        self.assertNotIn("transport layer chatter", cleaned)
        # 正文不受影响
        self.assertIn("FATAL EXCEPTION: main", cleaned)
        self.assertIn("MainActivity.onCreate", cleaned)

    def test_clean_log_for_ai_collapses_repeated_same_shape_lines(self):
        """同一句话按帧刷屏时按形态收敛，不能把预算全吃掉。

        厂商真会这么干（实测 isLandscapeFullscreenEnabled 连刷十几次），
        按原文去重拦不住 —— 时间戳每次都不同。
        """
        spam = [
            f"03-27 11:59:{index:02d}.000  1974  2100 D ActivityTaskManager: "
            f"The isLandscapeFullscreenEnabled is: {PACKAGE_NAME}/com.ehaier.mall.MainActivity"
            for index in range(30)
        ]
        log = "\n".join(spam + CRASH_LOG.splitlines())

        cleaned = clean_log_for_ai(log, PACKAGE_NAME)

        shape_hits = sum(1 for line in cleaned.splitlines() if "isLandscapeFullscreenEnabled" in line)
        self.assertLessEqual(shape_hits, 2)
        self.assertGreater(shape_hits, 0)
        self.assertIn("MainActivity.onCreate", cleaned)

    def test_clean_log_for_ai_never_trades_the_stack_for_context(self):
        """上下文再多也不能挤掉崩溃正文，总行数仍不超 100。

        正文本身必须超过 100 行才压得到预算 —— 正文只有几行的话，上下文再满也不会
        把正文挤掉，断言就成了恒真的。
        """
        long_stack = [
            f"03-27 12:00:00.000 12345 12345 E AndroidRuntime:     at {PACKAGE_NAME}."
            f"{'Abcdefghij' * 3}{index}(MainActivity.kt:{index})"
            for index in range(120)
        ]
        spam = [
            f"03-27 11:59:{index % 60:02d}.{index % 1000:03d}  1974  2100 I ActivityManager: "
            f"Displayed {PACKAGE_NAME}/com.ehaier.mall.MainActivity"
            f"{chr(65 + index // 26)}{chr(65 + index % 26)}: +{index}ms"
            for index in range(200)
        ]
        log = "\n".join(spam + CRASH_LOG.splitlines()[:3] + long_stack)

        cleaned = clean_log_for_ai(log, PACKAGE_NAME)
        lines = cleaned.splitlines()

        self.assertLessEqual(len(lines), 100)
        # 正文已满 100 行时，上下文额度为 0 —— 一条上下文都不该挤进来
        self.assertFalse(any("Displayed " in line for line in lines))
        self.assertIn("FATAL EXCEPTION: main", cleaned)
        self.assertIn("java.lang.NullPointerException", cleaned)

    def test_clean_log_for_ai_picks_lines_nearest_the_event_in_time(self):
        """必须按时间远近挑，不能按行号远近 —— 新快照布局下行号跟时间不是一回事。

        生产快照是「正文段在前、上下文段在后」（见 fastbot/logcat.py 的
        _compose_snapshot），上下文段里行号越大 = 时间越晚，而锚点在正文段里。
        按行号算会挑到上下文段最旧的一端，把事件前最近的行全丢。
        """
        far_before = [
            f"03-27 11:59:00.{index:03d}  1974  2100 I ActivityManager: "
            f"Displayed {PACKAGE_NAME}/far.before."
            f"{chr(65 + index // 26)}{chr(65 + index % 26)}"
            for index in range(30)
        ]
        near_before = [
            f"03-27 11:59:59.{index:03d}  1974  2100 I ActivityManager: "
            f"Displayed {PACKAGE_NAME}/near.before.{index}"
            for index in range(3)
        ]
        near_after = [
            f"03-27 12:00:00.{index:03d}  1974  2100 I ActivityManager: "
            f"Displayed {PACKAGE_NAME}/near.after.{index}"
            for index in range(3)
        ]
        far_after = [
            f"03-27 12:00:30.{index:03d}  1974  2100 I ActivityManager: "
            f"Displayed {PACKAGE_NAME}/far.after."
            f"{chr(65 + index // 26)}{chr(65 + index % 26)}"
            for index in range(30)
        ]
        # 崩溃正文段在前，上下文段在后，两段之间是 _compose_snapshot 的分隔行
        log = "\n".join(
            ["----- logcat crash buffer -----"]
            + CRASH_LOG.splitlines()
            + ["----- logcat context (main,system) -----"]
            + far_before
            + near_before
            + near_after
            + far_after
        )

        cleaned = clean_log_for_ai(log, PACKAGE_NAME)

        # 离事件最近的几行必须留，不管它们排在锚点后多远
        for expected in ("near.before.0", "near.before.1", "near.before.2",
                         "near.after.0", "near.after.1", "near.after.2"):
            self.assertIn(expected, cleaned, expected)
        # 离事件最远的那一端不占预算
        self.assertLessEqual(sum(1 for line in cleaned.splitlines() if "far.before." in line), 12)
        self.assertLessEqual(len(cleaned.splitlines()), 100)
        # 正文不受影响
        self.assertIn("FATAL EXCEPTION: main", cleaned)
        self.assertIn("MainActivity.onCreate", cleaned)

    def test_clean_log_for_ai_anr_path_also_keeps_context(self):
        """ANR 与崩溃走同一套上下文口径。"""
        log = "\n".join(
            [
                "03-27 11:06:30.000  1974  2100 I ActivityManager: "
                "Start proc 31955:com.ehaier.zgq.shop.mall/u0a123 for activity com.ehaier.mall.MainActivity",
            ]
            + ANR_LOG.splitlines()
        )

        cleaned = clean_log_for_ai(log, PACKAGE_NAME)

        self.assertIn("Start proc 31955:com.ehaier.zgq.shop.mall", cleaned)
        self.assertIn("ANR in com.ehaier.zgq.shop.mall", cleaned)
        self.assertLessEqual(len(cleaned.splitlines()), 100)


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

    async def test_call_llm_service_accepts_sse_response(self):
        class _FakeStreamingResponse:
            status_code = 200
            text = "\n".join(
                [
                    'data: {"id":"chatcmpl-1","object":"chat.completion.chunk","choices":[{"index":0,"delta":{"role":"assistant","content":"定位到"}}]}',
                    'data: {"id":"chatcmpl-1","object":"chat.completion.chunk","choices":[{"index":0,"delta":{"content":"主线程阻塞"}}],"usage":{"total_tokens":56}}',
                    "data: [DONE]",
                ]
            )

            def json(self):
                raise json.JSONDecodeError("Expecting value", self.text, 0)

        class _FakeStreamingAsyncClient:
            last_json = None
            last_headers = None

            def __init__(self, *args, **kwargs):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, exc_type, exc, tb):
                return False

            async def post(self, url, json=None, headers=None):
                type(self).last_json = json
                type(self).last_headers = headers
                return _FakeStreamingResponse()

        with patch(
            "backend.api.log_analysis._get_setting",
            side_effect=["test-key", "https://api.openai.com/v1", "gpt-4o-mini"],
        ), patch("httpx.AsyncClient", _FakeStreamingAsyncClient):
            result = await call_llm_service(
                cleaned_log=CRASH_LOG,
                package_name=PACKAGE_NAME,
                device_info="device-1",
                session=object(),
            )

        self.assertTrue(result["success"])
        self.assertEqual(result["analysis_result"], "定位到主线程阻塞")
        self.assertEqual(result["token_usage"], 56)
        self.assertFalse(_FakeStreamingAsyncClient.last_json["stream"])
        self.assertEqual(
            _FakeStreamingAsyncClient.last_headers["Accept"],
            "application/json",
        )


if __name__ == "__main__":
    unittest.main()
