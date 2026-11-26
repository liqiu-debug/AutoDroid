"""Android startup adapter with bounded, cancellable ADB subprocesses."""
from __future__ import annotations

import asyncio
import contextlib
import re
import time
import uuid
from pathlib import Path

from .contract import normalize_config
from .runtime import StartupCancelled, StartupInvalid, call_sync, check_cancel, interruptible_sleep


def parse_duration(value):
    text = str(value or "").strip().lstrip("+")
    if text.isdigit():
        return int(text)
    seconds = re.search(r"(\d+(?:\.\d+)?)s", text)
    millis = re.search(r"(\d+)ms", text)
    if not seconds and not millis:
        return None
    return int(float(seconds.group(1)) * 1000) if seconds and not millis else (
        (int(float(seconds.group(1)) * 1000) if seconds else 0) + (int(millis.group(1)) if millis else 0)
    )


def parse_am_start(result):
    raw = "\n".join(str(result.get(key) or "") for key in ("stdout", "stderr")).strip()
    fields = {}
    for line in raw.splitlines():
        match = re.match(r"\s*([A-Za-z]+):\s*(.*?)\s*$", line)
        if match:
            fields[match.group(1).lower()] = match.group(2)
    error = ""
    if int(result.get("returncode") or 0) != 0 or re.search(r"error:|exception", raw, re.I):
        error = raw or "ADB 启动命令失败"
    elif fields.get("status", "").lower() not in ("ok", ""):
        error = raw or "Android 未确认启动成功"
    return {
        "raw_output": raw, "error": error,
        "activity": fields.get("activity", ""), "launch_state": fields.get("launchstate", "").upper(),
        "this_time_ms": parse_duration(fields.get("thistime")),
        "total_time_ms": parse_duration(fields.get("totaltime")),
        "wait_time_ms": parse_duration(fields.get("waittime")),
    }


def _log_timings(log, package):
    values = {"displayed": None, "fully_drawn": None}
    for key, marker in (("displayed", "Displayed"), ("fully_drawn", "Fully drawn")):
        pattern = re.compile(r"\b" + marker + r"\s+([A-Za-z0-9_.$]+/[A-Za-z0-9_.$]+):\s*\+?([0-9sm.]+)", re.I)
        for line in log.splitlines():
            match = pattern.search(line)
            if match and match.group(1).split("/", 1)[0] == package:
                values[key] = {"component": match.group(1), "time_ms": parse_duration(match.group(2)), "line": line}
    return values


