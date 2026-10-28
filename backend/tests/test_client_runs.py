"""桌面客户端执行上报（client-runs）与模板上传（client-templates）测试。

覆盖：run_id 幂等创建、批量步骤落 TestResult+截图、增量批次合并、
finish 聚合状态/报告生成/飞书通知（notify 对齐 device-runs）、幂等收尾、
归属校验、run_source 兼容取值；模板上传 PNG 校验/内容哈希去重/image_path 形状；
R2'：create 载荷 started_at/env_id 采用、空步骤 success 语义、并发创建单记录、
桌面真实载荷契约；P2：场景同用例多 alias 步骤合并语义；R6'：exec_<id>.json 侧车清理。
"""
import base64
import io
import threading
import time
import unittest
from datetime import datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient
from jose import jwt
from sqlalchemy.pool import StaticPool
from sqlmodel import SQLModel, Session, create_engine, select

from backend.api import client_runs as client_runs_api
from backend.api import deps
from backend.core.security import ALGORITHM, SECRET_KEY
from backend.database import get_session
from backend.models import (
    ScenarioStep,
    TestCase,
    TestCaseStep,
    TestExecution,
    TestResult,
    TestScenario,
    User,
)


def _png_bytes() -> bytes:
    from PIL import Image

    buffer = io.BytesIO()
    Image.new("RGB", (4, 4), color=(12, 34, 56)).save(buffer, format="PNG")
    return buffer.getvalue()


class _ClientRunsBase(unittest.TestCase):
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

        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        tmp_path = Path(self.tmp.name)
        self.state_patch = patch.object(
            client_runs_api,
            "_run_state_path",
            lambda execution_id: tmp_path / "state" / f"exec_{execution_id}.json",
        )
        self.state_patch.start()
        self.addCleanup(self.state_patch.stop)

        import backend.scenario_results as scenario_results

        self.shots_patch = patch.object(
            scenario_results,
            "_get_reports_root_dir",
            return_value=str(tmp_path / "reports"),
        )
        self.shots_patch.start()
        self.addCleanup(self.shots_patch.stop)

        app = FastAPI()
        app.include_router(client_runs_api.router, prefix="/api/client-runs")
        app.include_router(client_runs_api.template_router, prefix="/api/client-templates")
        app.include_router(client_runs_api.router, prefix="/client-runs")
        app.dependency_overrides[get_session] = lambda: self.session
        self.client = TestClient(app)

    def tearDown(self) -> None:
        self.session.close()
        self.engine.dispose()

    def _add_user(self, username: str, role: str = "user") -> User:
        user = User(username=username, hashed_password="x", role=role, full_name=username)
        self.session.add(user)
        self.session.commit()
        self.session.refresh(user)
        return user

    @staticmethod
    def _headers(username: str) -> dict:
        token = jwt.encode({"sub": username}, SECRET_KEY, algorithm=ALGORITHM)
        return {"Authorization": f"Bearer {token}"}

    def _create_case(self, name: str = "登录用例", steps=2) -> TestCase:
        case = TestCase(name=name, steps=[], variables=[])
        self.session.add(case)
        self.session.commit()
        self.session.refresh(case)
        for order in range(1, steps + 1):
            self.session.add(
                TestCaseStep(
                    case_id=case.id,
                    order=order,
                    action="click" if order == 1 else "assert_text",
                    execute_on=["android", "ios"],
                    platform_overrides={"android": {"selector": "登录", "by": "text"}} if order == 1 else {},
                    args={} if order == 1 else {"expected_text": "欢迎", "match_mode": "contains"},
                    timeout=10,
                    error_strategy="ABORT",
                    retry_count=0,
                    description=f"step-{order}",
                )
            )
        self.session.commit()
        return case

    def _create_scenario(self, cases) -> TestScenario:
        scenario = TestScenario(name="下单场景")
        self.session.add(scenario)
        self.session.commit()
        self.session.refresh(scenario)
        for index, case in enumerate(cases, start=1):
            self.session.add(
                ScenarioStep(scenario_id=scenario.id, case_id=case.id, order=index, alias=f"step-{index}")
            )
        self.session.commit()
        return scenario

    def _start_run(self, headers=None, **overrides) -> dict:
        payload = {
            "run_id": "run-1",
            "kind": "case",
            "case_id": self.case.id if hasattr(self, "case") else None,
            "device_serial": "emulator-5554",
            "platform": "android",
        }
        payload.update(overrides)
        resp = self.client.post("/api/client-runs/", json=payload, headers=headers or self._headers("alice"))
        self.assertEqual(resp.status_code, 200, resp.text)
        return resp.json()

    def _step_payload(self, **overrides) -> dict:
        payload = {
            "case_id": self.case.id,
            "name": self.case.name,
            "steps": [
                {"order": 1, "action": "click", "status": "PASS", "duration": 1.0},
                {
                    "order": 2,
                    "action": "assert_text",
                    "status": "FAIL",
                    "duration": 0.5,
                    "error": "断言失败: 期望页面包含 \"欢迎\"，未找到",
                    "error_code": "E2003_ASSERT_TEXT_FAILED",
                    "attempts": 2,
                },
            ],
        }
        payload.update(overrides)
        return payload


