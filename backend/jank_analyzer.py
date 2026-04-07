"""
Perfetto 卡顿 trace 结构化分析器。

能力：
- 读取导出的 .perfetto-trace
- 基于 FrameTimeline / sched / slice 生成结构化卡顿分析结果
- 在缺少 FrameTimeline 时自动降级为线程/切片热点分析
"""
import os
from typing import Any, Dict, Iterable, List, Optional


DEFAULT_ANALYSIS_WINDOW_SEC = 30
DEFAULT_TIMELINE_BUCKET_SEC = 5
TRACE_PROCESSOR_LOAD_TIMEOUT_SEC = 60


def _sql_quote(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def _row_dict(row: Any) -> Dict[str, Any]:
    return dict(getattr(row, "__dict__", {}))


def _query_rows(tp: Any, sql: str) -> List[Dict[str, Any]]:
    return [_row_dict(row) for row in tp.query(sql)]


def _query_first(tp: Any, sql: str) -> Dict[str, Any]:
    rows = _query_rows(tp, sql)
    return rows[0] if rows else {}


def _table_exists(tp: Any, table_name: str) -> bool:
    table = _sql_quote(table_name)
    return bool(
        _query_first(
            tp,
            f"SELECT COUNT(*) AS c FROM sqlite_master WHERE type='table' AND name={table}",
        ).get("c")
    )


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        if value is None:
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        if value is None:
            return default
        return int(value)
    except (TypeError, ValueError):
        return default


def _percentile(values: Iterable[float], percentile: float) -> float:
    ordered = sorted(float(v) for v in values if v is not None)
    if not ordered:
        return 0.0
    if len(ordered) == 1:
        return ordered[0]

    rank = (len(ordered) - 1) * (percentile / 100.0)
    lower_index = int(rank)
    upper_index = min(lower_index + 1, len(ordered) - 1)
    weight = rank - lower_index
    return ordered[lower_index] * (1 - weight) + ordered[upper_index] * weight


def _process_filter(package_name: str) -> str:
    pkg = _sql_quote(package_name)
    pkg_glob = _sql_quote(f"{package_name}:*")
    return f"(process.name = {pkg} OR process.cmdline = {pkg} OR process.name GLOB {pkg_glob})"


def _overlap_expr(table_name: str, start_ns: int, end_ns: int) -> str:
    return (
        f"CASE "
        f"WHEN {table_name}.ts >= {end_ns} OR {table_name}.ts + {table_name}.dur <= {start_ns} THEN 0 "
        f"ELSE MIN({table_name}.ts + {table_name}.dur, {end_ns}) - MAX({table_name}.ts, {start_ns}) "
        f"END"
    )


def _build_suspected_causes(
    frame_stats: Dict[str, Any],
    jank_types: List[Dict[str, Any]],
    top_jank_frames: List[Dict[str, Any]],
    top_busy_threads: List[Dict[str, Any]],
    hot_slices: List[Dict[str, Any]],
) -> List[Dict[str, str]]:
    causes: List[Dict[str, str]] = []
    seen_tags = set()
    slice_rows = [
        {
            "thread_name": str(item.get("thread_name") or ""),
            "slice_name": str(item.get("slice_name") or ""),
            "total_ms": _safe_float(item.get("total_ms")),
        }
        for item in hot_slices
    ]
    thread_rows = [
        {
            "thread_name": str(item.get("thread_name") or ""),
            "running_ms": _safe_float(item.get("running_ms")),
        }
        for item in top_busy_threads
    ]

    def add(tag: str, title: str, evidence: str):
        if tag in seen_tags:
            return
        seen_tags.add(tag)
        causes.append({
            "tag": tag,
            "title": title,
            "evidence": evidence,
        })

    def matched_slices(*keywords: str) -> List[Dict[str, Any]]:
        normalized = [keyword.lower() for keyword in keywords if keyword]
        return [
            item for item in slice_rows
            if any(
                keyword in item["slice_name"].lower()
                or keyword in item["thread_name"].lower()
                for keyword in normalized
            )
        ]

    def matched_threads(*keywords: str) -> List[Dict[str, Any]]:
        normalized = [keyword.lower() for keyword in keywords if keyword]
        return [
            item for item in thread_rows
            if any(keyword in item["thread_name"].lower() for keyword in normalized)
        ]

    if jank_types:
        dominant = jank_types[0]
        jank_name = str(dominant.get("jank_type") or "")
        jank_count = int(dominant.get("count") or 0)
        if jank_name == "App Deadline Missed":
            add(
                "app_deadline_missed",
                "App 侧帧截止时间被错过",
                f"近窗口内 {jank_count} 帧属于 App Deadline Missed，优先检查主线程与 RenderThread 负载。",
            )
        elif jank_name == "Buffer Stuffing":
            add(
                "buffer_stuffing",
                "生产者/消费者缓冲区积压",
                f"近窗口内 Buffer Stuffing 占比最高，通常意味着提交节奏或 Surface 队列存在背压。",
            )
        elif jank_name == "Dropped Frame":
            add(
                "dropped_frame",
                "存在被直接丢弃的帧",
                f"近窗口内出现 {jank_count} 个 Dropped Frame，需要重点检查帧生产是否成批阻塞。",
            )

    if thread_rows:
        busiest = thread_rows[0]
        thread_name = busiest["thread_name"]
        running_ms = busiest["running_ms"]
        if thread_name == "RenderThread" and running_ms >= 300:
            add(
                "render_thread_busy",
                "RenderThread 持续高负载",
                f"RenderThread 在分析窗口内累计运行 {running_ms:.1f} ms，可能存在绘制提交或 GPU 回压。",
            )
        if "mqt_v_js" in thread_name and running_ms >= 300:
            add(
                "react_native_js_busy",
                "React Native JS 线程负载偏高",
                f"mqt_v_js 在线程运行时间排行靠前，累计 {running_ms:.1f} ms，可能有 JS 计算或 bridge 压力。",
            )

    slice_names = [item["slice_name"] for item in slice_rows]
    if any("MountItemDispatcher" in name or "IntBufferBatchMountItem" in name for name in slice_names):
        add(
            "react_native_mounting",
            "React Native 视图挂载开销偏高",
            "热点切片中出现 MountItemDispatcher / IntBufferBatchMountItem，说明 UI 挂载和批量更新较重。",
        )
    if any("queueBuffer" in name or "Vulkan finish frame" in name or "flush commands" in name for name in slice_names):
        add(
            "render_pipeline_backpressure",
            "渲染提交阶段存在背压",
            "热点切片中出现 queueBuffer / Vulkan finish frame / flush commands，说明帧提交链路可能拥塞。",
        )
    if any("MSG_PROCESS_INPUT_EVENTS" in name for name in slice_names):
        add(
            "input_event_burst",
            "输入事件处理存在突发开销",
            "主线程热点中出现 MSG_PROCESS_INPUT_EVENTS，可能是大量输入/触摸事件与渲染竞争。",
        )

    gc_slices = matched_slices("art::gc", "heaptaskdaemon", "concurrent copying", "mark compact", "gc ")
    gc_threads = matched_threads("HeapTaskDaemon", "FinalizerDaemon", "ReferenceQueueDaemon")
    if gc_slices or gc_threads:
        top_gc_slice = max(gc_slices, key=lambda item: item["total_ms"], default=None)
        top_gc_thread = max(gc_threads, key=lambda item: item["running_ms"], default=None)
        evidence_parts = []
        if top_gc_thread and top_gc_thread["running_ms"] >= 80:
            evidence_parts.append(
                f"{top_gc_thread['thread_name']} 累计运行 {top_gc_thread['running_ms']:.1f} ms"
            )
        if top_gc_slice and top_gc_slice["total_ms"] > 0:
            evidence_parts.append(
                f"热点切片出现 {top_gc_slice['slice_name']}（{top_gc_slice['total_ms']:.1f} ms）"
            )
        add(
            "gc_pressure",
            "GC 回收压力偏高",
            "，".join(evidence_parts) or "热点线程/切片中出现 GC 相关活动，可能导致短时停顿与帧提交延迟。",
        )

    binder_slices = matched_slices("binder transaction", "binder reply", "binder ioctl", "transact")
    binder_threads = matched_threads("Binder:")
    if binder_slices or binder_threads:
        top_binder_slice = max(binder_slices, key=lambda item: item["total_ms"], default=None)
        top_binder_thread = max(binder_threads, key=lambda item: item["running_ms"], default=None)
        evidence_parts = []
        if top_binder_thread and top_binder_thread["running_ms"] >= 80:
            evidence_parts.append(
                f"{top_binder_thread['thread_name']} 累计运行 {top_binder_thread['running_ms']:.1f} ms"
            )
        if top_binder_slice and top_binder_slice["total_ms"] > 0:
            evidence_parts.append(
                f"热点切片出现 {top_binder_slice['slice_name']}（{top_binder_slice['total_ms']:.1f} ms）"
            )
        add(
            "binder_blocking",
            "Binder 调用链可能存在阻塞",
            "，".join(evidence_parts) or "热点线程/切片中出现 Binder 事务，建议排查跨进程调用等待和系统服务响应。",
        )

    io_slices = matched_slices("sqlite", "fsync", "read", "write", "open", "close", "file", "cursorwindow")
    main_thread_io_slices = [
        item for item in io_slices
        if item["thread_name"] and "renderthread" not in item["thread_name"].lower()
    ]
    if main_thread_io_slices:
        top_io_slice = max(main_thread_io_slices, key=lambda item: item["total_ms"])
        add(
            "main_thread_io",
            "主线程疑似存在 I/O 或数据库阻塞",
            f"{top_io_slice['thread_name'] or '主线程相关线程'} 上出现 {top_io_slice['slice_name']}，累计 {top_io_slice['total_ms']:.1f} ms，建议排查文件/数据库读写是否落在关键渲染路径。",
        )

    webview_slices = matched_slices("crrenderermain", "webview", "awcontents", "chromium")
    webview_threads = matched_threads("CrRendererMain")
    if webview_slices or webview_threads:
        top_webview_thread = max(webview_threads, key=lambda item: item["running_ms"], default=None)
        top_webview_slice = max(webview_slices, key=lambda item: item["total_ms"], default=None)
        evidence_parts = []
        if top_webview_thread and top_webview_thread["running_ms"] >= 80:
            evidence_parts.append(
                f"{top_webview_thread['thread_name']} 累计运行 {top_webview_thread['running_ms']:.1f} ms"
            )
        if top_webview_slice and top_webview_slice["total_ms"] > 0:
            evidence_parts.append(
                f"热点切片出现 {top_webview_slice['slice_name']}（{top_webview_slice['total_ms']:.1f} ms）"
            )
        add(
            "webview_rendering",
            "WebView/Chromium 渲染链路负载偏高",
            "，".join(evidence_parts) or "热点线程中出现 WebView/Chromium 相关线程，建议排查 H5 页面脚本、样式回流和资源加载。",
        )

    layout_slices = matched_slices("performtraversals", "measure", "layout", "relayoutwindow", "drawframe")
    if layout_slices:
        top_layout_slice = max(layout_slices, key=lambda item: item["total_ms"])
        add(
            "layout_measure_heavy",
            "布局/测量阶段开销偏高",
            f"热点切片出现 {top_layout_slice['slice_name']}，累计 {top_layout_slice['total_ms']:.1f} ms，建议检查层级深度、频繁 requestLayout 和列表项重排。",
        )

    popup_frames = [frame for frame in top_jank_frames if "PopupWindow" in str(frame.get("layer_name") or "")]
    if popup_frames:
        add(
            "popup_window",
            "弹窗/浮层参与了卡顿帧",
            f"Top jank 帧中有 {len(popup_frames)} 帧来自 PopupWindow 图层，建议排查弹窗动画和浮层重绘。",
        )

    if not causes and float(frame_stats.get("jank_rate") or 0) > 0:
        add(
            "generic_jank",
            "检测到卡顿，但暂未定位单一热点",
            "当前 trace 已出现卡顿帧，建议结合主线程、RenderThread 和业务操作继续细查。",
        )

    return causes


def _query_distinct_actual_frames(tp: Any, package_name: str, start_ns: int, end_ns: int) -> List[Dict[str, Any]]:
    proc_filter = _process_filter(package_name)
    query = f"""
        SELECT
            CAST(
                COALESCE(
                    actual_frame_timeline_slice.display_frame_token,
                    actual_frame_timeline_slice.surface_frame_token,
                    actual_frame_timeline_slice.id
                ) AS TEXT
            ) AS frame_key,
            MIN(ts) AS frame_start_ns,
            MAX(ts + dur) AS frame_end_ns,
            ROUND(MAX(dur) / 1e6, 2) AS max_frame_ms,
            MAX(CASE WHEN jank_type != 'None' THEN 1 ELSE 0 END) AS is_jank,
            MAX(CASE WHEN LOWER(COALESCE(present_type, '')) LIKE '%late%' THEN 1 ELSE 0 END) AS is_late_present,
            MAX(CASE WHEN LOWER(COALESCE(present_type, '')) LIKE '%dropped%' THEN 1 ELSE 0 END) AS is_dropped,
            MIN(CASE WHEN on_time_finish THEN 1 ELSE 0 END) AS on_time_finish,
            COUNT(*) AS surface_slice_count
        FROM actual_frame_timeline_slice
        JOIN process USING(upid)
        WHERE {proc_filter}
          AND ts < {end_ns}
          AND ts + dur > {start_ns}
        GROUP BY COALESCE(
            actual_frame_timeline_slice.display_frame_token,
            actual_frame_timeline_slice.surface_frame_token,
            actual_frame_timeline_slice.id
        )
        ORDER BY frame_start_ns ASC
    """
    return _query_rows(tp, query)


def _query_distinct_expected_frames(tp: Any, package_name: str, start_ns: int, end_ns: int) -> List[Dict[str, Any]]:
    proc_filter = _process_filter(package_name)
    query = f"""
        SELECT
            CAST(
                COALESCE(
                    expected_frame_timeline_slice.display_frame_token,
                    expected_frame_timeline_slice.surface_frame_token,
                    expected_frame_timeline_slice.id
                ) AS TEXT
            ) AS frame_key,
            MIN(ts) AS expected_start_ns,
            MAX(ts + dur) AS expected_end_ns,
            ROUND(MAX(dur) / 1e6, 3) AS expected_frame_ms
        FROM expected_frame_timeline_slice
        JOIN process USING(upid)
        WHERE {proc_filter}
          AND ts < {end_ns}
          AND ts + dur > {start_ns}
        GROUP BY COALESCE(
            expected_frame_timeline_slice.display_frame_token,
            expected_frame_timeline_slice.surface_frame_token,
            expected_frame_timeline_slice.id
        )
        ORDER BY expected_start_ns ASC
    """
    return _query_rows(tp, query)


def _summarize_frames(
    actual_frames: List[Dict[str, Any]],
    expected_frames: List[Dict[str, Any]],
) -> Dict[str, Any]:
    expected_by_key = {
        str(item.get("frame_key") or ""): item
        for item in expected_frames
        if item.get("frame_key") is not None
    }

    total_frames = len(actual_frames)
    if total_frames == 0:
        return {
            "total_frames": 0,
            "jank_frames": 0,
            "jank_rate": 0.0,
            "avg_frame_ms": 0.0,
            "max_frame_ms": 0.0,
            "avg_jank_frame_ms": 0.0,
            "target_fps": 0.0,
            "cadence_fps": 0.0,
            "effective_fps": 0.0,
            "presented_fps": 0.0,
            "on_time_fps": 0.0,
            "on_time_ratio": 0.0,
            "late_present_ratio": 0.0,
            "dropped_frame_ratio": 0.0,
            "present_delay_avg_ms": 0.0,
            "present_delay_p50_ms": 0.0,
            "present_delay_p95_ms": 0.0,
            "present_delay_p99_ms": 0.0,
            "frame_budget_p50_ms": 0.0,
            "actual_frame_interval_p50_ms": 0.0,
            "actual_frame_interval_p95_ms": 0.0,
            "active_span_sec": 0.0,
        }

    jank_frames = sum(int(item.get("is_jank") or 0) for item in actual_frames)
    late_present_frames = sum(int(item.get("is_late_present") or 0) for item in actual_frames)
    dropped_frames = sum(int(item.get("is_dropped") or 0) for item in actual_frames)
    presented_frames = total_frames - dropped_frames
    on_time_presented_frames = sum(
        1
        for item in actual_frames
        if not int(item.get("is_dropped") or 0) and not int(item.get("is_late_present") or 0)
    )

    frame_durations_ms = [_safe_float(item.get("max_frame_ms")) for item in actual_frames]
    jank_frame_durations_ms = [
        _safe_float(item.get("max_frame_ms"))
        for item in actual_frames
        if int(item.get("is_jank") or 0)
    ]
    frame_starts = [
        _safe_int(item.get("frame_start_ns"))
        for item in actual_frames
        if item.get("frame_start_ns") is not None
    ]
    frame_ends = [
        _safe_int(item.get("frame_end_ns"))
        for item in actual_frames
        if item.get("frame_end_ns") is not None and _safe_int(item.get("frame_end_ns")) > 0
    ]
    frame_intervals_ms = [
        round((frame_starts[index] - frame_starts[index - 1]) / 1_000_000, 3)
        for index in range(1, len(frame_starts))
        if frame_starts[index] > frame_starts[index - 1]
    ]

    expected_frame_budgets_ms = []
    for item in actual_frames:
        frame_key = str(item.get("frame_key") or "")
        expected = expected_by_key.get(frame_key) or {}
        budget_ms = _safe_float(expected.get("expected_frame_ms"))
        if budget_ms <= 0:
            expected_start = _safe_int(expected.get("expected_start_ns"))
            expected_end = _safe_int(expected.get("expected_end_ns"))
            if expected_end > expected_start:
                budget_ms = (expected_end - expected_start) / 1_000_000
        if budget_ms > 0:
            expected_frame_budgets_ms.append(budget_ms)

    positive_present_delays_ms = []
    for item in actual_frames:
        frame_key = str(item.get("frame_key") or "")
        expected = expected_by_key.get(frame_key) or {}
        present_delay_ms = None
        if item.get("present_delay_ms") not in (None, ""):
            present_delay_ms = _safe_float(item.get("present_delay_ms"))
        else:
            expected_end = _safe_int(expected.get("expected_end_ns"))
            frame_end = _safe_int(item.get("frame_end_ns"))
            if expected_end > 0 and frame_end > 0:
                present_delay_ms = (frame_end - expected_end) / 1_000_000
        positive_present_delays_ms.append(max(0.0, float(present_delay_ms or 0.0)))

    frame_budget_p50_ms = round(_percentile(expected_frame_budgets_ms, 50), 3)
    target_fps = round((1000.0 / frame_budget_p50_ms), 1) if frame_budget_p50_ms > 0 else 0.0
    actual_frame_interval_p50_ms = round(_percentile(frame_intervals_ms, 50), 3)
    actual_frame_interval_p95_ms = round(_percentile(frame_intervals_ms, 95), 3)
    cadence_fps = round((1000.0 / actual_frame_interval_p50_ms), 1) if actual_frame_interval_p50_ms > 0 else target_fps

    active_span_ns = 0
    if frame_starts and frame_ends:
        active_span_ns = max(frame_ends) - min(frame_starts)
    if active_span_ns <= 0 and frame_budget_p50_ms > 0:
        active_span_ns = int(frame_budget_p50_ms * 1_000_000)
    active_span_sec = round(active_span_ns / 1_000_000_000, 3) if active_span_ns > 0 else 0.0

    presented_fps = round((presented_frames / active_span_sec), 1) if active_span_sec > 0 else 0.0
    on_time_fps = round((on_time_presented_frames / active_span_sec), 1) if active_span_sec > 0 else 0.0
    jank_rate = round((jank_frames / total_frames), 4)
    on_time_ratio = round((on_time_presented_frames / total_frames), 4) if total_frames > 0 else 0.0
    effective_fps = round(cadence_fps * on_time_ratio, 1) if cadence_fps > 0 else on_time_fps

    return {
        "total_frames": total_frames,
        "jank_frames": jank_frames,
        "jank_rate": jank_rate,
        "avg_frame_ms": round(sum(frame_durations_ms) / len(frame_durations_ms), 2) if frame_durations_ms else 0.0,
        "max_frame_ms": round(max(frame_durations_ms), 2) if frame_durations_ms else 0.0,
        "avg_jank_frame_ms": round(sum(jank_frame_durations_ms) / len(jank_frame_durations_ms), 2)
        if jank_frame_durations_ms
        else 0.0,
        "target_fps": target_fps,
        "cadence_fps": cadence_fps,
        "effective_fps": effective_fps,
        "presented_fps": presented_fps,
        "on_time_fps": on_time_fps,
        "on_time_ratio": on_time_ratio,
        "late_present_ratio": round((late_present_frames / total_frames), 4) if total_frames > 0 else 0.0,
        "dropped_frame_ratio": round((dropped_frames / total_frames), 4) if total_frames > 0 else 0.0,
        "present_delay_avg_ms": round(sum(positive_present_delays_ms) / len(positive_present_delays_ms), 3)
        if positive_present_delays_ms
        else 0.0,
        "present_delay_p50_ms": round(_percentile(positive_present_delays_ms, 50), 3),
        "present_delay_p95_ms": round(_percentile(positive_present_delays_ms, 95), 3),
        "present_delay_p99_ms": round(_percentile(positive_present_delays_ms, 99), 3),
        "frame_budget_p50_ms": frame_budget_p50_ms,
        "actual_frame_interval_p50_ms": actual_frame_interval_p50_ms,
        "actual_frame_interval_p95_ms": actual_frame_interval_p95_ms,
        "active_span_sec": active_span_sec,
    }


def _summarize_frame_metrics(tp: Any, package_name: str, start_ns: int, end_ns: int) -> Dict[str, Any]:
    actual_frames = _query_distinct_actual_frames(tp, package_name, start_ns, end_ns)
    expected_frames = (
        _query_distinct_expected_frames(tp, package_name, start_ns, end_ns)
        if _table_exists(tp, "expected_frame_timeline_slice")
        else []
    )
    return _summarize_frames(actual_frames, expected_frames)


def _build_frame_timeline_series(
    tp: Any,
    package_name: str,
    start_ns: int,
    end_ns: int,
    bucket_sec: int = DEFAULT_TIMELINE_BUCKET_SEC,
) -> List[Dict[str, Any]]:
    if end_ns <= start_ns or bucket_sec <= 0:
        return []

    actual_frames = _query_distinct_actual_frames(tp, package_name, start_ns, end_ns)
    if not actual_frames:
        return []

    expected_frames = (
        _query_distinct_expected_frames(tp, package_name, start_ns, end_ns)
        if _table_exists(tp, "expected_frame_timeline_slice")
        else []
    )
    expected_by_key = {
        str(item.get("frame_key") or ""): item
        for item in expected_frames
        if item.get("frame_key") is not None
    }

    bucket_ns = bucket_sec * 1_000_000_000
    bucket_frames: Dict[int, List[Dict[str, Any]]] = {}
    for frame in actual_frames:
        frame_start_ns = _safe_int(frame.get("frame_start_ns"))
        if frame_start_ns < start_ns or frame_start_ns >= end_ns:
            continue
        bucket_index = max(0, (frame_start_ns - start_ns) // bucket_ns)
        bucket_frames.setdefault(bucket_index, []).append(frame)

    series = []
    for bucket_index in sorted(bucket_frames):
        frames = bucket_frames[bucket_index]
        frame_keys = {
            str(item.get("frame_key") or "")
            for item in frames
            if item.get("frame_key") is not None
        }
        metrics = _summarize_frames(
            frames,
            [expected_by_key[key] for key in frame_keys if key in expected_by_key],
        )
        if metrics.get("total_frames", 0) <= 0:
            continue
        bucket_start_ns = start_ns + (bucket_index * bucket_ns)
        series.append({
            "offset_sec": round((bucket_start_ns - start_ns) / 1_000_000_000, 3),
            "window_sec": bucket_sec,
            "total_frames": metrics.get("total_frames", 0),
            "jank_rate": metrics.get("jank_rate", 0.0),
            "cadence_fps": metrics.get("cadence_fps", 0.0),
            "effective_fps": metrics.get("effective_fps", 0.0),
            "presented_fps": metrics.get("presented_fps", 0.0),
            "present_delay_p95_ms": metrics.get("present_delay_p95_ms", 0.0),
        })
    return series


def _query_jank_breakdown(tp: Any, package_name: str, start_ns: int, end_ns: int) -> List[Dict[str, Any]]:
    proc_filter = _process_filter(package_name)
    query = f"""
        SELECT
            jank_type,
            COUNT(*) AS count
        FROM actual_frame_timeline_slice
        JOIN process USING(upid)
        WHERE {proc_filter}
          AND jank_type != 'None'
          AND ts < {end_ns}
          AND ts + dur > {start_ns}
        GROUP BY jank_type
        ORDER BY count DESC, jank_type ASC
        LIMIT 6
    """
    return _query_rows(tp, query)


def _query_top_jank_frames(tp: Any, package_name: str, start_ns: int, end_ns: int) -> List[Dict[str, Any]]:
    proc_filter = _process_filter(package_name)
    query = f"""
        SELECT
            layer_name,
            jank_type,
            jank_severity_type,
            present_type,
            on_time_finish,
            ROUND(dur / 1e6, 2) AS dur_ms,
            ROUND((ts - {start_ns}) / 1e6, 2) AS relative_start_ms
        FROM actual_frame_timeline_slice
        JOIN process USING(upid)
        WHERE {proc_filter}
          AND jank_type != 'None'
          AND ts < {end_ns}
          AND ts + dur > {start_ns}
        ORDER BY dur DESC
        LIMIT 10
    """
    return _query_rows(tp, query)


def _query_top_busy_threads(tp: Any, package_name: str, start_ns: int, end_ns: int) -> List[Dict[str, Any]]:
    if not _table_exists(tp, "sched"):
        return []
    proc_filter = _process_filter(package_name)
    overlap = _overlap_expr("sched", start_ns, end_ns)
    query = f"""
        SELECT
            thread.name AS thread_name,
            ROUND(SUM({overlap}) / 1e6, 2) AS running_ms,
            COUNT(*) AS sched_slices
        FROM sched
        JOIN thread USING(utid)
        JOIN process USING(upid)
        WHERE {proc_filter}
          AND sched.dur > 0
          AND sched.ts < {end_ns}
          AND sched.ts + sched.dur > {start_ns}
        GROUP BY thread.name
        HAVING SUM({overlap}) > 0
        ORDER BY SUM({overlap}) DESC
        LIMIT 10
    """
    return _query_rows(tp, query)


def _query_thread_hot_slices(
    tp: Any,
    package_name: str,
    start_ns: int,
    end_ns: int,
    thread_clause: str,
    limit: int = 6,
) -> List[Dict[str, Any]]:
    if not _table_exists(tp, "slice") or not _table_exists(tp, "thread_track"):
        return []
    proc_filter = _process_filter(package_name)
    overlap = _overlap_expr("slice", start_ns, end_ns)
    query = f"""
        SELECT
            slice.name AS slice_name,
            ROUND(SUM({overlap}) / 1e6, 2) AS total_ms,
            ROUND(MAX({overlap}) / 1e6, 2) AS max_ms,
            COUNT(*) AS count
        FROM slice
        JOIN thread_track ON slice.track_id = thread_track.id
        JOIN thread USING(utid)
        JOIN process USING(upid)
        WHERE {proc_filter}
          AND {thread_clause}
          AND slice.dur > 0
          AND slice.ts < {end_ns}
          AND slice.ts + slice.dur > {start_ns}
        GROUP BY slice.name
        HAVING SUM({overlap}) > 0
        ORDER BY SUM({overlap}) DESC
        LIMIT {limit}
    """
    return _query_rows(tp, query)


def _query_thread_summary(
    tp: Any,
    package_name: str,
    start_ns: int,
    end_ns: int,
    thread_clause: str,
    summary_label: str,
) -> Dict[str, Any]:
    if not _table_exists(tp, "sched"):
        return {
            "label": summary_label,
            "thread_name": "",
            "running_ms": 0,
            "max_slice_ms": 0,
            "sched_slices": 0,
            "top_slices": [],
        }
    proc_filter = _process_filter(package_name)
    overlap = _overlap_expr("sched", start_ns, end_ns)
    query = f"""
        SELECT
            thread.name AS thread_name,
            ROUND(SUM({overlap}) / 1e6, 2) AS running_ms,
            ROUND(MAX({overlap}) / 1e6, 2) AS max_slice_ms,
            COUNT(*) AS sched_slices
        FROM sched
        JOIN thread USING(utid)
        JOIN process USING(upid)
        WHERE {proc_filter}
          AND {thread_clause}
          AND sched.dur > 0
          AND sched.ts < {end_ns}
          AND sched.ts + sched.dur > {start_ns}
        GROUP BY thread.name
        HAVING SUM({overlap}) > 0
        ORDER BY SUM({overlap}) DESC
        LIMIT 1
    """
    summary = _query_first(tp, query)
    if not summary:
        return {
            "label": summary_label,
            "thread_name": "",
            "running_ms": 0,
            "max_slice_ms": 0,
            "sched_slices": 0,
            "top_slices": [],
        }
    summary["label"] = summary_label
    summary["top_slices"] = _query_thread_hot_slices(
        tp,
        package_name,
        start_ns,
        end_ns,
        thread_clause=thread_clause,
        limit=6,
    )
    return summary


def _query_hot_slices(tp: Any, package_name: str, start_ns: int, end_ns: int) -> List[Dict[str, Any]]:
    if not _table_exists(tp, "slice") or not _table_exists(tp, "thread_track"):
        return []
    proc_filter = _process_filter(package_name)
    overlap = _overlap_expr("slice", start_ns, end_ns)
    query = f"""
        SELECT
            thread.name AS thread_name,
            slice.name AS slice_name,
            ROUND(SUM({overlap}) / 1e6, 2) AS total_ms,
            ROUND(MAX({overlap}) / 1e6, 2) AS max_ms,
            COUNT(*) AS count
        FROM slice
        JOIN thread_track ON slice.track_id = thread_track.id
        JOIN thread USING(utid)
        JOIN process USING(upid)
        WHERE {proc_filter}
          AND slice.dur > 0
          AND slice.ts < {end_ns}
          AND slice.ts + slice.dur > {start_ns}
        GROUP BY thread.name, slice.name
        HAVING SUM({overlap}) > 0
        ORDER BY SUM({overlap}) DESC
        LIMIT 12
    """
    return _query_rows(tp, query)


def _trace_time_bounds(tp: Any, package_name: str) -> Dict[str, int]:
    proc_filter = _process_filter(package_name)
    if _table_exists(tp, "actual_frame_timeline_slice"):
        row = _query_first(
            tp,
            f"""
                SELECT
                    MIN(ts) AS trace_start_ns,
                    MAX(ts + dur) AS trace_end_ns,
                    MAX(ts) AS latest_frame_ts_ns,
                    COUNT(*) AS frame_count
                FROM actual_frame_timeline_slice
                JOIN process USING(upid)
                WHERE {proc_filter}
            """,
        )
        if int(row.get("frame_count") or 0) > 0:
            return {
                "trace_start_ns": int(row.get("trace_start_ns") or 0),
                "trace_end_ns": int(row.get("trace_end_ns") or 0),
                "latest_frame_ts_ns": int(row.get("latest_frame_ts_ns") or 0),
            }

    row = _query_first(
        tp,
        f"""
            SELECT
                MIN(sched.ts) AS trace_start_ns,
                MAX(sched.ts + sched.dur) AS trace_end_ns
            FROM sched
            JOIN thread USING(utid)
            JOIN process USING(upid)
            WHERE {proc_filter}
        """,
    )
    end_ns = int(row.get("trace_end_ns") or 0)
    return {
        "trace_start_ns": int(row.get("trace_start_ns") or 0),
        "trace_end_ns": end_ns,
        "latest_frame_ts_ns": end_ns,
    }


def _analysis_window(bounds: Dict[str, int], window_sec: int) -> Dict[str, int]:
    end_ns = int(bounds.get("trace_end_ns") or bounds.get("latest_frame_ts_ns") or 0)
    start_ns = max(int(bounds.get("trace_start_ns") or 0), end_ns - (window_sec * 1_000_000_000))
    return {
        "start_ns": start_ns,
        "end_ns": end_ns,
    }


def _get_trace_processor(trace_path: str):
    from perfetto.trace_processor import TraceProcessor, TraceProcessorConfig

    bin_path = os.getenv("AUTODROID_TRACE_PROCESSOR_BIN", "").strip() or None
    config = TraceProcessorConfig(
        bin_path=bin_path,
        load_timeout=TRACE_PROCESSOR_LOAD_TIMEOUT_SEC,
    )
    return TraceProcessor(trace=trace_path, config=config)


def analyze_loaded_trace(
    tp: Any,
    package_name: str,
    window_sec: int = DEFAULT_ANALYSIS_WINDOW_SEC,
    capture_mode: str = "diagnostic",
) -> Dict[str, Any]:
    bounds = _trace_time_bounds(tp, package_name)
    if capture_mode == "continuous":
        window = {
            "start_ns": int(bounds.get("trace_start_ns") or 0),
            "end_ns": int(bounds.get("trace_end_ns") or bounds.get("latest_frame_ts_ns") or 0),
        }
        analysis_scope = "full_trace"
    else:
        window = _analysis_window(bounds, window_sec)
        analysis_scope = "last_window_before_trace_end"

    has_frame_timeline = _table_exists(tp, "actual_frame_timeline_slice")
    has_sched = _table_exists(tp, "sched")
    has_slice = _table_exists(tp, "slice") and _table_exists(tp, "thread_track")

    frame_stats: Dict[str, Any] = {}
    frame_timeline_series: List[Dict[str, Any]] = []
    jank_breakdown: List[Dict[str, Any]] = []
    top_jank_frames: List[Dict[str, Any]] = []
    if has_frame_timeline:
        frame_stats = _summarize_frame_metrics(tp, package_name, window["start_ns"], window["end_ns"])
        frame_timeline_series = _build_frame_timeline_series(
            tp,
            package_name,
            int(bounds.get("trace_start_ns") or window["start_ns"]),
            int(bounds.get("trace_end_ns") or window["end_ns"]),
        )
        jank_breakdown = _query_jank_breakdown(tp, package_name, window["start_ns"], window["end_ns"])
        top_jank_frames = _query_top_jank_frames(tp, package_name, window["start_ns"], window["end_ns"])

    top_busy_threads = _query_top_busy_threads(tp, package_name, window["start_ns"], window["end_ns"])
    main_thread_summary = _query_thread_summary(
        tp,
        package_name,
        window["start_ns"],
        window["end_ns"],
        thread_clause="thread.is_main_thread = 1",
        summary_label="main_thread",
    )
    render_thread_summary = _query_thread_summary(
        tp,
        package_name,
        window["start_ns"],
        window["end_ns"],
        thread_clause="thread.name = 'RenderThread'",
        summary_label="render_thread",
    )
    hot_slices = _query_hot_slices(tp, package_name, window["start_ns"], window["end_ns"])
    suspected_causes = _build_suspected_causes(
        frame_stats,
        jank_breakdown,
        top_jank_frames,
        top_busy_threads,
        hot_slices,
    )

    if has_frame_timeline and has_sched and has_slice:
        analysis_level = "full"
    elif has_frame_timeline:
        analysis_level = "frame_timeline_only"
    else:
        analysis_level = "partial"

    actual_window_sec = round(max(0, window["end_ns"] - window["start_ns"]) / 1_000_000_000, 3)

    return {
        "status": "ANALYZED",
        "analysis": {
            "engine": "perfetto-python",
            "analysis_level": analysis_level,
            "analysis_scope": analysis_scope,
            "analysis_window_sec": actual_window_sec,
            "frame_timeline_available": has_frame_timeline,
            "trace_bounds_ns": bounds,
            "window_ns": window,
            "frame_stats": frame_stats,
            "frame_timeline_series": frame_timeline_series,
            "jank_type_breakdown": jank_breakdown,
            "top_jank_frames": top_jank_frames,
            "top_busy_threads": top_busy_threads,
            "thread_summaries": {
                "main_thread": main_thread_summary,
                "render_thread": render_thread_summary,
            },
            "hot_slices": hot_slices,
            "suspected_causes": suspected_causes,
        },
        "error": "",
    }


def analyze_perfetto_trace(
    trace_path: str,
    package_name: str,
    window_sec: int = DEFAULT_ANALYSIS_WINDOW_SEC,
    capture_mode: str = "diagnostic",
) -> Dict[str, Any]:
    if not trace_path or not os.path.exists(trace_path):
        return {
            "status": "TRACE_MISSING",
            "analysis": None,
            "error": f"trace file not found: {trace_path}",
        }

    try:
        tp = _get_trace_processor(trace_path)
    except ModuleNotFoundError as exc:
        return {
            "status": "TOOL_MISSING",
            "analysis": None,
            "error": str(exc),
        }
    except Exception as exc:
        return {
            "status": "FAILED",
            "analysis": None,
            "error": str(exc),
        }

    try:
        return analyze_loaded_trace(tp, package_name, window_sec=window_sec, capture_mode=capture_mode)
    except Exception as exc:
        return {
            "status": "FAILED",
            "analysis": None,
            "error": str(exc),
        }
    finally:
        try:
            tp.close()
        except Exception:
            pass
