"""AI 连接测试接口与 LLM 总时长封顶的行为测试。

背景：httpx 的 timeout 是逐次网络读之间的超时，上游慢速滴流时总耗时无上限；
Cloudflare 等反代约 100s 会掐断回源连接并回错误页（前端只能看到「响应无效/
不完整」）。因此所有同步等 LLM 的调用都必须有总时长封顶，且「测试 AI 连接」
走轻量探针，保证在反代窗口内返回干净 JSON。
"""

import asyncio
import unittest
from unittest.mock import patch

from fastapi import HTTPException

from backend.api.ai import TestConnectionRequest, test_ai_connection
from backend.api.log_analysis import call_llm_service
from backend.api_testing.ai_service import call_model


class _FakeResponse:
    def __init__(self, status_code=200, text='{"choices":[{"message":{"content":"pong"}}],"usage":{"total_tokens":3}}'):
        self.status_code = status_code
        self.text = text
        self.content = text.encode("utf-8")


class _FakeAsyncClient:
    last_url = None
    last_json = None
    last_headers = None
    response = None

    def __init__(self, *args, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def post(self, url, json=None, headers=None):
        type(self).last_url = url
        type(self).last_json = json
        type(self).last_headers = headers
        return type(self).response or _FakeResponse()


class _SlowAsyncClient(_FakeAsyncClient):
    delay = 0.5

    async def post(self, url, json=None, headers=None):
        await asyncio.sleep(type(self).delay)
        return type(self).response or _FakeResponse()


class TestConnectionApiTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        _FakeAsyncClient.last_url = None
        _FakeAsyncClient.last_json = None
        _FakeAsyncClient.last_headers = None
        _FakeAsyncClient.response = None

    async def test_success_uses_request_values_without_hitting_db(self):
        with patch(
            "backend.api.ai._get_setting",
            side_effect=AssertionError("表单已给全值，不应读库"),
        ) as get_mock, patch("httpx.AsyncClient", _FakeAsyncClient):
            resp = await test_ai_connection(
                TestConnectionRequest(
                    api_key="form-key",
                    api_base="https://llm.example/v1/",
                    model="form-model",
                ),
                session=object(),
            )

        self.assertTrue(resp.success)
        self.assertEqual(resp.model, "form-model")
        self.assertGreaterEqual(resp.latency_ms, 0)
        self.assertEqual(
            _FakeAsyncClient.last_url,
            "https://llm.example/v1/chat/completions",
        )
        self.assertEqual(_FakeAsyncClient.last_headers["Authorization"], "Bearer form-key")
        self.assertFalse(_FakeAsyncClient.last_json["stream"])
        self.assertLessEqual(_FakeAsyncClient.last_json["max_tokens"], 8)
        get_mock.assert_not_called()

    async def test_falls_back_to_saved_settings(self):
        with patch(
            "backend.api.ai._get_setting",
            side_effect=["saved-key", "https://api.openai.com/v1", "gpt-4o-mini"],
        ) as get_mock, patch("httpx.AsyncClient", _FakeAsyncClient):
            resp = await test_ai_connection(None, session=object())

        self.assertTrue(resp.success)
        self.assertEqual(resp.model, "gpt-4o-mini")
        self.assertEqual(
            _FakeAsyncClient.last_headers["Authorization"],
            "Bearer saved-key",
        )
        self.assertEqual(get_mock.call_count, 3)

    async def test_missing_api_key_returns_400(self):
        with patch("backend.api.ai._get_setting", return_value=""):
            with self.assertRaises(HTTPException) as ctx:
                await test_ai_connection(None, session=object())
        self.assertEqual(ctx.exception.status_code, 400)

    async def test_upstream_http_error_maps_to_502(self):
        _FakeAsyncClient.response = _FakeResponse(
            status_code=401,
            text='{"error":{"message":"bad key"}}',
        )
        with patch(
            "backend.api.ai._get_setting",
            side_effect=["k", "https://x/v1", "m"],
        ), patch("httpx.AsyncClient", _FakeAsyncClient):
            with self.assertRaises(HTTPException) as ctx:
                await test_ai_connection(None, session=object())

        self.assertEqual(ctx.exception.status_code, 502)
        self.assertIn("401", ctx.exception.detail)

    async def test_total_deadline_maps_to_504(self):
        with patch(
            "backend.api.ai._get_setting",
            side_effect=["k", "https://x/v1", "m"],
        ), patch("backend.api.ai.TEST_CONNECTION_DEADLINE_S", 0.05), patch(
            "httpx.AsyncClient", _SlowAsyncClient
        ):
            with self.assertRaises(HTTPException) as ctx:
                await test_ai_connection(None, session=object())

        self.assertEqual(ctx.exception.status_code, 504)


class LlmDeadlineTests(unittest.IsolatedAsyncioTestCase):
    async def test_call_llm_service_total_deadline_maps_to_504(self):
        with patch(
            "backend.api.log_analysis._get_setting",
            side_effect=["k", "https://x/v1", "m"],
        ), patch("backend.api.log_analysis.LLM_TOTAL_DEADLINE_S", 0.05), patch(
            "httpx.AsyncClient", _SlowAsyncClient
        ):
            with self.assertRaises(HTTPException) as ctx:
                await call_llm_service(
                    cleaned_log="FATAL EXCEPTION: main",
                    package_name="com.test.app",
                    device_info="device-1",
                    session=object(),
                )

        self.assertEqual(ctx.exception.status_code, 504)

    async def test_call_model_total_deadline_maps_to_504(self):
        with patch("backend.api_testing.ai_service.LLM_TOTAL_DEADLINE_S", 0.05), patch(
            "httpx.AsyncClient", _SlowAsyncClient
        ):
            with self.assertRaises(HTTPException) as ctx:
                await call_model(
                    {"model": "m", "base": "https://x/v1", "key": "k"},
                    "failures",
                    {},
                    {"type": "object"},
                )

        self.assertEqual(ctx.exception.status_code, 504)


if __name__ == "__main__":
    unittest.main()