class ClientRunLifecycleTests(_ClientRunsBase):
    def setUp(self) -> None:
        super().setUp()
        self.case = self._create_case()

    def test_create_requires_auth(self):
        resp = self.client.post(
            "/api/client-runs/",
            json={"run_id": "run-x", "kind": "case", "case_id": self.case.id},
        )
        self.assertEqual(resp.status_code, 401)

    def test_create_registers_execution_with_platform_run_source(self):
        body = self._start_run()
        self.assertTrue(body["created"])
        self.assertEqual(body["status"], "RUNNING")
        self.assertEqual(body["run_id"], "run-1")

        execution = self.session.get(TestExecution, body["execution_id"])
        self.assertIsNotNone(execution)
        self.assertEqual(execution.batch_id, "run-1")
        self.assertEqual(execution.run_source, "platform")
        self.assertEqual(execution.platform, "android")
        self.assertEqual(execution.device_serial, "emulator-5554")
        self.assertEqual(execution.scenario_name, self.case.name)

    def test_create_is_idempotent_on_run_id(self):
        first = self._start_run()
        second = self._start_run()
        self.assertFalse(second["created"])
        self.assertEqual(second["execution_id"], first["execution_id"])
        rows = self.session.exec(select(TestExecution)).all()
        self.assertEqual(len(rows), 1)

    def test_create_rejects_foreign_run_id(self):
        self._start_run(headers=self._headers("alice"))
        resp = self.client.post(
            "/api/client-runs/",
            json={"run_id": "run-1", "kind": "case", "case_id": self.case.id},
            headers=self._headers("bob"),
        )
        self.assertEqual(resp.status_code, 409)

    def test_create_validates_run_source_and_kind(self):
        resp = self.client.post(
            "/api/client-runs/",
            json={"run_id": "run-bad", "kind": "case", "case_id": self.case.id, "run_source": "desktop"},
            headers=self._headers("alice"),
        )
        self.assertEqual(resp.status_code, 400)
        resp = self.client.post(
            "/api/client-runs/",
            json={"run_id": "run-bad", "kind": "nope", "case_id": self.case.id},
            headers=self._headers("alice"),
        )
        self.assertEqual(resp.status_code, 400)
        resp = self.client.post(
            "/api/client-runs/",
            json={"run_id": "run-bad", "kind": "case", "case_id": 99999},
            headers=self._headers("alice"),
        )
        self.assertEqual(resp.status_code, 404)

    def test_steps_persist_test_results_and_screenshots(self):
        run = self._start_run()
        screenshot = base64.b64encode(_png_bytes()).decode("ascii")
        resp = self.client.post(
            f"/api/client-runs/{run['execution_id']}/steps",
            json=self._step_payload(
                steps=[
                    {"order": 1, "action": "click", "status": "PASS", "duration": 1.0,
                     "screenshot_base64": screenshot},
                    {"order": 2, "action": "assert_text", "status": "FAIL", "duration": 0.5, "error": "boom"},
                ]
            ),
            headers=self._headers("alice"),
        )
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertEqual(resp.json()["accepted_steps"], 2)

        results = self.session.exec(
            select(TestResult).where(TestResult.execution_id == run["execution_id"]).order_by(TestResult.step_order)
        ).all()
        self.assertEqual([row.status for row in results], ["PASS", "FAIL"])
        self.assertEqual(results[0].step_order, 1)
        self.assertTrue(str(results[0].screenshot_path or "").startswith("screenshots/"))
        shot_file = Path(self.tmp.name) / "reports" / results[0].screenshot_path
        self.assertTrue(shot_file.is_file())

    def test_incremental_step_batches_merge_into_single_case(self):
        run = self._start_run()
        first = self.client.post(
            f"/api/client-runs/{run['execution_id']}/steps",
            json=self._step_payload(
                steps=[{"order": 1, "action": "click", "status": "PASS", "duration": 1.0}]
            ),
            headers=self._headers("alice"),
        )
        self.assertEqual(first.status_code, 200, first.text)
        second = self.client.post(
            f"/api/client-runs/{run['execution_id']}/steps",
            json=self._step_payload(
                steps=[{"order": 2, "action": "assert_text", "status": "PASS", "duration": 0.5}]
            ),
            headers=self._headers("alice"),
        )
        self.assertEqual(second.status_code, 200, second.text)
        self.assertEqual(second.json()["total_steps"], 2)

        with patch.object(client_runs_api.report_generator, "generate_report", return_value="report-x.html") as gen, \
             patch.object(client_runs_api.NotificationService, "send_report_card") as notify:
            resp = self.client.post(
                f"/api/client-runs/{run['execution_id']}/finish",
                json={"duration": 3.0, "notify": True},
                headers=self._headers("alice"),
            )
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertEqual(resp.json()["status"], "PASS")
        steps = gen.call_args.kwargs["steps_results"]
        self.assertEqual(len(steps), 2)
        notify.assert_called_once()

    def test_finish_aggregates_status_builds_report_and_notifies(self):
        run = self._start_run()
        self.client.post(
            f"/api/client-runs/{run['execution_id']}/steps",
            json=self._step_payload(),
            headers=self._headers("alice"),
        )

        with patch.object(
            client_runs_api.report_generator, "generate_report", return_value="report-test.html"
        ) as gen, patch.object(client_runs_api.NotificationService, "send_report_card") as notify:
            resp = self.client.post(
                f"/api/client-runs/{run['execution_id']}/finish",
                json={"duration": 5.5, "notify": True},
                headers=self._headers("alice"),
            )

        self.assertEqual(resp.status_code, 200, resp.text)
        body = resp.json()
        self.assertEqual(body["status"], "FAIL")
        self.assertEqual(body["report_id"], "report-test.html")
        self.assertEqual(body["duration"], 5.5)
        self.assertEqual(body["total_steps"], 2)
        self.assertEqual(body["failed_steps"], 1)

        execution = self.session.get(TestExecution, run["execution_id"])
        self.assertEqual(execution.status, "FAIL")
        self.assertEqual(execution.report_id, "report-test.html")
        self.assertEqual(execution.duration, 5.5)
        self.assertIsNotNone(execution.end_time)

        case = self.session.get(TestCase, self.case.id)
        self.assertEqual(case.last_run_status, "FAIL")

        gen.assert_called_once()
        notify.assert_called_once()
        card = notify.call_args.kwargs
        self.assertEqual(card["status"], "FAIL")
        self.assertEqual(card["total"], 2)
        self.assertEqual(card["passed"], 1)
        self.assertEqual(card["failed"], 1)
        self.assertEqual(card["execution_id"], run["execution_id"])
        self.assertTrue(card["errors"])

    def test_finish_is_idempotent_and_skips_second_notification(self):
        run = self._start_run()
        self.client.post(
            f"/api/client-runs/{run['execution_id']}/steps",
            json=self._step_payload(
                steps=[{"order": 1, "action": "click", "status": "PASS", "duration": 1.0}]
            ),
            headers=self._headers("alice"),
        )
        with patch.object(client_runs_api.report_generator, "generate_report", return_value="r.html"), \
             patch.object(client_runs_api.NotificationService, "send_report_card") as notify:
            first = self.client.post(
                f"/api/client-runs/{run['execution_id']}/finish", json={"notify": True}, headers=self._headers("alice")
            )
            second = self.client.post(
                f"/api/client-runs/{run['execution_id']}/finish", json={"notify": True}, headers=self._headers("alice")
            )

        self.assertEqual(first.status_code, 200)
        self.assertTrue(first.json()["finished"])
        self.assertEqual(second.status_code, 200)
        self.assertFalse(second.json()["finished"])
        self.assertEqual(second.json()["status"], "PASS")
        notify.assert_called_once()

    def test_finish_notify_omitted_defaults_to_silent(self):
        """默认不发（与 Web 一致）：不带 notify 字段的收尾不发飞书——
        手动/回归执行静默，仅显式 notify=true 或定时任务勾选才发。"""
        run = self._start_run()
        self.client.post(
            f"/api/client-runs/{run['execution_id']}/steps",
            json=self._step_payload(
                steps=[{"order": 1, "action": "click", "status": "PASS", "duration": 1.0}]
            ),
            headers=self._headers("alice"),
        )
        with patch.object(client_runs_api.report_generator, "generate_report", return_value="r.html"), \
             patch.object(client_runs_api.NotificationService, "send_report_card") as notify:
            resp = self.client.post(
                f"/api/client-runs/{run['execution_id']}/finish", json={"duration": 1.0},
                headers=self._headers("alice"),
            )
        self.assertEqual(resp.status_code, 200, resp.text)
        notify.assert_not_called()

    def test_finish_notify_false_skips_card(self):
        run = self._start_run()
        self.client.post(
            f"/api/client-runs/{run['execution_id']}/steps",
            json=self._step_payload(
                steps=[{"order": 1, "action": "click", "status": "PASS", "duration": 1.0}]
            ),
            headers=self._headers("alice"),
        )
        with patch.object(client_runs_api.report_generator, "generate_report", return_value="r.html"), \
             patch.object(client_runs_api.NotificationService, "send_report_card") as notify:
            resp = self.client.post(
                f"/api/client-runs/{run['execution_id']}/finish",
                json={"notify": False},
                headers=self._headers("alice"),
            )
        self.assertEqual(resp.status_code, 200, resp.text)
        notify.assert_not_called()

    def test_finish_honors_explicit_status_and_aborted(self):
        run = self._start_run()
        with patch.object(client_runs_api.report_generator, "generate_report", return_value="r.html"), \
             patch.object(client_runs_api.NotificationService, "send_report_card"):
            resp = self.client.post(
                f"/api/client-runs/{run['execution_id']}/finish",
                json={"status": "warning", "aborted": True},
                headers=self._headers("alice"),
            )
        self.assertEqual(resp.status_code, 200, resp.text)
        # 显式 status 优先于 aborted
        self.assertEqual(resp.json()["status"], "WARNING")

        run2 = self._start_run(run_id="run-2")
        with patch.object(client_runs_api.report_generator, "generate_report", return_value="r.html"), \
             patch.object(client_runs_api.NotificationService, "send_report_card"):
            resp2 = self.client.post(
                f"/api/client-runs/{run2['execution_id']}/finish",
                json={"aborted": True},
                headers=self._headers("alice"),
            )
        self.assertEqual(resp2.json()["status"], "ABORTED")

    def test_steps_rejected_after_finish(self):
        run = self._start_run()
        with patch.object(client_runs_api.report_generator, "generate_report", return_value="r.html"), \
             patch.object(client_runs_api.NotificationService, "send_report_card"):
            self.client.post(
                f"/api/client-runs/{run['execution_id']}/finish", json={}, headers=self._headers("alice")
            )
        resp = self.client.post(
            f"/api/client-runs/{run['execution_id']}/steps",
            json=self._step_payload(),
            headers=self._headers("alice"),
        )
        self.assertEqual(resp.status_code, 409)

    def test_foreign_user_cannot_report(self):
        run = self._start_run(headers=self._headers("alice"))
        resp = self.client.post(
            f"/api/client-runs/{run['execution_id']}/steps",
            json=self._step_payload(),
            headers=self._headers("bob"),
        )
        self.assertEqual(resp.status_code, 403)
        resp = self.client.post(
            f"/api/client-runs/{run['execution_id']}/finish", json={}, headers=self._headers("bob")
        )
        self.assertEqual(resp.status_code, 403)
        # admin 可代管
        ok = self.client.post(
            f"/api/client-runs/{run['execution_id']}/finish", json={}, headers=self._headers("admin")
        )
        self.assertEqual(ok.status_code, 200)

    def test_accepts_run_id_ref_and_bare_alias(self):
        body = self._start_run()
        self.assertTrue(body["created"])
        # run_id 作为路径引用
        resp = self.client.post(
            "/api/client-runs/run-1/steps",
            json=self._step_payload(
                steps=[{"order": 1, "action": "click", "status": "PASS", "duration": 1.0}]
            ),
            headers=self._headers("alice"),
        )
        self.assertEqual(resp.status_code, 200, resp.text)
        # 裸别名可达
        bare = self.client.post(
            "/client-runs/run-1/finish", json={"notify": False}, headers=self._headers("alice")
        )
        self.assertEqual(bare.status_code, 200, bare.text)

    def test_missing_run_ref_returns_404(self):
        resp = self.client.post(
            "/api/client-runs/nope/steps",
            json=self._step_payload(),
            headers=self._headers("alice"),
        )
        self.assertEqual(resp.status_code, 404)


