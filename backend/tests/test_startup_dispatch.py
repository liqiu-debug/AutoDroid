"""Startup command identity, routing, cancellation and recovery without devices."""
import json
import tempfile
import unittest
import asyncio
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient
from jose import jwt
from sqlmodel import Session, SQLModel, create_engine, select

from backend import startup_dispatch as dispatch
from backend.api import client_devices, client_fastbot, fastbot
from backend.core.security import ALGORITHM, SECRET_KEY
from backend.database import get_session
from backend.execution_limiter import reset_execution_limiter
from backend.models import ClientInstance, Device, FastbotReport, FastbotTask, StartupExecution, User


class StartupDispatchTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.engine = create_engine(f"sqlite:///{Path(self.tmp.name) / 'commands.db'}", connect_args={"check_same_thread": False})
        SQLModel.metadata.create_all(self.engine)
        self.session = Session(self.engine)
        self.alice = User(username="alice", hashed_password="x")
        self.bob = User(username="bob", hashed_password="x")
        self.session.add_all([self.alice, self.bob])
        self.session.commit()
        self.session.refresh(self.alice)
        self.session.refresh(self.bob)
        self.session.add(ClientInstance(id="desktop-a", owner_id=self.alice.id,
            startup_capabilities=dispatch.default_capabilities(), last_seen_at=datetime.now()))
        self.session.add(Device(serial="phone-a", platform="android", status="IDLE", owner_id=self.alice.id,
            client_instance_id="desktop-a", last_seen_at=datetime.now()))
        self.session.commit()
        app = FastAPI()
        app.include_router(fastbot.router, prefix="/api/fastbot")
        app.include_router(client_fastbot.router, prefix="/api/client-fastbot")
        app.include_router(client_devices.router, prefix="/api/client-devices")
        app.dependency_overrides[get_session] = lambda: self.session
        self.client = TestClient(app)
        self.headers = {"Authorization": "Bearer " + jwt.encode({"sub": "alice"}, SECRET_KEY, algorithm=ALGORITHM)}
        self.bob_headers = {"Authorization": "Bearer " + jwt.encode({"sub": "bob"}, SECRET_KEY, algorithm=ALGORITHM)}
        self.config = dict(package_name="com.example.app", platform="android", device_serials=["phone-a"], request_id="request-a",
            iterations=2, cooldown_sec=0, startup_modes=["cold"], perfetto_slow_trace={"enabled": False})
        reset_execution_limiter()

    def tearDown(self):
        for lease in list(dispatch._leases.values()):
            lease.limiter_lease.release()
        dispatch._leases.clear()
        dispatch._stops.clear()
        self.session.close()
        self.engine.dispose()
        self.tmp.cleanup()
        reset_execution_limiter()

    def submit(self, config=None):
        response = self.client.post("/api/fastbot/startup/run", json=config or self.config, headers=self.headers)
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()[0]

    def claim(self):
        response = self.client.post("/api/client-fastbot/startup/claim", json={"client_instance_id": "desktop-a"}, headers=self.headers)
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()["command"]

    @staticmethod
    def guard(command):
        return {key: command[key] for key in ("client_instance_id", "attempt", "claim_token")}

    def test_web_dispatch_uses_desktop_and_same_request_returns_same_task(self):
        with patch.object(dispatch, "make_server_adapter", side_effect=AssertionError("must not touch server devices")):
            task = self.submit()
            again = self.submit()
        self.assertEqual(task["id"], again["id"])
        self.assertEqual(task["executor_kind"], "desktop")
        self.assertEqual(task["status"], "QUEUED")
        self.assertEqual(len(self.session.exec(select(FastbotTask)).all()), 1)
        self.assertEqual(len(self.session.exec(select(FastbotReport)).all()), 1)
        self.assertEqual(self.session.exec(select(Device)).one().lease_task_id, f"startup:{task['id']}")
        changed = self.client.post("/api/fastbot/startup/run", json={**self.config, "iterations": 3}, headers=self.headers)
        self.assertEqual(changed.status_code, 409)

    def test_foreign_account_cannot_dispatch_claim_or_observe_task_list(self):
        task = self.submit()
        rejected = self.client.post("/api/fastbot/startup/run", json={**self.config, "request_id": "bob-run"}, headers=self.bob_headers)
        self.assertEqual(rejected.status_code, 403)
        rejected = self.client.post("/api/client-fastbot/startup/claim", json={"client_instance_id": "desktop-a"}, headers=self.bob_headers)
        self.assertEqual(rejected.status_code, 403)
        listed = self.client.get("/api/fastbot/startup/tasks", headers=self.bob_headers)
        self.assertEqual(listed.json()["total"], 0)
        stop = self.client.post(f"/api/fastbot/startup/tasks/{task['id']}/stop", headers=self.bob_headers)
        self.assertEqual(stop.status_code, 403)

    def test_claim_is_idempotent_and_progress_and_finish_are_fenced(self):
        task = self.submit()
        command = self.claim()
        self.assertEqual(self.claim()["claim_token"], command["claim_token"])
        invalid = self.client.post(f"/api/client-fastbot/startup/{task['id']}/progress",
            json={**self.guard(command), "attempt": 99, "progress": {}}, headers=self.headers)
        self.assertEqual(invalid.status_code, 409)
        report = {"summary": {"completed_count": 1, "requested_count": 2, "valid_count": 1,
            "startup_runs": [{"mode": "cold", "total_time_ms": 100}]}}
        progress = self.client.post(f"/api/client-fastbot/startup/{task['id']}/progress",
            json={**self.guard(command), "progress": {"completed": 1, "requested": 2}, "partial_report": report}, headers=self.headers)
        self.assertEqual(progress.status_code, 200, progress.text)
        read = self.client.get(f"/api/fastbot/tasks/{task['id']}").json()
        self.assertEqual(read["progress"]["completed_count"], 1)
        self.assertEqual(read["summary"]["startup_runs"][0]["total_time_ms"], 100)
        finished = self.client.post(f"/api/client-fastbot/startup/{task['id']}/finish",
            json={**self.guard(command), "status": "COMPLETED", "report": report}, headers=self.headers)
        self.assertEqual(finished.status_code, 200, finished.text)
        duplicate = self.client.post(f"/api/client-fastbot/startup/{task['id']}/finish",
            json={**self.guard(command), "status": "FAILED", "report": {}}, headers=self.headers)
        self.assertEqual(duplicate.json()["status"], "COMPLETED")
        self.session.expire_all()
        self.assertEqual(self.session.exec(select(Device)).one().status, "IDLE")
        self.assertEqual(len(self.session.exec(select(FastbotReport)).all()), 1)

    def test_live_report_reads_cannot_start_trace_analysis(self):
        task = self.submit()
        command = self.claim()
        partial = {"summary": {"completed_count": 1}, "trace_artifacts": [{"path": "traces/slow.perfetto-trace"}]}
        self.client.post(f"/api/client-fastbot/startup/{task['id']}/progress",
            json={**self.guard(command), "partial_report": partial}, headers=self.headers)
        with patch("backend.fastbot.perfetto._analyze_exported_traces", side_effect=AssertionError("live analysis")):
            response = self.client.get(f"/api/fastbot/reports/{task['id']}")
            self.assertEqual(response.status_code, 200, response.text)
            ai = self.client.post(f"/api/fastbot/reports/{task['id']}/trace_ai_summary",
                json={"trace_path": "traces/slow.perfetto-trace"})
            self.assertEqual(ai.status_code, 409, ai.text)

    def test_slow_terminal_analysis_uses_latest_summary_and_preserves_recovered_artifacts(self):
        task = self.submit()
        command = self.claim()
        self.client.post(f"/api/client-fastbot/startup/{task['id']}/finish",
            json={**self.guard(command), "status": "COMPLETED", "report": {"summary": {"completed_count": 2},
                  "trace_artifacts": [{"path": "traces/slow.perfetto-trace"}]}}, headers=self.headers)
        def update_summary(package_name, artifacts, events):
            artifacts[0]["analysis_status"] = "ANALYZED"
            with Session(self.engine) as writer:
                row = writer.exec(select(FastbotReport).where(FastbotReport.task_id == task["id"])).one()
                summary = json.loads(row.summary)
                summary["recovered_marker"] = "newer than analysis"
                row.summary = json.dumps(summary)
                writer.add(row)
                writer.commit()
        with patch("backend.fastbot.perfetto._analyze_exported_traces", side_effect=update_summary):
            response = self.client.get(f"/api/fastbot/reports/{task['id']}")
        self.assertEqual(response.json()["summary"]["recovered_marker"], "newer than analysis")
        self.assertEqual(response.json()["summary"]["analyzed_trace_count"], 1)

        row = self.session.exec(select(FastbotReport).where(FastbotReport.task_id == task["id"])).one()
        row.trace_artifacts = json.dumps([{"path": "traces/slow.perfetto-trace"}])
        self.session.add(row)
        self.session.commit()
        def recover_new_trace(package_name, artifacts, events):
            artifacts[0]["analysis_status"] = "ANALYZED"
            with Session(self.engine) as writer:
                row = writer.exec(select(FastbotReport).where(FastbotReport.task_id == task["id"])).one()
                row.trace_artifacts = json.dumps([{"path": "traces/recovered.perfetto-trace", "analysis_status": "DISABLED"}])
                writer.add(row)
                writer.commit()
        with patch("backend.fastbot.perfetto._analyze_exported_traces", side_effect=recover_new_trace):
            response = self.client.get(f"/api/fastbot/reports/{task['id']}")
        self.assertEqual(response.json()["trace_artifacts"][0]["path"], "traces/recovered.perfetto-trace")

    def test_expired_claim_retains_partial_and_recovery_only_adds_evidence(self):
        task = self.submit()
        command = self.claim()
        partial = {"summary": {"startup_runs": [{"sample_valid": True}], "completed_count": 1, "requested_count": 2}}
        self.client.post(f"/api/client-fastbot/startup/{task['id']}/progress",
            json={**self.guard(command), "partial_report": partial}, headers=self.headers)
        row = dispatch.execution_for_task(self.session, task["id"])
        row.lease_expires_at = datetime.now() - timedelta(seconds=1)
        self.session.add(row)
        self.session.commit()
        self.assertEqual(dispatch.reap_commands(self.session), 1)
        self.assertIsNone(self.claim())
        recovered = self.client.post(f"/api/client-fastbot/startup/{task['id']}/recover",
            json={**self.guard(command), "claim_token": "", "run_id": command["run_id"]}, headers=self.headers)
        self.assertEqual(recovered.status_code, 200, recovered.text)
        self.assertEqual(recovered.json()["status"], "ABORTED")
        finish = self.client.post(f"/api/client-fastbot/startup/{task['id']}/finish",
            json={**self.guard(recovered.json()), "status": "COMPLETED", "report": partial}, headers=self.headers)
        self.assertEqual(finish.json()["status"], "ABORTED")
        self.assertEqual(dispatch._summary(self.session, task["id"])["completed_count"], 1)

    def test_cancel_queued_releases_device_and_preserves_report(self):
        task = self.submit()
        stopped = self.client.post(f"/api/fastbot/startup/tasks/{task['id']}/stop", headers=self.headers)
        self.assertEqual(stopped.json()["status"], "ABORTED")
        self.assertEqual(self.session.exec(select(Device)).one().status, "IDLE")
        self.assertIsNone(self.claim())

    def test_local_create_preserves_run_id_and_requires_device_instance(self):
        payload = dict(run_id="local-stable-id", kind="startup", package_name=self.config["package_name"],
            device_serial="phone-a", client_instance_id="desktop-a", startup_config=self.config)
        response = self.client.post("/api/client-fastbot/", json=payload, headers=self.headers)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["run_id"], "local-stable-id")
        again = self.client.post("/api/client-fastbot/", json=payload, headers=self.headers)
        self.assertEqual(again.status_code, 200, again.text)
        self.assertEqual(response.json()["claim_token"], again.json()["claim_token"])

    def test_batch_preflight_failure_does_not_reserve_first_device(self):
        self.session.add(Device(serial="phone-b", owner_id=self.alice.id, client_instance_id="old-desktop",
            last_seen_at=datetime.now(), status="IDLE"))
        self.session.commit()
        response = self.client.post("/api/fastbot/startup/run", headers=self.headers,
            json={**self.config, "device_serials": ["phone-a", "phone-b"]})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(self.session.exec(select(Device).where(Device.serial == "phone-a")).one().status, "IDLE")
        self.assertEqual(len(self.session.exec(select(FastbotTask)).all()), 0)

    def test_artifact_requires_attempt_guard_and_namespaces_output(self):
        task = self.submit()
        command = self.claim()
        with patch.object(client_fastbot, "REPORTS_DIR", self.tmp.name):
            no_guard = self.client.post(f"/api/client-fastbot/{task['id']}/artifacts",
                data={"path": "screenshots/failure.png"}, files={"file": ("f.png", b"png", "image/png")}, headers=self.headers)
            self.assertEqual(no_guard.status_code, 403)
            uploaded = self.client.post(f"/api/client-fastbot/{task['id']}/artifacts",
                data={"path": "screenshots/failure.png", **self.guard(command)},
                files={"file": ("f.png", b"png", "image/png")}, headers=self.headers)
            self.assertEqual(uploaded.status_code, 200, uploaded.text)
            self.assertIn("/attempts/1/screenshots/failure.png", uploaded.json()["path"])

    def test_migration_restart_closes_server_and_client_running_without_relaunch(self):
        task = self.submit()
        command = self.claim()
        self.assertEqual(dispatch.reap_commands(self.session, restart=True), 1)
        self.assertEqual(self.session.get(FastbotTask, task["id"]).status, "ABORTED")
        self.assertEqual(json.loads(self.session.exec(select(FastbotReport)).one().summary)["status"], "ABORTED")

    def test_server_adapter_is_created_only_after_lease_and_same_report_is_updated(self):
        self.session.add(Device(serial="server-ios", platform="ios", status="IDLE", owner_id=self.alice.id))
        self.session.commit()
        config = {**self.config, "platform": "ios", "device_serials": ["server-ios"], "request_id": "ios-run"}
        commands = dispatch.submit_commands(self.session, self.alice, config,
            [self.session.exec(select(Device).where(Device.serial == "server-ios")).one()])
        task_id = commands[0].task_id
        events = []
        class FakeAdapter:
            platform = "ios"
            device_serial = "server-ios"
            async def close(self):
                events.append("close")
        def make_adapter(session, device, **kwargs):
            self.assertIn(task_id, dispatch._leases)
            self.assertEqual(device.lease_task_id, f"startup:{task_id}")
            events.append("factory")
            return FakeAdapter()
        async def run(adapter, config, *, stop_event, progress):
            report = {"summary": {"status": "COMPLETED", "completed_count": 2, "requested_count": 2,
                "valid_count": 2, "startup_runs": [{"platform": "ios", "automation_ms": 20}]}}
            await progress(report)
            await adapter.close()
            return report
        with patch.object(dispatch, "make_server_adapter", side_effect=make_adapter), \
             patch("backend.startup.runner.run_startup", side_effect=run):
            asyncio.run(dispatch.execute_server_command(task_id, db_engine=self.engine))
        self.session.expire_all()
        self.assertEqual(self.session.get(FastbotTask, task_id).status, "COMPLETED")
        self.assertEqual(events, ["factory", "close"])
        self.assertEqual(len(self.session.exec(select(FastbotReport)).all()), 1)
        self.assertEqual(self.session.exec(select(Device).where(Device.serial == "server-ios")).one().status, "IDLE")

    def test_server_long_preflight_and_trace_analysis_keep_lease_and_same_report(self):
        from backend.startup import trace_analysis
        self.session.add(Device(serial="server-android", platform="android", status="IDLE", owner_id=self.alice.id))
        self.session.commit()
        commands = dispatch.submit_commands(self.session, self.alice,
            {**self.config, "device_serials": ["server-android"], "request_id": "server-long-phases"},
            [self.session.exec(select(Device).where(Device.serial == "server-android")).one()])
        task_id = commands[0].task_id
        events, heartbeat_phases = [], []
        phase = {"value": "constructing"}
        class FakeAdapter:
            platform = "android"
            device_serial = "server-android"
            async def close(self):
                events.append("close")
        def real_or_mock_factory(session, device, *, artifact_dir):
            import time
            root = Path(artifact_dir) / "traces"
            root.mkdir(parents=True)
            (root / "original.perfetto-trace").write_bytes(b"original-sample")
            time.sleep(0.12)
            return FakeAdapter()
        async def run(adapter, config, *, stop_event, progress):
            phase["value"] = "measuring"
            result = {"summary": {"status": "COMPLETED", "completed_count": 1, "requested_count": 1,
                                 "startup_runs": [{"total_time_ms": 123}]},
                      "trace_artifacts": [{"path": "traces/original.perfetto-trace", "analysis_status": "PENDING",
                                           "analysis_level": "startup", "capture_mode": "original_sample"}]}
            await progress(result)
            await adapter.close()
            return result
        def analyze(path, package, **kwargs):
            import time
            self.assertEqual(events, ["close"])
            self.assertEqual(Path(path).read_bytes(), b"original-sample")
            phase["value"] = "analyzing"
            # Many shortened leases elapse without any report progress calls.
            time.sleep(0.14)
            return {"analysis_status": "PARTIAL", "analysis_level": "startup_evidence",
                    "main_thread_slices": [{"name": "bindApplication", "duration_ms": 5}],
                    "reason": "原轮次线程证据"}
        actual_renew = dispatch._renew_server_command
        def renew(*args):
            renewed = actual_renew(*args)
            heartbeat_phases.append(phase["value"])
            with Session(self.engine) as observer:
                dispatch.reap_commands(observer)
                current = dispatch.execution_for_task(observer, task_id)
                self.assertFalse(current.cancel_requested)
            return renewed
        with patch.object(dispatch, "SERVER_HEARTBEAT_SECONDS", 0.01), patch.object(dispatch, "LEASE_SECONDS", 0.04), \
             patch.object(dispatch, "_renew_server_command", side_effect=renew), \
             patch.object(dispatch, "make_server_adapter", side_effect=real_or_mock_factory), \
             patch("backend.fastbot.reporting.FASTBOT_REPORTS_DIR", self.tmp.name), \
             patch("backend.startup.runner.run_startup", side_effect=run), \
             patch.object(trace_analysis, "analyze_startup_trace", side_effect=analyze):
            asyncio.run(dispatch.execute_server_command(task_id, db_engine=self.engine))
        self.assertGreater(heartbeat_phases.count("constructing"), 2)
        self.assertGreater(heartbeat_phases.count("analyzing"), 2)
        self.session.expire_all()
        self.assertEqual(self.session.get(FastbotTask, task_id).status, "COMPLETED")
        reports = self.session.exec(select(FastbotReport).where(FastbotReport.task_id == task_id)).all()
        self.assertEqual(len(reports), 1)
        artifact = json.loads(reports[0].trace_artifacts)[0]
        self.assertEqual(artifact["path"], f"reports/fastbot/{task_id}/traces/original.perfetto-trace")
        self.assertEqual(artifact["analysis_status"], "PARTIAL")
        self.assertEqual(artifact["analysis_level"], "startup_evidence")
        with patch("backend.fastbot.perfetto._analyze_exported_traces", side_effect=AssertionError("generic analysis")):
            read = self.client.get(f"/api/fastbot/reports/{task_id}")
        self.assertEqual(read.json()["trace_artifacts"][0]["analysis"]["main_thread_slices"][0]["duration_ms"], 5)
        with patch("backend.api.fastbot.summarize_jank_analysis", side_effect=AssertionError("legacy AI")):
            ai = self.client.post(f"/api/fastbot/reports/{task_id}/trace_ai_summary",
                                  json={"trace_path": artifact["path"]})
        self.assertEqual(ai.status_code, 400)
        self.assertNotIn(task_id, dispatch._leases)

    def test_server_trace_missing_tool_or_invalid_path_finishes_unavailable(self):
        command = StartupExecution(task_id=42, run_id="offline", request_key="k", owner_id=self.alice.id,
            node_owner_id=self.alice.id, device_serial="phone-a", platform="android", executor_kind="server",
            config=self.config, config_digest="x")
        result = {"trace_artifacts": [{"path": "traces/original.perfetto-trace", "analysis_status": "PENDING"},
                                       {"path": "traces/../../outside.perfetto-trace", "analysis_status": "PENDING"}]}
        with patch("backend.startup.trace_analysis.analyze_startup_trace", return_value={
            "analysis_status": "TOOL_MISSING", "tool": {"status": "UNSUPPORTED_PLATFORM"},
            "reason": "当前系统或处理器架构不支持内置 Trace Processor"}) as analysis:
            asyncio.run(dispatch._analyze_server_startup_traces(result, command, Path(self.tmp.name)))
        self.assertEqual(analysis.call_count, 1)
        self.assertTrue(all(item["analysis_status"] == "UNAVAILABLE" for item in result["trace_artifacts"]))
        self.assertEqual(result["trace_artifacts"][0]["analysis"]["source_status"], "TOOL_MISSING")

    def test_server_heartbeat_syncs_cancel_and_fences_finished_attempt(self):
        self.session.add(Device(serial="server-android", platform="android", status="IDLE", owner_id=self.alice.id))
        self.session.commit()
        command = dispatch.submit_commands(self.session, self.alice,
            {**self.config, "device_serials": ["server-android"], "request_id": "server-control"},
            [self.session.exec(select(Device).where(Device.serial == "server-android")).one()])[0]
        command.status = "RUNNING"
        command.attempt = 1
        command.claim_token = "fenced"
        command.cancel_requested = True
        self.session.add(command)
        self.session.commit()
        stop = dispatch.threading.Event()
        self.assertTrue(dispatch._renew_server_command(command, stop, self.engine))
        self.assertTrue(stop.is_set())
        self.session.refresh(command)
        command.status = "COMPLETED"
        command.lease_expires_at = None
        self.session.add(command)
        self.session.commit()
        self.assertFalse(dispatch._renew_server_command(command, stop, self.engine))
        self.session.refresh(command)
        self.assertIsNone(command.lease_expires_at)

    def test_server_shutdown_reaches_same_runner_event_during_preflight_and_sample(self):
        from backend.startup.runtime import check_cancel
        for phase in ("preflight", "sample"):
            with self.subTest(phase=phase):
                serial = f"server-shutdown-{phase}"
                self.session.add(Device(serial=serial, platform="android", status="IDLE", owner_id=self.alice.id))
                self.session.commit()
                command = dispatch.submit_commands(self.session, self.alice,
                    {**self.config, "device_serials": [serial], "request_id": serial},
                    [self.session.exec(select(Device).where(Device.serial == serial)).one()])[0]
                task_id, events, seen = command.task_id, [], {}
                class FakeAdapter:
                    platform = "android"
                    device_serial = serial
                    async def preflight(adapter, config):
                        events.append("preflight")
                        if phase == "preflight":
                            await receive_shutdown()
                        return {}
                    async def prepare(adapter, mode, config, stop_event):
                        self.assertIs(stop_event, seen["before_lease"])
                        events.append("sample")
                        await receive_shutdown()
                        check_cancel(stop_event)
                        return {}
                    async def close(adapter):
                        events.append("close")
                async def receive_shutdown():
                    current = dispatch._stops[task_id]
                    self.assertIs(current, seen["before_lease"])
                    await asyncio.sleep(0.03)
                    dispatch.shutdown_startup_runs()
                    self.assertTrue(current.is_set())
                original_acquire = dispatch._acquire_execution_lease
                def acquire(command, db_engine):
                    before = dispatch._stops[task_id]
                    seen["before_lease"] = before
                    lease = original_acquire(command, db_engine)
                    self.assertIs(dispatch._stops[task_id], before)
                    return lease
                with patch.object(dispatch, "_acquire_execution_lease", side_effect=acquire), \
                     patch.object(dispatch, "make_server_adapter", return_value=FakeAdapter()):
                    # The actual shared runner performs cancellation and close.
                    asyncio.run(dispatch.execute_server_command(task_id, db_engine=self.engine))
                self.session.expire_all()
                self.assertEqual(self.session.get(FastbotTask, task_id).status, "ABORTED")
                self.assertEqual(events, ["preflight", "close"] if phase == "preflight" else ["preflight", "sample", "close"])
                self.assertNotIn(task_id, dispatch._stops)
                self.assertNotIn(task_id, dispatch._leases)
                device = self.session.exec(select(Device).where(Device.serial == serial)).one()
                self.assertEqual(device.status, "IDLE")

    def test_recover_completed_local_result_can_finish_before_lease_expiry(self):
        task = self.submit()
        command = self.claim()
        response = self.client.post(f"/api/client-fastbot/startup/{task['id']}/recover", headers=self.headers,
            json={**self.guard(command), "claim_token": "", "run_id": command["run_id"], "local_status": "COMPLETED"})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["status"], "RUNNING")
        done = self.client.post(f"/api/client-fastbot/startup/{task['id']}/finish", headers=self.headers,
            json={**self.guard(response.json()), "status": "COMPLETED", "report": {"summary": {"completed_count": 2}}})
        self.assertEqual(done.json()["status"], "COMPLETED")

    def test_claim_skips_locally_active_run_and_dispatches_other_device(self):
        self.session.add(Device(serial="phone-b", platform="android", owner_id=self.alice.id,
            client_instance_id="desktop-a", last_seen_at=datetime.now(), status="IDLE"))
        self.session.commit()
        first = self.submit()
        active = self.claim()
        second = self.submit({**self.config, "request_id": "request-b", "device_serials": ["phone-b"]})
        response = self.client.post("/api/client-fastbot/startup/claim", headers=self.headers,
            json={"client_instance_id": "desktop-a", "active_run_ids": [active["run_id"]]})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["command"]["task_id"], second["id"])
        self.assertNotEqual(first["id"], second["id"])

    def test_device_instance_registration_can_switch_accounts_without_adopting_old_run(self):
        queued = self.submit()
        response = self.client.post("/api/client-devices/register", headers=self.bob_headers, json={
            "platform": "android", "serial": "phone-a", "model": "Pixel", "brand": "google",
            "android_version": "14", "os_version": "14", "resolution": "1080x2400",
            "client_instance_id": "desktop-a", "startup_capabilities": dispatch.default_capabilities(),
        })
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(self.session.get(ClientInstance, "desktop-a").owner_id, self.bob.id)
        self.assertEqual(self.session.get(FastbotTask, queued["id"]).status, "ABORTED")
        self.assertEqual(self.session.exec(select(Device)).one().owner_id, self.bob.id)

    def test_logged_out_instance_rebind_without_device_keeps_old_attempt_terminal_and_fenced(self):
        task = self.submit()
        command = self.claim()
        rejected = self.client.post("/api/client-fastbot/startup/claim", headers=self.bob_headers,
            json={"client_instance_id": "desktop-a", "capabilities": dispatch.default_capabilities()})
        self.assertEqual(rejected.status_code, 403)
        logout = self.client.post("/api/client-devices/logout", headers=self.headers,
                                 json={"client_instance_id": "desktop-a"})
        self.assertEqual(logout.status_code, 200)
        rebind = self.client.post("/api/client-fastbot/startup/claim", headers=self.bob_headers,
            json={"client_instance_id": "desktop-a", "capabilities": dispatch.default_capabilities()})
        self.assertEqual(rebind.status_code, 200, rebind.text)
        self.assertIsNone(rebind.json()["command"])
        self.session.expire_all()
        self.assertEqual(self.session.get(ClientInstance, "desktop-a").owner_id, self.bob.id)
        row = dispatch.execution_for_task(self.session, task["id"])
        self.assertEqual(row.status, "ABORTED")
        self.assertTrue(row.cancel_requested)
        self.assertIsNotNone(row.reservation_release_at)
        self.assertEqual(self.session.exec(select(Device)).one().lease_task_id, f"startup:{task['id']}")
        late = self.client.post(f"/api/client-fastbot/startup/{task['id']}/progress", headers=self.headers,
                               json={**self.guard(command), "partial_report": {"summary": {"status": "RUNNING"}}})
        self.assertEqual(late.status_code, 409)
        old_account = self.client.post("/api/client-fastbot/startup/claim", headers=self.headers,
                                      json={"client_instance_id": "desktop-a"})
        self.assertEqual(old_account.status_code, 403)
        wrong_owner = self.client.post(f"/api/client-fastbot/startup/{task['id']}/recover", headers=self.bob_headers,
            json={**self.guard(command), "run_id": command["run_id"], "claim_token": ""})
        self.assertEqual(wrong_owner.status_code, 403)
        # The original owner's recovery can only upload evidence, never resume.
        recovered = self.client.post(f"/api/client-fastbot/startup/{task['id']}/recover", headers=self.headers,
            json={**self.guard(command), "run_id": command["run_id"], "claim_token": "", "local_status": "COMPLETED"})
        self.assertEqual(recovered.status_code, 200, recovered.text)
        self.assertEqual(recovered.json()["status"], "ABORTED")
        finish = self.client.post(f"/api/client-fastbot/startup/{task['id']}/finish", headers=self.headers,
            json={**self.guard(recovered.json()), "status": "COMPLETED", "report": {"summary": {"completed_count": 1}}})
        self.assertEqual(finish.json()["status"], "ABORTED")

    def test_fresh_physical_register_rebind_aborts_running_old_account(self):
        task = self.submit()
        command = self.claim()
        response = self.client.post("/api/client-devices/register", headers=self.bob_headers, json={
            "platform": "android", "serial": "phone-a", "model": "Pixel", "brand": "google",
            "android_version": "14", "os_version": "14", "resolution": "1080x2400",
            "client_instance_id": "desktop-a", "startup_capabilities": dispatch.default_capabilities(),
        })
        self.assertEqual(response.status_code, 200, response.text)
        self.session.expire_all()
        self.assertEqual(self.session.get(ClientInstance, "desktop-a").owner_id, self.bob.id)
        row = dispatch.execution_for_task(self.session, task["id"])
        self.assertEqual(row.status, "ABORTED")
        self.assertIsNotNone(row.reservation_release_at)
        device = self.session.exec(select(Device)).one()
        self.assertEqual(device.owner_id, self.bob.id)
        self.assertEqual(device.lease_task_id, f"startup:{task['id']}")
        rejected = self.client.post(f"/api/client-fastbot/startup/{task['id']}/progress", headers=self.headers,
                                   json={**self.guard(command), "progress": {}})
        self.assertEqual(rejected.status_code, 409)

    def test_lost_desktop_reservation_is_held_during_grace_then_released_with_owner_fence(self):
        task = self.submit()
        self.claim()
        row = dispatch.execution_for_task(self.session, task["id"])
        row.lease_expires_at = datetime.now() - timedelta(seconds=1)
        self.session.add(row)
        self.session.commit()
        dispatch.reap_commands(self.session)
        self.session.expire_all()
        device = self.session.exec(select(Device)).one()
        self.assertEqual(device.lease_task_id, f"startup:{task['id']}")
        row = dispatch.execution_for_task(self.session, task["id"])
        self.assertIsNotNone(row.reservation_release_at)
        dispatch.reap_commands(self.session)
        self.assertEqual(device.lease_task_id, f"startup:{task['id']}")
        row.reservation_release_at = datetime.now() - timedelta(seconds=1)
        self.session.add(row)
        self.session.commit()
        dispatch.reap_commands(self.session)
        self.session.expire_all()
        self.assertIsNone(self.session.exec(select(Device)).one().lease_task_id)
        self.assertTrue(dispatch._summary(self.session, task["id"])["reservation_released_after_grace"])
        self.assertEqual(self.session.get(FastbotTask, task["id"]).status, "ABORTED")

    def test_grace_reaper_cannot_release_new_devices_owner(self):
        task = self.submit()
        self.claim()
        row = dispatch.execution_for_task(self.session, task["id"])
        dispatch.close_command(self.session, row, "ABORTED", reason="连接丢失", release_device=False)
        row.reservation_release_at = datetime.now() - timedelta(seconds=1)
        self.session.add(row)
        device = self.session.exec(select(Device)).one()
        device.lease_task_id = "scenario:new-owner"
        device.lease_kind = "scenario"
        self.session.add(device)
        self.session.commit()
        dispatch.reap_commands(self.session)
        self.session.expire_all()
        self.assertEqual(self.session.exec(select(Device)).one().lease_task_id, "scenario:new-owner")
        self.assertEqual(self.session.exec(select(Device)).one().status, "BUSY")


if __name__ == "__main__":
    unittest.main()
