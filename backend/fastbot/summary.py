"""报告汇总：性能/卡顿统计聚合与流畅度结论。"""
from typing import Dict, List, Optional

from backend.fastbot.perfetto import PerfettoSessionState


def _resolve_jank_monitoring_mode(
    enable_jank_frame_monitor: bool,
    perfetto_state: Optional[PerfettoSessionState] = None,
    use_framestats: bool = False,
) -> str:
    if not enable_jank_frame_monitor:
        return "disabled"
    base = "framestats" if use_framestats else "gfxinfo"
    if perfetto_state and (perfetto_state.started_successfully or perfetto_state.available):
        return f"{base}+perfetto"
    return base


def _compute_jank_summary(
    jank_data: List[Dict],
    jank_events: List[Dict],
    trace_artifacts: Optional[List[Dict]] = None,
    enable_jank_frame_monitor: bool = False,
    frame_timeline_supported: bool = False,
    jank_monitoring_mode: str = "disabled",
) -> Dict:
    trace_count = len(trace_artifacts or [])
    analyzed_trace_count = sum(1 for artifact in (trace_artifacts or []) if artifact.get("analysis_status") == "ANALYZED")
    active_samples = [sample for sample in jank_data if not bool(sample.get("is_idle"))]
    active_throughputs = [
        float(sample.get("render_throughput", sample.get("fps", 0)) or 0)
        for sample in active_samples
    ]
    active_jank_rates = [float(sample.get("jank_rate", 0) or 0) for sample in active_samples]
    all_jank_rates = [float(sample.get("jank_rate", 0) or 0) for sample in jank_data]

    peak_window = {}
    if active_samples:
        peak_sample = max(active_samples, key=lambda sample: float(sample.get("jank_rate", 0) or 0))
        peak_window = {
            "time": peak_sample.get("time"),
            "jank_rate": round(float(peak_sample.get("jank_rate", 0) or 0), 4),
            "render_throughput": round(float(peak_sample.get("render_throughput", peak_sample.get("fps", 0)) or 0), 1),
            "total_frames": int(peak_sample.get("total_frames", 0) or 0),
        }

    if not jank_data:
        return {
            "avg_fps": 0,
            "min_fps": 0,
            "avg_render_throughput": 0,
            "min_render_throughput": 0,
            "avg_jank_rate": 0,
            "active_avg_jank_rate": 0,
            "max_jank_rate": 0,
            "peak_jank_rate_window": {},
            "total_jank_events": len(jank_events),
            "severe_jank_events": sum(1 for e in jank_events if e.get("severity") == "CRITICAL"),
            "trace_artifact_count": trace_count,
            "analyzed_trace_count": analyzed_trace_count,
            "frame_timeline_supported": frame_timeline_supported,
            "jank_monitoring_mode": jank_monitoring_mode if enable_jank_frame_monitor else "disabled",
            "active_sample_count": 0,
        }

    fps_values = [float(p.get("fps", 0) or 0) for p in jank_data]

    result = {
        "avg_fps": round(sum(fps_values) / len(fps_values), 1),
        "min_fps": round(min(fps_values), 1),
        "avg_render_throughput": round(sum(active_throughputs) / len(active_throughputs), 1) if active_throughputs else 0,
        "min_render_throughput": round(min(active_throughputs), 1) if active_throughputs else 0,
        "avg_jank_rate": round(sum(all_jank_rates) / len(all_jank_rates), 4),
        "active_avg_jank_rate": round(sum(active_jank_rates) / len(active_jank_rates), 4) if active_jank_rates else 0,
        "max_jank_rate": round(max(active_jank_rates), 4) if active_jank_rates else round(max(all_jank_rates), 4),
        "peak_jank_rate_window": peak_window,
        "total_jank_events": len(jank_events),
        "severe_jank_events": sum(1 for e in jank_events if e.get("severity") == "CRITICAL"),
        "trace_artifact_count": trace_count,
        "analyzed_trace_count": analyzed_trace_count,
        "frame_timeline_supported": frame_timeline_supported,
        "jank_monitoring_mode": jank_monitoring_mode,
        "active_sample_count": len(active_samples),
    }

    framestats_samples = [s for s in active_samples if s.get("source") == "framestats"]
    if framestats_samples:
        all_p50 = [s["frame_time_p50_ms"] for s in framestats_samples if "frame_time_p50_ms" in s]
        all_p95 = [s["frame_time_p95_ms"] for s in framestats_samples if "frame_time_p95_ms" in s]
        all_p99 = [s["frame_time_p99_ms"] for s in framestats_samples if "frame_time_p99_ms" in s]
        all_max = [s["frame_time_max_ms"] for s in framestats_samples if "frame_time_max_ms" in s]
        result["frame_time_p50_ms"] = round(sum(all_p50) / len(all_p50), 2) if all_p50 else None
        result["frame_time_p95_ms"] = round(max(all_p95), 2) if all_p95 else None
        result["frame_time_p99_ms"] = round(max(all_p99), 2) if all_p99 else None
        result["frame_time_max_ms"] = round(max(all_max), 2) if all_max else None

    return result