class ClientRunScenarioTests(_ClientRunsBase):
    def setUp(self) -> None:
        super().setUp()
        self.case_a = self._create_case(name="用例A")
        self.case_b = self._create_case(name="用例B")
        self.scenario = self._create_scenario([self.case_a, self.case_b])

    def test_scenario_run_builds_scenario_report(self):
        run = self._start_run(
            run_id="scen-1",
            kind="scenario",
            scenario_id=self.scenario.id,
            case_id=None,
        )
        self.assertEqual(run["execution_id"], run["execution_id"])

        for case, alias in ((self.case_a, "step-1"), (self.case_b, "step-2")):
            resp = self.client.post(
                f"/api/client-runs/{run['execution_id']}/steps",
                json={
                    "case_id": case.id,
                    "name": case.name,
                    "alias": alias,
                    "steps": [{"order": 1, "action": "click", "status": "PASS", "duration": 0.5}],
                },
                headers=self._headers("alice"),
            )
            self.assertEqual(resp.status_code, 200, resp.text)

        with patch.object(
            client_runs_api.report_generator, "generate_scenario_report", return_value="scen.html"
        ) as gen, patch.object(client_runs_api.NotificationService, "send_report_card") as notify:
            resp = self.client.post(
                f"/api/client-runs/{run['execution_id']}/finish",
                json={"duration": 2.0, "notify": True},
                headers=self._headers("alice"),
            )

        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertEqual(resp.json()["status"], "PASS")
        self.assertEqual(resp.json()["report_id"], "scen.html")
        cases_results = gen.call_args.kwargs["cases_results"]
        self.assertEqual([entry["alias"] for entry in cases_results], ["step-1", "step-2"])
        notify.assert_called_once()

        execution = self.session.get(TestExecution, run["execution_id"])
        self.assertEqual(execution.status, "PASS")
        scenario = self.session.get(TestScenario, self.scenario.id)
        self.assertEqual(scenario.last_run_status, "PASS")
        self.assertEqual(scenario.last_report_id, "scen.html")
        self.assertEqual(scenario.last_execution_id, run["execution_id"])


