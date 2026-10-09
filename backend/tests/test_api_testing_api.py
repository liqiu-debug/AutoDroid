import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

import httpx
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlmodel import SQLModel, Session, create_engine

from backend.api.api_testing import router
from backend.api.deps import get_current_active_user
from backend.api.environments import delete_environment
from backend.api.tasks import _validate_scheduled_target, _run_scheduled_scenario
from backend.api_testing import service
from backend.api_testing.engine import make_client
from backend.api_testing.schemas import DebugExecute, Step, Value, Assertion, literal, from_json
from backend.database import get_session, _migrate_api_testing_schema, _migrate_api_testing_owners
from backend.models import (
    ApiRun,
    ApiStepResult,
    Environment,
    GlobalVariable,
    ScheduledTask,
    User,
)
from backend.notification_service import NotificationService
from backend.retention_service import _cleanup_expired_api_runs


class ApiTestingApiTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="autodroid-api-test-")
        self.engine = create_engine(
            f"sqlite:///{Path(self.tmp.name) / 'test.db'}", connect_args={"check_same_thread": False}
        )
        SQLModel.metadata.create_all(self.engine)
        with Session(self.engine) as s:
            self.user = User(username="api-tester", hashed_password="unused", role="admin")
            s.add(self.user)
            s.commit()
            s.refresh(self.user)
            s.expunge(self.user)
        self.app = FastAPI()
        self.app.include_router(router, prefix="/api/api-testing")

        def session_override():
            with Session(self.engine) as session:
                yield session

        self.app.dependency_overrides[get_session] = session_override
        self.app.dependency_overrides[get_current_active_user] = lambda: self.user
        self.client = TestClient(self.app)
        self.jobs = []
        self.patch_pool = patch.object(
            service.pool, "submit", side_effect=lambda fn, *args: self.jobs.append((fn, args))
        )
        self.patch_pool.start()

    def tearDown(self):
        self.patch_pool.stop()
        service.controls.clear()
        service.debug_sessions.clear()
        service.scheduled_active.clear()
        self.client.close()
        self.engine.dispose()
        self.tmp.cleanup()

    def call(self, method, path, **kwargs):
        return getattr(self.client, method)("/api/api-testing" + path, **kwargs)

    def interface(self, name="login"):
        step = Step(id="first")
        step.snapshot.request.url = literal("https://example.com/login")
        response = self.call(
            "post",
            "/interfaces",
            json={
                "name": name,
                "config": step.snapshot.model_dump(),
                "sample": {"body": {"token": "sample-only-token"}},
            },
        )
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def scenario(self):
        interface = self.interface()
        step = Step(id="first", interface_id=interface["id"], interface_version=1, snapshot=interface["config"])
        response = self.call("post", "/scenarios", json={"name": "登录回归", "steps": [step.model_dump()]})
        self.assertEqual(response.status_code, 200, response.text)
        return response.json(), interface

    def test_crud_versions_samples_dependencies(self):
        scenario, interface = self.scenario()
        self.assertEqual(interface["sample"]["body"]["token"], "sample-only-token")
        payload = {k: interface[k] for k in ["name", "description", "folder_id", "version", "config", "sample"]}
        payload["description"] = "updated"
        self.assertEqual(self.call("put", f"/interfaces/{interface['id']}", json=payload).status_code, 200)
        self.assertEqual(self.call("put", f"/interfaces/{interface['id']}", json=payload).status_code, 409)
        self.assertEqual(self.call("delete", f"/interfaces/{interface['id']}").status_code, 409)
        persisted = self.call("get", f"/scenarios/{scenario['id']}").json()
        self.assertEqual(persisted["steps"][0]["interface_version"], 1)
        self.assertEqual(self.call("delete", f"/scenarios/{scenario['id']}").status_code, 200)
        self.assertEqual(self.call("delete", f"/interfaces/{interface['id']}").status_code, 200)

    def bump_interface(self, interface, url="https://example.com/login/v2"):
        payload = {k: interface[k] for k in ["name", "description", "folder_id", "version", "config", "sample"]}
        payload["config"]["request"]["url"] = literal(url).model_dump()
        response = self.call("put", f"/interfaces/{interface['id']}", json=payload)
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def save_step_override(self, scenario, override):
        steps = scenario["steps"]
        steps[0]["overrides"] = {"request": override}
        payload = {
            key: scenario[key] for key in ["name", "description", "folder_id", "version", "env_id", "steps"]
        }
        response = self.call("put", f"/scenarios/{scenario['id']}", json=payload)
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def sync_apply(self, interface, scenario, **item):
        return self.call(
            "post",
            f"/interfaces/{interface['id']}/sync-apply",
            json={"items": [{"scenario_id": scenario["id"], "step_id": "first", "version": scenario["version"], **item}]},
        ).json()

    def test_interface_usages_report_stale_steps_and_conflicts(self):
        scenario, interface = self.scenario()
        usages = self.call("get", f"/interfaces/{interface['id']}/usages").json()
        self.assertEqual((usages["total_scenarios"], usages["total_steps"], usages["stale_steps"]), (1, 1, 0))
        self.assertEqual(usages["items"][0]["steps"][0]["conflicts"], [])
        self.assertEqual(self.call("get", "/scenarios").json()["items"][0]["stale_steps"], 0)

        interface = self.bump_interface(interface)
        usages = self.call("get", f"/interfaces/{interface['id']}/usages").json()
        self.assertEqual(usages["stale_steps"], 1)
        self.assertEqual(usages["items"][0]["scenario_name"], "登录回归")
        self.assertTrue(usages["items"][0]["steps"][0]["stale"])
        self.assertEqual(usages["items"][0]["steps"][0]["conflicts"], [])
        self.assertEqual(self.call("get", "/scenarios").json()["items"][0]["stale_steps"], 1)

        # A local edit on the very field that changed is a decision, not a silent overwrite.
        self.save_step_override(scenario, {"url": literal("https://local.test/login").model_dump()})
        usages = self.call("get", f"/interfaces/{interface['id']}/usages").json()
        self.assertEqual(usages["items"][0]["steps"][0]["conflicts"], [["request", "url"]])

    def test_sync_apply_updates_clean_steps_and_requires_conflict_decisions(self):
        scenario, interface = self.scenario()
        interface = self.bump_interface(interface)
        result = self.sync_apply(interface, scenario)
        self.assertEqual((result["applied"], result["failed"]), (1, 0))
        self.assertTrue(result["items"][0]["changed"])
        refreshed = self.call("get", f"/scenarios/{scenario['id']}").json()
        self.assertEqual(refreshed["version"], scenario["version"] + 1)
        self.assertEqual(refreshed["steps"][0]["interface_version"], 2)
        self.assertEqual(refreshed["steps"][0]["snapshot"]["request"]["url"]["value"], "https://example.com/login/v2")
        self.assertEqual(self.call("get", f"/interfaces/{interface['id']}/usages").json()["stale_steps"], 0)

        scenario = self.save_step_override(refreshed, {"url": literal("https://local.test/login").model_dump()})
        interface = self.bump_interface(interface, "https://example.com/login/v3")
        refused = self.sync_apply(interface, scenario)
        self.assertEqual((refused["applied"], refused["failed"]), (0, 1))
        self.assertEqual(refused["items"][0]["conflicts"], [["request", "url"]])
        untouched = self.call("get", f"/scenarios/{scenario['id']}").json()
        self.assertEqual(untouched["version"], scenario["version"])
        self.assertEqual(untouched["steps"][0]["interface_version"], 2)

        kept = self.sync_apply(interface, scenario, choices={"request.url": "keep"})
        self.assertEqual(kept["applied"], 1)
        preserved = self.call("get", f"/scenarios/{scenario['id']}").json()
        self.assertEqual(preserved["steps"][0]["interface_version"], 3)
        self.assertEqual(preserved["steps"][0]["snapshot"]["request"]["url"]["value"], "https://example.com/login/v3")
        self.assertEqual(preserved["steps"][0]["overrides"]["request"]["url"]["value"], "https://local.test/login")

        interface = self.bump_interface(interface, "https://example.com/login/v4")
        applied = self.sync_apply(interface, preserved, choices={"request.url": "template"})
        self.assertEqual(applied["applied"], 1)
        dropped = self.call("get", f"/scenarios/{preserved['id']}").json()
        self.assertEqual(dropped["steps"][0]["interface_version"], 4)
        self.assertIsNone(dropped["steps"][0]["overrides"].get("request", {}).get("url"))

    def test_sync_apply_refuses_stale_version_running_scenario_and_current_steps(self):
        scenario, interface = self.scenario()
        current = self.sync_apply(interface, scenario)
        self.assertEqual((current["applied"], current["failed"]), (0, 0))
        self.assertTrue(current["items"][0]["ok"])
        self.assertFalse(current["items"][0]["changed"])
        self.assertEqual(self.call("get", f"/scenarios/{scenario['id']}").json()["version"], scenario["version"])

        interface = self.bump_interface(interface)
        stale = self.sync_apply(interface, {**scenario, "version": scenario["version"] + 5})
        self.assertEqual((stale["applied"], stale["failed"]), (0, 1))
        self.assertIn("刷新", stale["items"][0]["error"])

        with Session(self.engine) as session:
            session.add(ApiRun(id="active-run", scenario_id=scenario["id"], scenario_name="登录回归", status="RUNNING"))
            session.commit()
        running = self.sync_apply(interface, scenario)
        self.assertEqual((running["applied"], running["failed"]), (0, 1))
        self.assertIn("正在运行", running["items"][0]["error"])
        self.assertEqual(self.call("get", f"/scenarios/{scenario['id']}").json()["steps"][0]["interface_version"], 1)

    def test_document_import_parses_then_creates_only_selected_entries(self):
        document = {
            "openapi": "3.0.0",
            "servers": [{"url": "https://api.demo.test"}],
            "paths": {
                "/orders": {
                    "post": {
                        "summary": "创建订单",
                        "requestBody": {"content": {"application/json": {"schema": {"type": "object", "properties": {"sku": {"type": "string"}}}}}},
                        "responses": {"201": {"content": {"application/json": {"example": {"code": 0}}}}},
                    }
                },
                "/orders/{id}": {"get": {"summary": "查询订单", "responses": {}}},
            },
        }
        parsed = self.call("post", "/imports/spec", json={"content": json.dumps(document)})
        self.assertEqual(parsed.status_code, 200, parsed.text)
        result = parsed.json()
        self.assertEqual(result["format"], "openapi")
        self.assertEqual([item["key"] for item in result["candidates"]], ["POST /orders", "GET /orders/{id}"])
        self.assertEqual(self.call("post", "/imports/spec", json={"content": "not: ["}).status_code, 422)

        folder = self.call("post", "/folders", json={"name": "订单", "kind": "interface"}).json()
        chosen = result["candidates"][0]
        created = self.call(
            "post",
            "/imports/apply",
            json={"folder_id": folder["id"], "items": [{"name": chosen["name"], "config": chosen["config"], "sample": chosen["sample"]}]},
        )
        self.assertEqual(created.status_code, 200, created.text)
        row = created.json()["created"][0]
        self.assertEqual(row["name"], "创建订单")
        self.assertEqual(row["folder_id"], folder["id"])
        self.assertEqual(row["sample_fields"][0]["path"], ["body"])
        self.assertEqual(row["config"]["request"]["url"]["value"], "https://api.demo.test/orders")
        self.assertEqual(self.call("get", "/interfaces").json()["total"], 1)

    def test_document_import_is_all_or_nothing(self):
        valid = Step(id="first").snapshot.model_dump()
        invalid = Step(id="second").snapshot.model_dump()
        invalid["request"]["body_type"] = "json"
        invalid["request"]["body"] = Value(kind="ref", step_id="absent", path=["body", "id"]).model_dump()
        response = self.call(
            "post",
            "/imports/apply",
            json={"items": [{"name": "有效", "config": valid}, {"name": "无效", "config": invalid}]},
        )
        self.assertEqual(response.status_code, 422, response.text)
        self.assertEqual(self.call("get", "/interfaces").json()["total"], 0)

        scenario_folder = self.call("post", "/folders", json={"name": "场景目录", "kind": "scenario"}).json()
        mismatch = self.call("post", "/imports/apply", json={"folder_id": scenario_folder["id"], "items": [{"name": "有效", "config": valid}]})
        self.assertEqual(mismatch.status_code, 422)
        self.assertEqual(self.call("get", "/interfaces").json()["total"], 0)

    def test_auth_and_delete_permissions(self):
        item = self.interface()
        self.user.role, self.user.id = "user", 999
        self.assertEqual(self.call("delete", f"/interfaces/{item['id']}").status_code, 403)
        self.assertEqual(self.call("get", "/policy").status_code, 404)
        self.app.dependency_overrides.pop(get_current_active_user)
        self.assertEqual(self.call("get", "/interfaces").status_code, 401)

    def test_shared_edits_preserve_creator_and_track_updater(self):
        scenario, interface = self.scenario()
        creator_id = self.user.id
        with Session(self.engine) as session:
            editor = User(username="colleague", full_name="团队同事", hashed_password="unused", role="user")
            session.add(editor)
            session.commit()
            session.refresh(editor)
            session.expunge(editor)
        self.user = editor
        for resource, item in [("interfaces", interface), ("scenarios", scenario)]:
            keys = ["name", "description", "folder_id", "version"]
            keys += ["config", "sample"] if resource == "interfaces" else ["steps", "env_id"]
            payload = {key: item[key] for key in keys}
            payload["description"] = "由同事更新"
            result = self.call("put", f"/{resource}/{item['id']}", json=payload)
            self.assertEqual(result.status_code, 200, result.text)
            for data in [result.json(), self.call("get", f"/{resource}").json()["items"][0]]:
                self.assertEqual(data["user_id"], creator_id)
                self.assertEqual(data["creator_name"], "api-tester")
                self.assertEqual(data["updater_name"], "团队同事")
                self.assertEqual(data["created_at"], item["created_at"])
            self.assertEqual(self.call("put", f"/{resource}/{item['id']}", json=payload).status_code, 409)
            self.assertEqual(self.call("delete", f"/{resource}/{item['id']}").status_code, 403)

    def test_owner_migration_upgrades_existing_tables_idempotently(self):
        import sqlite3
        with sqlite3.connect(":memory:") as conn:
            cursor = conn.cursor()
            for table in ("apidefinition", "apiscenario"):
                cursor.execute(f"CREATE TABLE {table} (id INTEGER PRIMARY KEY, name TEXT, user_id INTEGER)")
                cursor.execute(f"INSERT INTO {table} VALUES (1, 'existing', 7)")
            _migrate_api_testing_owners(cursor)
            _migrate_api_testing_owners(cursor)
            for table in ("apidefinition", "apiscenario"):
                self.assertEqual(cursor.execute(f"SELECT name, user_id, updater_id FROM {table}").fetchone(), ("existing", 7, None))

    def test_run_persists_plaintext_report_and_notifies(self):
        scenario, _ = self.scenario()
        with Session(self.engine) as session:
            env = Environment(name="plaintext-test")
            session.add(env)
            session.commit()
            session.refresh(env)
            env_id = env.id
            session.add(GlobalVariable(env_id=env_id, key="AT", value="env-access-token", is_secret=True))
            session.commit()
        payload = {k: scenario[k] for k in ["name", "description", "folder_id", "version", "steps"]}
        request = payload["steps"][0]["snapshot"]["request"]
        request["auth"] = {"kind": "bearer", "token": {"kind": "env", "name": "AT"}}
        request["headers"] = [{"name": "Cookie", "value": literal("session=request-cookie").model_dump()}]
        version = self.call("put", f"/scenarios/{scenario['id']}", json=payload).json()["version"]
        response = self.call("post", f"/scenarios/{scenario['id']}/runs", json={"version": version, "notify": True, "env_id": env_id})
        self.assertEqual(response.status_code, 202, response.text)
        run_id = response.json()["id"]

        def handler(_):
            return httpx.Response(200, json={"token": "runtime-secret", "at": "returned-at", "text": "<script>alert(1)</script>"}, headers={"Set-Cookie": "session=response-cookie; Path=/"})

        with (
            patch.object(
                service,
                "make_client",
                side_effect=lambda *_args, **_kwargs: make_client(transport=httpx.MockTransport(handler)),
            ),
            patch.object(
                NotificationService, "send_api_report_card", return_value={"status": "FAILED", "error": "模拟发送失败"}
            ) as notify,
        ):
            fn, args = self.jobs.pop(0)
            fn(*args)
        result = self.call("get", f"/runs/{run_id}").json()
        self.assertEqual(result["status"], "PASS", result)
        self.assertEqual(result["notification_status"], "FAILED")
        self.assertEqual(result["summary"]["passed"], 1)
        self.assertEqual(result["steps"][0]["detail"]["response"]["body"]["token"], "runtime-secret")
        detail = result["steps"][0]["detail"]
        self.assertEqual(detail["request"]["headers"]["authorization"], "Bearer env-access-token")
        self.assertEqual(detail["request"]["headers"]["cookie"], "session=request-cookie")
        self.assertEqual(detail["response"]["body"]["at"], "returned-at")
        self.assertEqual(detail["response"]["cookies"], {"session": "response-cookie"})
        self.assertIn("session=request-cookie", json.dumps(result["snapshot"]))
        self.assertNotIn("sample-only-token", json.dumps(result))
        self.assertEqual(notify.call_args.kwargs["run_id"], run_id)
        self.assertNotIn("runtime-secret", json.dumps(notify.call_args.kwargs))
        exported = self.call("get", f"/runs/{run_id}/download")
        self.assertEqual(exported.status_code, 200)
        self.assertIn("attachment;", exported.headers["content-disposition"])
        self.assertNotIn("<script>", exported.text)
        self.assertIn("runtime-secret", exported.text)
        self.assertIn("env-access-token", exported.text)
        self.assertIn("returned-at", exported.text)
        self.assertIn("response-cookie", exported.text)
        self.assertIn("&lt;script&gt;", exported.text)
        self.assertEqual(self.call("delete", f"/runs/{run_id}").status_code, 200)

    def test_cancel_and_failure_skip_following_steps(self):
        scenario, _ = self.scenario()
        payload = {k: scenario[k] for k in ["name", "description", "folder_id", "version", "env_id", "steps"]}
        second = dict(payload["steps"][0], id="second", name="second")
        payload["steps"].append(second)
        updated = self.call("put", f"/scenarios/{scenario['id']}", json=payload).json()
        run = self.call("post", f"/scenarios/{scenario['id']}/runs", json={"version": updated["version"]}).json()
        calls = []
        with patch.object(
            service,
            "make_client",
            side_effect=lambda *_args, **_kwargs: make_client(
                transport=httpx.MockTransport(lambda r: (calls.append(r), httpx.Response(500))[1])
            ),
        ):
            fn, args = self.jobs.pop(0)
            fn(*args)
        report = self.call("get", f"/runs/{run['id']}").json()
        self.assertEqual(report["status"], "FAIL")
        self.assertEqual([s["status"] for s in report["steps"]], ["FAIL", "SKIP"])
        self.assertEqual(len(calls), 1)
        run = self.call("post", f"/scenarios/{scenario['id']}/runs", json={"version": updated["version"]}).json()
        self.call("post", f"/runs/{run['id']}/cancel")
        fn, args = self.jobs.pop(0)
        fn(*args)
        self.assertEqual(self.call("get", f"/runs/{run['id']}").json()["status"], "ABORTED")

    def test_debug_isolation_invalidation_and_cookie_snapshots(self):
        one, two = Step(id="one"), Step(id="two")
        one.snapshot.request.url = literal("https://example.com/login")
        two.snapshot.request.url = literal("https://example.com/next")
        two.snapshot.request.auth.kind = "bearer"
        two.snapshot.request.auth.token = Value(kind="ref", step_id="one", path=["body", "token"])
        item = service.create_debug(self.user.id, "editor", None)
        with self.assertRaises(Exception):
            service.get_debug(item.id, 999)
        service.start_debug(item, DebugExecute(editor_id="editor", steps=[one, two], step_id="one"), {})
        with patch.object(
            service,
            "make_client",
            side_effect=lambda *args, **kwargs: make_client(
                [],
                transport=httpx.MockTransport(
                    lambda r: httpx.Response(200, json={"token": "real"}, headers={"Set-Cookie": "session=one; Path=/"})
                ),
            ),
        ):
            fn, args = self.jobs.pop(0)
            fn(*args)
        self.assertIn("one", item.outputs)
        # Configure B after learning A's output: A and its cookies must survive.
        two.snapshot.request.body_type = "json"
        two.snapshot.request.body = from_json({"count": 2})
        service.start_debug(item, DebugExecute(editor_id="editor", steps=[one, two], step_id="two"), {})
        self.assertIn("one", item.outputs)
        self.assertEqual(item.cookies.get("session"), "one")

        def handler(request):
            self.assertEqual(request.headers["authorization"], "Bearer real")
            self.assertIn("session=one", request.headers["cookie"])
            return httpx.Response(200, json={"id": 2})

        with patch.object(
            service,
            "make_client",
            side_effect=lambda cookies: make_client(cookies, transport=httpx.MockTransport(handler)),
        ):
            fn, args = self.jobs.pop(0)
            fn(*args)
        self.assertEqual(item.status, "PASS")
        one.snapshot.request.url = literal("https://example.com/changed")
        service.start_debug(item, DebugExecute(editor_id="editor", steps=[one, two], step_id="two"), {})
        self.assertNotIn("one", item.outputs)
        self.assertNotIn("two", item.outputs)
        self.assertFalse(list(item.cookies.jar))

    def test_recheck_assertions_without_http_and_block_failed_predecessors(self):
        one, two = Step(id="login", name="登录"), Step(id="order", name="订单")
        one.snapshot.request.url = literal("https://example.com/login")
        two.snapshot.request.url = literal("https://example.com/order")
        two.snapshot.request.auth.kind = "bearer"
        two.snapshot.request.auth.token = Value(kind="ref", step_id="login", path=["body", "token"])
        item = service.create_debug(self.user.id, "editor", None)
        calls = []
        def handler(request):
            calls.append(request)
            return httpx.Response(200, json={"token": "real", "count": 2}, headers={"Set-Cookie": "session=one; Path=/"})
        with patch.object(service, "make_client", side_effect=lambda cookies=None: make_client(cookies, transport=httpx.MockTransport(handler))):
            service.start_debug(item, DebugExecute(editor_id="editor", steps=[one,two], step_id="login"), {})
            fn, args = self.jobs.pop(0)
            fn(*args)
            one.name = "登录已改名"
            check = Assertion(path=["body", "count"], op="eq", expected=literal(3))
            one.snapshot.assertions.append(check)
            payload = DebugExecute(editor_id="editor", steps=[one,two], step_id="login")
            response = self.call("post", f"/debug-sessions/{item.id}/assertions", json=payload.model_dump())
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(response.json()["result"]["status"], "FAIL")
            self.assertEqual(response.json()["result"]["name"], "登录已改名")
            self.assertEqual(len(calls), 1)
            with self.assertRaisesRegex(Exception, "前序步骤"):
                service.start_debug(item, DebugExecute(editor_id="editor", steps=[one,two], step_id="order"), {})
            check.expected = literal(2)
            response = service.recheck_debug(item, DebugExecute(editor_id="editor", steps=[one,two], step_id="login"), {})
            self.assertEqual(response["status"], "PASS")
            service.start_debug(item, DebugExecute(editor_id="editor", steps=[one,two], step_id="order"), {})
            fn, args = self.jobs.pop(0)
            fn(*args)
            self.assertEqual(len(calls), 2)
            self.assertEqual(calls[1].headers["authorization"], "Bearer real")
            self.assertIn("session=one", calls[1].headers["cookie"])
            one.snapshot.request.url = literal("https://example.com/changed")
            with self.assertRaisesRegex(Exception, "匹配的真实响应"):
                service.recheck_debug(item, DebugExecute(editor_id="editor", steps=[one,two], step_id="login"), {})
            self.assertFalse(item.outputs)
            self.assertEqual(len(calls), 2)

    def test_recheck_cannot_use_saved_example_or_another_user_session(self):
        step = Step(id="one", response_schema={"fields":[{"path":["body","token"],"type":"string","example":"sample"}],"source":"debug"})
        step.snapshot.request.url = literal("https://example.com")
        item = service.create_debug(self.user.id, "editor", None)
        payload = DebugExecute(editor_id="editor", steps=[step], step_id="one").model_dump()
        response = self.call("post", f"/debug-sessions/{item.id}/assertions", json=payload)
        self.assertEqual(response.status_code, 409)
        self.user.id = 999
        self.assertEqual(self.call("post", f"/debug-sessions/{item.id}/assertions", json=payload).status_code, 404)
        self.assertFalse(self.jobs)

    def test_pending_assertions_preserve_downstream_cache_and_allow_reverting(self):
        one, two = Step(id="one", name="登录"), Step(id="two")
        one.snapshot.request.url = literal("https://example.com/one")
        two.snapshot.request.url = literal("https://example.com/two")
        item = service.create_debug(self.user.id, "editor", None)
        with patch.object(service, "make_client", side_effect=lambda *args: make_client(transport=httpx.MockTransport(lambda _: httpx.Response(200, json={"id": 42})))):
            service.start_debug(item, DebugExecute(editor_id="editor", steps=[one,two], step_id="two", mode="through"), {})
            fn, args = self.jobs.pop(0)
            fn(*args)
        cached = item.responses["two"]
        one.snapshot.assertions.append(Assertion(path=["body","id"], expected=literal(42)))
        with self.assertRaisesRegex(Exception, "前序步骤"):
            service.recheck_debug(item, DebugExecute(editor_id="editor", steps=[one,two], step_id="two"), {})
        self.assertIs(item.responses["two"], cached)
        self.assertTrue(item.results["one"]["assertions_pending"])
        one.snapshot.assertions.pop()
        self.assertEqual(service.recheck_debug(item, DebugExecute(editor_id="editor", steps=[one,two], step_id="two"), {})["status"], "PASS")
        self.assertFalse(item.results["one"]["assertions_pending"])
        # Incomplete downstream configuration must not prevent editing/debugging login.
        two.snapshot.request.url = Value(kind="env", name="NOT_CONFIGURED_YET")
        self.assertEqual(service.recheck_debug(item, DebugExecute(editor_id="editor", steps=[one,two], step_id="one"), {})["status"], "PASS")
        self.assertFalse(self.jobs)

    def test_latest_run_summary_and_readable_failure_export(self):
        scenario, _ = self.scenario()
        first = Step.model_validate(scenario["steps"][0])
        first.name = "登录"
        second = first.model_copy(deep=True)
        second.id, second.name = "order", "查询订单"
        second.snapshot.request.auth.kind = "bearer"
        second.snapshot.request.auth.token = Value(kind="ref", step_id=first.id, path=["body", "token"])
        second.snapshot.assertions.append(Assertion(path=["body", "state"], expected=literal("created")))
        updated = self.call("put", f"/scenarios/{scenario['id']}", json={"name":scenario["name"], "version":scenario["version"], "steps":[first.model_dump(),second.model_dump()]}).json()
        run = self.call("post", f"/scenarios/{scenario['id']}/runs", json={"version":updated["version"]}).json()
        with patch.object(service, "make_client", side_effect=lambda *args: make_client(transport=httpx.MockTransport(lambda _: httpx.Response(200, json={"token":"demo", "state":"processing"})))):
            fn, args = self.jobs.pop(0)
            fn(*args)
        latest = self.call("get", "/scenarios").json()["items"][0]
        self.assertEqual(latest["step_count"], 2)
        self.assertEqual(latest["last_run"]["id"], run["id"])
        self.assertEqual(latest["last_run"]["status"], "FAIL")
        html = self.call("get", f"/runs/{run['id']}/download").text
        self.assertIn("第 2 步「查询订单」失败", html)
        self.assertIn("登录 → body › token", html)
        self.assertIn("processing", html)
        self.assertIn("响应正文", html)

    def test_retried_step_is_labelled_in_the_exported_report(self):
        scenario, interface = self.scenario()
        with Session(self.engine) as session:
            session.add(ApiRun(id="retried-run", scenario_id=scenario["id"], scenario_name="登录回归", status="PASS"))
            session.add(
                ApiStepResult(
                    run_id="retried-run",
                    step_id="first",
                    position=0,
                    name="登录",
                    status="PASS",
                    detail={
                        "attempts": 2,
                        "retry_history": [{"attempt": 1, "error": "无法连接目标服务，请检查地址、网络和证书", "error_category": "connection"}],
                        "assertions": [],
                    },
                )
            )
            session.commit()
        html = self.call("get", "/runs/retried-run/download").text
        self.assertIn("共尝试 2 次", html)
        self.assertIn("重试记录", html)
        self.assertIn("无法连接目标服务", html)

        with Session(self.engine) as session:
            session.add(
                ApiStepResult(
                    run_id="retried-run", step_id="second", position=1, name="查询订单", status="PASS",
                    detail={"attempts": 1, "assertions": []},
                )
            )
            session.commit()
        html = self.call("get", "/runs/retried-run/download").text
        # A step that never retried must not claim it did.
        self.assertEqual(html.count("共尝试"), 1)

    def test_precheck_location_and_persisted_editor_schema(self):
        scenario, _ = self.scenario()
        scenario["steps"][0]["snapshot"]["request"]["headers"] = [{"name":"AT","value":{"kind":"env","name":"MISSING"}}]
        scenario["steps"][0]["response_schema"] = {"fields":[{"path":["body","a.b",0,"id"],"type":"number","example":42}],"source":"debug","env_name":"测试","captured_at":"2026-09-30T10:00:00"}
        payload = {k:scenario[k] for k in ("name","version","steps","env_id")}
        checked = self.call("post", "/precheck", json=payload).json()["errors"][0]
        self.assertEqual(checked["location"], ["request","headers",0,"value"])
        self.assertEqual(checked["section"], "headers")
        self.assertEqual(checked["step_id"], "first")
        saved = self.call("put", f"/scenarios/{scenario['id']}", json=payload)
        self.assertEqual(saved.status_code, 200, saved.text)
        loaded = self.call("get", f"/scenarios/{scenario['id']}").json()
        self.assertEqual(loaded["steps"][0]["response_schema"]["fields"][0]["example"], 42)
        listed = self.call("get", "/scenarios").json()["items"][0]
        self.assertEqual(listed["step_count"], 1)
        self.assertIsNone(listed["last_run"])

    def test_standalone_drafts_and_run_schedule_prechecks_share_requirements(self):
        step = Step(id="standalone", name="场景内请求")
        step.snapshot.assertions = []
        created = self.call("post", "/scenarios", json={"name": "独立请求", "steps": [step.model_dump()]})
        self.assertEqual(created.status_code, 200, created.text)
        scenario = created.json()
        self.assertIsNone(scenario["steps"][0]["interface_id"])
        self.assertEqual(self.call("get", "/interfaces").json()["total"], 0)
        run = self.call("post", f"/scenarios/{scenario['id']}/runs", json={"version": scenario["version"]})
        self.assertEqual(run.status_code, 422)
        self.assertEqual({tuple(error["location"]) for error in run.json()["detail"]}, {("request", "url"), ("assertions",)})
        self.assertFalse(self.jobs)
        step.snapshot.request.url = literal("https://example.com/orders")
        payload = {"name": scenario["name"], "version": scenario["version"], "steps": [step.model_dump()]}
        self.assertEqual(self.call("post", "/precheck", json={**payload, "validation_mode": "debug"}).json()["errors"], [])
        self.assertEqual(self.call("post", "/precheck", json=payload).json()["errors"][0]["location"], ["assertions"])
        scenario = self.call("put", f"/scenarios/{scenario['id']}", json=payload).json()
        config = {"_task_type": "api", "api_scenario_id": scenario["id"]}
        with Session(self.engine) as session:
            with self.assertRaises(Exception) as caught:
                _validate_scheduled_target(session=session, scenario_id=None, device_serials=[], config=config)
            self.assertEqual(caught.exception.detail[0]["location"], ["assertions"])
            task = ScheduledTask(name="零断言定时", strategy="DAILY", user_id=self.user.id,
                                 enable_notification=False, strategy_config=json.dumps(config))
            session.add(task)
            session.commit()
            session.refresh(task)
            run_id = service.run_scheduled(task.id, self.engine)
            self.assertEqual(session.get(ApiRun, run_id).status, "ERROR")
        scheduled_report = self.call("get", f"/runs/{run_id}").json()
        self.assertEqual(scheduled_report["error_category"], "configuration")
        self.assertEqual(scheduled_report["precheck_errors"][0]["location"], ["assertions"])
        self.assertIn("至少需要一条断言", scheduled_report["error"])
        self.assertFalse(self.jobs)
        step.snapshot.assertions = [Assertion(path=["status_code"], op="eq", expected=literal(400))]
        scenario = self.call("put", f"/scenarios/{scenario['id']}", json={"name": scenario["name"], "version": scenario["version"], "steps": [step.model_dump()]}).json()
        run = self.call("post", f"/scenarios/{scenario['id']}/runs", json={"version": scenario["version"]})
        self.assertEqual(run.status_code, 202, run.text)
        with patch.object(service, "make_client", side_effect=lambda: make_client(transport=httpx.MockTransport(lambda _: httpx.Response(400)))):
            fn, args = self.jobs.pop(0)
            fn(*args)
        self.assertEqual(self.call("get", f"/runs/{run.json()['id']}").json()["status"], "PASS")
        # A wait-only draft remains editable, but is not a runnable API test.
        wait_payload = {"name": "等待草稿", "steps": [Step(kind="wait", seconds=0).model_dump()]}
        wait_scenario = self.call("post", "/scenarios", json=wait_payload).json()
        rejected = self.call("post", f"/scenarios/{wait_scenario['id']}/runs", json={"version": wait_scenario["version"]})
        self.assertEqual(rejected.status_code, 422)
        self.assertEqual(rejected.json()["detail"][0]["section"], "steps")

    def test_unchecked_debug_keeps_response_for_recheck_and_blocks_downstream(self):
        one, two = Step(id="one", name="未校验请求"), Step(id="two")
        one.snapshot.request.url = literal("https://example.com/one")
        one.snapshot.assertions = []
        two.snapshot.request.url = literal("https://example.com/two")
        item = service.create_debug(self.user.id, "editor", None)
        calls = []
        def handler(request):
            calls.append(request)
            return httpx.Response(500, json={"error": "expected"})
        with patch.object(service, "make_client", side_effect=lambda cookies=None: make_client(cookies, transport=httpx.MockTransport(handler))):
            service.start_debug(item, DebugExecute(editor_id="editor", steps=[one, two], step_id="two", mode="through"), {})
            fn, args = self.jobs.pop(0)
            fn(*args)
        self.assertEqual(item.status, "UNCHECKED")
        self.assertEqual(len(calls), 1)
        self.assertEqual(item.responses["one"]["status_code"], 500)
        self.assertNotIn("one", item.outputs)
        self.assertNotIn("two", item.results)
        for operation in (service.start_debug, service.recheck_debug):
            with self.assertRaisesRegex(Exception, "前序步骤"):
                operation(item, DebugExecute(editor_id="editor", steps=[one, two], step_id="two"), {})
        recheck = DebugExecute(editor_id="editor", steps=[one, two], step_id="one")
        self.assertEqual(service.recheck_debug(item, recheck, {})["status"], "UNCHECKED")
        one.snapshot.assertions = [Assertion(path=["status_code"], op="eq", expected=literal(500))]
        self.assertEqual(service.recheck_debug(item, DebugExecute(editor_id="editor", steps=[one, two], step_id="one"), {})["status"], "PASS")
        self.assertIn("one", item.outputs)
        one.snapshot.assertions = []
        self.assertEqual(service.recheck_debug(item, DebugExecute(editor_id="editor", steps=[one, two], step_id="one"), {})["status"], "UNCHECKED")
        self.assertNotIn("one", item.outputs)
        self.assertEqual(len(calls), 1)
        self.assertFalse(self.jobs)

    def test_historical_zero_assertion_reports_warn_without_changing_status_or_waits(self):
        request, wait = Step(id="request", name="旧请求"), Step(id="wait", name="正常等待", kind="wait")
        request.snapshot.assertions = []
        with Session(self.engine) as session:
            run = ApiRun(id="legacy-unchecked", scenario_name="旧报告", status="PASS", executor_id=self.user.id,
                         snapshot={"steps": [request.model_dump(), wait.model_dump()]})
            session.add(run)
            session.add(ApiStepResult(run_id=run.id, step_id=request.id, position=0, name=request.name, status="PASS",
                                      detail={"request": {}, "response": {"status_code": 500}, "assertions": []}))
            session.add(ApiStepResult(run_id=run.id, step_id=wait.id, position=1, name=wait.name, status="PASS", detail={"assertions": []}))
            session.commit()
        report = self.call("get", "/runs/legacy-unchecked").json()
        self.assertEqual(report["status"], "PASS")
        self.assertEqual([step["status"] for step in report["steps"]], ["PASS", "PASS"])
        self.assertEqual(len(report["warnings"]), 1)
        self.assertIn("旧请求", report["warnings"][0])
        self.assertNotIn("正常等待", report["warnings"][0])
        self.assertIn(report["warnings"][0], self.call("get", "/runs/legacy-unchecked/download").text)

    def test_notification_status_exposes_only_boolean(self):
        from backend.models import SystemSetting
        self.assertEqual(self.call("get", "/notification-status").json(), {"configured":False})
        with Session(self.engine) as session:
            session.add(SystemSetting(key="api_testing_webhook",value="https://example.com/private-webhook"))
            session.commit()
        self.assertEqual(self.call("get", "/notification-status").json(), {"configured":True})

    def test_schedule_overlap_recovery_retention_and_dependencies(self):
        scenario, _ = self.scenario()
        with Session(self.engine) as session:
            env = Environment(name="stage")
            session.add(env)
            session.commit()
            session.refresh(env)
            task = ScheduledTask(
                name="定时接口",
                strategy="DAILY",
                user_id=self.user.id,
                strategy_config=json.dumps({"_task_type": "api", "api_scenario_id": scenario["id"], "env_id": env.id}),
            )
            session.add(task)
            session.commit()
            session.refresh(task)
            task_id = task.id
            with self.assertRaises(Exception):
                delete_environment(env.id, session)
        schedules = self.call("get", f"/scenarios/{scenario['id']}/schedules").json()["items"]
        self.assertEqual([task["id"] for task in schedules], [task_id])
        self.assertEqual(schedules[0]["strategy_config"]["env_id"], env.id)
        run_id = service.run_scheduled(task_id, self.engine)
        self.assertIsNotNone(run_id)
        self.assertIsNone(service.run_scheduled(task_id, self.engine))
        self.assertEqual(self.call("delete", f"/scenarios/{scenario['id']}").status_code, 409)
        service.recover_runs(self.engine)
        self.assertEqual(self.call("get", f"/runs/{run_id}").json()["status"], "ERROR")
        with Session(self.engine) as session:
            run = session.get(ApiRun, run_id)
            run.finished_at = datetime.now() - timedelta(days=60)
            session.add(run)
            session.commit()
            self.assertEqual(_cleanup_expired_api_runs(session, datetime.now() - timedelta(days=30)), 1)
            session.commit()
            self.assertIsNone(session.get(ApiRun, run_id))

    def test_schedule_dispatch_and_validation_do_not_use_devices(self):
        scenario, _ = self.scenario()
        config = {"_task_type": "api", "api_scenario_id": scenario["id"]}
        with Session(self.engine) as session:
            self.assertIsNone(
                _validate_scheduled_target(session=session, scenario_id=None, device_serials=[], config=config)
            )
            with self.assertRaises(Exception):
                _validate_scheduled_target(session=session, scenario_id=None, device_serials=["phone"], config=config)
            task = ScheduledTask(
                name="api only", strategy="DAILY", user_id=self.user.id, strategy_config=json.dumps(config)
            )
            session.add(task)
            session.commit()
            session.refresh(task)
            with patch("backend.database.engine", self.engine), patch.object(service, "run_scheduled") as start:
                _run_scheduled_scenario(task.id)
                start.assert_called_once_with(task.id, db_engine=self.engine)

    def test_scheduled_precheck_failure_creates_report_and_notifies(self):
        scenario, _ = self.scenario()
        with Session(self.engine) as session:
            task = ScheduledTask(
                name="bad environment",
                strategy="DAILY",
                user_id=self.user.id,
                strategy_config=json.dumps({"_task_type": "api", "api_scenario_id": scenario["id"], "env_id": 999}),
            )
            session.add(task)
            session.commit()
            session.refresh(task)
            with patch.object(NotificationService, "send_api_report_card", return_value={"status": "SENT"}) as send:
                run_id = service.run_scheduled(task.id, self.engine)
            self.assertIsNotNone(run_id)
            self.assertEqual(send.call_args.kwargs["status"], "ERROR")
            self.assertEqual(session.get(ApiRun, run_id).notification_status, "SENT")

    def test_debug_expiration_and_environment_change(self):
        item = service.create_debug(self.user.id, "editor", None)
        with self.assertRaises(Exception):
            service.start_debug(item, DebugExecute(editor_id="other", steps=[Step()], step_id="x"), {})
        item.touched -= 1801
        service.reap_debug_sessions()
        self.assertNotIn(item.id, service.debug_sessions)

    def test_legacy_sensitive_fields_display_plaintext_across_debug_jobs(self):
        one, two = Step(id="one"), Step(id="two")
        one.snapshot.request.url = literal("https://example.com/one")
        one.snapshot.sensitive_paths = [["body", "private_id"]]
        two.snapshot.request.url = literal("https://example.com/two")
        two.snapshot.request.body_type = "json"
        two.snapshot.request.body = Value(kind="ref", step_id="one", path=["body", "private_id"])
        item = service.create_debug(self.user.id, "editor", None)
        with patch.object(
            service,
            "make_client",
            side_effect=lambda *args, **kwargs: make_client(
                transport=httpx.MockTransport(lambda _: httpx.Response(200, json={"private_id": 87654321}))
            ),
        ):
            service.start_debug(item, DebugExecute(editor_id="editor", steps=[one, two], step_id="one"), {})
            fn, args = self.jobs.pop(0)
            fn(*args)
            service.start_debug(item, DebugExecute(editor_id="editor", steps=[one, two], step_id="two"), {})
            fn, args = self.jobs.pop(0)
            fn(*args)
        self.assertEqual(item.outputs["one"]["body"]["private_id"], 87654321)
        self.assertEqual(item.results["one"]["detail"]["response"]["body"]["private_id"], 87654321)
        self.assertEqual(item.results["two"]["detail"]["request"]["body"], 87654321)

    def test_masked_sample_retains_declared_numeric_type(self):
        from backend.api.api_testing import sample_data
        from backend.api_testing.schemas import DefinitionConfig, SampleType

        sample = sample_data(
            {"body": {"private_id": "••••••"}},
            DefinitionConfig(),
            [SampleType(path=["body", "private_id"], type="number")],
        )
        field = sample["fields"][0]["children"][0]
        self.assertEqual(field["type"], "number")
        self.assertEqual(field["example"], "••••••")

    def test_active_task_unique_index_migration_is_idempotent(self):
        raw = self.engine.raw_connection()
        try:
            _migrate_api_testing_schema(raw.cursor())
            _migrate_api_testing_schema(raw.cursor())
            raw.commit()
            indexes = (
                raw.cursor()
                .execute("SELECT name FROM sqlite_master WHERE type='index' AND name='uq_apirun_active_task'")
                .fetchall()
            )
            self.assertEqual(len(indexes), 1)
        finally:
            raw.close()


