"""WDA lifecycle measurements; no Android timing fields or hidden relaunches."""
from __future__ import annotations

import base64
import contextlib
import time
from pathlib import Path

import httpx

from .contract import normalize_config
from .runtime import StartupInvalid, call_sync, check_cancel, interruptible_sleep

STATE_NAMES = {0: "unknown", 1: "not_running", 2: "background_suspended", 3: "background", 4: "foreground"}


class IOSStartupAdapter:
    platform = "ios"

    def __init__(self, driver, *, device_serial="", artifact_dir=None, http_client=None):
        self.driver = driver
        self.device_serial = str(device_serial or getattr(driver, "device_id", ""))
        self.artifact_dir = Path(artifact_dir) if artifact_dir else None
        url = str(getattr(driver, "wda_url", "") or "").rstrip("/")
        if not url:
            raise ValueError("iOS 启动测试需要该设备实际可达的 WDA 地址")
        # A synchronous connection pool is safe across the route's preflight
        # loop and the worker thread's loop. Every request has a finite timeout.
        self.http = http_client or httpx.Client(base_url=url, timeout=5, trust_env=False)
        self._owns_http = http_client is None
        self._session_id = None
        self._created_session = False
        self._settings = {}
        self._settings_changed = False
        self._preflight_key = None
        self._metadata = {}
        self.package_name = ""
        self._closed = False
        self._prepared_process_id = None

    async def _call(self, method, path, body=None, *, timeout=5):
        def request():
            response = self.http.request(method, path, json=body, timeout=timeout)
            try:
                payload = response.json()
            except ValueError:
                raise RuntimeError("WDA 返回了无效响应") from None
            value = payload.get("value") if isinstance(payload, dict) else None
            if response.status_code >= 400 or (isinstance(value, dict) and value.get("error")):
                message = value.get("message") if isinstance(value, dict) else None
                raise RuntimeError(f"WDA 请求失败: {message or response.status_code}")
            return payload
        return await call_sync(request)

    def _path(self, suffix):
        if not self._session_id:
            raise RuntimeError("WDA 会话尚未初始化")
        return f"/session/{self._session_id}{suffix}"

    async def preflight(self, config):
        config = normalize_config(config, platform="ios", device_serial=self.device_serial)
        package = config["package_name"]
        if self._preflight_key == package:
            return dict(self._metadata)
        started = time.monotonic()
        status = await self._call("GET", "/status")
        value = status.get("value") or {}
        if not isinstance(value, dict) or value.get("ready") is False:
            raise RuntimeError("WDA 尚未就绪，请确认设备已解锁且 WDA 正常运行")
        build = value.get("build") or {}
        runner_id = str(build.get("productBundleIdentifier") or build.get("product_bundle_identifier") or "")
        if package == runner_id or package.lower().endswith(".xctrunner") or "webdriveragentrunner" in package.lower():
            raise ValueError("目标 Bundle ID 不能是当前 WDA Runner")
        if (await self._call("GET", "/wda/locked")).get("value") is True:
            raise RuntimeError("iOS 设备已锁屏，请先解锁")
        self._session_id = status.get("sessionId") or value.get("sessionId")
        if not self._session_id:
            session = await self._call("POST", "/session", {"capabilities": {}}, timeout=15)
            session_value = session.get("value") or {}
            self._session_id = session.get("sessionId") or session_value.get("sessionId")
            self._created_session = True
        if not self._session_id:
            raise RuntimeError("WDA 未提供可用的 sessionId")
        self.package_name = package
        initial = await self.state()
        if initial["value"] == 0:
            raise ValueError(f"目标 App 未安装或 WDA 无法确认应用: {package}")
        settings = (await self._call("GET", self._path("/appium/settings"))).get("value") or {}
        if not isinstance(settings, dict):
            raise RuntimeError("WDA 不支持启动测量所需的等待策略设置")
        keys = ("waitForIdleTimeout", "animationCoolOffTimeout")
        if any(key not in settings for key in keys):
            raise RuntimeError("WDA 缺少等待策略能力，请升级 WDA")
        self._settings = {key: settings[key] for key in keys}
        await self._call("POST", self._path("/appium/settings"), {"settings": {key: 0 for key in keys}})
        self._settings_changed = True
        self._metadata = {
            "wda_version": build.get("version"), "initial_state": initial, "wda_runner_bundle_id": runner_id or None,
            "waitForIdleTimeout": 0, "animationCoolOffTimeout": 0,
            "ready_poll_interval_ms": 200, "ready_query_timeout_sec": 3,
            "initialization_ms": int(round((time.monotonic() - started) * 1000)),
            "trace_supported": False, "trace_reason": "iOS 不支持 Android Perfetto；首期提供应用状态与失败截图",
            "native_startup_metric": "unavailable", "cold_preserves_app_data": True,
            "automation_source": "WDA launch/activate 及前台状态确认",
        }
        self._preflight_key = package
        return dict(self._metadata)

    async def state(self):
        raw = (await self._call("POST", self._path("/wda/apps/state"), {"bundleId": self.package_name})).get("value")
        if isinstance(raw, dict):
            raw = raw.get("value", raw.get("state"))
        try:
            number = int(raw)
        except (ValueError, TypeError):
            raise RuntimeError("WDA 未返回有效应用状态") from None
        if number not in STATE_NAMES:
            raise RuntimeError(f"WDA 返回了未知应用状态: {number}")
        return {"value": number, "state": STATE_NAMES[number], "process_id": None}

    async def is_foreground(self):
        return (await self.state())["value"] == 4

    async def _wait_state(self, accepted, *, timeout=10, stop_event=None):
        deadline = time.monotonic() + timeout
        last = None
        while time.monotonic() < deadline:
            check_cancel(stop_event)
            last = await self.state()
            if last["value"] in accepted:
                return last
            await interruptible_sleep(0.1, stop_event)
        raise StartupInvalid(f"未确认 iOS 应用的预期状态，当前为 {(last or {}).get('state', 'unknown')}")

    async def ready_exists(self, options):
        using = {"accessibility_id": "accessibility id", "name": "predicate string", "label": "predicate string",
                 "predicate": "predicate string", "xpath": "xpath"}[options["locator_type"]]
        value = options["locator_value"]
        if options["locator_type"] in {"name", "label"}:
            escaped = value.replace("\\", "\\\\").replace("'", "\\'")
            value = f"{options['locator_type']} == '{escaped}'"
        result = await self._call("POST", self._path("/elements"), {"using": using, "value": value}, timeout=3)
        elements = result.get("value")
        if not isinstance(elements, list):
            raise RuntimeError("WDA 就绪查询未返回元素列表")
        return bool(elements)

    async def prepare(self, mode, config, stop_event=None):
        check_cancel(stop_event)
        if (await self._call("GET", "/wda/locked")).get("value") is True:
            raise RuntimeError("采样期间 iOS 设备已锁屏")
        if mode == "cold":
            self._prepared_process_id = None
            await self._call("POST", self._path("/wda/apps/terminate"), {"bundleId": self.package_name}, timeout=15)
            await self._wait_state({1}, stop_event=stop_event)
        else:
            # Warmup is deliberately outside the measurement interval.
            await self._call("POST", self._path("/wda/apps/launch"),
                             {"bundleId": self.package_name, "shouldWaitForQuiescence": False}, timeout=15)
            await self._wait_state({4}, stop_event=stop_event)
            try:
                current = (await self._call("GET", "/wda/activeAppInfo", timeout=3)).get("value") or {}
                self._prepared_process_id = current.get("pid") if current.get("bundleId") == self.package_name else None
            except Exception:
                self._prepared_process_id = None
            if config["ready_check"]["enabled"]:
                deadline = time.monotonic() + config["ready_check"]["timeout_sec"]
                while not await self.ready_exists(config["ready_check"]):
                    if time.monotonic() >= deadline:
                        raise StartupInvalid("热恢复准备阶段首页未就绪")
                    await interruptible_sleep(0.2, stop_event)
            await self._call("POST", "/wda/homescreen", timeout=5)
            await self._wait_state({2, 3}, stop_event=stop_event)
        await interruptible_sleep(config["cooldown_sec"], stop_event)
        state = await self.state()
        accepted = {1} if mode == "cold" else {2, 3}
        if state["value"] not in accepted:
            raise StartupInvalid("冷启动前应用仍在运行" if mode == "cold" else "后台应用已退出或不在后台，不能作为热恢复样本")
        if mode == "hot":
            state["process_id"] = self._prepared_process_id
            state["process_id_source"] = "preparation_foreground" if self._prepared_process_id is not None else "unavailable"
        return {"state_before": state, "actual_mode": mode, "sample_valid": True}

    async def validate_before_launch(self, run, config, stop_event=None):
        check_cancel(stop_event)
        # Recheck immediately before the lifecycle command. activate() can
        # otherwise launch a process reclaimed after the preparation phase.
        before = await self.state()
        if before["value"] not in ({1} if run["mode"] == "cold" else {2, 3}):
            raise StartupInvalid("计时前应用状态已变化，本轮不属于请求的启动模式")

    async def launch(self, mode, config, stop_event=None):
        check_cancel(stop_event)
        path = "/wda/apps/launch" if mode == "cold" else "/wda/apps/activate"
        body = {"bundleId": self.package_name}
        if mode == "cold":
            body["shouldWaitForQuiescence"] = False
        await self._call("POST", self._path(path), body, timeout=15)
        after = await self._wait_state({4}, stop_event=stop_event)
        finished_at = time.monotonic()
        try:
            current = (await self._call("GET", "/wda/activeAppInfo", timeout=3)).get("value") or {}
            if current.get("bundleId") == self.package_name:
                after["process_id"] = current.get("pid")
        except Exception:
            pass
        same_process = (mode != "hot" or self._prepared_process_id is None or after["process_id"] is None
                        or self._prepared_process_id == after["process_id"])
        return {"launch_success": True, "state_after": after, "sample_valid": same_process,
                "_automation_finished_at": finished_at,
                "actual_mode": mode if same_process else "cold",
                "invalid_reason": "" if same_process else "热恢复时应用进程已重建，不纳入热启动统计",
                "launch_state": "COLD" if mode == "cold" else "BACKGROUND_RESUME", "activity": "",
                "this_time_ms": None, "total_time_ms": None, "wait_time_ms": None}

    async def collect(self, run, config):
        result = {"trace_artifacts": [], "trace_status": "NOT_APPLICABLE"}
        if self.artifact_dir and run["status"] in {"FAIL", "INVALID"}:
            try:
                shot = (await self._call("GET", "/screenshot", timeout=5)).get("value")
                if not isinstance(shot, str):
                    raise RuntimeError("WDA 未返回截图")
                data = base64.b64decode(shot, validate=True)
                directory = self.artifact_dir / "screenshots"
                directory.mkdir(parents=True, exist_ok=True)
                relative = f"screenshots/startup_{run['mode']}_{run['iteration']:03d}.png"
                (self.artifact_dir / relative).write_bytes(data)
                result["screenshot_path"] = relative
            except Exception as exc:
                result["screenshot_error"] = str(exc)
        if self.artifact_dir and config["capture_log"]:
            import json

            directory = self.artifact_dir / "logs"
            directory.mkdir(parents=True, exist_ok=True)
            relative = f"logs/startup_{run['mode']}_{run['iteration']:03d}.json"
            (self.artifact_dir / relative).write_text(json.dumps(run, ensure_ascii=False, indent=2))
            result["log_path"] = relative
            result["log_source"] = "WDA 应用状态和操作结果（非 iOS 系统日志）"
        return result

    async def close(self):
        if self._closed:
            return
        self._closed = True
        try:
            if self._settings_changed and self._session_id:
                await self._call("POST", self._path("/appium/settings"), {"settings": self._settings})
                self._settings_changed = False
        finally:
            if self._created_session and self._session_id:
                with contextlib.suppress(Exception):
                    await self._call("DELETE", f"/session/{self._session_id}", timeout=5)
            if self._owns_http:
                await call_sync(self.http.close)