class ClientTemplateUploadTests(_ClientRunsBase):
    def setUp(self) -> None:
        super().setUp()
        self.case = self._create_case()
        self.images_dir = Path(self.tmp.name) / "images"
        self.images_patch = patch.object(
            client_runs_api, "TEMPLATE_IMAGE_DIR", self.images_dir
        )
        self.images_patch.start()
        self.addCleanup(self.images_patch.stop)

    def test_upload_png_saves_content_addressed_template(self):
        data = _png_bytes()
        resp = self.client.post(
            "/api/client-templates",
            files={"file": ("tpl.png", data, "image/png")},
            headers=self._headers("alice"),
        )
        self.assertEqual(resp.status_code, 200, resp.text)
        image_path = resp.json()["image_path"]
        self.assertRegex(image_path, r"^static/images/element_[0-9a-f]{16}\.png$")

        saved = self.images_dir / Path(image_path).name
        self.assertTrue(saved.is_file())
        self.assertEqual(saved.read_bytes(), data)

        # 内容哈希去重：同图重复上传返回同一路径
        again = self.client.post(
            "/api/client-templates",
            files={"file": ("other.png", data, "image/png")},
            headers=self._headers("alice"),
        )
        self.assertEqual(again.json()["image_path"], image_path)

    def test_upload_rejects_non_png(self):
        resp = self.client.post(
            "/api/client-templates",
            files={"file": ("tpl.jpg", b"not-a-png", "image/png")},
            headers=self._headers("alice"),
        )
        self.assertEqual(resp.status_code, 400)

    def test_upload_requires_auth(self):
        resp = self.client.post(
            "/api/client-templates",
            files={"file": ("tpl.png", _png_bytes(), "image/png")},
        )
        self.assertEqual(resp.status_code, 401)

    def test_image_path_shape_matches_crop_template(self):
        """image_path 引用格式与 recording.crop_template 返回值一致（static/images/<file>）。"""
        resp = self.client.post(
            "/api/client-templates",
            files={"file": ("tpl.png", _png_bytes(), "image/png")},
            headers=self._headers("alice"),
        )
        image_path = resp.json()["image_path"]
        self.assertTrue(image_path.startswith("static/images/"))
        self.assertTrue(image_path.endswith(".png"))
        self.assertEqual(list(resp.json().keys()), ["image_path"])