def _pick_trace_effective_fps(trace_artifacts: Optional[List[Dict]]) -> float:
    analyzed_artifacts = [
        artifact for artifact in (trace_artifacts or [])
        if artifact.get("analysis_status") == "ANALYZED"
        and isinstance(artifact.get("analysis"), dict)
        and isinstance((artifact.get("analysis") or {}).get("frame_stats"), dict)
    ]
    if not analyzed_artifacts:
        return 0.0

    preferred_artifacts = [
        artifact for artifact in analyzed_artifacts
        if str(artifact.get("capture_mode") or "") == "continuous"
    ] or analyzed_artifacts

    fps_values = [
        float(((artifact.get("analysis") or {}).get("frame_stats") or {}).get("effective_fps", 0) or 0)
        for artifact in preferred_artifacts
    ]
    fps_values = [value for value in fps_values if value > 0]
    if not fps_values:
        return 0.0
    return round(sum(fps_values) / len(fps_values), 1)


def _build_jank_verdict(
    jank_summary: Dict,
    trace_artifacts: Optional[List[Dict]] = None,
) -> Dict:
    active_avg_jank_rate = float(jank_summary.get("active_avg_jank_rate", 0) or 0)
    severe_jank_events = int(jank_summary.get("severe_jank_events", 0) or 0)
    effective_fps = _pick_trace_effective_fps(trace_artifacts)

    if severe_jank_events >= 3 or active_avg_jank_rate >= 0.2 or (effective_fps > 0 and effective_fps < 40):
        return {
            "level": "POOR",
            "label": "较差",
            "reason": "活跃渲染窗口内卡顿明显，已达到需要重点排查的程度。",
            "suggestion": "优先查看严重卡顿事件和 Perfetto Trace 的首要怀疑点。",
        }
    if severe_jank_events > 0 or active_avg_jank_rate >= 0.08 or (effective_fps > 0 and effective_fps < 55):
        return {
            "level": "FAIR",
            "label": "一般",
            "reason": "存在可感知卡顿，建议结合 Trace 进一步确认瓶颈窗口。",
            "suggestion": "重点关注活跃窗口平均卡顿率和最差窗口时间点。",
        }
    return {
        "level": "GOOD",
        "label": "良好",
        "reason": "活跃渲染窗口整体平稳，未发现明显严重卡顿。",
        "suggestion": "如需进一步优化，可继续关注偶发峰值窗口。",
    }


def _compute_summary(
    perf_data: List[Dict],
    crash_events: List[Dict],
    jank_data: Optional[List[Dict]] = None,
    jank_events: Optional[List[Dict]] = None,
    trace_artifacts: Optional[List[Dict]] = None,
    enable_performance_monitor: bool = True,
    enable_jank_frame_monitor: bool = False,
    perfetto_state: Optional[PerfettoSessionState] = None,
) -> Dict:
    """汇总性能与异常统计"""
    use_framestats = any(s.get("source") == "framestats" for s in (jank_data or []))
    jank_summary = _compute_jank_summary(
        jank_data or [],
        jank_events or [],
        trace_artifacts=trace_artifacts or [],
        enable_jank_frame_monitor=enable_jank_frame_monitor,
        frame_timeline_supported=bool(perfetto_state and perfetto_state.frame_timeline_supported),
        jank_monitoring_mode=_resolve_jank_monitoring_mode(
            enable_jank_frame_monitor,
            perfetto_state=perfetto_state,
            use_framestats=use_framestats,
        ),
    )
    crashes = sum(1 for e in crash_events if e["type"] == "CRASH")
    anrs = sum(1 for e in crash_events if e["type"] == "ANR")

    if not perf_data:
        summary = {
            "avg_cpu": 0, "max_cpu": 0,
            "avg_mem": 0, "max_mem": 0,
            "total_crashes": crashes, "total_anrs": anrs,
            "performance_monitor_enabled": enable_performance_monitor,
            "jank_frame_monitor_enabled": enable_jank_frame_monitor,
        }
        summary.update(jank_summary)
        summary["verdict"] = _build_jank_verdict(jank_summary, trace_artifacts=trace_artifacts or [])
        return summary

    cpus = [p["cpu"] for p in perf_data]
    mems = [p["mem"] for p in perf_data]

    summary = {
        "avg_cpu": round(sum(cpus) / len(cpus), 1),
        "max_cpu": round(max(cpus), 1),
        "avg_mem": round(sum(mems) / len(mems), 1),
        "max_mem": round(max(mems), 1),
        "total_crashes": crashes,
        "total_anrs": anrs,
        "performance_monitor_enabled": enable_performance_monitor,
        "jank_frame_monitor_enabled": enable_jank_frame_monitor,
    }
    summary.update(jank_summary)
    summary["verdict"] = _build_jank_verdict(jank_summary, trace_artifacts=trace_artifacts or [])
    return summary
