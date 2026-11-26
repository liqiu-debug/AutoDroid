"""Offline Android startup trace evidence via the pinned native Trace Processor.

No runtime downloads, Python perfetto package, remote traces, or iOS TTID guesses.
This supplies bounded evidence queries; it does not label scheduling correlation
as a proven startup root cause.

The standalone implementation matches the desktop engine. For this module,
parents[2] is the AutoDroid repository root, so server resources never depend
on a sibling desktop checkout.
"""
from __future__ import annotations

import csv
import hashlib
import io
import json
import os
from pathlib import Path
import platform
import re
import subprocess
import sys
import time


def trace_processor_platform(system=None, machine=None):
    system = (system or platform.system()).lower()
    machine = (machine or platform.machine()).lower()
    if system == "darwin" and machine in {"arm64", "aarch64"}:
        return "mac-arm64"
    if system == "darwin" and machine in {"x86_64", "amd64", "x64"}:
        return "mac-x64"
    if system == "windows" and machine in {"amd64", "x86_64", "x64"}:
        return "windows-x64"
    return None


def _roots():
    roots = []
    if os.getenv("AUTODROID_ENGINE_ASSETS_DIR", "").strip():
        roots.append(Path(os.environ["AUTODROID_ENGINE_ASSETS_DIR"]))
    if getattr(sys, "_MEIPASS", None):
        frozen = Path(sys._MEIPASS)
        roots.extend([frozen, frozen / "assets"])
    roots.append(Path(__file__).resolve().parents[2] / "assets")
    return roots


def resolve_trace_processor(*, target=None):
    """Return an existing local binary, never search PATH or fetch a launcher."""
    for name in ("AUTODROID_TRACE_PROCESSOR", "AUTODROID_TRACE_PROCESSOR_BIN"):
        override = os.getenv(name, "").strip()
        if override:
            path = Path(override).expanduser()
            return path.resolve() if path.is_file() else None
    target = target or trace_processor_platform()
    if not target:
        return None
    executable = "trace_processor_shell.exe" if target.startswith("windows") else "trace_processor_shell"
    for root in _roots():
        for path in (root / "trace_processor" / target / executable, root / "trace_processor" / executable):
            if path.is_file():
                return path.resolve()
    return None


def _manifest_paths(binary):
    for parent in (binary.parent, *list(binary.parents)[:3]):
        for filename in ("manifest.json", "trace-processor-manifest.json"):
            yield parent / filename
    for root in _roots():
        for filename in ("manifest.json", "trace-processor-manifest.json"):
            yield root / "trace_processor" / filename
    yield Path(__file__).resolve().parents[2] / "scripts" / "trace-processor-manifest.json"


def _manifest(binary):
    for candidate in _manifest_paths(binary):
        if candidate.is_file():
            try:
                data = json.loads(candidate.read_text(encoding="utf-8"))
                if data.get("schema_version") == 1 and isinstance(data.get("platforms"), dict):
                    return data
            except (OSError, ValueError):
                continue
    return None


def _sha256(path):
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _command(argv, *, timeout):
    kwargs = {"capture_output": True, "text": True, "encoding": "utf-8", "errors": "replace", "timeout": timeout}
    if os.name == "nt":
        kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
    result = subprocess.run([str(arg) for arg in argv], **kwargs)
    if result.returncode:
        raise RuntimeError((result.stderr or result.stdout or f"Trace Processor exited {result.returncode}").strip()[-2000:])
    return result.stdout


def verify_trace_processor(binary_path=None, *, timeout=10):
    """Return JSON-serializable capability state with SHA256 + actual --version.

    Success: {available: True, status: 'READY', path, version, sha256, verified,
    platform, offline: True}. Failure returns available=False with reason and a
    specific status; an unverified override is never executed.
    """
    target = trace_processor_platform()
    binary = Path(binary_path).expanduser().resolve() if binary_path else resolve_trace_processor()
    base = {"available": False, "offline": True, "platform": target, "verified": False,
            "path": str(binary) if binary else None}
    if not target:
        return dict(base, status="UNSUPPORTED_PLATFORM", reason="当前系统或处理器架构不支持内置 Trace Processor")
    if not binary or not binary.is_file():
        return dict(base, status="TOOL_MISSING", reason="缺少内置 Trace Processor，请在构建部署时运行 scripts/fetch-trace-processor.py")
    manifest = _manifest(binary)
    spec = (manifest or {}).get("platforms", {}).get(target)
    if not spec:
        return dict(base, status="MANIFEST_MISSING", reason="缺少当前平台的固定版本与校验清单")
    try:
        checksum = _sha256(binary)
        if checksum != spec.get("sha256") or binary.stat().st_size != spec.get("file_size"):
            return dict(base, status="CHECKSUM_MISMATCH", sha256=checksum, reason="Trace Processor 文件大小或 SHA256 校验失败")
        version = _command([binary, "--version"], timeout=timeout).strip()
        expected = str(manifest["version"]).removeprefix("v")
        if not re.search(rf"\bPerfetto\s+v?{re.escape(expected)}(?:[-\s]|$)", version):
            return dict(base, status="VERSION_MISMATCH", sha256=checksum, reason="Trace Processor 版本与固定清单不一致")
        return dict(base, available=True, verified=True, status="READY", version=version,
                    pinned_version=manifest["version"], sha256=checksum)
    except subprocess.TimeoutExpired:
        return dict(base, status="TOOL_TIMEOUT", reason="Trace Processor 自检超时")
    except (OSError, RuntimeError) as exc:
        return dict(base, status="TOOL_ERROR", reason=str(exc))