class ClientRunCreateFieldTests(_ClientRunsBase):
    """R2'：create 载荷 started_at/env_id 不得被丢弃（桌面 R2 契约字段）。"""

    def setUp(self) -> None:
        super().setUp()
        self.case = self._create_case()

    def test_create_adopts_started_at_iso_and_env_id(self):
        body = self._start_run(started_at="2026-10-22T08:30:00", env_id=7)
        execution = self.session.get(TestExecution, body["execution_id"])
        self.assertEqual(execution.start_time, datetime(2026, 10, 22, 8, 30, 0))
        self.assertIsNone(execution.start_time.tzinfo)  # 与 datetime.now() 口径一致（naive）
        self.assertEqual(execution.env_id, 7)

    def test_create_adopts_epoch_started_at(self):
        """桌面当前发送 time.time()（epoch 秒）；按真实时刻落到 start_time（naive 本地）。"""
        epoch = datetime(2026, 10, 22, 8, 30, 0).timestamp()
        body = self._start_run(started_at=epoch, env_id=None)
        execution = self.session.get(TestExecution, body["execution_id"])
        self.assertEqual(execution.start_time, datetime(2026, 10, 22, 8, 30, 0))
        self.assertIsNone(execution.env_id)

    def test_create_without_optional_fields_keeps_now_start_time(self):
        before = datetime.now()
        body = self._start_run()
        execution = self.session.get(TestExecution, body["execution_id"])
        self.assertGreaterEqual(execution.start_time, before)
        self.assertIsNone(execution.env_id)

    def test_env_id_column_survives_migration(self):
        """历史库升级路径：testexecution.env_id 补列（_migration_add_columns 幂等）。

        预标记除本轮 env_id 迁移外的全部版本，模拟 036 之前的生产库升级。
        """
        import os
        import sqlite3
        import tempfile

        from backend.database import _run_migrations_with_conn

        scratch = sqlite3.connect(":memory:")
        _run_migrations_with_conn(scratch)
        versions = [row[0] for row in scratch.execute("SELECT version FROM schema_migration")]
        scratch.close()
        env_id_versions = [item for item in versions if item.endswith("testexecution_env_id")]
        self.assertTrue(env_id_versions, versions)

        tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        tmp.close()
        self.addCleanup(lambda: os.path.exists(tmp.name) and os.remove(tmp.name))
        conn = sqlite3.connect(tmp.name)
        self.addCleanup(conn.close)
        cursor = conn.cursor()
        cursor.executescript(
            """
            CREATE TABLE testexecution (
                id INTEGER PRIMARY KEY,
                scenario_name VARCHAR
            );
            INSERT INTO testexecution(id, scenario_name) VALUES (1, 'legacy');
            CREATE TABLE schema_migration (
                version VARCHAR PRIMARY KEY,
                applied_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            """
        )
        for version in versions:
            if version in env_id_versions:
                continue
            cursor.execute("INSERT INTO schema_migration(version) VALUES (?)", (version,))
        conn.commit()

        _run_migrations_with_conn(conn)

        columns = {row[1] for row in cursor.execute("PRAGMA table_info(testexecution)").fetchall()}
        self.assertIn("env_id", columns)


