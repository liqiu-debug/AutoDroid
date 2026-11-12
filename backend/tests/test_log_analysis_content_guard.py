"""AI 日志分析的正文守卫与缓存自愈测试。

背景（已实测复现）：POST /api/fastbot/analyze_log 会间歇性返回**纯文本** 500
「Internal Server Error」。链路是——上游思考型模型（Qwen 等）把 max_tokens 预算
耗在 reasoning 上时，`choices[0].message.content` 返回 null 或分片数组；纯 JSON
分支原样透传，`LogAnalysisResponse.analysis_result`（str）在 pydantic 校验时抛
ValidationError，而这一步没有 try，于是被 Starlette 兜底成纯文本 500。更糟的是
缓存写在响应构造之前，一次失败会把 None 写进缓存，这条日志从此永久分析不了。

所以这里锁四件事：
1. 纯 JSON 分支与 SSE 分支同样把 content 归一化成 str（且空内容不抛异常，
   连接探针 max_tokens=4 依赖「空内容也算连通」）；
2. analyze_log 拿到非空字符串以外的正文一律 502，绝不放行到响应模型；
3. 失败不污染缓存，脏缓存值能自愈；
4. usage 为 null 不再 AttributeError。

全部离线：LLM 调用与 httpx 一律替身，绝不联网。
"""

import asyncio
import hashlib
import json
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import HTTPException

from backend import jank_ai_service
from backend.api import ai as ai_api
from backend.api.log_analysis import (
    EMPTY_CONTENT_ERROR_DETAIL,
    _analysis_cache,
    analyze_log,
    call_llm_service,
    clean_log_for_ai,
)
from backend.jank_ai_service import summarize_jank_analysis
from backend.openai_compat import extract_message_content, parse_chat_completion_payload
from backend.schemas import LogAnalysisRequest

PACKAGE_NAME = "com.ehaier.zgq.shop.mall"

CRASH_LOG = "\n".join(
    [
        "03-27 12:00:00.000 12345 12345 E AndroidRuntime: FATAL EXCEPTION: main",
        f"03-27 12:00:00.000 12345 12345 E AndroidRuntime: Process: {PACKAGE_NAME}, PID: 12345",
        "03-27 12:00:00.000 12345 12345 E AndroidRuntime: java.lang.NullPointerException: boom",
        "03-27 12:00:00.000 12345 12345 E AndroidRuntime:     at com.ehaier.mall.MainActivity.onCreate(MainActivity.kt:42)",
        "03-27 12:00:00.000 12345 12345 E AndroidRuntime:     at android.app.Activity.performCreate(Activity.java:1)",
    ]
)


def _completion_json(content) -> str:
    """构造一份标准 JSON completion，content 由调用方决定形状。"""
    return json.dumps(
        {
            "choices": [{"message": {"role": "assistant", "content": content}}],
            "usage": {"total_tokens": 7},
        },
        ensure_ascii=False,
    )


class _FakeResponse:
    def __init__(self, text, status_code=200):
        self.status_code = status_code
        self.text = text


