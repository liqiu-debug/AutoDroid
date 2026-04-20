"""
基于结构化卡顿分析结果的 AI 总结服务。
"""
import hashlib
import json
import logging
import time
from collections import OrderedDict
from typing import Any, Dict, Optional

import httpx
from fastapi import HTTPException
from sqlmodel import Session

from backend.api.log_analysis import _get_setting
from backend.openai_compat import parse_chat_completion_payload

logger = logging.getLogger(__name__)

JANK_AI_CACHE_MAXSIZE = 100
JANK_AI_CACHE_TTL_SEC = 3600


class _TtlCache:
    def __init__(self, maxsize: int, ttl: int):
        self.maxsize = max(1, int(maxsize))
        self.ttl = max(1, int(ttl))
        self._store: "OrderedDict[str, tuple[str, float]]" = OrderedDict()

    def _purge_expired(self) -> None:
        now = time.time()
        expired_keys = [
            key for key, (_, expires_at) in self._store.items()
            if expires_at <= now
        ]
        for key in expired_keys:
            self._store.pop(key, None)

    def get(self, key: str) -> Optional[str]:
        self._purge_expired()
        value = self._store.get(key)
        if value is None:
            return None
        content, expires_at = value
        if expires_at <= time.time():
            self._store.pop(key, None)
            return None
        self._store.move_to_end(key)
        return content

    def set(self, key: str, value: str) -> None:
        self._purge_expired()
        expires_at = time.time() + self.ttl
        self._store[key] = (value, expires_at)
        self._store.move_to_end(key)
        while len(self._store) > self.maxsize:
            self._store.popitem(last=False)

    def clear(self) -> None:
        self._store.clear()

    def __len__(self) -> int:
        self._purge_expired()
        return len(self._store)


_jank_ai_cache = _TtlCache(maxsize=JANK_AI_CACHE_MAXSIZE, ttl=JANK_AI_CACHE_TTL_SEC)


def _build_system_prompt(package_name: str, device_info: str = "") -> str:
    device_ctx = f"\n设备信息: {device_info}" if device_info else ""
    return f"""你是一位资深 Android 性能优化专家，擅长分析 Perfetto / FrameTimeline 卡顿数据。
当前应用包名: {package_name}{device_ctx}

你将收到结构化卡顿分析结果，请基于这些事实输出简洁、可执行的 Markdown 结论。
不要臆造不存在的事实；如果证据不足，要明确写出“证据有限”。
结论的作用域必须严格限制在当前 trace 片段，不得把局部片段外推为整段录制或整条链路都卡顿严重。
FrameTimeline 中的 jank_rate / Late Present 不能直接等同于“用户明显感知到严重卡顿”；必须结合 effective_fps、present_delay_p95_ms、max_frame_ms 和 payload 里的 experience_severity_hint 一起判断。
如果 experience_severity_hint 是“轻微波动”或“局部可感知波动”，除非 payload 里有更强直接证据，否则不要写成“严重卡顿”或“卡顿严重”。

请严格按照以下结构输出：

### 现象摘要
用 2-3 句话总结本次卡顿表现、主要线程/图层和影响范围。

### 最可能原因
列出 1-3 条最主要原因，每条都要引用具体证据。

### 次要怀疑点
列出 1-3 条次要怀疑点；如果没有可疑项，写“暂无明显次要怀疑点”。

### 排查建议
给出 3-5 条具体建议，优先可落地的排查和优化动作。"""


def _safe_number(value: Any) -> float:
    try:
        if value in (None, ""):
            return 0.0
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _build_experience_severity_hint(artifact: Dict[str, Any]) -> Dict[str, Any]:
    analysis = artifact.get("analysis") or {}
    frame_stats = analysis.get("frame_stats") or {}
    capture_mode = str(artifact.get("capture_mode") or "diagnostic")
    analysis_level = str(analysis.get("analysis_level") or "")
    top_busy_threads = analysis.get("top_busy_threads") or []
    hot_slices = analysis.get("hot_slices") or []

    jank_rate = _safe_number(frame_stats.get("jank_rate"))
    effective_fps = _safe_number(frame_stats.get("effective_fps"))
    present_delay_p95_ms = _safe_number(frame_stats.get("present_delay_p95_ms"))
    max_frame_ms = _safe_number(frame_stats.get("max_frame_ms"))

    reasons = []
    if effective_fps > 0:
        reasons.append(f"effective_fps={effective_fps:.1f}")
    if present_delay_p95_ms > 0:
        reasons.append(f"present_delay_p95_ms={present_delay_p95_ms:.1f}")
    if max_frame_ms > 0:
        reasons.append(f"max_frame_ms={max_frame_ms:.1f}")
    if jank_rate > 0:
        reasons.append(f"frame_timeline_jank_rate={jank_rate:.4f}")

    if effective_fps >= 60 and present_delay_p95_ms <= 8 and max_frame_ms < 80:
        level = "MILD"
        label = "轻微波动"
    elif (
        (effective_fps > 0 and effective_fps < 35)
        or present_delay_p95_ms >= 60
        or max_frame_ms >= 150
        or (jank_rate >= 0.4 and present_delay_p95_ms >= 30)
        or (jank_rate >= 0.5 and effective_fps > 0 and effective_fps < 55)
    ):
        level = "SEVERE"
        label = "明显卡顿"
    elif (
        (effective_fps > 0 and effective_fps < 55)
        or present_delay_p95_ms >= 20
        or max_frame_ms >= 100
        or jank_rate >= 0.25
    ):
        level = "MODERATE"
        label = "局部可感知波动"
    elif jank_rate > 0 or max_frame_ms > 0 or present_delay_p95_ms > 0:
        level = "MILD"
        label = "轻微波动"
    else:
        level = "UNKNOWN"
        label = "证据有限"

    evidence_notes = []
    if capture_mode == "diagnostic":
        evidence_notes.append("这是异常触发附近的局部 trace 片段，不代表整段录制。")
    else:
        evidence_notes.append("这是当前连续采样 trace 覆盖到的片段，不应直接外推为整段录制体验。")
    if analysis_level in {"frame_timeline_only", "partial"}:
        evidence_notes.append("当前分析主要基于 FrameTimeline，缺少完整线程调度与热点切片证据。")
    if not top_busy_threads and not hot_slices:
        evidence_notes.append("当前没有可用的线程热点或切片热点，根因证据偏弱。")
    if jank_rate >= 0.25 and effective_fps >= 55 and present_delay_p95_ms <= 8:
        evidence_notes.append("FrameTimeline jank_rate 偏高，但 effective_fps 仍高且 P95 呈现延迟很低，更像节奏抖动或晚到统计，不宜直接表述为严重卡顿。")

    confidence = "high"
    if analysis_level in {"frame_timeline_only", "partial"} or (not top_busy_threads and not hot_slices):
        confidence = "limited"
    elif capture_mode == "diagnostic":
        confidence = "medium"

    return {
        "level": level,
        "label": label,
        "confidence": confidence,
        "scope_note": (
            "仅代表当前 trace 片段，不代表整段录制或整体页面体验。"
        ),
        "reasoning": reasons,
        "evidence_notes": evidence_notes,
    }