class ClientRunStepsSuccessSemanticsTests(_ClientRunsBase):
    """R6'：空步骤批次不得被 all([])==True 推导为 PASS（对齐 DeviceReportCase.success 默认）。"""

    def setUp(self) -> None:
        super().setUp()
        self.case = self._create_case()

    def _finish_status(self, run) -> str:
        with patch.object(client_runs_api.report_generator, "generate_report", return_value="r.html"), \
             patch.object(client_runs_api.NotificationService, "send_report_card"):
            resp = self.client.post(
                f"/api/client-runs/{run['execution_id']}/finish", json={"notify": False}, headers=self._headers("alice")
            )
        self.assertEqual(resp.status_code, 200, resp.text)
        return resp.json()["status"]

    def test_empty_steps_batch_without_explicit_success_defaults_to_fail(self):
        run = self._start_run()
        resp = self.client.post(
            f"/api/client-runs/{run['execution_id']}/steps",
            json={"case_id": self.case.id, "name": self.case.name, "steps": []},
            headers=self._headers("alice"),
        )
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertEqual(self._finish_status(run), "FAIL")

    def test_empty_steps_batch_with_explicit_success_true_passes(self):
        run = self._start_run()
        resp = self.client.post(
            f"/api/client-runs/{run['execution_id']}/steps",
            json={"case_id": self.case.id, "name": self.case.name, "success": True, "steps": []},
            headers=self._headers("alice"),
        )
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertEqual(self._finish_status(run), "PASS")

    def test_non_empty_steps_still_derive_success_from_statuses(self):
        run = self._start_run()
        self.client.post(
            f"/api/client-runs/{run['execution_id']}/steps",
            json=self._step_payload(steps=[{"order": 1, "action": "click", "status": "PASS", "duration": 0.1}]),
            headers=self._headers("alice"),
        )
        self.assertEqual(self._finish_status(run), "PASS")

    def test_steps_accept_case_name_alias_when_name_missing(self):
        """桌面可双写 name/case_name；仅 case_name 时同样落用例名（兼容旧载荷）。"""
        run = self._start_run()
        resp = self.client.post(
            f"/api/client-runs/{run['execution_id']}/steps",
            json={
                "case_id": self.case.id,
                "case_name": "别名用例名",
                "success": True,
                "steps": [{"order": 1, "action": "click", "status": "PASS", "duration": 0.1}],
            },
            headers=self._headers("alice"),
        )
        self.assertEqual(resp.status_code, 200, resp.text)
        state = client_runs_api._load_run_state(run["execution_id"])
        self.assertEqual(state["cases"][0]["case_name"], "别名用例名")


