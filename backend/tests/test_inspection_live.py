import json
import os
import threading
import unittest
from datetime import datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine
from starlette.websockets import WebSocketDisconnect

from backend.api.inspections import (
    create_run_live_session,
    get_run_live_snapshot,
    get_state_action_map,
    router,
    ws_router,
)
from backend.core.api_tokens import generate_api_token, hash_api_token, token_display_prefix
from backend.database import get_session
from backend.feature_flags import FLAG_MODEL_INSPECTION
from backend.inspection.live import InspectionLiveRegistry, inspection_live_registry
from backend.models import (
    ApiToken,
    InspectionBranchRun,
    InspectionRun,
    InspectionState,
    SystemSetting,
    User,
)


class InspectionLiveRegistryTests(unittest.TestCase):
    def setUp(self):
        self.now = [100.0]
        self.registry = InspectionLiveRegistry(
            clock=lambda: self.now[0],
            ticket_ttl_seconds=60,
            terminal_retention_seconds=600,
            max_sessions_per_run=3,
        )

    def test_publish_builds_safe_complete_snapshot_and_recent_twenty(self):
        started = self.registry.start_run(7, "android-1")
        self.assertTrue(started["stream_id"])
        self.assertIsNotNone(datetime.fromisoformat(started["stream_started_at"]))
        first = self.registry.publish(
            7,
            "PAGE_ACTIONS",
            branch_key="guest",
            phase="EXPLORE",
            current_stage="枚举首页",
            overlay_visible=True,
            page={
                "state_id": 11,
                "screen_width": 1080,
                "screen_height": 2400,
                "activity": "MainActivity",
                "xml": "<secret/>",
                "screenshot_base64": "do-not-send",
                "screenshot_url": "/api/inspections/runs/7/assets?path=safe",
                "thumbnail_url": "/api/inspections/runs/8/assets?path=other-run",
                "screenshot_path": "inspection/8/guest/1/screenshot.png",
            },
            actions=[
                {
                    "action_key": "checkout",
                    "label": "结算",
                    "bounds": [1, 2, 100, 200],
                    "page_order": 1,
                    "locator_type": "description",
                    "failure_type": "PATH_DIVERGED",
                    "execution_disposition": "NOT_REACHED",
                    "coverage_source_transition_id": 42,
                    "action_role_key": "command:checkout",
                    "selector": "do-not-send",
                    "value": "top-secret",
                },
                {
                    "action_key": "password",
                    "label": "actual-password",
                    "sensitive": True,
                    "bounds": [2, 3, 20, 30],
                    "secret": "do-not-send",
                },
            ],
            secret="do-not-send",
        )
        self.assertEqual(first["revision"], 1)
        self.assertEqual(first["page"]["state_id"], 11)
        self.assertNotIn("xml", first["page"])
        self.assertNotIn("screenshot_base64", first["page"])
        self.assertEqual(
            first["page"]["screenshot_url"],
            "/api/inspections/runs/7/assets?path=safe",
        )
        self.assertNotIn("thumbnail_url", first["page"])
        self.assertNotIn("screenshot_path", first["page"])
        self.assertNotIn("selector", first["actions"][0])
        self.assertNotIn("value", first["actions"][0])
        self.assertEqual(first["actions"][0]["failure_type"], "PATH_DIVERGED")
        self.assertEqual(
            first["actions"][0]["execution_disposition"],
            "NOT_REACHED",
        )
        self.assertEqual(first["actions"][0]["coverage_source_transition_id"], 42)
        self.assertEqual(
            first["actions"][0]["action_role_key"],
            "command:checkout",
        )
        self.assertEqual(first["actions"][1]["label"], "敏感输入框")
        self.assertNotIn("secret", str(first).lower())
        failed = self.registry.publish(
            7,
            "ACTION_FINISHED",
            current_action={
                "action_key": "failed",
                "label": "普通按钮",
                "class_name": "android.widget.Button",
                "error": "xpath=/secret actual_value=top-secret",
            },
        )
        self.assertNotIn("class_name", failed["current_action"])
        self.assertEqual(failed["current_action"]["error"], "动作执行异常")
        self.assertNotIn("top-secret", str(failed))

        for index in range(25):
            self.registry.publish(7, "RUN_STAGE", current_stage=f"stage-{index}")
        latest = self.registry.snapshot(7)
        self.assertEqual(latest["revision"], 27)
        self.assertEqual(len(latest["recent_events"]), 20)
        self.assertEqual(latest["current_stage"], "stage-24")

    def test_restarted_channel_gets_new_stream_incarnation(self):
        first = self.registry.start_run(17, "android-1", "RUNNING")
        published = self.registry.publish(
            17,
            "RUN_STAGE",
            current_stage="旧进程",
            stream_id="must-not-replace-stream",
            stream_started_at="2000-01-01T00:00:00+00:00",
        )

        self.assertEqual(published["stream_id"], first["stream_id"])
        self.assertEqual(
            published["stream_started_at"],
            first["stream_started_at"],
        )
        self.assertEqual(published["revision"], 1)

        self.registry.clear()
        restarted = self.registry.start_run(17, "android-1", "RUNNING")

        self.assertNotEqual(restarted["stream_id"], first["stream_id"])
        self.assertEqual(restarted["revision"], 0)
        self.assertIsNotNone(datetime.fromisoformat(restarted["stream_started_at"]))

    def test_v4_actions_and_frontier_survive_live_phase_events(self):
        self.registry.start_run(8, "android-2", "RUNNING")
        action = {
            "action_key": "open-category",
            "action_role": "CATEGORY_TAB",
            "action_role_key": "category_tab:home",
            "execution_disposition": "FAMILY_REUSED",
            "failure_type": "COORDINATE_STALE",
            "coverage_source_transition_id": 73,
            "recovery_attempt_count": 1,
            "selector": "//must-not-leak",
        }
        event_types = (
            "PHASE_CHANGED",
            "FRONTIER_UPDATED",
            "ACTION_DEFERRED",
            "ACTION_RESUMED",
            "ACTION_COVERED_BY_FAMILY",
        )
        snapshot = None
        for event_type in event_types:
            snapshot = self.registry.publish(
                8,
                event_type,
                phase="verify" if event_type == "PHASE_CHANGED" else "explore",
                current_stage="验证稳定路径" if event_type == "PHASE_CHANGED" else "探索页面",
                actions=[action],
                current_action=action,
                frontier={
                    "queued_count": 3,
                    "deferred_count": 1,
                    "pending_action_count": 12,
                    "expanding_count": 1,
                    "internal_state_id": 99,
                },
            )

        self.assertIsNotNone(snapshot)
        visible_action = snapshot["actions"][0]
        self.assertEqual(visible_action["action_role"], "CATEGORY_TAB")
        self.assertEqual(visible_action["action_role_key"], "category_tab:home")
        self.assertEqual(visible_action["execution_disposition"], "FAMILY_REUSED")
        self.assertEqual(visible_action["failure_type"], "COORDINATE_STALE")
        self.assertEqual(visible_action["coverage_source_transition_id"], 73)
        self.assertEqual(visible_action["recovery_attempt_count"], 1)
        self.assertNotIn("selector", visible_action)
        self.assertEqual(snapshot["frontier"], {
            "queued_count": 3,
            "deferred_count": 1,
            "pending_action_count": 12,
            "expanding_count": 1,
        })
        self.assertEqual(snapshot["event_type"], "ACTION_COVERED_BY_FAMILY")
        self.assertEqual(
            [item["type"] for item in snapshot["recent_events"]],
            list(event_types),
        )

    def test_frontier_update_cannot_replace_active_action_panel(self):
        self.registry.start_run(9, "android-3", "RUNNING")
        parent_action = {
            "action_key": "open-product",
            "label": "打开商品",
            "status": "ACTIVE",
            "bounds": [10, 20, 300, 180],
        }
        parent = self.registry.publish(
            9,
            "PAGE_ACTIONS",
            expansion_owner_state_id=101,
            expansion_epoch=1,
            page={"state_id": 101, "activity": "ProductList"},
            actions=[parent_action],
            current_action=parent_action,
            action_panel={
                "state_id": 101,
                "expansion_epoch": 1,
                "expansion_status": "EXPANDING",
                "page": {"state_id": 101, "activity": "ProductList"},
                "actions": [parent_action],
                "current_action": parent_action,
                "canvas_matches_panel": True,
            },
            device_context={
                "state_id": 101,
                "activity": "ProductList",
                "canvas_matches_panel": True,
            },
            canvas_matches_panel=True,
            overlay_visible=True,
        )
        self.assertEqual(parent["action_panel"]["state_id"], 101)

        # A newly observed depth-2 State is queued while State 101 is still
        # being expanded.  Even a malformed frontier publisher cannot switch
        # the logical action panel or its canvas visibility.
        latest = self.registry.publish(
            9,
            "FRONTIER_UPDATED",
            expansion_owner_state_id=202,
            expansion_epoch=2,
            page={"state_id": 202, "activity": "ProductDetail"},
            actions=[{"action_key": "buy-now", "label": "立即购买"}],
            current_action={"action_key": "buy-now"},
            action_panel={"state_id": 202, "actions": []},
            device_context={
                "state_id": 202,
                "activity": "ProductDetail",
                "canvas_matches_panel": False,
            },
            canvas_matches_panel=False,
            overlay_visible=False,
            frontier={"queued_count": 1, "pending_action_count": 3},
        )

        self.assertEqual(latest["expansion_owner_state_id"], 101)
        self.assertEqual(latest["expansion_epoch"], 1)
        self.assertEqual(latest["page"]["state_id"], 101)
        self.assertEqual(latest["actions"][0]["action_key"], "open-product")
        self.assertEqual(latest["action_panel"]["state_id"], 101)
        self.assertTrue(latest["canvas_matches_panel"])
        self.assertTrue(latest["overlay_visible"])
        self.assertEqual(latest["frontier"]["queued_count"], 1)

        # A reconnect receives the same complete owner snapshot even though
        # the transient discovery event itself may have been dropped.
        subscription = self.registry.subscribe(9)
        reconnected = subscription.get(timeout=0.1)
        self.assertEqual(reconnected["action_panel"]["state_id"], 101)
        self.assertEqual(reconnected["expansion_epoch"], 1)
        subscription.close()

    def test_slow_subscriber_keeps_only_latest_snapshot(self):
        self.registry.start_run(1, "serial")
        subscription = self.registry.subscribe(1)
        self.registry.publish(1, "RUN_STAGE", current_stage="one")
        expected = self.registry.publish(1, "RUN_STAGE", current_stage="two")
        received = subscription.get(timeout=0.1)
        self.assertEqual(received["revision"], expected["revision"])
        self.assertEqual(received["current_stage"], "two")
        subscription.close()
        self.assertIsNone(subscription.get(timeout=0.1))

    def test_run_snapshots_and_subscribers_are_isolated(self):
        self.registry.start_run(1, "serial-1")
        self.registry.start_run(2, "serial-2")
        first = self.registry.subscribe(1)
        second = self.registry.subscribe(2)
        self.assertEqual(first.get(timeout=0.1)["run_id"], 1)
        self.assertEqual(second.get(timeout=0.1)["run_id"], 2)

        self.registry.publish(1, "RUN_STAGE", current_stage="run-one")
        self.registry.publish(2, "RUN_STAGE", current_stage="run-two")
        self.assertEqual(first.get(timeout=0.1)["current_stage"], "run-one")
        self.assertEqual(second.get(timeout=0.1)["current_stage"], "run-two")
        first.close()
        second.close()

    def test_concurrent_publish_is_revision_safe(self):
        self.registry.start_run(3, "serial")

        def publish_many(worker: int):
            for index in range(25):
                self.registry.publish(
                    3,
                    "RUN_STAGE",
                    current_stage=f"{worker}-{index}",
                )

        threads = [threading.Thread(target=publish_many, args=(index,)) for index in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(self.registry.snapshot(3)["revision"], 200)

    def test_terminal_snapshot_expires_after_ten_minutes(self):
        self.registry.start_run(9, "serial")
        terminal = self.registry.finish_run(9, "PASS", "完成", "队列耗尽")
        self.assertTrue(terminal["terminal"])
        self.assertEqual(terminal["run_status"], "PASS")
        self.now[0] += 599
        self.assertIsNotNone(self.registry.snapshot(9))
        self.now[0] += 1
        self.assertIsNone(self.registry.snapshot(9))

    def test_terminal_cleanup_closes_slow_subscribers(self):
        self.registry.start_run(10, "serial")
        subscription = self.registry.subscribe(10)
        self.registry.finish_run(10, "PASS", "完成")
        self.now[0] += 600
        self.assertIsNone(self.registry.snapshot(10))
        self.assertIsNone(subscription.get(timeout=0.1))

    def test_tickets_are_scoped_one_use_expiring_and_session_limited(self):
        sessions = [self.registry.create_live_session(5, user_id=8) for _ in range(3)]
        with self.assertRaises(RuntimeError):
            self.registry.create_live_session(5, user_id=9)

        first = sessions[0]
        event_claim = self.registry.consume_ticket(
            first["event_ticket"], run_id=5, kind="event"
        )
        with self.assertRaises(ValueError):
            self.registry.consume_ticket(first["event_ticket"], run_id=5, kind="event")
        video_claim = self.registry.consume_ticket(
            first["video_ticket"], run_id=5, kind="video"
        )
        self.registry.release_channel(event_claim.session_id, "event")
        self.assertEqual(self.registry.active_session_count(5), 3)
        self.registry.release_channel(video_claim.session_id, "video")
        self.assertEqual(self.registry.active_session_count(5), 2)
        self.registry.create_live_session(5, user_id=10)

        wrong = self.registry.create_live_session(6, user_id=8)
        with self.assertRaises(ValueError):
            self.registry.consume_ticket(wrong["event_ticket"], run_id=7, kind="event")
        with self.assertRaises(ValueError):
            self.registry.consume_ticket(wrong["event_ticket"], run_id=6, kind="event")

        expiring = self.registry.create_live_session(8, user_id=8)
        self.now[0] += 60
        with self.assertRaises(ValueError):
            self.registry.consume_ticket(expiring["video_ticket"], run_id=8, kind="video")
        self.assertEqual(self.registry.active_session_count(8), 0)

    def test_session_limit_is_atomic_under_concurrency(self):
        barrier = threading.Barrier(12)
        issued = []
        rejected = []
        result_lock = threading.Lock()

        def issue_session(index: int):
            barrier.wait()
            try:
                value = self.registry.create_live_session(12, user_id=index + 1)
                with result_lock:
                    issued.append(value)
            except RuntimeError:
                with result_lock:
                    rejected.append(index)

        threads = [
            threading.Thread(target=issue_session, args=(index,))
            for index in range(12)
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(len(issued), 3)
        self.assertEqual(len(rejected), 9)
        self.assertEqual(self.registry.active_session_count(12), 3)


class InspectionLiveApiTests(unittest.TestCase):
    def setUp(self):
        inspection_live_registry.clear()
        self.engine = create_engine(
            "sqlite://",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        SQLModel.metadata.create_all(self.engine)
        self.session = Session(self.engine)
        self.user = User(username="live-viewer", hashed_password="x")
        self.run = InspectionRun(
            name="live run",
            package_name="com.example",
            device_serial="serial-1",
            selected_branches=["guest"],
            status="RUNNING",
            current_stage="探索首页",
        )
        self.session.add(self.user)
        self.session.add(SystemSetting(key=FLAG_MODEL_INSPECTION, value="true"))
        self.session.add(self.run)
        self.session.commit()
        self.session.refresh(self.user)
        self.session.refresh(self.run)
        app = FastAPI()
        app.include_router(router, prefix="/api/inspections")

        def override_get_session():
            with Session(self.engine) as session:
                yield session

        app.dependency_overrides[get_session] = override_get_session
        self.client = TestClient(app)

    def tearDown(self):
        inspection_live_registry.clear()
        self.client.close()
        self.session.close()

    def test_session_endpoint_and_snapshot_seed_from_database(self):
        issued = create_run_live_session(
            self.run.id,
            session=self.session,
            current_user=self.user,
        )
        self.assertEqual(issued["run_id"], self.run.id)
        self.assertIn("event_ticket", issued)
        self.assertIn("video_ticket", issued)
        self.assertEqual(issued["expires_in"], 60)
        self.assertTrue(issued["event_ws_url"].startswith("/ws/inspections/"))
        self.assertTrue(issued["video_available"])

        snapshot = get_run_live_snapshot(
            self.run.id,
            session=self.session,
            current_user=self.user,
        )
        self.assertEqual(snapshot["device_serial"], "serial-1")
        self.assertEqual(snapshot["current_stage"], "探索首页")

    def test_restart_restores_only_authoritative_expanding_owner(self):
        branch = InspectionBranchRun(
            run_id=self.run.id,
            branch_key="guest",
            branch_name="未登录",
            status="PASS",
        )
        self.session.add(branch)
        self.session.commit()
        self.session.refresh(branch)
        state = InspectionState(
            run_id=self.run.id,
            branch_run_id=branch.id,
            branch_key="guest",
            cluster_key="cluster-terminal",
            state_key="state-terminal",
            activity="MainActivity",
            foreground_package="com.example",
            expansion_status="EXPANDING",
        )
        self.session.add(state)
        self.session.commit()
        self.session.refresh(state)
        state.screenshot_path = (
            f"inspection/{self.run.id}/guest/{state.id}/screenshot.png"
        )
        self.run.status = "PASS"
        self.run.current_stage = "任务完成"
        self.run.stop_reason = "队列自然耗尽"
        self.session.add(state)
        self.session.add(self.run)
        self.session.commit()
        queued_child = InspectionState(
            run_id=self.run.id,
            branch_run_id=branch.id,
            branch_key="guest",
            cluster_key="cluster-newer-child",
            state_key="state-newer-child",
            activity="QueuedChildActivity",
            foreground_package="com.example",
            expansion_status="QUEUED",
            parent_state_id=state.id,
        )
        self.session.add(queued_child)
        self.session.commit()
        inspection_live_registry.clear()

        with TemporaryDirectory() as temp_dir:
            state_dir = (
                Path(temp_dir)
                / "reports"
                / "inspection"
                / str(self.run.id)
                / "guest"
                / str(state.id)
            )
            state_dir.mkdir(parents=True)
            (state_dir / "screenshot.png").write_bytes(b"sanitized-image")
            (state_dir / "actions.json").write_text(
                json.dumps(
                    {
                        "screen_width": 1080,
                        "screen_height": 2400,
                        "captured_at": "2026-07-20T12:00:00",
                        "actions": [
                            {
                                "action_key": "open-next",
                                "label": "下一页",
                                "bounds": [10, 20, 100, 200],
                                "status": "PASS",
                                "selector": "//secret",
                                "secret": "top-secret",
                            }
                        ],
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            with patch(
                "backend.inspection.engine.project_path",
                side_effect=lambda *parts: Path(temp_dir).joinpath(*parts),
            ):
                snapshot = get_run_live_snapshot(
                    self.run.id,
                    session=self.session,
                    current_user=self.user,
                )

        self.assertTrue(snapshot["terminal"])
        self.assertFalse(snapshot["overlay_visible"])
        self.assertEqual(snapshot["page"]["state_id"], state.id)
        self.assertEqual(snapshot["page"]["activity"], "MainActivity")
        self.assertEqual(snapshot["page"]["screen_width"], 1080)
        self.assertEqual(
            snapshot["page"]["screenshot_path"],
            state.screenshot_path,
        )
        self.assertEqual(snapshot["actions"][0]["label"], "下一页")
        self.assertNotIn("selector", snapshot["actions"][0])
        self.assertNotIn("top-secret", str(snapshot))
        self.assertEqual(snapshot["expansion_owner_state_id"], state.id)
        self.assertEqual(snapshot["expansion_epoch"], 1)
        self.assertEqual(snapshot["action_panel"]["state_id"], state.id)
        self.assertEqual(
            snapshot["action_panel"]["expansion_status"],
            "EXPANDING",
        )
        self.assertFalse(snapshot["action_panel"]["canvas_matches_panel"])

    def test_restart_without_expanding_owner_does_not_guess_latest_state(self):
        branch = InspectionBranchRun(
            run_id=self.run.id,
            branch_key="guest",
            branch_name="未登录",
            status="RUNNING",
        )
        self.session.add(branch)
        self.session.commit()
        self.session.refresh(branch)
        queued = InspectionState(
            run_id=self.run.id,
            branch_run_id=branch.id,
            branch_key="guest",
            cluster_key="cluster-queued",
            state_key="state-queued",
            activity="QueuedActivity",
            foreground_package="com.example",
            expansion_status="QUEUED",
        )
        self.session.add(queued)
        self.session.commit()
        inspection_live_registry.clear()

        snapshot = get_run_live_snapshot(
            self.run.id,
            session=self.session,
            current_user=self.user,
        )

        self.assertIsNone(snapshot["page"])
        self.assertEqual(snapshot["actions"], [])
        self.assertIsNone(snapshot["action_panel"])
        self.assertIsNone(snapshot["expansion_owner_state_id"])
        self.assertEqual(snapshot["expansion_epoch"], 0)

    def test_session_limit_returns_http_429(self):
        for _ in range(3):
            create_run_live_session(
                self.run.id,
                session=self.session,
                current_user=self.user,
            )
        with self.assertRaises(HTTPException) as context:
            create_run_live_session(
                self.run.id,
                session=self.session,
                current_user=self.user,
            )
        self.assertEqual(context.exception.status_code, 429)

    def test_live_session_requires_login_and_rejects_api_tokens(self):
        unauthenticated = self.client.post(
            f"/api/inspections/runs/{self.run.id}/live-session"
        )
        self.assertEqual(unauthenticated.status_code, 401)

        token = generate_api_token()
        self.session.add(
            ApiToken(
                name="ci",
                token_hash=hash_api_token(token),
                token_prefix=token_display_prefix(token),
                user_id=self.user.id,
            )
        )
        self.session.commit()
        machine_credential = self.client.post(
            f"/api/inspections/runs/{self.run.id}/live-session",
            headers={"Authorization": f"Bearer {token}"},
        )
        self.assertEqual(machine_credential.status_code, 403)

    def test_action_map_is_scoped_to_run_and_must_be_json_object(self):
        branch = InspectionBranchRun(
            run_id=self.run.id,
            branch_key="guest",
            branch_name="未登录",
        )
        self.session.add(branch)
        self.session.commit()
        self.session.refresh(branch)
        state = InspectionState(
            run_id=self.run.id,
            branch_run_id=branch.id,
            branch_key="guest",
            cluster_key="cluster",
            state_key="state",
        )
        self.session.add(state)
        self.session.commit()
        self.session.refresh(state)

        with TemporaryDirectory() as temp_dir:
            target = Path(temp_dir) / "actions.json"
            target.write_text(
                json.dumps(
                    {
                        "run_id": 999,
                        "state_id": 999,
                        "branch_key": "other",
                        "activity": "SpoofedActivity",
                        "screenshot_path": "inspection/999/private.png",
                        "screen_width": 1080,
                        "screen_height": 2400,
                        "xml": "<secret/>",
                        "actions": [
                            {
                                "action_key": "home",
                                "label": "首页",
                                "class_name": "android.widget.Button",
                                "selector": "//secret",
                                "value": "top-secret",
                                "bounds": [1, 2, 3, 4],
                            },
                            {
                                "action_key": "password",
                                "label": "password",
                                "password": True,
                                "secret": "top-secret",
                                "error": "selector=//secret value=top-secret",
                            },
                        ],
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            with patch(
                "backend.api.inspections.resolve_inspection_asset",
                return_value=target,
            ):
                payload = get_state_action_map(
                    self.run.id,
                    state.id,
                    session=self.session,
                    current_user=self.user,
                )
        self.assertEqual(payload["actions"][0]["label"], "首页")
        self.assertEqual(payload["run_id"], self.run.id)
        self.assertEqual(payload["state_id"], state.id)
        self.assertEqual(payload["branch_key"], "guest")
        self.assertEqual(payload["activity"], "")
        self.assertNotIn("screenshot_path", payload)
        self.assertNotIn("class_name", payload["actions"][0])
        self.assertNotIn("selector", payload["actions"][0])
        self.assertNotIn("value", payload["actions"][0])
        self.assertEqual(payload["actions"][1]["label"], "敏感输入框")
        self.assertEqual(payload["actions"][1]["error"], "动作执行异常")
        self.assertNotIn("top-secret", str(payload))
        self.assertNotIn("//secret", str(payload))

        with self.assertRaises(HTTPException) as context:
            get_state_action_map(
                self.run.id + 1,
                state.id,
                session=self.session,
                current_user=self.user,
            )
        self.assertEqual(context.exception.status_code, 404)

        if hasattr(os, "symlink"):
            with TemporaryDirectory() as temp_dir:
                reports = Path(temp_dir) / "reports"
                state_dir = (
                    reports
                    / "inspection"
                    / str(self.run.id)
                    / "guest"
                    / str(state.id)
                )
                state_dir.mkdir(parents=True)
                outside = Path(temp_dir) / "outside-actions.json"
                outside.write_text('{"actions":[]}', encoding="utf-8")
                (state_dir / "actions.json").symlink_to(outside)
                with patch(
                    "backend.inspection.engine.project_path",
                    side_effect=lambda *parts: Path(temp_dir).joinpath(*parts),
                ), self.assertRaises(HTTPException) as symlink_context:
                    get_state_action_map(
                        self.run.id,
                        state.id,
                        session=self.session,
                        current_user=self.user,
                    )
            self.assertEqual(symlink_context.exception.status_code, 400)

        state.branch_key = "../escape"
        self.session.add(state)
        self.session.commit()
        with self.assertRaises(HTTPException) as traversal_context:
            get_state_action_map(
                self.run.id,
                state.id,
                session=self.session,
                current_user=self.user,
            )
        self.assertEqual(traversal_context.exception.status_code, 400)


class InspectionLiveWebSocketTests(unittest.TestCase):
    def setUp(self):
        inspection_live_registry.clear()
        self.run_id = 901
        inspection_live_registry.start_run(self.run_id, "serial-live", "RUNNING")
        app = FastAPI()
        app.include_router(ws_router)
        self.client = TestClient(app)

    def tearDown(self):
        inspection_live_registry.clear()

    def test_event_socket_gets_latest_complete_snapshots_and_terminal(self):
        issued = inspection_live_registry.create_live_session(self.run_id, user_id=1)
        url = f"/ws/inspections/runs/{self.run_id}/live?ticket={issued['event_ticket']}"
        with self.client.websocket_connect(url) as websocket:
            initial = websocket.receive_json()
            self.assertEqual(initial["revision"], 0)
            inspection_live_registry.publish(
                self.run_id,
                "PAGE_ACTIONS",
                page={"state_id": 42, "screen_width": 1080, "screen_height": 2400},
                actions=[{"action_key": "home", "label": "首页", "bounds": [0, 0, 50, 50]}],
                overlay_visible=True,
            )
            update = websocket.receive_json()
            self.assertEqual(update["page"]["state_id"], 42)
            self.assertTrue(update["overlay_visible"])
            inspection_live_registry.finish_run(self.run_id, "PASS", "完成")
            terminal = websocket.receive_json()
            self.assertTrue(terminal["terminal"])
        # The unused paired video ticket keeps this viewing reservation until
        # ticket expiry; disconnecting the event socket did not alter run state.
        self.assertEqual(inspection_live_registry.snapshot(self.run_id)["run_status"], "PASS")

    def test_video_socket_reuses_existing_device_generator(self):
        issued = inspection_live_registry.create_live_session(self.run_id, user_id=1)
        url = f"/ws/inspections/runs/{self.run_id}/video?ticket={issued['video_ticket']}"
        packets = iter([b"\x00\x00\x00\x01first", b"\x00\x00\x00\x01second"])
        with patch(
            "backend.device_stream.manager.device_manager.get_video_generator",
            return_value=packets,
        ) as generator, patch(
            "backend.device_stream.manager.device_manager.reconnect_device",
        ) as reconnect:
            with self.client.websocket_connect(url) as websocket:
                self.assertEqual(websocket.receive_bytes(), b"\x00\x00\x00\x01first")
                self.assertEqual(websocket.receive_bytes(), b"\x00\x00\x00\x01second")
        generator.assert_called_once_with("serial-live")
        reconnect.assert_not_called()

    def test_video_socket_closes_when_run_reaches_terminal_state(self):
        release_second = threading.Event()

        class BlockingPackets:
            def __init__(self):
                self.index = 0
                self.closed = False

            def __iter__(self):
                return self

            def __next__(self):
                if self.index == 0:
                    self.index += 1
                    return b"\x00\x00\x00\x01first"
                release_second.wait(timeout=2)
                self.index += 1
                return b"\x00\x00\x00\x01should-not-send"

            def close(self):
                self.closed = True
                release_second.set()

        packets = BlockingPackets()
        issued = inspection_live_registry.create_live_session(self.run_id, user_id=1)
        url = f"/ws/inspections/runs/{self.run_id}/video?ticket={issued['video_ticket']}"
        with patch(
            "backend.device_stream.manager.device_manager.get_video_generator",
            return_value=packets,
        ):
            with self.client.websocket_connect(url) as websocket:
                self.assertEqual(
                    websocket.receive_bytes(),
                    b"\x00\x00\x00\x01first",
                )
                inspection_live_registry.finish_run(
                    self.run_id,
                    "PASS",
                    "完成",
                )
                release_second.set()
                with self.assertRaises(WebSocketDisconnect) as context:
                    websocket.receive_bytes()
                self.assertEqual(context.exception.code, 1000)
        self.assertTrue(packets.closed)

    def test_websocket_ticket_cannot_be_reused(self):
        issued = inspection_live_registry.create_live_session(self.run_id, user_id=1)
        url = f"/ws/inspections/runs/{self.run_id}/live?ticket={issued['event_ticket']}"
        with self.client.websocket_connect(url) as websocket:
            self.assertEqual(websocket.receive_json()["run_id"], self.run_id)
        with self.assertRaises(WebSocketDisconnect) as context:
            with self.client.websocket_connect(url):
                pass
        self.assertEqual(context.exception.code, 4401)


if __name__ == "__main__":
    unittest.main()