def build_jank_ai_payload_text(artifact: Dict[str, Any]) -> str:
    analysis = artifact.get("analysis") or {}
    experience_hint = _build_experience_severity_hint(artifact)
    payload = {
        "trigger_time": artifact.get("trigger_time"),
        "trigger_reason": artifact.get("trigger_reason"),
        "trace_path": artifact.get("path"),
        "capture_mode": artifact.get("capture_mode"),
        "analysis_level": analysis.get("analysis_level"),
        "analysis_scope": analysis.get("analysis_scope"),
        "analysis_window_sec": analysis.get("analysis_window_sec"),
        "frame_timeline_available": analysis.get("frame_timeline_available"),
        "experience_severity_hint": experience_hint,
        "frame_stats": analysis.get("frame_stats"),
        "jank_type_breakdown": (analysis.get("jank_type_breakdown") or [])[:5],
        "suspected_causes": (analysis.get("suspected_causes") or [])[:5],
        "top_busy_threads": (analysis.get("top_busy_threads") or [])[:5],
        "thread_summaries": analysis.get("thread_summaries"),
        "top_jank_frames": (analysis.get("top_jank_frames") or [])[:8],
        "hot_slices": (analysis.get("hot_slices") or [])[:8],
    }
    return json.dumps(payload, ensure_ascii=False, indent=2)


async def summarize_jank_analysis(
    artifact: Dict[str, Any],
    package_name: str,
    device_info: str,
    session: Session,
    force_refresh: bool = False,
) -> Dict[str, Any]:
    analysis = artifact.get("analysis")
    if not isinstance(analysis, dict) or not analysis:
        raise HTTPException(status_code=400, detail="当前 trace 还没有可用的结构化分析结果。")

    payload_text = build_jank_ai_payload_text(artifact)
    system_prompt = _build_system_prompt(package_name, device_info)
    cache_key = hashlib.md5(f"{system_prompt}\n{payload_text}".encode("utf-8")).hexdigest()
    cached_result = None if force_refresh else _jank_ai_cache.get(cache_key)
    if cached_result is not None:
        return {
            "success": True,
            "analysis_result": cached_result,
            "token_usage": 0,
            "cached": True,
        }

    api_key = _get_setting(session, "ai_api_key", "")
    api_base = _get_setting(session, "ai_api_base", "https://api.openai.com/v1")
    model = _get_setting(session, "ai_model", "gpt-3.5-turbo")

    if not api_key:
        raise HTTPException(status_code=400, detail="未配置 AI API Key。请先在系统设置中完成配置。")

    api_base = api_base.rstrip("/")
    if api_base.endswith("/chat/completions"):
        api_base = api_base[:-len("/chat/completions")]

    request_url = f"{api_base}/chat/completions"
    user_prompt = f"请基于以下结构化卡顿分析结果生成总结：\n\n```json\n{payload_text}\n```"

    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        "temperature": 0.2,
        "max_tokens": 1800,
        "stream": False,
    }
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }

    try:
        async with httpx.AsyncClient(timeout=60) as client:
            resp = await client.post(request_url, json=payload, headers=headers)
            raw_text = resp.text

            if resp.status_code != 200:
                raise HTTPException(
                    status_code=502,
                    detail=f"AI 服务返回错误 (HTTP {resp.status_code}): {raw_text[:300]}",
                )

            data = parse_chat_completion_payload(raw_text)
            if "choices" not in data or not data["choices"]:
                raise HTTPException(status_code=502, detail="AI 服务返回格式异常（缺少 choices）。")

            content = data["choices"][0]["message"]["content"]
            token_usage = (data.get("usage") or {}).get("total_tokens", 0)

            _jank_ai_cache.set(cache_key, content)
            return {
                "success": True,
                "analysis_result": content,
                "token_usage": token_usage,
                "cached": False,
            }
    except HTTPException:
        raise
    except httpx.TimeoutException:
        raise HTTPException(status_code=504, detail="AI 服务响应超时，请稍后重试。")
    except httpx.ConnectError:
        raise HTTPException(status_code=502, detail="无法连接 AI 服务，请检查 API 配置。")
    except Exception as exc:
        logger.error("AI 卡顿分析调用失败: %s", exc)
        raise HTTPException(status_code=500, detail=f"AI 服务调用异常: {exc}")
