"""桌面客户端兼容性回放上报面（/api/client-compatibility）契约测试。

契约（供桌面引擎 cloud_bridge 对齐，参考 client-runs 模式）：
- POST /api/client-compatibility/runs：创建 INSTALLED_REPLAY 客户端执行 run
  （installed_package 由桌面本机 adb 读取上报，云端不做设备 adb、不派发后台
  worker；run/cells 建档 PENDING，client_executed=True）；
- POST /api/client-compatibility/runs/{run_id}/report：逐 cell × 逐 chain 幂等
  上报结果（CompatibilityPageResult + 逐步 trace + 证据资产 CAS），全 cell 终态
  后收尾 run 状态；云端执行的 run（client_executed=False）拒绝上报；
- 鉴权 get_current_user，写操作限 run 创建人本人或 admin。
"""
import base64
import os
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient
from jose import jwt
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, select

from backend.api import client_compatibility
from backend.core.security import ALGORITHM, SECRET_KEY
from backend.database import get_session
from backend.compatibility_replay import package_snapshot_digest
from backend.models import (
    CompatibilityCell,
    CompatibilityPageResult,
    CompatibilityRun,
    Device,
    InspectionBranchRun,
    InspectionRun,
    TestCase,
    User,
)

PNG_1PX = base64.b64encode(
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
    b"\x08\x06\x00\x00\x00\x1f\x15\xc4\x89\x00\x00\x00\nIDATx\x9cc\x00\x01"
    b"\x00\x00\x05\x00\x01\r\n-\xb4\x00\x00\x00\x00IEND\xaeB`\x82"
).decode("ascii")


class ClientCompatibilityApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = create_engine(
            "sqlite://",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        SQLModel.metadata.create_all(self.engine)
        self.session = Session(self.engine)
        self.user = User(username="alice", hashed_password="x", role="user")
        self.other = User(username="bob", hashed_password="x", role="user")
        self.session.add(self.user)
        self.session.add(self.other)
        self.session.commit()
        self.session.refresh(self.user)
        self.session.refresh(self.other)

        self.case = TestCase(name="safe entry", steps=[])
        self.session.add(self.case)
        self.session.commit()
        self.session.refresh(self.case)
        self.source = InspectionRun(
            name="inspection source",
            package_name="com.demo.app",
            status="WARNING",
            device_serial="android-1",
            profile_snapshot={
                "package_snapshot": {
                    "package_name": "com.demo.app",
                    "version_name": "1.0",
                    "version_code": "100",
                },
                "branches": {
                    "authenticated": {
                        "name": "已登录",
                        "entry_case_id": self.case.id,
                        "prepare_case_id": self.case.id,
                        "ready_assertion": {"by": "text", "selector": "首页", "timeout": 1},
                    }
                },
            },
        )
        self.session.add(self.source)
        self.session.commit()
        self.session.refresh(self.source)
        self.session.add(
            InspectionBranchRun(
                run_id=self.source.id,
                branch_key="authenticated",
                branch_name="已登录",
                status="WARNING",
            )
        )
        self.session.add(
            Device(serial="android-1", platform="android", model="Pixel", status="IDLE")
        )
        self.session.commit()

        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self._patches = [
            patch(
                "backend.artifact_store.project_path",
                side_effect=lambda *parts: self.root.joinpath(*parts),
            ),
            patch(
                "backend.api.compatibility.project_path",
                side_effect=lambda *parts: self.root.joinpath(*parts),
            ),
        ]
        for item in self._patches:
            item.start()

        app = FastAPI()
        app.include_router(client_compatibility.router, prefix="/api/client-compatibility")
        app.dependency_overrides[get_session] = lambda: self.session
        self.client = TestClient(app)
        self.token = jwt.encode({"sub": "alice"}, SECRET_KEY, algorithm=ALGORITHM)
        self.headers = {"Authorization": f"Bearer {self.token}"}

    def tearDown(self) -> None:
        for item in reversed(self._patches):
            item.stop()
        self.tmp.cleanup()
        self.session.close()
        self.engine.dispose()

    @staticmethod
    def _snapshot() -> dict:
        value = {
            "package_name": "com.demo.app",
            "version_name": "1.0",
            "version_code": "100",
            "first_install_time": "2026-07-22 10:00:00",
            "last_update_time": "2026-07-22 10:00:00",
            "signing_digest": "a" * 64,
            "installed": True,
            "known": True,
            "source": "device",
            "captured_at": datetime.now().isoformat(),
        }
        value["snapshot_digest"] = package_snapshot_digest(value)
        return value

    @staticmethod
    def _plan() -> dict:
        return {
            "plan_version": 1,
            "digest": "plan-digest",
            "summary": {"selected": 1},
            "excluded": {},
            "chains": [
                {
                    "chain_id": "chain-home",
                    "path_key": "path-home",
                    "name": "HOME",
                    "page_name": "HOME",
                    "display_index": 1,
                    "display_label": "P001",
                    "endpoint_state_id": 1,
                    "source_observation_id": 2,
                    "source_observation_index": 1,
                    "evidence_level": "OBSERVED_ONCE",
                    "first_path": [],
                    "checkpoints": [
                        {
                            "checkpoint_index": 0,
                            "role": "HOME",
                            "page_subtype": "HOME",
                            "expectation": {"role": "HOME"},
                        }
                    ],
                    "covered_roles": ["HOME"],
                    "covered_family_ids": [],
                    "covered_family_keys": [],
                    "depth": 0,
                }
            ],
        }

    def _create_payload(self, snapshot: dict, **overrides) -> dict:
        payload = {
            "name": "客户端回放",
            "execution_mode": "installed_replay",
            "inspection_run_id": self.source.id,
            "replay_branch_key": "authenticated",
            "selected_chain_ids": ["chain-home"],
            "plan_digest": "plan-digest",
            "device_snapshot_digest": snapshot["snapshot_digest"],
            "manual_install_confirmed": True,
            "duration_seconds": 300,
            "device_serials": ["android-1"],
            "installed_package": snapshot,
        }
        payload.update(overrides)
        return payload

    def _create_run(self, headers=None, **overrides) -> dict:
        has_pkg = "installed_package" in overrides
        installed = overrides.pop("installed_package", None)
        snapshot = self._snapshot()
        with patch(
            "backend.api.compatibility.build_replay_plan",
            return_value=self._plan(),
        ):
            resp = self.client.post(
                "/api/client-compatibility/runs",
                json=self._create_payload(
                    snapshot,
                    installed_package=installed if has_pkg else snapshot,
                    **overrides,
                ),
                headers=headers or self.headers,
            )
        return resp

    # -- 创建 --

    def test_create_client_run_skips_cloud_worker_and_stays_pending(self):
        resp = self._create_run()
        self.assertEqual(resp.status_code, 200, resp.text)
        body = resp.json()
        self.assertEqual(str(body["execution_mode"]).upper(), "INSTALLED_REPLAY")
        self.assertEqual(body["status"], "PENDING")

        run = self.session.get(CompatibilityRun, body["id"])
        self.assertTrue(run.client_executed)
        self.assertEqual(run.status, "PENDING")
        self.assertEqual(run.target_package_snapshot["snapshot_digest"], self._snapshot()["snapshot_digest"])
        cells = self.session.exec(select(CompatibilityCell).where(CompatibilityCell.run_id == run.id)).all()
        self.assertEqual(len(cells), 1)
        self.assertEqual(cells[0].status, "PENDING")
        # 冻结链路进入 page_set_snapshot（客户端上报按 chain_id 对账）
        self.assertEqual(run.page_set_snapshot[0]["chain_id"], "chain-home")

    def test_create_requires_installed_package(self):
        snapshot = self._snapshot()
        resp = self._create_run(installed_package={})
        self.assertEqual(resp.status_code, 400, resp.text)
        self.assertIn("installed_package", resp.json()["detail"])

    def test_create_rejects_comparison_mode(self):
        resp = self._create_run(
            execution_mode="comparison",
            selected_chain_ids=[],
            replay_branch_key=None,
            inspection_run_id=None,
            plan_digest=None,
            device_snapshot_digest=None,
            duration_seconds=None,
            manual_install_confirmed=False,
            new_package_id=None,
        )
        # comparison 模式拒绝（schema 校验 422 或 handler 400 均为显式拒绝）
        self.assertIn(resp.status_code, (400, 422), resp.text)

    def test_create_rejects_stale_device_snapshot_digest(self):
        resp = self._create_run(device_snapshot_digest="stale")
        self.assertEqual(resp.status_code, 409, resp.text)

    # -- 上报 --

    def _report_payload(self, status="PASS", chain_status="PASS", **chain_overrides) -> dict:
        chain = {
            "chain_id": "chain-home",
            "status": chain_status,
            "reason": None,
            "failure_type": None,
            "failed_step_index": None,
            "completed_checkpoints": 1,
            "duration_ms": 123.0,
            "warning_codes": [],
            "trace": [
                {"step_index": 0, "status": "PASS", "duration_ms": 123.0},
            ],
            "screenshot_png_base64": PNG_1PX,
            "xml_text": "<hierarchy><node text='首页'/></hierarchy>",
            "activity": "com.demo.app/.HomeActivity",
        }
        chain.update(chain_overrides)
        return {
            "cells": [
                {
                    "device_serial": "android-1",
                    "status": status,
                    "error_message": None,
                    "current_stage": "完成",
                    "installed_package_snapshot": self._snapshot(),
                    "chains": [chain],
                }
            ]
        }

    def test_report_writes_page_results_and_finalizes_run(self):
        created = self._create_run().json()
        resp = self.client.post(
            f"/api/client-compatibility/runs/{created['id']}/report",
            json=self._report_payload(),
            headers=self.headers,
        )
        self.assertEqual(resp.status_code, 200, resp.text)
        body = resp.json()
        self.assertTrue(body["success"])
        self.assertTrue(body["finished"])
        self.assertEqual(body["run_status"], "PASS")
        self.assertEqual(body["reported_chains"], 1)

        row = self.session.exec(select(CompatibilityPageResult)).one()
        self.assertEqual(row.status, "PASS")
        self.assertEqual(row.page_key, "chain-home")
        self.assertEqual(len(row.replay_trace), 1)
        self.assertTrue(row.candidate_screenshot_path)
        self.assertTrue(row.candidate_xml_path)
        self.assertEqual(row.candidate_activity, "com.demo.app/.HomeActivity")

        run = self.session.get(CompatibilityRun, created["id"])
        self.assertEqual(run.status, "PASS")
        self.assertIsNotNone(run.finished_at)
        cell = self.session.exec(select(CompatibilityCell)).one()
        self.assertEqual(cell.status, "PASS")

    def test_report_partial_then_terminal(self):
        created = self._create_run().json()
        intermediate = self.client.post(
            f"/api/client-compatibility/runs/{created['id']}/report",
            json=self._report_payload(status="RUNNING"),
            headers=self.headers,
        )
        self.assertEqual(intermediate.status_code, 200, intermediate.text)
        self.assertFalse(intermediate.json()["finished"])
        self.assertEqual(intermediate.json()["run_status"], "RUNNING")

        final = self.client.post(
            f"/api/client-compatibility/runs/{created['id']}/report",
            json=self._report_payload(status="FAIL", chain_status="FAIL", failure_type="CHECKPOINT_FAILED"),
            headers=self.headers,
        )
        self.assertEqual(final.status_code, 200, final.text)
        self.assertTrue(final.json()["finished"])
        self.assertEqual(final.json()["run_status"], "FAIL")

    def test_report_idempotent_upsert_by_chain(self):
        created = self._create_run().json()
        self.client.post(
            f"/api/client-compatibility/runs/{created['id']}/report",
            json=self._report_payload(status="RUNNING"),
            headers=self.headers,
        )
        self.client.post(
            f"/api/client-compatibility/runs/{created['id']}/report",
            json=self._report_payload(status="PASS"),
            headers=self.headers,
        )
        rows = self.session.exec(select(CompatibilityPageResult)).all()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].status, "PASS")

    def test_report_rejects_cloud_executed_run(self):
        run = CompatibilityRun(
            name="cloud run",
            source_type="inspection",
            inspection_run_id=self.source.id,
            package_name="com.demo.app",
            execution_mode="INSTALLED_REPLAY",
            status="RUNNING",
            user_id=self.user.id,
            client_executed=False,
        )
        self.session.add(run)
        self.session.commit()
        self.session.refresh(run)

        resp = self.client.post(
            f"/api/client-compatibility/runs/{run.id}/report",
            json=self._report_payload(),
            headers=self.headers,
        )
        self.assertEqual(resp.status_code, 409, resp.text)

    def test_report_rejects_comparison_run(self):
        run = CompatibilityRun(
            name="comparison run",
            source_type="inspection",
            package_name="com.demo.app",
            execution_mode="COMPARISON",
            status="RUNNING",
            user_id=self.user.id,
            client_executed=True,
        )
        self.session.add(run)
        self.session.commit()
        self.session.refresh(run)

        resp = self.client.post(
            f"/api/client-compatibility/runs/{run.id}/report",
            json=self._report_payload(),
            headers=self.headers,
        )
        self.assertEqual(resp.status_code, 400, resp.text)

    def test_report_rejects_unknown_chain(self):
        created = self._create_run().json()
        resp = self.client.post(
            f"/api/client-compatibility/runs/{created['id']}/report",
            json=self._report_payload(chain_id="chain-missing"),
            headers=self.headers,
        )
        self.assertEqual(resp.status_code, 400, resp.text)
        self.assertIn("chain-missing", resp.json()["detail"])

    def test_report_rejects_terminal_run(self):
        created = self._create_run().json()
        self.client.post(
            f"/api/client-compatibility/runs/{created['id']}/report",
            json=self._report_payload(),
            headers=self.headers,
        )
        resp = self.client.post(
            f"/api/client-compatibility/runs/{created['id']}/report",
            json=self._report_payload(),
            headers=self.headers,
        )
        self.assertEqual(resp.status_code, 409, resp.text)

    def test_report_owner_scope(self):
        created = self._create_run().json()
        bob_token = jwt.encode({"sub": "bob"}, SECRET_KEY, algorithm=ALGORITHM)
        resp = self.client.post(
            f"/api/client-compatibility/runs/{created['id']}/report",
            json=self._report_payload(),
            headers={"Authorization": f"Bearer {bob_token}"},
        )
        self.assertEqual(resp.status_code, 403, resp.text)

    def test_requires_token(self):
        resp = self.client.post("/api/client-compatibility/runs", json={})
        self.assertEqual(resp.status_code, 401)


if __name__ == "__main__":
    unittest.main()