class NotificationTests(unittest.TestCase):
    def test_all_notification_clients_ignore_malformed_proxy_environment(self):
        requests = []
        settings = {key: "https://example.com/robot/test" for key in (
            "api_testing_webhook", "feishu_webhook", "fastbot_webhook")}
        settings["system_base_url"] = "https://autodroid.example.com"

        def send(_transport, request):
            requests.append(request)
            return httpx.Response(200, json={"code": 0})

        with (
            patch.dict(os.environ, {"no_proxy": "::1", "https_proxy": "http://proxy.invalid:bad"}, clear=True),
            patch.object(NotificationService, "_get_setting", side_effect=settings.get),
            patch.object(httpx.HTTPTransport, "handle_request", send),
        ):
            # Keep the real HTTPX constructor: the regression occurs before post().
            self.assertTrue(NotificationService.send_test_message(settings["feishu_webhook"])["success"])
            result = NotificationService.send_api_report_card(
                run_id="run", name="api", status="PASS", env_name="test", duration_ms=1,
                total=1, passed=1, failed=0, skipped=0,
            )
            self.assertEqual(result["status"], "SENT")
            NotificationService.send_report_card("ui", 1, "PASS", 1, 1, 0, 1)
            NotificationService.send_fastbot_report_card("explore", 1, "test.app", "test-device", "PASS", 1)
        self.assertEqual(len(requests), 4)
        self.assertTrue(all(str(request.url) == settings["feishu_webhook"] for request in requests))

    def test_card_link_summary_only_and_failures(self):
        settings = {
            "api_testing_webhook": "https://example.com/robot/test",
            "feishu_webhook": "https://example.com/robot/ui-only",
            "system_base_url": "https://autodroid.example.com",
        }
        payload = dict(
            run_id="run-123",
            name="订单回归",
            status="FAIL",
            env_name="预发布",
            duration_ms=1234,
            total=3,
            passed=1,
            failed=1,
            skipped=1,
        )
        cards = []

        def handler(request):
            self.assertEqual(str(request.url), settings["api_testing_webhook"])
            cards.append(json.loads(request.content))
            return httpx.Response(200, json={"code": 0})

        transport = httpx.MockTransport(handler)
        with (
            patch.object(NotificationService, "_get_setting", side_effect=settings.get),
            patch(
                "backend.notification_service.httpx.Client", return_value=httpx.Client(transport=transport)
            ),
        ):
            result = NotificationService.send_api_report_card(**payload)
            self.assertEqual(result["status"], "SENT")
        self.assertEqual(
            cards[0]["card"]["elements"][1]["actions"][0]["url"],
            "https://autodroid.example.com/execution/reports/api/run-123",
        )
        self.assertNotIn("设备", json.dumps(cards, ensure_ascii=False))
        self.assertNotIn("request", json.dumps(cards))
        with patch.object(NotificationService, "_get_setting", return_value=None):
            self.assertEqual(NotificationService.send_api_report_card(**payload)["status"], "SKIPPED")
        with (
            patch.object(NotificationService, "_get_setting", side_effect={"feishu_webhook": settings["feishu_webhook"]}.get),
            patch("backend.notification_service.httpx.Client") as client,
        ):
            self.assertEqual(NotificationService.send_api_report_card(**payload)["status"], "SKIPPED")
            client.assert_not_called()
        with (
            patch.object(NotificationService, "_get_setting", side_effect=settings.get),
            patch("backend.notification_service.httpx.Client", side_effect=RuntimeError("url-containing-secret")),
        ):
            result = NotificationService.send_api_report_card(**payload)
            self.assertEqual(result["status"], "FAILED")
            self.assertNotIn("url-containing-secret", str(result))


if __name__ == "__main__":
    unittest.main()