class AndroidStartupAdapter:
    platform = "android"

    def __init__(self, device_serial, *, adb_binary="adb", driver=None, artifact_dir=None):
        self.device_serial = str(device_serial)
        self.adb_binary = str(adb_binary)
        self.driver = getattr(driver, "_device", driver)
        self.artifact_dir = Path(artifact_dir) if artifact_dir else None
        self.package_name = ""
        self.component = ""
        self._preflight_key = None
        self._metadata = {}
        self._trace = None
        self._trace_exports = 0
        self._log_started = ""

    async def _run(self, args, *, timeout=8, stop_event=None, check=True):
        check_cancel(stop_event)
        proc = await asyncio.create_subprocess_exec(
            self.adb_binary, "-s", self.device_serial, *map(str, args),
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        communicate = asyncio.create_task(proc.communicate())
        deadline = time.monotonic() + timeout
        try:
            while not communicate.done():
                check_cancel(stop_event)
                if time.monotonic() >= deadline:
                    raise RuntimeError(f"ADB 命令超时（{timeout} 秒）: {args[0]}")
                await asyncio.wait({communicate}, timeout=min(0.1, max(0.01, deadline - time.monotonic())))
            stdout, stderr = await communicate
            result = {"stdout": stdout.decode(errors="replace").strip(), "stdout_bytes": stdout,
                      "stderr": stderr.decode(errors="replace").strip(), "returncode": proc.returncode}
            if check and proc.returncode:
                raise RuntimeError(result["stderr"] or result["stdout"] or f"ADB 命令失败: {args[0]}")
            return result
        finally:
            if proc.returncode is None:
                with contextlib.suppress(ProcessLookupError):
                    proc.terminate()
                try:
                    await asyncio.wait_for(proc.wait(), 1)
                except asyncio.TimeoutError:
                    with contextlib.suppress(ProcessLookupError):
                        proc.kill()
                    await proc.wait()
            if not communicate.done():
                communicate.cancel()
            await asyncio.gather(communicate, return_exceptions=True)

    async def _shell(self, *args, timeout=8, stop_event=None, check=True):
        return await self._run(["shell", *args], timeout=timeout, stop_event=stop_event, check=check)

    async def preflight(self, config):
        config = normalize_config(config, platform="android", device_serial=self.device_serial)
        key = (config["package_name"], config.get("activity_name"))
        if key == self._preflight_key:
            return dict(self._metadata)
        started = time.monotonic()
        state = await self._run(["get-state"])
        if state["stdout"] != "device":
            raise RuntimeError("Android 设备未连接或尚未授权本机")
        package = config["package_name"]
        installed = await self._shell("pm", "path", package)
        if not installed["stdout"].startswith("package:"):
            raise ValueError(f"目标 App 未安装: {package}")
        window = await self._shell("dumpsys", "window", "policy")
        if re.search(r"(?:mShowingLockscreen|mKeyguardShowing|keyguardShowing|isStatusBarKeyguard|mDreamingLockscreen)\s*[=:]\s*true", window["stdout"], re.I):
            raise RuntimeError("Android 设备已锁屏，请先解锁")
        if self.driver is None:
            import uiautomator2 as u2

            self.driver = await call_sync(u2.connect, self.device_serial)
        info = await call_sync(lambda: self.driver.info)
        if isinstance(info, dict) and info.get("screenOn") is False:
            raise RuntimeError("Android 屏幕未亮起，请先解锁设备")
        activity = config.get("activity_name")
        if activity:
            component = activity if "/" in activity else f"{package}/{activity}"
            if component.split("/", 1)[0] != package:
                raise ValueError("Activity 与目标包名不一致")
        else:
            resolved = await self._shell("cmd", "package", "resolve-activity", "--brief", "-a",
                                         "android.intent.action.MAIN", "-c", "android.intent.category.LAUNCHER", package)
            candidates = [line.strip().split()[-1] for line in resolved["stdout"].splitlines()
                          if "/" in line and line.strip()]
            component = next((item for item in reversed(candidates) if item.startswith(package + "/")), "")
            if not component or not re.fullmatch(r"[A-Za-z0-9_.$/-]+", component):
                raise ValueError(f"无法解析 {package} 的 Launcher Activity，请填写 Activity")
        sdk = await self._shell("getprop", "ro.build.version.sdk")
        sdk_int = int(sdk["stdout"]) if sdk["stdout"].isdigit() else 0
        perfetto = await self._shell("which", "perfetto", check=False)
        self.package_name, self.component = package, component
        self._metadata = {
            "resolved_component": component, "sdk_int": sdk_int,
            "trace_supported": sdk_int >= 29 and bool(perfetto["stdout"]) and self.artifact_dir is not None,
            "trace_reason": "" if sdk_int >= 29 and perfetto["stdout"] and self.artifact_dir else "Perfetto 不可用或未提供产物目录",
            "initialization_ms": int(round((time.monotonic() - started) * 1000)),
            "ready_poll_interval_ms": 200, "ready_query_timeout_sec": 3,
            "automation_source": "ADB 命令及前台状态确认", "cold_preserves_app_data": True,
        }
        self._preflight_key = key
        return dict(self._metadata)

    async def state(self, stop_event=None):
        result = await self._shell("pidof", self.package_name, check=False, stop_event=stop_event)
        if result["returncode"] not in (0, 1) or result["stderr"]:
            raise RuntimeError(result["stderr"] or "读取 Android 进程状态失败")
        pids = [int(pid) for pid in result["stdout"].split() if pid.isdigit()]
        if not pids:
            return {"state": "not_running", "pids": [], "process_id": None}
        activities = await self._shell("dumpsys", "activity", "activities", stop_event=stop_event)
        lines = [line for line in activities["stdout"].splitlines()
                 if re.search(r"(?:mResumedActivity|topResumedActivity|ResumedActivity)\s*[=:]", line)]
        foreground = any(re.search(r"(?<![\w.])" + re.escape(self.package_name) + r"/", line) for line in lines)
        return {"state": "foreground" if foreground else ("background" if lines else "unknown"),
                "pids": pids, "process_id": pids[0]}

    async def is_foreground(self):
        return (await self.state())["state"] == "foreground"

    async def ready_exists(self, options):
        locator, value = options["locator_type"], options["locator_value"]
        if locator == "xpath":
            def xpath_query():
                from lxml import etree

                xml = self.driver.jsonrpc.dumpWindowHierarchy(False, 50, http_timeout=3)
                if not xml:
                    raise RuntimeError("读取 Android 页面层级失败")
                root = etree.fromstring(xml.encode(), parser=etree.XMLParser(resolve_entities=False, no_network=True))
                # Match the class-tag tree exposed by uiautomator2's XPath API.
                for node in root.iter("node"):
                    node.tag = str(node.get("class") or "node").replace("$", "-")
                return bool(root.xpath(value))
            return await call_sync(xpath_query)
        key = {"text": "text", "resource_id": "resourceId", "description": "description"}[locator]
        def native_query():
            from uiautomator2._selector import Selector

            return bool(self.driver.jsonrpc.exist(Selector(**{key: value}), http_timeout=3))
        return await call_sync(native_query)

    async def _start(self, *, timed, stop_event=None):
        args = ["am", "start"] + (["-W"] if timed else [])
        return await self._shell(*args, "-a", "android.intent.action.MAIN", "-c", "android.intent.category.LAUNCHER",
                                 "-n", self.component, timeout=30, stop_event=stop_event, check=False)

    async def _wait_foreground(self, stop_event=None, seconds=10):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            check_cancel(stop_event)
            state = await self.state(stop_event)
            if state["state"] == "foreground":
                return state
            await interruptible_sleep(0.1, stop_event)
        raise RuntimeError("Android 启动后未确认目标 App 在前台")

    async def prepare(self, mode, config, stop_event=None):
        self._log_started = ""
        if mode == "cold":
            await self._shell("am", "force-stop", self.package_name, stop_event=stop_event)
        else:
            warmup = parse_am_start(await self._start(timed=True, stop_event=stop_event))
            if warmup["error"]:
                raise RuntimeError(f"热启动准备失败: {warmup['error']}")
            await self._wait_foreground(stop_event)
            if config["ready_check"]["enabled"]:
                deadline = time.monotonic() + config["ready_check"]["timeout_sec"]
                while not await self.ready_exists(config["ready_check"]):
                    if time.monotonic() >= deadline:
                        raise StartupInvalid("热启动准备阶段首页未就绪")
                    await interruptible_sleep(0.2, stop_event)
            await self._shell("input", "keyevent", "HOME", stop_event=stop_event)
        await interruptible_sleep(config["cooldown_sec"], stop_event)
        state = await self.state(stop_event)
        if mode == "cold" and state["state"] != "not_running":
            raise StartupInvalid("停止应用后进程仍存在，不能作为冷启动样本")
        if mode == "hot" and state["state"] != "background":
            raise StartupInvalid("恢复前未确认应用仍在后台，不能作为热启动样本")
        return {"state_before": state, "actual_mode": mode, "sample_valid": True}

    async def launch(self, mode, config, stop_event=None):
        started_wall = time.time()
        self._log_started = time.strftime("%m-%d %H:%M:%S", time.localtime(started_wall)) + f".{int(started_wall % 1 * 1000):03d}"
        parsed = parse_am_start(await self._start(timed=True, stop_event=stop_event))
        if parsed["error"]:
            return dict(parsed, launch_success=False)
        state_after = await self._wait_foreground(stop_event)
        actual = parsed["launch_state"].lower()
        valid = True
        reason = ""
        if mode == "hot" and actual != "hot":
            valid = False
            reason = "热恢复时 Activity 被重建" if actual in {"cold", "warm"} else "系统未返回 LaunchState，无法确认热恢复"
        elif mode == "cold" and actual and actual != "cold":
            valid = False
            reason = "系统返回的实际启动状态不是冷启动"
        return dict(parsed, launch_success=True, sample_valid=valid, invalid_reason=reason,
                    actual_mode=actual if actual in {"cold", "warm", "hot"} else ("cold" if mode == "cold" else "unknown"),
                    state_after=state_after)

    async def validate_before_launch(self, run, config, stop_event=None):
        current = await self.state(stop_event)
        before = run.get("state_before") or {}
        if run["mode"] == "cold" and current["state"] != "not_running":
            raise StartupInvalid("计时前目标应用已重新启动，不能作为冷启动样本")
        if run["mode"] == "hot" and (current["state"] != "background" or current.get("pids") != before.get("pids")):
            raise StartupInvalid("计时前后台进程状态已变化，不能作为热恢复样本")

    async def before_sample(self, run, config, stop_event=None):
        if not config["perfetto_slow_trace"]["enabled"]:
            run["trace_status"] = "DISABLED"
            return
        if not self._metadata.get("trace_supported"):
            run.update(trace_status="UNAVAILABLE", trace_error=self._metadata.get("trace_reason"))
            return
        if self._trace_exports >= 6:
            run.update(trace_status="LIMIT_REACHED", trace_error="本任务已保留六份原轮次 Trace")
            return
        token = uuid.uuid4().hex
        directory = self.artifact_dir / "traces"
        directory.mkdir(parents=True, exist_ok=True)
        config_path = directory / f"startup_{token}.cfg"
        remote_config = f"/data/local/tmp/autodroid-startup-{token}.cfg"
        remote_trace = f"/data/misc/perfetto-traces/autodroid-startup-{token}.perfetto-trace"
        config_text = '\n'.join([
            'duration_ms: 120000', 'max_file_size_bytes: 33554432',
            'buffers { size_kb: 16384 fill_policy: RING_BUFFER }',
            'data_sources { config { name: "linux.ftrace" ftrace_config {',
            'ftrace_events: "sched/sched_switch"', 'ftrace_events: "sched/sched_wakeup"',
            'ftrace_events: "binder/binder_transaction"', 'ftrace_events: "binder/binder_transaction_received"',
            'atrace_categories: "am"', 'atrace_categories: "wm"', 'atrace_categories: "view"',
            'atrace_categories: "gfx"', f'atrace_apps: "{self.package_name}"', '}}}',
            'data_sources { config { name: "linux.process_stats" process_stats_config { scan_all_processes_on_start: true } } }',
        ]) + '\n'
        config_path.write_text(config_text)
        self._trace = {"remote_config": remote_config, "remote_trace": remote_trace, "config_path": config_path, "pid": None}
        try:
            await self._run(["push", str(config_path), remote_config], stop_event=stop_event)
            await self._shell("chmod", "644", remote_config, stop_event=stop_event)
            started = await self._shell("perfetto", "--txt", "-c", remote_config, "-o", remote_trace,
                                        "--background-wait", stop_event=stop_event, check=False)
            pid = re.search(r"\b(\d+)\b", started["stdout"])
            if started["returncode"] or not pid:
                raise RuntimeError(started["stderr"] or started["stdout"] or "Perfetto 未返回进程标识")
            self._trace["pid"] = int(pid.group(1))
            run.update(trace_status="CAPTURING", trace_capture_mode="original_sample")
        except StartupCancelled:
            raise
        except Exception as exc:
            run.update(trace_status="UNAVAILABLE", trace_error=str(exc))
            await self._finish_trace(run, save=False)

    async def _finish_trace(self, run, save):
        await self._stop_trace(run)
        trace, self._trace = self._trace, None
        if not trace:
            return []
        try:
            if not save or not trace["pid"] or trace.get("finalize_failed"):
                return []
            filename = f"startup_{run['mode']}_{run['iteration']:03d}.perfetto-trace"
            local = self.artifact_dir / "traces" / filename
            await self._run(["pull", trace["remote_trace"], str(local)], timeout=30)
            if not local.is_file() or local.stat().st_size == 0:
                raise RuntimeError("原轮次 Trace 为空")
            self._trace_exports += 1
            relative = f"traces/{filename}"
            run.update(trace_status="EXPORTED", trace_path=relative, trace_exported=True)
            return [{"path": relative, "filename": filename, "size": local.stat().st_size,
                     "startup_mode": run["mode"], "iteration": run["iteration"],
                     "startup_total_time_ms": run.get("total_time_ms"), "capture_mode": "original_sample",
                     "diagnostic": False, "trigger_time": run["time"],
                     "trigger_reason": f"STARTUP_{'SLOW' if run.get('performance_passed') is False else 'FAIL'}_{run['mode'].upper()}",
                     "analysis_status": "PENDING", "analysis_level": "startup"}]
        finally:
            with contextlib.suppress(Exception):
                await self._shell("rm", "-f", trace["remote_config"], trace["remote_trace"])
            with contextlib.suppress(OSError):
                trace["config_path"].unlink()

    async def _stop_trace(self, run):
        trace = self._trace
        if not trace or not trace["pid"] or trace.get("stopped"):
            return
        await self._shell("kill", "-INT", str(trace["pid"]), check=False)
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            alive = await self._shell("kill", "-0", str(trace["pid"]), check=False)
            if alive["returncode"]:
                break
            await asyncio.sleep(0.1)
        else:
            await self._shell("kill", str(trace["pid"]), check=False)
            run["trace_error"] = "Perfetto 结束超过等待预算"
            trace["finalize_failed"] = True
        trace["stopped"] = True

    async def collect(self, run, config):
        data = {"trace_artifacts": []}
        try:
            # Stop at the end of the measured window, but retain the original
            # trace until target-app logs determine whether this round failed.
            await self._stop_trace(run)
        except Exception as exc:
            data["trace_error"] = str(exc)
        if config["capture_log"] and self._log_started:
            try:
                result = await self._shell("logcat", "-d", "-v", "threadtime", "-T", self._log_started or "1", timeout=8)
                log = result["stdout"][-2 * 1024 * 1024:]
                data.update(_log_timings(log, self.package_name))
                if self.artifact_dir:
                    directory = self.artifact_dir / "logs"
                    directory.mkdir(parents=True, exist_ok=True)
                    rel = f"logs/startup_{run['mode']}_{run['iteration']:03d}.log"
                    (self.artifact_dir / rel).write_text(log)
                    data["log_path"] = rel
                events = []
                lines = log.splitlines()
                target_crash = any(
                    "FATAL EXCEPTION" in line and re.search(r"Process:\s*" + re.escape(self.package_name) + r"(?:\b|:)", "\n".join(lines[index:index + 15]))
                    for index, line in enumerate(lines)
                )
                target_anr = bool(re.search(r"ANR in " + re.escape(self.package_name) + r"(?:\b|:)", log))
                for occurred, kind in ((target_crash, "CRASH"), (target_anr, "ANR")):
                    if occurred:
                        events.append({"type": kind, "time": run["time"], "full_log": log[-65536:],
                                       "startup_mode": run["mode"], "iteration": run["iteration"]})
                data["crash_events"] = events
                if events:
                    run.update(status="FAIL", success=False, error=run.get("error") or "采样期间应用发生崩溃或 ANR")
            except Exception as exc:
                data["log_error"] = str(exc)
        try:
            data["trace_artifacts"] = await self._finish_trace(
                run, save=run.get("performance_passed") is False or run.get("status") in {"FAIL", "ABORTED"})
        except Exception as exc:
            data.update(trace_status="EXPORT_FAILED", trace_error=str(exc))
        if self.artifact_dir and run["status"] in {"FAIL", "INVALID"}:
            try:
                result = await self._run(["exec-out", "screencap", "-p"], timeout=8)
                image = result.get("stdout_bytes", b"")
                if image.startswith(b"\x89PNG\r\n\x1a\n"):
                    directory = self.artifact_dir / "screenshots"
                    directory.mkdir(parents=True, exist_ok=True)
                    relative = f"screenshots/startup_{run['mode']}_{run['iteration']:03d}.png"
                    (self.artifact_dir / relative).write_bytes(image)
                    data["screenshot_path"] = relative
                else:
                    data["screenshot_error"] = "设备未返回有效 PNG 截图"
            except Exception as exc:
                data["screenshot_error"] = str(exc)
        return data

    async def close(self):
        if self._trace:
            await self._finish_trace({}, save=False)