class ClientRunDesktopContractTests(_ClientRunsBase):
    """桌面 R2 修正后真实载荷形状的契约锁死（create/steps/finish 逐字段）。"""

    def setUp(self) -> None:
        super().setUp()
        self.case = self._create_case()

    def test_desktop_shaped_create_steps_finish_roundtrip(self):
        # POST /client-runs/ 与 engine/core/cloud_bridge.py 契约头一致
        create_payload = {
            "run_id": "run-desktop",
            "kind": "case",
            "case_id": self.case.id,
            "scenario_id": None,
            "device_serial": "emulator-5554",
            "platform": "android",
            "run_source": "platform",
            "started_at": time.time(),
            "name": self.case.name,
            "env_id": 3,
        }
        resp = self.client.post("/api/client-runs/", json=create_payload, headers=self._headers("alice"))
        self.assertEqual(resp.status_code, 200, resp.text)
        run = resp.json()
        execution = self.session.get(TestExecution, run["execution_id"])
        self.assertEqual(execution.env_id, 3)
        self.assertEqual(execution.run_source, "platform")
        self.assertIsNotNone(execution.start_time)

        # steps：name/case_name 双写 + success 显式 + exported_variables
        steps_payload = {
            "case_id": self.case.id,
            "name": self.case.name,
            "case_name": self.case.name,
            "success": True,
            "exported_variables": {"token": "abc"},
            "steps": [
                {
                    "order": 1,
                    "action": "click",
                    "status": "PASS",
                    "duration": 0.2,
                    "attempts": 1,
                    "error": None,
                    "error_code": None,
                    "screenshot_base64": None,
                    "output": {"echo": 1},
                }
            ],
        }
        resp = self.client.post(
            f"/api/client-runs/{run['execution_id']}/steps", json=steps_payload, headers=self._headers("alice")
        )
        self.assertEqual(resp.status_code, 200, resp.text)

        # finish：status/error 为真实字段；success/finished_at/logs 为兼容字段（不得 422）
        finish_payload = {
            "status": "PASS",
            "success": True,
            "aborted": False,
            "finished_at": time.time(),
            "duration": 1.5,
            "notify": False,
            "logs": ["step ok"],
            "error": None,
            "exported_variables": {"token": "abc"},
        }
        with patch.object(
            client_runs_api.report_generator, "generate_report", return_value="r.html"
        ) as gen, patch.object(client_runs_api.NotificationService, "send_report_card"):
            resp = self.client.post(
                f"/api/client-runs/{run['execution_id']}/finish", json=finish_payload, headers=self._headers("alice")
            )
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertEqual(resp.json()["status"], "PASS")
        execution = self.session.get(TestExecution, run["execution_id"])
        self.assertEqual(execution.status, "PASS")
        self.assertEqual(execution.duration, 1.5)
        variables = gen.call_args.kwargs.get("variables")
        self.assertIn({"key": "token", "value": "abc"}, variables or [])


class ClientRunConcurrentCreateTests(unittest.TestCase):
    """R2'：并发创建同一 run_id 只允许一条执行记录（BEGIN IMMEDIATE 串行化）。"""

    def test_concurrent_create_same_run_id_creates_single_execution(self):
        import os

        from backend.models import TestCase as _TestCase

        with TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "concurrent.db"
            engine = create_engine(
                f"sqlite:///{db_path}",
                connect_args={"check_same_thread": False, "timeout": 30},
            )
            SQLModel.metadata.create_all(engine)
            with Session(engine) as session:
                user = User(username="alice", hashed_password="x", role="user", full_name="alice")
                session.add(user)
                case = _TestCase(name="并发用例", steps=[], variables=[])
                session.add(case)
                session.commit()
                session.refresh(user)
                session.refresh(case)
                user_id, case_id = user.id, case.id

            state_patch = patch.object(
                client_runs_api,
                "_run_state_path",
                lambda execution_id: Path(tmp) / "state" / f"exec_{execution_id}.json",
            )
            state_patch.start()
            self.addCleanup(state_patch.stop)

            # 两个请求都走到「查重之后、落库之前」的窗口：无串行化时必然双写
            gate = threading.Barrier(2, timeout=1.0)

            class _GatedSession(Session):
                def get(self, entity, ident, *args, **kwargs):
                    try:
                        gate.wait()
                    except threading.BrokenBarrierError:
                        pass  # 对端已被 BEGIN IMMEDIATE 挡在写锁外：继续单边执行
                    return super().get(entity, ident, *args, **kwargs)

            app = FastAPI()
            app.include_router(client_runs_api.router, prefix="/api/client-runs")
            app.dependency_overrides[get_session] = lambda: _GatedSession(engine)
            app.dependency_overrides[deps.get_current_user] = lambda: SimpleNamespace(
                id=user_id, role="user", full_name="alice", username="alice"
            )

            results = []
            errors = []

            def _create() -> None:
                client = TestClient(app)
                try:
                    resp = client.post(
                        "/api/client-runs/",
                        json={
                            "run_id": "run-race",
                            "kind": "case",
                            "case_id": case_id,
                            "device_serial": "emulator-5554",
                            "platform": "android",
                        },
                    )
                    results.append(resp)
                except Exception as exc:  # pragma: no cover - 失败时输出可见
                    errors.append(exc)

            threads = [threading.Thread(target=_create, name=f"create-{index}") for index in range(2)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=30)

            self.assertEqual(errors, [])
            self.assertEqual(len(results), 2, [getattr(r, "text", r) for r in results])
            for resp in results:
                self.assertEqual(resp.status_code, 200, resp.text)
            created_flags = sorted(resp.json()["created"] for resp in results)
            self.assertEqual(created_flags, [False, True])
            self.assertEqual(len({resp.json()["execution_id"] for resp in results}), 1)

            with Session(engine) as session:
                rows = session.exec(select(TestExecution)).all()
                self.assertEqual(len(rows), 1, rows)
            engine.dispose()
            self.assertTrue(os.path.exists(db_path))