def _csv_value(value):
    if value == "[NULL]":
        return None
    if re.fullmatch(r"-?\d+", value or ""):
        return int(value)
    if re.fullmatch(r"-?\d+\.\d+(?:[eE][+-]?\d+)?", value or ""):
        return float(value)
    return value


def _csv_tables(output):
    # Trace Processor writes load/progress diagnostics on stderr. stdout contains
    # one CSV table per SELECT with a blank line between result sets.
    tables, current = [], []
    for row in csv.reader(io.StringIO(output)):
        if not row:
            if current:
                tables.append(current)
                current = []
        else:
            current.append(row)
    if current:
        tables.append(current)
    return [[{key: _csv_value(value) for key, value in zip(table[0], row)} for row in table[1:]] for table in tables]


def query_trace(trace_path, sql, *, binary_path=None, timeout=30):
    """Run fixed local SQL, return CSV tables; no remote URL/launcher supported."""
    trace = Path(trace_path).expanduser().resolve()
    if not trace.is_file():
        raise FileNotFoundError(f"Trace file is missing: {trace}")
    binary = Path(binary_path) if binary_path else resolve_trace_processor()
    if not binary:
        raise FileNotFoundError("Trace Processor is not bundled")
    return _csv_tables(_command([binary, "query", trace, sql], timeout=timeout))


def _literal(text):
    return "'" + str(text).replace("'", "''") + "'"


