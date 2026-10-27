"""桌面客户端 Fastbot 本地执行上报（client-fastbot）契约测试。

契约（供桌面引擎 cloud_bridge 对齐）：
- POST /api/client-fastbot/            创建任务（run_id 幂等），status=RUNNING
- POST /api/client-fastbot/{id}/finish 收尾（status/counts + report JSON），upsert FastbotReport
- POST /api/client-fastbot/{id}/artifacts  产物上传（path 相对 reports/fastbot/{id}/）

鉴权 get_current_user；写操作仅执行人本人或 admin。上报后既有读接口
（GET /api/fastbot/tasks、/reports/{id}、/startup/tasks、/fluency/sessions）
按 summary.session_type 正常归类，无需前端改动。
"""
import os
import tempfile
import unittest
from datetime import datetime

from fastapi import FastAPI
from fastapi.testclient import TestClient
from jose import jwt
from sqlalchemy.pool import StaticPool
from sqlmodel import SQLModel, Session, create_engine, select

from backend.api import client_fastbot, fastbot as fastbot_api
from backend.core.security import ALGORITHM, SECRET_KEY
from backend.database import get_session
from backend.models import FastbotReport, FastbotTask, User


def _create_payload(run_id: str = "run-1", **overrides) -> dict:
    payload = dict(
        run_id=run_id,
        kind="fastbot",
        package_name="com.example.app",
        device_serial="emulator-5554",
        duration=600,
        throttle=500,
        ignore_crashes=False,
        capture_log=True,
    )
    payload.update(overrides)
    return payload


def _finish_payload(**overrides) -> dict:
    payload = dict(
        status="COMPLETED",
        total_crashes=1,
        total_anrs=0,
        report={
            "performance_data": [{"time": "0", "cpu": 10.0, "mem": 100.0}],
            "jank_data": [{"time": "0", "fps": 58}],
            "jank_events": [{"time": "0", "severity": "warning"}],
            "trace_artifacts": [{
                "path": "reports/fastbot/1/traces/t.perfetto-trace",
                "analysis_status": "ANALYZED",
            }],
            "crash_events": [{"time": "1", "type": "crash", "full_log": "boom"}],
            "summary": {"avg_cpu": 12.5, "max_cpu": 30.0, "avg_mem": 120.0, "max_mem": 200.0,
                        "total_crashes": 1, "total_anrs": 0},
        },
    )
    payload.update(overrides)
    return payload


class ClientFastbotApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = create_engine(
            "sqlite://",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        SQLModel.metadata.create_all(self.engine)
        self.session = Session(self.engine)
        self.alice = self._add_user("alice")
        self.bob = self._add_user("bob")
        self.admin = self._add_user("admin", role="admin")

        self.reports_root = tempfile.TemporaryDirectory()
        self._reports_patch = patch_reports_dir(os.path.join(self.reports_root.name, "fastbot"))
        self._reports_patch.start()
        self.addCleanup(self._reports_patch.stop)
        self.addCleanup(self.reports_root.cleanup)

        app = FastAPI()
        app.include_router(client_fastbot.router, prefix="/api/client-fastbot")
        app.include_router(fastbot_api.router, prefix="/api/fastbot")
        app.dependency_overrides[get_session] = lambda: self.session
        self.client = TestClient(app)
        self.tokens = {
            "alice": jwt.encode({"sub": "alice"}, SECRET_KEY, algorithm=ALGORITHM),
            "bob": jwt.encode({"sub": "bob"}, SECRET_KEY, algorithm=ALGORITHM),
            "admin": jwt.encode({"sub": "admin"}, SECRET_KEY, algorithm=ALGORITHM),
        }

    def tearDown(self) -> None:
        self.session.close()
        self.engine.dispose()

    def _add_user(self, username: str, role: str = "user") -> User:
        user = User(username=username, hashed_password="x", role=role)
        self.session.add(user)
        self.session.commit()
        self.session.refresh(user)
        return user

    def _headers(self, who: str = "alice") -> dict:
        return {"Authorization": f"Bearer {self.tokens[who]}"}

    # ---------- create ----------

    def test_create_requires_token(self):
        resp = self.client.post("/api/client-fastbot/", json=_create_payload())
        self.assertEqual(resp.status_code, 401)

    def test_create_returns_running_task_and_is_idempotent_on_run_id(self):
        resp = self.client.post("/api/client-fastbot/", json=_create_payload(), headers=self._headers())
        self.assertEqual(resp.status_code, 200, resp.text)
        body = resp.json()
        self.assertEqual(body["package_name"], "com.example.app")
        self.assertEqual(body["device_serial"], "emulator-5554")
        self.assertEqual(body["status"], "RUNNING")
        self.assertIsNotNone(body["started_at"])

        again = self.client.post("/api/client-fastbot/", json=_create_payload(), headers=self._headers())
        self.assertEqual(again.status_code, 200, again.text)
        self.assertEqual(again.json()["id"], body["id"])

        rows = self.session.exec(select(FastbotTask)).all()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].run_id, "run-1")
        self.assertEqual(rows[0].executor_id, self.alice.id)

    def test_create_honors_started_at_and_executor_name(self):
        started = datetime(2026, 10, 26, 8, 30, 0)
        resp = self.client.post(
            "/api/client-fastbot/",
            json=_create_payload(
                run_id="run-ts",
                started_at=started.isoformat(),
                executor_name="桌面客户端: 客厅机",
            ),
            headers=self._headers(),
        )
        self.assertEqual(resp.status_code, 200, resp.text)
        body = resp.json()
        self.assertEqual(body["executor_name"], "桌面客户端: 客厅机")
        self.assertTrue(str(body["started_at"]).startswith("2026-10-26T08:30:00"))

    def test_startup_kind_seeds_session_type_for_read_side(self):
        resp = self.client.post(
            "/api/client-fastbot/",
            json=_create_payload(run_id="run-startup", kind="startup"),
            headers=self._headers(),
        )
        self.assertEqual(resp.status_code, 200, resp.text)
        task_id = resp.json()["id"]

        startup = self.client.get("/api/fastbot/startup/tasks").json()
        self.assertEqual([item["id"] for item in startup["items"]], [task_id])

        explore = self.client.get("/api/fastbot/tasks").json()
        self.assertEqual(explore["total"], 0)

    # ---------- finish ----------

    def test_finish_requires_token(self):
        resp = self.client.post("/api/client-fastbot/1/finish", json=_finish_payload())
        self.assertEqual(resp.status_code, 401)

    def test_finish_rejects_foreign_executor(self):
        created = self.client.post(
            "/api/client-fastbot/", json=_create_payload(), headers=self._headers()
        ).json()
        resp = self.client.post(
            f"/api/client-fastbot/{created['id']}/finish",
            json=_finish_payload(),
            headers=self._headers("bob"),
        )
        self.assertEqual(resp.status_code, 403)

    def test_finish_writes_report_and_task_status(self):
        created = self.client.post(
            "/api/client-fastbot/", json=_create_payload(), headers=self._headers()
        ).json()
        task_id = created["id"]

        resp = self.client.post(
            f"/api/client-fastbot/{task_id}/finish",
            json=_finish_payload(),
            headers=self._headers(),
        )
        self.assertEqual(resp.status_code, 200, resp.text)
        body = resp.json()
        self.assertEqual(body["task_id"], task_id)
        self.assertEqual(body["status"], "COMPLETED")

        report = self.client.get(f"/api/fastbot/reports/{task_id}").json()
        self.assertEqual(report["summary"]["avg_cpu"], 12.5)
        self.assertEqual(report["performance_data"], [{"time": "0", "cpu": 10.0, "mem": 100.0}])
        self.assertEqual(len(report["crash_events"]), 1)
        self.assertEqual(
            report["trace_artifacts"][0]["path"],
            "reports/fastbot/1/traces/t.perfetto-trace",
        )

        task = self.client.get(f"/api/fastbot/tasks/{task_id}").json()
        self.assertEqual(task["status"], "COMPLETED")
        self.assertEqual(task["total_crashes"], 1)
        self.assertIsNotNone(task["finished_at"])

    def test_finish_upserts_report_and_keeps_session_type(self):
        created = self.client.post(
            "/api/client-fastbot/",
            json=_create_payload(run_id="run-fluency", kind="fluency"),
            headers=self._headers(),
        ).json()
        task_id = created["id"]

        first = self.client.post(
            f"/api/client-fastbot/{task_id}/finish",
            json=_finish_payload(
                report={"summary": {"avg_fps": 58, "marker_count": 2}},
            ),
            headers=self._headers(),
        ).json()
        second = self.client.post(
            f"/api/client-fastbot/{task_id}/finish",
            json=_finish_payload(
                status="FAILED",
                report={"summary": {"avg_fps": 55}},
            ),
            headers=self._headers(),
        ).json()
        self.assertEqual(first["report_id"], second["report_id"])
        self.assertEqual(second["status"], "FAILED")

        report = self.client.get(f"/api/fastbot/reports/{task_id}").json()
        self.assertEqual(report["summary"]["session_type"], "fluency_manual")
        self.assertEqual(report["summary"]["avg_fps"], 55)

        sessions = self.client.get("/api/fastbot/fluency/sessions").json()
        self.assertEqual([item["task_id"] for item in sessions], [task_id])

    def test_admin_can_finish_other_user_task(self):
        created = self.client.post(
            "/api/client-fastbot/", json=_create_payload(), headers=self._headers()
        ).json()
        resp = self.client.post(
            f"/api/client-fastbot/{created['id']}/finish",
            json=_finish_payload(),
            headers=self._headers("admin"),
        )
        self.assertEqual(resp.status_code, 200, resp.text)

    # ---------- artifacts ----------

    def test_artifact_requires_token_and_ownership(self):
        created = self.client.post(
            "/api/client-fastbot/", json=_create_payload(), headers=self._headers()
        ).json()
        task_id = created["id"]

        resp = self.client.post(
            f"/api/client-fastbot/{task_id}/artifacts",
            files={"file": ("crash_1.mp4", b"\x00\x01", "video/mp4")},
            data={"path": "replays/crash_1.mp4"},
        )
        self.assertEqual(resp.status_code, 401)

        resp = self.client.post(
            f"/api/client-fastbot/{task_id}/artifacts",
            files={"file": ("crash_1.mp4", b"\x00\x01", "video/mp4")},
            data={"path": "replays/crash_1.mp4"},
            headers=self._headers("bob"),
        )
        self.assertEqual(resp.status_code, 403)

    def test_artifact_rejects_unsafe_paths(self):
        created = self.client.post(
            "/api/client-fastbot/", json=_create_payload(), headers=self._headers()
        ).json()
        task_id = created["id"]

        for bad in ("../evil.mp4", "/etc/passwd", "replays/../../x", "..\\evil.mp4", ""):
            resp = self.client.post(
                f"/api/client-fastbot/{task_id}/artifacts",
                files={"file": ("evil.mp4", b"x", "application/octet-stream")},
                data={"path": bad},
                headers=self._headers(),
            )
            self.assertEqual(resp.status_code, 400, bad)

    def test_artifact_upload_lands_in_reports_dir_and_is_served(self):
        created = self.client.post(
            "/api/client-fastbot/", json=_create_payload(), headers=self._headers()
        ).json()
        task_id = created["id"]

        resp = self.client.post(
            f"/api/client-fastbot/{task_id}/artifacts",
            files={"file": ("crash_1.mp4", b"mp4-bytes", "video/mp4")},
            data={"path": "replays/crash_1.mp4"},
            headers=self._headers(),
        )
        self.assertEqual(resp.status_code, 200, resp.text)
        body = resp.json()
        self.assertEqual(body["path"], f"reports/fastbot/{task_id}/replays/crash_1.mp4")
        self.assertEqual(body["size"], len(b"mp4-bytes"))

        expected = os.path.join(
            fastbot_api.FASTBOT_REPORTS_DIR, str(task_id), "replays", "crash_1.mp4"
        )
        self.assertTrue(os.path.isfile(expected))
        with open(expected, "rb") as handle:
            self.assertEqual(handle.read(), b"mp4-bytes")

        served = self.client.get(f"/api/fastbot/replays/{task_id}/crash_1.mp4")
        self.assertEqual(served.status_code, 200, served.text)
        self.assertEqual(served.content, b"mp4-bytes")


def patch_reports_dir(path: str):
    """把 client-fastbot 与 fastbot 读侧的产物目录一起指向临时目录。"""
    from unittest.mock import patch

    class _Stack:
        def __init__(self) -> None:
            self._patchers = [
                patch.object(client_fastbot, "REPORTS_DIR", path),
                patch.object(fastbot_api, "FASTBOT_REPORTS_DIR", path),
            ]

        def start(self):
            for item in self._patchers:
                item.start()
            os.makedirs(path, exist_ok=True)
            return self

        def stop(self):
            for item in self._patchers:
                item.stop()

    return _Stack()


if __name__ == "__main__":
    unittest.main()