class ClientRunStateSidecarTests(_ClientRunsBase):
    """R6'：reports/client_runs/exec_<id>.json 侧车纳入产物清理（retention 复用同一入口）。"""

    def setUp(self) -> None:
        super().setUp()
        self.case = self._create_case()

    def test_delete_execution_artifacts_removes_client_run_state_sidecar(self):
        from backend.api.reports import _delete_execution_artifacts

        run = self._start_run()
        sidecar = client_runs_api._run_state_path(run["execution_id"])
        self.assertTrue(sidecar.is_file())

        execution = self.session.get(TestExecution, run["execution_id"])
        _delete_execution_artifacts(execution, [])
        self.assertFalse(sidecar.is_file())


class ClientRunScenarioAliasTests(_ClientRunsBase):
    """P2：kind=scenario 的步骤合并语义支持同一用例多 alias（键含 alias，互不串批）。"""

    def setUp(self) -> None:
        super().setUp()
        self.case_a = self._create_case(name="用例A")
        self.case_b = self._create_case(name="用例B")

    def _report(self, run, case, alias, steps, **extra):
        payload = {
            "case_id": case.id,
            "name": case.name,
            "alias": alias,
            "steps": steps,
        }
        payload.update(extra)
        resp = self.client.post(
            f"/api/client-runs/{run['execution_id']}/steps", json=payload, headers=self._headers("alice")
        )
        self.assertEqual(resp.status_code, 200, resp.text)
        return resp.json()

    def _finish(self, run):
        with patch.object(
            client_runs_api.report_generator, "generate_scenario_report", return_value="scen.html"
        ) as gen, patch.object(client_runs_api.NotificationService, "send_report_card"):
            resp = self.client.post(
                f"/api/client-runs/{run['execution_id']}/finish",
                json={"duration": 2.0, "notify": False},
                headers=self._headers("alice"),
            )
        self.assertEqual(resp.status_code, 200, resp.text)
        return resp.json(), gen.call_args.kwargs["cases_results"]

    def test_same_case_two_aliases_produce_separate_case_entries(self):
        scenario = self._create_scenario([self.case_a, self.case_a])
        run = self._start_run(
            run_id="scen-alias-1", kind="scenario", scenario_id=scenario.id, case_id=None
        )

        self._report(run, self.case_a, "step-1", [{"order": 1, "action": "click", "status": "PASS", "duration": 0.1}])
        self._report(run, self.case_a, "step-2", [{"order": 1, "action": "click", "status": "FAIL", "duration": 0.2, "error": "boom"}])

        body, cases_results = self._finish(run)
        self.assertEqual([entry["alias"] for entry in cases_results], ["step-1", "step-2"])
        self.assertEqual([entry["status"] for entry in cases_results], ["success", "failed"])
        self.assertEqual(len(cases_results[0]["steps"]), 1)
        self.assertEqual(len(cases_results[1]["steps"]), 1)
        self.assertEqual(body["status"], "FAIL")

    def test_incremental_batches_merge_within_alias_only(self):
        scenario = self._create_scenario([self.case_a, self.case_a])
        run = self._start_run(
            run_id="scen-alias-2", kind="scenario", scenario_id=scenario.id, case_id=None
        )

        self._report(run, self.case_a, "step-1", [{"order": 1, "action": "click", "status": "PASS", "duration": 0.1}])
        self._report(run, self.case_a, "step-2", [{"order": 1, "action": "click", "status": "PASS", "duration": 0.1}])
        self._report(run, self.case_a, "step-1", [{"order": 2, "action": "click", "status": "PASS", "duration": 0.1}])

        _, cases_results = self._finish(run)
        self.assertEqual([entry["alias"] for entry in cases_results], ["step-1", "step-2"])
        self.assertEqual([len(entry["steps"]) for entry in cases_results], [2, 1])

        results = self.session.exec(
            select(TestResult).where(TestResult.execution_id == run["execution_id"]).order_by(TestResult.step_order)
        ).all()
        # TestResult 按上报顺序全局编号；alias 前缀区分同用例多入口
        self.assertEqual([row.step_name.split("]")[0] + "]" for row in results], ["[step-1]", "[step-2]", "[step-1]"])