def analyze_startup_trace(trace_path, package_name, *, platform_name="android", timeout=45, binary_path=None):
    """Summarize startup intervals, main-thread slices and scheduler evidence.

    Returns analysis_status=ANALYZED/PARTIAL/UNAVAILABLE/TOOL_MISSING/FAILED.
    Interval values originate from Android trace events and never from WDA.
    """
    if platform_name != "android":
        return {"analysis_status": "UNAVAILABLE", "analysis_level": "startup_evidence",
                "reason": "Perfetto 启动分析仅支持 Android；iOS WDA 控制耗时不是系统首帧指标"}
    trace = Path(trace_path).expanduser().resolve()
    if not trace.is_file():
        return {"analysis_status": "TRACE_MISSING", "reason": "原轮次 Trace 文件不存在"}
    tool = verify_trace_processor(binary_path)
    if not tool["available"]:
        return {"analysis_status": "TOOL_MISSING", "reason": tool["reason"], "tool": tool}
    package = str(package_name or "").strip()
    if not package or len(package) > 255 or "\x00" in package:
        return {"analysis_status": "FAILED", "reason": "缺少有效的 Android 包名"}
    binary = Path(tool["path"])
    deadline = time.monotonic() + max(1, timeout)
    errors = []

    def query(sql):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise subprocess.TimeoutExpired([str(binary)], timeout)
        return query_trace(trace, sql, binary_path=binary, timeout=remaining)

    result = {"analysis_status": "PARTIAL", "analysis_level": "startup_evidence", "tool": tool,
              "capture_mode": "original_sample", "package_name": package, "startup_intervals": [],
              "main_thread_slices": [], "cpu_running": [], "main_thread_states": [],
              "limitations": ["线程与调度统计是启动区间内的证据，不自动证明根因。"]}
    try:
        startups = query("INCLUDE PERFETTO MODULE android.startup.startups; "
                         f"SELECT startup_id, ts, dur, package, startup_type FROM android_startups WHERE package={_literal(package)} AND dur>=0 ORDER BY ts DESC LIMIT 20;")
        result["startup_intervals"] = startups[-1] if startups else []
    except (RuntimeError, subprocess.TimeoutExpired, OSError) as exc:
        errors.append(f"启动事件不可用：{str(exc)[-500:]}")
    intervals = result["startup_intervals"]
    if intervals:
        # A retained original-sample trace normally contains one launch. Do not
        # aggregate gaps between unrelated launches when a larger trace is used.
        selected = max(intervals, key=lambda row: row["ts"])
        start_ns = selected["ts"]
        end_ns = selected["ts"] + max(0, selected["dur"])
        result["selected_startup_id"] = selected["startup_id"]
        if len(intervals) > 1:
            result["limitations"].append("Trace 中有多次目标应用启动，线程证据限定到最近一次启动，未混合间隔。")
        result["window_source"] = "android.startup.startups"
    else:
        try:
            tables = query("SELECT start_ts, end_ts FROM trace_bounds;")
            bounds = tables[-1][0]
            start_ns, end_ns = bounds["start_ts"], bounds["end_ts"]
            result["window_source"] = "trace_bounds_fallback"
            result["limitations"].append("Trace 缺少关联的 Android 启动事件，以下证据覆盖本次录制区间，不能当作系统启动耗时。")
        except (RuntimeError, OSError, subprocess.TimeoutExpired, KeyError, IndexError) as exc:
            return dict(result, analysis_status="FAILED", reason=f"无法读取 Trace：{str(exc)[-500:]}")
    result["window"] = {"start_ns": start_ns, "end_ns": end_ns}
    process_filter = f"(p.name={_literal(package)} OR substr(p.name,1,{len(package)+1})={_literal(package + ':')})"

    def overlap(alias):
        return f"MAX(0, MIN({alias}.ts+{alias}.dur,{end_ns})-MAX({alias}.ts,{start_ns}))"

    queries = {
        "main_thread_slices": f"""SELECT p.name AS process_name, t.tid, s.name,
            ROUND(SUM({overlap('s')})/1e6,3) AS duration_ms, COUNT(*) AS occurrences
            FROM slice s JOIN thread_track tt ON s.track_id=tt.id
            JOIN thread t ON tt.utid=t.utid JOIN process p ON t.upid=p.upid
            WHERE {process_filter} AND t.is_main_thread=1 AND s.dur>=0
              AND s.ts<{end_ns} AND s.ts+s.dur>{start_ns}
            GROUP BY p.name,t.tid,s.name ORDER BY duration_ms DESC LIMIT 12;""",
        "cpu_running": f"""SELECT p.name AS process_name,t.name AS thread_name,t.tid,
            ROUND(SUM({overlap('s')})/1e6,3) AS running_ms,COUNT(*) AS sched_slices
            FROM sched s JOIN thread t ON s.utid=t.utid JOIN process p ON t.upid=p.upid
            WHERE {process_filter} AND s.dur>=0 AND s.ts<{end_ns} AND s.ts+s.dur>{start_ns}
            GROUP BY p.name,t.name,t.tid ORDER BY running_ms DESC LIMIT 12;""",
        "main_thread_states": f"""SELECT p.name AS process_name,t.tid,s.state,
            ROUND(SUM({overlap('s')})/1e6,3) AS duration_ms,COUNT(*) AS intervals
            FROM thread_state s JOIN thread t ON s.utid=t.utid JOIN process p ON t.upid=p.upid
            WHERE {process_filter} AND t.is_main_thread=1 AND s.dur>=0
              AND s.ts<{end_ns} AND s.ts+s.dur>{start_ns}
            GROUP BY p.name,t.tid,s.state ORDER BY duration_ms DESC LIMIT 12;""",
    }
    for key, sql in queries.items():
        try:
            tables = query(sql)
            result[key] = tables[-1] if tables else []
        except (RuntimeError, subprocess.TimeoutExpired, OSError) as exc:
            errors.append(f"{key}：{str(exc)[-500:]}")
    if errors:
        result["query_errors"] = errors
    has_thread_evidence = bool(result["main_thread_slices"])
    has_scheduler_evidence = bool(result["cpu_running"] and result["main_thread_states"])
    if not has_scheduler_evidence:
        result["limitations"].append("缺少目标进程的调度或主线程状态数据，无法完整评估 CPU 运行与等待时间。")
    if not has_thread_evidence:
        result["limitations"].append("缺少主线程 atrace 切片，无法展示启动期间的主线程调用区间。")
    result["analysis_status"] = "ANALYZED" if intervals and has_thread_evidence and has_scheduler_evidence and not errors else "PARTIAL"
    result["reason"] = "已提取原轮次启动、主线程和调度证据" if intervals else "未找到系统启动事件，已保留录制区间内可用的线程证据"
    if not intervals and not any(result[key] for key in queries):
        result.update(analysis_status="UNAVAILABLE", reason="Trace 中没有目标应用的启动或线程证据；需要系统调度与 App atrace 数据")
    return result