class _FakeAsyncClient:
    """httpx.AsyncClient 替身：返回类属性上的预置响应，不发起任何网络请求。"""

    response = None
    call_count = 0
    last_json = None

    def __init__(self, *args, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def post(self, url, json=None, headers=None):
        type(self).call_count += 1
        type(self).last_json = json
        return type(self).response


@pytest.fixture(autouse=True)
def _reset_state():
    """每个用例都从干净的缓存/替身状态开始，避免用例间互相污染。"""
    _analysis_cache.clear()
    jank_ai_service._jank_ai_cache.clear()
    _FakeAsyncClient.response = None
    _FakeAsyncClient.call_count = 0
    _FakeAsyncClient.last_json = None
    yield
    _analysis_cache.clear()
    jank_ai_service._jank_ai_cache.clear()


def _run_analyze(force_refresh: bool = False):
    req = LogAnalysisRequest(
        log_text=CRASH_LOG,
        package_name=PACKAGE_NAME,
        device_info="device-1",
        force_refresh=force_refresh,
    )
    return asyncio.run(analyze_log(req, session=object()))


def _run_llm(raw_text: str):
    with patch(
        "backend.api.log_analysis._get_setting",
        side_effect=["test-key", "https://api.openai.com/v1", "gpt-4o-mini"],
    ), patch("httpx.AsyncClient", _FakeAsyncClient):
        _FakeAsyncClient.response = _FakeResponse(raw_text)
        return asyncio.run(
            call_llm_service(
                cleaned_log=CRASH_LOG,
                package_name=PACKAGE_NAME,
                device_info="device-1",
                session=object(),
            )
        )


# ==================== parse_chat_completion_payload 归一化 ====================


class TestParseChatCompletionPayload:
    def test_json_branch_normalizes_null_content_to_empty_string(self):
        data = parse_chat_completion_payload(_completion_json(None))

        assert data["choices"][0]["message"]["content"] == ""
        assert extract_message_content(data) == ""

    def test_json_branch_flattens_list_content(self):
        data = parse_chat_completion_payload(
            _completion_json(
                [
                    {"type": "text", "text": "定位到"},
                    {"type": "text", "text": "主线程阻塞"},
                ]
            )
        )

        assert data["choices"][0]["message"]["content"] == "定位到主线程阻塞"
        assert isinstance(data["choices"][0]["message"]["content"], str)

    def test_json_branch_keeps_plain_string_content(self):
        data = parse_chat_completion_payload(_completion_json("### 根因分析\nNPE"))

        assert data["choices"][0]["message"]["content"] == "### 根因分析\nNPE"

    def test_json_branch_accepts_empty_content(self):
        # 连接探针（max_tokens=4）依赖「空内容也算连通」，这里绝不能抛异常
        data = parse_chat_completion_payload(_completion_json(""))

        assert extract_message_content(data) == ""

    def test_json_branch_normalizes_null_usage_to_dict(self):
        data = parse_chat_completion_payload(
            '{"choices":[{"message":{"content":"ok"}}],"usage":null}'
        )

        assert data["usage"] == {}
        # 归一化后调用方可以安全地 usage.get(...)
        assert data["usage"].get("total_tokens", 0) == 0

    def test_json_branch_normalizes_missing_usage_to_dict(self):
        data = parse_chat_completion_payload('{"choices":[{"message":{"content":"ok"}}]}')

        assert data["usage"] == {}

    def test_sse_branch_behavior_unchanged(self):
        data = parse_chat_completion_payload(
            "\n".join(
                [
                    'data: {"choices":[{"delta":{"role":"assistant","content":"定位到"}}]}',
                    'data: {"choices":[{"delta":{"content":"主线程阻塞"}}],"usage":{"total_tokens":56}}',
                    "data: [DONE]",
                ]
            )
        )

        assert data["choices"][0]["message"]["role"] == "assistant"
        assert data["choices"][0]["message"]["content"] == "定位到主线程阻塞"
        assert data["usage"]["total_tokens"] == 56

    def test_sse_branch_still_rejects_empty_content(self):
        # SSE 分支原有的「空内容 = 响应无效」语义保持不变
        with pytest.raises(ValueError):
            parse_chat_completion_payload(
                'data: {"choices":[{"delta":{"role":"assistant"}}]}\n\ndata: [DONE]'
            )


class TestExtractMessageContent:
    def test_returns_empty_string_for_missing_or_non_text_shapes(self):
        assert extract_message_content(None) == ""
        assert extract_message_content({"choices": []}) == ""
        assert extract_message_content({"choices": "oops"}) == ""
        assert extract_message_content({"choices": [None]}) == ""
        assert extract_message_content({"choices": [{"message": None}]}) == ""
        assert extract_message_content({"choices": [{"message": {"content": 123}}]}) == ""

    def test_flattens_list_content(self):
        assert (
            extract_message_content(
                {"choices": [{"message": {"content": [{"type": "text", "text": "hi"}]}}]}
            )
            == "hi"
        )


# ==================== analyze_log 正文守卫 ====================


class TestAnalyzeLogContentGuard:
    def test_null_content_maps_to_502_and_leaves_cache_clean(self):
        with patch(
            "backend.api.log_analysis.call_llm_service",
            new=AsyncMock(
                return_value={"success": True, "analysis_result": None, "token_usage": 0}
            ),
        ):
            with pytest.raises(HTTPException) as excinfo:
                _run_analyze()

        assert excinfo.value.status_code == 502
        assert excinfo.value.detail == EMPTY_CONTENT_ERROR_DETAIL
        assert _analysis_cache == {}

    def test_list_content_maps_to_502(self):
        with patch(
            "backend.api.log_analysis.call_llm_service",
            new=AsyncMock(
                return_value={
                    "success": True,
                    "analysis_result": [{"type": "text", "text": "hi"}],
                    "token_usage": 0,
                }
            ),
        ):
            with pytest.raises(HTTPException) as excinfo:
                _run_analyze()

        assert excinfo.value.status_code == 502
        assert _analysis_cache == {}

    def test_empty_content_maps_to_502(self):
        with patch(
            "backend.api.log_analysis.call_llm_service",
            new=AsyncMock(
                return_value={"success": True, "analysis_result": "   ", "token_usage": 0}
            ),
        ):
            with pytest.raises(HTTPException) as excinfo:
                _run_analyze()

        assert excinfo.value.status_code == 502
        assert _analysis_cache == {}

    def test_plain_string_content_is_returned_and_cached(self):
        with patch(
            "backend.api.log_analysis.call_llm_service",
            new=AsyncMock(
                return_value={"success": True, "analysis_result": "### 根因分析", "token_usage": 9}
            ),
        ):
            first = _run_analyze()
            second = _run_analyze()

        assert first.success is True
        assert first.analysis_result == "### 根因分析"
        assert first.token_usage == 9
        assert first.cached is False
        assert second.cached is True
        assert second.analysis_result == "### 根因分析"

    def test_failed_analysis_does_not_poison_cache_for_next_request(self):
        """一次 None 正文不得让这条日志永久分析不了。"""
        llm_mock = AsyncMock(
            side_effect=[
                {"success": True, "analysis_result": None, "token_usage": 0},
                {"success": True, "analysis_result": "第二次成功", "token_usage": 12},
            ]
        )
        with patch("backend.api.log_analysis.call_llm_service", new=llm_mock):
            with pytest.raises(HTTPException) as excinfo:
                _run_analyze()
            assert excinfo.value.status_code == 502

            # 不带 force_refresh：必须重新调 LLM，而不是命中脏缓存再炸一次
            retry = _run_analyze()

        assert llm_mock.await_count == 2
        assert retry.success is True
        assert retry.analysis_result == "第二次成功"
        assert retry.cached is False

        # 成功后才允许命中缓存
        with patch(
            "backend.api.log_analysis.call_llm_service",
            new=AsyncMock(side_effect=AssertionError("命中缓存时不应再调 LLM")),
        ):
            cached = _run_analyze()
        assert cached.cached is True
        assert cached.analysis_result == "第二次成功"

    def test_legacy_non_string_cache_values_are_discarded(self):
        """存量脏缓存值（历史 bug 写进去的 None）要自愈，不要拿去构造响应。"""
        cache_key = hashlib.md5(clean_log_for_ai(CRASH_LOG, PACKAGE_NAME).encode("utf-8")).hexdigest()
        _analysis_cache[cache_key] = None

        llm_mock = AsyncMock(
            return_value={"success": True, "analysis_result": "重新分析结果", "token_usage": 3}
        )
        with patch("backend.api.log_analysis.call_llm_service", new=llm_mock):
            resp = _run_analyze()

        assert llm_mock.await_count == 1
        assert resp.success is True
        assert resp.analysis_result == "重新分析结果"
        assert resp.cached is False

    def test_llm_unexpected_error_maps_to_json_500(self):
        """兜底异常必须带 detail，不能产出纯文本 Internal Server Error。

        detail 会被前端 normalizeError 原样显示，所以只给异常类型名，
        原始异常文本只留在服务端日志里，不往界面上吐。
        """
        with patch(
            "backend.api.log_analysis.call_llm_service",
            new=AsyncMock(side_effect=RuntimeError("boom")),
        ):
            with pytest.raises(HTTPException) as excinfo:
                _run_analyze()

        assert excinfo.value.status_code == 500
        assert "RuntimeError" in str(excinfo.value.detail)
        assert "boom" not in str(excinfo.value.detail)


# ==================== call_llm_service 取正文 ====================


class TestCallLlmServiceContent:
    def test_null_content_maps_to_502(self):
        with pytest.raises(HTTPException) as excinfo:
            _run_llm(_completion_json(None))

        assert excinfo.value.status_code == 502
        assert excinfo.value.detail == EMPTY_CONTENT_ERROR_DETAIL

    def test_list_content_is_flattened_to_text(self):
        result = _run_llm(
            _completion_json([{"type": "text", "text": "定位到"}, {"type": "text", "text": "死锁"}])
        )

        assert result["success"] is True
        assert result["analysis_result"] == "定位到死锁"
        assert result["token_usage"] == 7

    def test_null_usage_yields_zero_tokens(self):
        result = _run_llm(
            '{"choices":[{"message":{"content":"ok"}}],"usage":null}'
        )

        assert result["analysis_result"] == "ok"
        assert result["token_usage"] == 0


# ==================== jank 卡顿分析共用同一套守卫 ====================


class TestJankAiContentGuard:
    def _artifact(self):
        return {
            "trigger_time": "10:00:00",
            "trigger_reason": "窗口卡顿率超阈值",
            "path": "reports/fastbot/37/jank_trace_001.perfetto-trace",
            "analysis": {
                "analysis_level": "full",
                "frame_stats": {"jank_rate": 0.32},
                "top_busy_threads": [{"thread_name": "main", "running_ms": 10}],
            },
        }

    def _summarize(self):
        return asyncio.run(
            summarize_jank_analysis(
                artifact=self._artifact(),
                package_name=PACKAGE_NAME,
                device_info="device-1",
                session=object(),
            )
        )

    def test_null_content_maps_to_502_and_leaves_cache_clean(self):
        with patch(
            "backend.jank_ai_service._get_setting",
            side_effect=["test-key", "https://api.openai.com/v1", "gpt-4o-mini"],
        ), patch("backend.jank_ai_service.httpx.AsyncClient", _FakeAsyncClient):
            _FakeAsyncClient.response = _FakeResponse(_completion_json(None))
            with pytest.raises(HTTPException) as excinfo:
                self._summarize()

        assert excinfo.value.status_code == 502
        assert excinfo.value.detail == EMPTY_CONTENT_ERROR_DETAIL
        assert len(jank_ai_service._jank_ai_cache) == 0

    def test_retry_after_empty_content_succeeds(self):
        with patch(
            "backend.jank_ai_service._get_setting",
            side_effect=["test-key", "https://api.openai.com/v1", "gpt-4o-mini"] * 2,
        ), patch("backend.jank_ai_service.httpx.AsyncClient", _FakeAsyncClient):
            _FakeAsyncClient.response = _FakeResponse(_completion_json(None))
            with pytest.raises(HTTPException):
                self._summarize()

            _FakeAsyncClient.response = _FakeResponse(_completion_json("### 现象摘要\n测试结果"))
            result = self._summarize()

        assert result["success"] is True
        assert result["analysis_result"] == "### 现象摘要\n测试结果"
        assert result["cached"] is False


# ==================== NL2Script 降级语义 ====================


class TestNl2ScriptFallback:
    def test_null_content_degrades_to_mock_steps(self):
        with patch(
            "backend.api.ai._get_setting",
            side_effect=["test-key", "https://api.openai.com/v1", "gpt-4o-mini"],
        ), patch("httpx.AsyncClient", _FakeAsyncClient):
            _FakeAsyncClient.response = _FakeResponse(_completion_json(None))
            resp = asyncio.run(
                ai_api.generate_steps(ai_api.GenerateStepsRequest(text="登录"), session=object())
            )

        assert resp.success is True
        assert resp.data  # 兜底模板步骤
        assert "兜底" in (resp.message or "")

    def test_list_content_is_flattened_before_json_parse(self):
        steps = json.dumps(
            [
                {
                    "action": "click",
                    "args": {},
                    "platform_overrides": {"android": {"selector": "登录按钮", "by": "text"}},
                    "description": "点击登录按钮",
                }
            ],
            ensure_ascii=False,
        )
        with patch(
            "backend.api.ai._get_setting",
            side_effect=["test-key", "https://api.openai.com/v1", "gpt-4o-mini"],
        ), patch("httpx.AsyncClient", _FakeAsyncClient):
            _FakeAsyncClient.response = _FakeResponse(
                _completion_json([{"type": "text", "text": steps}])
            )
            resp = asyncio.run(
                ai_api.generate_steps(ai_api.GenerateStepsRequest(text="点击登录"), session=object())
            )

        assert resp.success is True
        assert resp.data[0]["action"] == "click"
        assert resp.data[0]["description"] == "点击登录按钮"
