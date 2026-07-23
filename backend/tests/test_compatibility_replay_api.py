import asyncio
import inspect
import time
import unittest
import threading
from datetime import datetime
from unittest.mock import AsyncMock, Mock, patch

from fastapi import BackgroundTasks
from sqlmodel import Session, SQLModel, create_engine, select

from backend.api.compatibility import (
    _capture_activity,
    _capture_logcat_errors,
    _clear_logcat,
    _execute_cell_installed_replay_body,
    _execute_run_async,
    _record_replay_result,
    create_run,
    replay_preflight,
)
from backend.compatibility_replay import (
    entry_case_safety_issues,
    package_snapshot_digest,
    select_and_freeze_chains,
)
from backend.inspection.device import InspectionAborted
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
from backend.schemas import (
    ActionType,
    CompatibilityReplayPreflightRequest,
    CompatibilityRunCreate,
    Step,
)


class InstalledReplayApiTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.engine = create_engine(
            "sqlite://",
            connect_args={"check_same_thread": False},
        )
        SQLModel.metadata.create_all(self.engine)
        self.session = Session(self.engine)
        self.user = User(username="replay-user", hashed_password="x")
        self.session.add(self.user)
        self.session.commit()
        self.session.refresh(self.user)
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
                        "ready_assertion": {
                            "by": "text",
                            "selector": "首页",
                            "timeout": 1,
                        },
                    }
                }
            },
        )
        self.session.add(self.source)
        self.session.commit()
        self.session.refresh(self.source)
        self.branch = InspectionBranchRun(
            run_id=self.source.id,
            branch_key="authenticated",
            branch_name="已登录",
            status="WARNING",
        )
        self.session.add(self.branch)
        self.session.add(
            Device(
                serial="android-1",
                platform="android",
                model="Pixel",
                status="IDLE",
            )
        )
        self.session.commit()

    def tearDown(self):
        self.session.close()

    @staticmethod
    def _snapshot():
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
    def _plan():
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

    def _seed_replay_run(self, snapshot, *, chain_count=1):
        base_chain = select_and_freeze_chains(
            self._plan(),
            ["chain-home"],
            source_run=self.source,
            branch_key="authenticated",
        )[0]
        chains = []
        for index in range(chain_count):
            chain = dict(base_chain)
            if index:
                chain["chain_id"] = f"chain-home-{index + 1}"
                chain["path_key"] = f"path-home-{index + 1}"
            chains.append(chain)
        run = CompatibilityRun(
            name="installed replay seeded",
            source_type="inspection",
            inspection_run_id=self.source.id,
            package_name="com.demo.app",
            execution_mode="INSTALLED_REPLAY",
            replay_branch_key="authenticated",
            replay_duration_seconds=300,
            target_package_snapshot=dict(snapshot),
            page_set_snapshot=chains,
            device_serials=["android-1"],
            status="RUNNING",
        )
        self.session.add(run)
        self.session.flush()
        cell = CompatibilityCell(
            run_id=run.id,
            device_serial="android-1",
            status="RUNNING",
        )
        self.session.add(cell)
        self.session.commit()
        self.session.refresh(run)
        self.session.refresh(cell)
        return run, cell, chains

    def test_result_persists_target_boundary_evidence_and_display_metadata(self):
        snapshot = self._snapshot()
        run, cell, chains = self._seed_replay_run(snapshot)
        chain = {
            **chains[0],
            "replay_scope": "PREFIX_TO_SAFETY_BOUNDARY",
            "terminal_outcome": "SAFETY_BLOCKED",
            "boundary_evidence": "VERIFIED",
        }

        row = _record_replay_result(
            self.session,
            run=run,
            cell=cell,
            chain=chain,
            result={
                "status": "WARNING",
                "reason": "安全边界发生变化",
                "failure_type": "SAFETY_BOUNDARY_CHANGED",
                "completed_checkpoints": 1,
                "warning_codes": ["SAFETY_BOUNDARY_CHANGED"],
                "boundary_evidence": "CHANGED",
                "trace": [
                    {
                        "step_index": 0,
                        "status": "BOUNDARY_CHANGED",
                        "boundary_evidence": "CHANGED",
                        "failure_type": "SAFETY_BOUNDARY_CHANGED",
                        "reason": "风险分类已变化",
                        "action_role": "PAYMENT:SUBMIT",
                    }
                ],
            },
        )

        self.assertEqual(row.page_name, "P001 · HOME")
        self.assertEqual(row.metrics["display_label"], "P001")
        self.assertEqual(row.metrics["source_observation_index"], 1)
        self.assertEqual(row.metrics["source_boundary_evidence"], "VERIFIED")
        self.assertEqual(row.metrics["replay_boundary_evidence"], "CHANGED")
        self.assertEqual(row.metrics["boundary_evidence"], "CHANGED")
        self.assertEqual(
            row.metrics["boundary_results"][0]["status"],
            "BOUNDARY_CHANGED",
        )

    async def test_preflight_allows_same_version_with_warning(self):
        snapshot = self._snapshot()
        request = CompatibilityReplayPreflightRequest(
            inspection_run_id=self.source.id,
            branch_key="authenticated",
            device_serial="android-1",
        )
        with patch(
            "backend.api.compatibility.read_installed_package",
            new=AsyncMock(return_value=snapshot),
        ), patch(
            "backend.api.compatibility.build_replay_plan",
            return_value=self._plan(),
        ):
            result = await replay_preflight(
                request,
                session=self.session,
                current_user=self.user,
            )
        self.assertEqual(result.execution_mode, "installed_replay")
        self.assertFalse(result.blockers)
        self.assertIn("SAME_VERSION_REPLAY", {item.code for item in result.warnings})
        self.assertEqual(result.device_snapshot_digest, snapshot["snapshot_digest"])
        self.assertEqual(result.chains[0].chain_id, "chain-home")

    def test_create_installed_replay_freezes_plan_without_apk(self):
        snapshot = self._snapshot()
        payload = CompatibilityRunCreate(
            name="installed replay",
            execution_mode="installed_replay",
            inspection_run_id=self.source.id,
            replay_branch_key="authenticated",
            selected_chain_ids=["chain-home"],
            plan_digest="plan-digest",
            device_snapshot_digest=snapshot["snapshot_digest"],
            manual_install_confirmed=True,
            duration_seconds=300,
            device_serials=["android-1"],
        )
        tasks = BackgroundTasks()
        with patch(
            "backend.api.compatibility.read_installed_package_sync",
            return_value=snapshot,
        ), patch(
            "backend.api.compatibility.build_replay_plan",
            return_value=self._plan(),
        ), patch(
            "backend.api.compatibility.ensure_asset_capacity_for_new_run",
            return_value={"can_start": True},
        ):
            result = create_run(
                payload,
                tasks,
                session=self.session,
                current_user=self.user,
            )
        self.assertEqual(result.execution_mode, "installed_replay")
        self.assertIsNone(result.new_package_id)
        self.assertIsNone(result.compare_mode)
        self.assertEqual(result.duration_seconds, 300)
        self.assertEqual(result.replay_plan_digest, "plan-digest")
        self.assertEqual(result.page_set_snapshot[0].chain_id, "chain-home")
        run = self.session.get(CompatibilityRun, result.id)
        self.assertIsNone(run.new_package_id)
        self.assertEqual(run.replay_duration_seconds, 300)
        self.assertEqual(run.target_package_snapshot["snapshot_digest"], snapshot["snapshot_digest"])
        cell = self.session.exec(select(CompatibilityCell)).one()
        self.assertEqual(cell.new_install_status, "SKIPPED")
        self.assertEqual(len(tasks.tasks), 1)

    async def test_replay_worker_never_installs_prepares_or_diffs(self):
        snapshot = self._snapshot()
        payload = CompatibilityRunCreate(
            name="installed replay worker",
            execution_mode="installed_replay",
            inspection_run_id=self.source.id,
            replay_branch_key="authenticated",
            selected_chain_ids=["chain-home"],
            plan_digest="plan-digest",
            device_snapshot_digest=snapshot["snapshot_digest"],
            manual_install_confirmed=True,
            duration_seconds=300,
            device_serials=["android-1"],
        )
        with patch(
            "backend.api.compatibility.read_installed_package_sync",
            return_value=snapshot,
        ), patch(
            "backend.api.compatibility.build_replay_plan",
            return_value=self._plan(),
        ), patch(
            "backend.api.compatibility.ensure_asset_capacity_for_new_run",
            return_value={"can_start": True},
        ):
            create_run(
                payload,
                BackgroundTasks(),
                session=self.session,
                current_user=self.user,
            )
        run = self.session.exec(select(CompatibilityRun)).one()
        cell = self.session.exec(select(CompatibilityCell)).one()

        class FakeResult:
            last_capture = None

            def to_dict(self):
                return {
                    "status": "PASS",
                    "reason": None,
                    "failure_type": None,
                    "trace": [],
                    "completed_checkpoints": 1,
                    "warning_codes": [],
                }

        with patch(
            "backend.api.compatibility.read_installed_package",
            new=AsyncMock(return_value=snapshot),
        ), patch(
            "backend.api.compatibility._run_case_for_capture",
            return_value={"success": True},
        ), patch(
            "backend.api.compatibility.connect_android",
            return_value=object(),
        ), patch(
            "backend.api.compatibility.ready_assertion_exists",
            return_value=True,
        ), patch(
            "backend.api.compatibility.execute_replay_chain",
            return_value=FakeResult(),
        ), patch(
            "backend.api.compatibility._capture_logcat_errors",
            new=AsyncMock(return_value=""),
        ), patch(
            "backend.api.compatibility._environment_secret_values",
            return_value=[],
        ), patch(
            "backend.api.compatibility.install_app_package_to_device",
        ) as install_mock, patch(
            "backend.api.compatibility._prepare_branch",
        ) as prepare_mock, patch(
            "backend.api.compatibility.compare_page_snapshots",
        ) as diff_mock:
            await _execute_cell_installed_replay_body(
                self.session,
                run,
                cell,
                list(run.page_set_snapshot or []),
                threading.Event(),
                int(run.id),
            )
        install_mock.assert_not_called()
        prepare_mock.assert_not_called()
        diff_mock.assert_not_called()
        self.session.refresh(cell)
        self.assertIn(cell.status, {"PASS", "WARNING"})

    def test_select_and_freeze_chains_accepts_one_shot_iterators(self):
        plan = self._plan()
        plan["chains"][0]["prefix_path_key"] = "prefix-home"
        frozen = select_and_freeze_chains(
            plan,
            iter(["chain-home"]),
            source_run=self.source,
            branch_key="authenticated",
        )
        self.assertEqual([item["chain_id"] for item in frozen], ["chain-home"])
        legacy = select_and_freeze_chains(
            plan,
            iter(["path-home"]),
            source_run=self.source,
            branch_key="authenticated",
        )
        self.assertEqual([item["chain_id"] for item in legacy], ["chain-home"])
        prefix = select_and_freeze_chains(
            plan,
            iter(["prefix-home"]),
            source_run=self.source,
            branch_key="authenticated",
        )
        self.assertEqual([item["chain_id"] for item in prefix], ["chain-home"])
        aliases = select_and_freeze_chains(
            plan,
            ["chain-home", "path-home"],
            source_run=self.source,
            branch_key="authenticated",
        )
        self.assertEqual([item["chain_id"] for item in aliases], ["chain-home"])

    def test_entry_case_safety_limits_package_operations_to_target_app(self):
        session = Mock()
        session.get.return_value = Mock(
            steps=[
                {"action": "stop_app", "selector": "com.demo.app"},
                {"action": "start_app", "options": {"app_key": "com.demo.app"}},
                {"action": "wait_until_exists", "selector": "Home"},
            ]
        )
        self.assertEqual(
            entry_case_safety_issues(
                session,
                {"entry_case_id": 1},
                package_name="com.demo.app",
            ),
            [],
        )

        session.get.return_value = Mock(
            steps=[{"action": "start_app", "selector": "com.external.app"}]
        )
        issues = entry_case_safety_issues(
            session,
            {"entry_case_id": 1},
            package_name="com.demo.app",
        )
        self.assertEqual([item["code"] for item in issues], ["UNSAFE_ENTRY_CASE"])

    def test_entry_case_safety_accepts_pydantic_action_enums(self):
        session = Mock()
        session.get.return_value = Mock(
            steps=[
                Step(
                    action=ActionType.STOP_APP,
                    selector="com.demo.app",
                    selector_type="text",
                ),
                Step(
                    action=ActionType.START_APP,
                    selector="com.demo.app",
                    selector_type="text",
                ),
                Step(action=ActionType.SLEEP, value="1"),
                Step(
                    action=ActionType.WAIT_UNTIL_EXISTS,
                    selector="首页",
                    selector_type="text",
                ),
            ]
        )
        self.assertEqual(
            entry_case_safety_issues(
                session,
                {"entry_case_id": 1},
                package_name="com.demo.app",
            ),
            [],
        )

    def test_entry_case_safety_blocks_unknown_and_english_risk_actions(self):
        session = Mock()
        session.get.return_value = Mock(
            steps=[
                {"action": "shell", "selector": "echo harmless"},
                {
                    "action": "click",
                    "selector": "submit",
                    "options": {"description": "Place order"},
                },
            ]
        )
        issues = entry_case_safety_issues(
            session,
            {"entry_case_id": 1},
            package_name="com.demo.app",
        )
        self.assertEqual(len(issues), 2)
        self.assertTrue(all(item["code"] == "UNSAFE_ENTRY_CASE" for item in issues))

    async def test_adb_helpers_quote_device_serial(self):
        commands = []

        async def fake_adb(command, timeout=120):
            commands.append(command)
            return ""

        with patch("backend.api.compatibility._run_adb_command", new=AsyncMock(side_effect=fake_adb)):
            malicious = "device; touch /tmp/compatibility-should-not-run"
            await _capture_logcat_errors(malicious, "com.demo.app")
            await _clear_logcat(malicious)
            await _capture_activity(malicious)

        self.assertEqual(len(commands), 3)
        for command in commands:
            self.assertIn("'device; touch /tmp/compatibility-should-not-run'", command)
        self.assertIn("'*:E'", commands[0])

    async def test_replay_clears_logcat_before_each_chain(self):
        snapshot = self._snapshot()
        payload = CompatibilityRunCreate(
            name="installed replay logcat",
            execution_mode="installed_replay",
            inspection_run_id=self.source.id,
            replay_branch_key="authenticated",
            selected_chain_ids=["chain-home"],
            plan_digest="plan-digest",
            device_snapshot_digest=snapshot["snapshot_digest"],
            manual_install_confirmed=True,
            duration_seconds=300,
            device_serials=["android-1"],
        )
        with patch(
            "backend.api.compatibility.read_installed_package_sync",
            return_value=snapshot,
        ), patch(
            "backend.api.compatibility.build_replay_plan",
            return_value=self._plan(),
        ), patch(
            "backend.api.compatibility.ensure_asset_capacity_for_new_run",
            return_value={"can_start": True},
        ):
            create_run(
                payload,
                BackgroundTasks(),
                session=self.session,
                current_user=self.user,
            )
        run = self.session.exec(select(CompatibilityRun)).one()
        cell = self.session.exec(select(CompatibilityCell)).one()

        class FakeResult:
            last_capture = None

            def to_dict(self):
                return {
                    "status": "PASS",
                    "reason": None,
                    "failure_type": None,
                    "trace": [],
                    "completed_checkpoints": 1,
                    "warning_codes": [],
                }

        with patch(
            "backend.api.compatibility.read_installed_package",
            new=AsyncMock(return_value=snapshot),
        ), patch(
            "backend.api.compatibility._clear_logcat",
            new=AsyncMock(),
        ) as clear_logcat, patch(
            "backend.api.compatibility._run_case_for_capture",
            return_value={"success": True},
        ), patch(
            "backend.api.compatibility.connect_android",
            return_value=object(),
        ), patch(
            "backend.api.compatibility.ready_assertion_exists",
            return_value=True,
        ), patch(
            "backend.api.compatibility.execute_replay_chain",
            return_value=FakeResult(),
        ), patch(
            "backend.api.compatibility._capture_logcat_errors",
            new=AsyncMock(return_value=""),
        ), patch(
            "backend.api.compatibility._environment_secret_values",
            return_value=[],
        ):
            await _execute_cell_installed_replay_body(
                self.session,
                run,
                cell,
                [
                    *list(run.page_set_snapshot or []),
                    {
                        **dict((run.page_set_snapshot or [])[0]),
                        "chain_id": "chain-home-second",
                        "path_key": "path-home-second",
                    },
                ],
                threading.Event(),
                int(run.id),
            )
        self.assertEqual(clear_logcat.await_count, 2)
        clear_logcat.assert_any_await("android-1")

    async def test_cancel_during_replay_preserves_partial_trace(self):
        snapshot = self._snapshot()
        run, cell, chains = self._seed_replay_run(snapshot, chain_count=2)
        abort_event = threading.Event()

        class FakeResult:
            last_capture = None

            def to_dict(self):
                return {
                    "status": "ABORTED",
                    "reason": "replay cancelled",
                    "failure_type": "CANCELLED",
                    "trace": [
                        {
                            "step_index": 0,
                            "action_type": "click",
                            "action_role": "NAV:HOME",
                            "status": "ABORTED",
                        }
                    ],
                    "completed_checkpoints": 1,
                    "failed_step_index": 0,
                    "warning_codes": [],
                }

        def cancel_from_replay(*args, **kwargs):
            abort_event.set()
            return FakeResult()

        with patch(
            "backend.api.compatibility.read_installed_package",
            new=AsyncMock(return_value=snapshot),
        ), patch(
            "backend.api.compatibility._clear_logcat",
            new=AsyncMock(),
        ), patch(
            "backend.api.compatibility._run_case_for_capture",
            return_value={"success": True},
        ), patch(
            "backend.api.compatibility.connect_android",
            return_value=object(),
        ), patch(
            "backend.api.compatibility.ready_assertion_exists",
            return_value=True,
        ), patch(
            "backend.api.compatibility.execute_replay_chain",
            side_effect=cancel_from_replay,
        ), patch(
            "backend.api.compatibility._environment_secret_values",
            return_value=[],
        ):
            with self.assertRaises(asyncio.CancelledError):
                await _execute_cell_installed_replay_body(
                    self.session,
                    run,
                    cell,
                    chains,
                    abort_event,
                    int(run.id),
                )

        rows = self.session.exec(
            select(CompatibilityPageResult).where(
                CompatibilityPageResult.cell_id == cell.id
            )
        ).all()
        by_key = {row.page_key: row for row in rows}
        self.assertEqual(by_key["chain-home"].status, "CANCELLED")
        self.assertEqual(by_key["chain-home"].failed_step_index, 0)
        self.assertEqual(by_key["chain-home"].replay_trace[0]["step_index"], 0)
        self.assertEqual(by_key["chain-home-2"].status, "CANCELLED")
        self.assertEqual(by_key["chain-home-2"].replay_trace, [])

    async def test_capture_persistence_failure_keeps_results_and_continues(self):
        snapshot = self._snapshot()
        run, cell, chains = self._seed_replay_run(snapshot, chain_count=2)

        class FakeResult:
            last_capture = object()

            def to_dict(self):
                return {
                    "status": "PASS",
                    "reason": None,
                    "failure_type": None,
                    "trace": [{"step_index": 0, "status": "PASS"}],
                    "completed_checkpoints": 1,
                    "warning_codes": [],
                }

        with patch(
            "backend.api.compatibility.read_installed_package",
            new=AsyncMock(return_value=snapshot),
        ), patch(
            "backend.api.compatibility._clear_logcat",
            new=AsyncMock(),
        ), patch(
            "backend.api.compatibility._run_case_for_capture",
            return_value={"success": True},
        ), patch(
            "backend.api.compatibility.connect_android",
            return_value=object(),
        ), patch(
            "backend.api.compatibility.ready_assertion_exists",
            return_value=True,
        ), patch(
            "backend.api.compatibility.execute_replay_chain",
            return_value=FakeResult(),
        ) as execute, patch(
            "backend.api.compatibility._persist_replay_capture",
            side_effect=OSError("disk full"),
        ), patch(
            "backend.api.compatibility._capture_logcat_errors",
            new=AsyncMock(return_value=""),
        ), patch(
            "backend.api.compatibility._environment_secret_values",
            return_value=[],
        ):
            await _execute_cell_installed_replay_body(
                self.session,
                run,
                cell,
                chains,
                threading.Event(),
                int(run.id),
            )

        rows = self.session.exec(
            select(CompatibilityPageResult)
            .where(CompatibilityPageResult.cell_id == cell.id)
            .order_by(CompatibilityPageResult.id)
        ).all()
        self.assertEqual(execute.call_count, 2)
        self.assertEqual(len(rows), 2)
        self.assertEqual({row.status for row in rows}, {"WARNING"})
        self.assertEqual(
            {row.failure_type for row in rows},
            {"ASSET_PERSIST_FAILED"},
        )
        self.assertTrue(
            all("ASSET_PERSIST_FAILED" in row.metrics["warning_codes"] for row in rows)
        )
        self.assertTrue(all(row.candidate_screenshot_path is None for row in rows))
        self.assertTrue(all(row.replay_trace for row in rows))

    async def test_budget_limit_preserves_current_trace_only(self):
        snapshot = self._snapshot()
        run, cell, chains = self._seed_replay_run(snapshot, chain_count=2)
        real_monotonic = time.monotonic
        replay_returned = False

        class FakeResult:
            last_capture = None

            def to_dict(self):
                return {
                    "status": "ABORTED",
                    "reason": "replay time budget reached",
                    "failure_type": "CANCELLED",
                    "trace": [
                        {
                            "step_index": 0,
                            "action_role": "NAV:HOME",
                            "status": "ABORTED",
                        }
                    ],
                    "completed_checkpoints": 1,
                    "failed_step_index": 0,
                    "warning_codes": [],
                }

        def finish_at_deadline(*args, **kwargs):
            nonlocal replay_returned
            replay_returned = True
            return FakeResult()

        def replay_clock():
            if replay_returned and any(
                frame.function == "_execute_cell_installed_replay_body"
                for frame in inspect.stack(context=0)
            ):
                return real_monotonic() + 301
            return real_monotonic()

        with patch(
            "backend.api.compatibility.read_installed_package",
            new=AsyncMock(return_value=snapshot),
        ), patch(
            "backend.api.compatibility._clear_logcat",
            new=AsyncMock(),
        ), patch(
            "backend.api.compatibility._run_case_for_capture",
            return_value={"success": True},
        ), patch(
            "backend.api.compatibility.connect_android",
            return_value=object(),
        ), patch(
            "backend.api.compatibility.ready_assertion_exists",
            return_value=True,
        ), patch(
            "backend.api.compatibility.execute_replay_chain",
            side_effect=finish_at_deadline,
        ), patch(
            "backend.api.compatibility._capture_logcat_errors",
            new=AsyncMock(return_value=""),
        ), patch(
            "backend.api.compatibility._environment_secret_values",
            return_value=[],
        ), patch(
            "backend.api.compatibility.time.monotonic",
            side_effect=replay_clock,
        ):
            await _execute_cell_installed_replay_body(
                self.session,
                run,
                cell,
                chains,
                threading.Event(),
                int(run.id),
            )

        rows = self.session.exec(
            select(CompatibilityPageResult).where(
                CompatibilityPageResult.cell_id == cell.id
            )
        ).all()
        by_key = {row.page_key: row for row in rows}
        self.assertEqual(by_key["chain-home"].failure_type, "BUDGET_LIMIT")
        self.assertEqual(by_key["chain-home"].failed_step_index, 0)
        self.assertEqual(by_key["chain-home"].replay_trace[0]["step_index"], 0)
        self.assertEqual(
            by_key["chain-home-2"].failure_type,
            "BUDGET_NOT_REACHED",
        )
        self.assertEqual(by_key["chain-home-2"].replay_trace, [])

    async def test_cancelled_entry_case_persists_all_chain_results(self):
        snapshot = self._snapshot()
        run, cell, chains = self._seed_replay_run(snapshot, chain_count=2)
        abort_event = threading.Event()

        def cancel_from_entry(*args, **kwargs):
            abort_event.set()
            raise InspectionAborted("entry cancelled")

        with patch(
            "backend.api.compatibility.read_installed_package",
            new=AsyncMock(return_value=snapshot),
        ), patch(
            "backend.api.compatibility._clear_logcat",
            new=AsyncMock(),
        ), patch(
            "backend.api.compatibility._run_case_for_capture",
            side_effect=cancel_from_entry,
        ), patch(
            "backend.api.compatibility._environment_secret_values",
            return_value=[],
        ):
            with self.assertRaises(asyncio.CancelledError):
                await _execute_cell_installed_replay_body(
                    self.session,
                    run,
                    cell,
                    chains,
                    abort_event,
                    int(run.id),
                )

        rows = self.session.exec(
            select(CompatibilityPageResult).where(
                CompatibilityPageResult.cell_id == cell.id
            )
        ).all()
        self.assertEqual(len(rows), 2)
        self.assertEqual({row.status for row in rows}, {"CANCELLED"})

    async def test_entry_logical_step_guard_stops_at_deadline(self):
        snapshot = self._snapshot()
        run, cell, chains = self._seed_replay_run(snapshot)
        real_monotonic = time.monotonic

        def run_entry(*args, **kwargs):
            # Fifth/sixth optional callbacks are the real-device and logical
            # step guards. The logical hook includes sleep steps.
            self.assertTrue(callable(args[4]))
            self.assertTrue(callable(args[5]))
            args[5]("sleep")
            raise AssertionError("deadline guard should have interrupted entry")

        def monotonic_with_expired_entry_deadline():
            # Patch only calls made from the installed replay's guard.  The
            # asyncio test loop also uses time.monotonic and must retain the
            # real clock.
            if any(frame.function == "guard_device_step" for frame in inspect.stack(context=0)):
                return real_monotonic() + 301
            return real_monotonic()

        with patch(
            "backend.api.compatibility.read_installed_package",
            new=AsyncMock(return_value=snapshot),
        ), patch(
            "backend.api.compatibility._clear_logcat",
            new=AsyncMock(),
        ), patch(
            "backend.api.compatibility._run_case_for_capture",
            side_effect=run_entry,
        ), patch(
            "backend.api.compatibility._environment_secret_values",
            return_value=[],
        ), patch(
            "backend.api.compatibility.time.monotonic",
            side_effect=monotonic_with_expired_entry_deadline,
        ):
            await _execute_cell_installed_replay_body(
                self.session,
                run,
                cell,
                chains,
                threading.Event(),
                int(run.id),
            )

        row = self.session.exec(
            select(CompatibilityPageResult).where(
                CompatibilityPageResult.cell_id == cell.id
            )
        ).one()
        self.assertEqual(row.status, "WARNING")
        self.assertEqual(row.failure_type, "BUDGET_NOT_REACHED")

    async def test_run_unwind_error_does_not_override_aborted_status(self):
        snapshot = self._snapshot()
        run, _cell, chains = self._seed_replay_run(snapshot)
        run.status = "ABORTED"
        self.session.add(run)
        self.session.commit()

        with patch("backend.api.compatibility.engine", self.engine), patch(
            "backend.api.compatibility._execute_cell",
            new=AsyncMock(side_effect=RuntimeError("lease unwind failed")),
        ):
            await _execute_run_async(int(run.id), chains)

        self.session.expire_all()
        persisted = self.session.get(CompatibilityRun, run.id)
        self.assertEqual(persisted.status, "ABORTED")
        self.assertIsNone(persisted.error_message)


if __name__ == "__main__":
    unittest.main()
