import io
import threading
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock, patch

from PIL import Image
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, select

from backend.inspection.device import CapturedPage
from backend.inspection.engine import (
    BudgetExceeded,
    BudgetGuard,
    ExplorationBudgetExceeded,
    ExplorationBudgetView,
    NavigationEntry,
    PathDiverged,
    StateWork,
    _persist_state,
    _persist_fault_assets,
    _replay_path,
    _replay_model_expectation,
    _restore_parent_after_transition,
    _run_case,
    _serialize_action,
    _state_actions,
)
from backend.inspection.sanitizer import InspectionArtifactSanitizer
from backend.inspection.semantics import (
    InspectionAction,
    build_page_model,
    compare_page_models,
)
from backend.models import (
    AssetReference,
    InspectionBranchRun,
    InspectionFault,
    InspectionObservation,
    InspectionRun,
    InspectionState,
    SystemSetting,
    TestCase,
)


def _png(color: str) -> bytes:
    output = io.BytesIO()
    Image.new("RGB", (100, 100), color).save(output, format="PNG")
    return output.getvalue()


def _capture(
    xml: str,
    *,
    color: str = "white",
    phash: str = "0" * 16,
    activity: str = ".ProductDetailActivity",
) -> CapturedPage:
    png = _png(color)
    model = build_page_model(
        xml,
        package_name="com.demo",
        activity=activity,
        screenshot_phash=phash,
    )
    return CapturedPage(
        package_name="com.demo",
        activity=activity,
        xml=xml,
        screenshot_png=png,
        screenshot_sha=f"sha-{color}",
        perceptual_hash=phash,
        model=model,
        stable_by="exact",
    )


def _product_xml(extra_nodes: int = 0, *, checked: bool = False) -> str:
    repeated = "".join(
        '<node package="com.demo" class="android.widget.TextView" '
        f'bounds="[0,{100 + index * 10}][90,{108 + index * 10}]" '
        'enabled="true"/>'
        for index in range(100 + extra_nodes)
    )
    return (
        "<hierarchy>"
        '<node package="com.demo" class="android.widget.FrameLayout" '
        'bounds="[0,0][100,100]" enabled="true">'
        '<node package="com.demo" class="android.widget.Button" '
        'text="立即购买" clickable="true" enabled="true" '
        f'checked="{"true" if checked else "false"}" '
        'bounds="[0,0][50,50]"/>'
        f"{repeated}"
        "</node></hierarchy>"
    )


def _product_depth_xml(extra_depth: int = 0) -> str:
    nested = (
        '<node package="com.demo" class="android.widget.TextView" '
        'bounds="[0,50][90,90]" enabled="true"/>'
    )
    for _ in range(100 + extra_depth):
        nested = (
            '<node package="com.demo" class="android.widget.FrameLayout" '
            'bounds="[0,50][100,100]" enabled="true">'
            f"{nested}</node>"
        )
    return (
        "<hierarchy>"
        '<node package="com.demo" class="android.widget.FrameLayout" '
        'bounds="[0,0][100,100]" enabled="true">'
        '<node package="com.demo" class="android.widget.TextView" '
        'text="商品 Alpha" bounds="[0,0][90,20]" enabled="true"/>'
        '<node package="com.demo" class="android.widget.Button" '
        'text="立即购买" clickable="true" enabled="true" '
        'bounds="[0,0][50,50]"/>'
        f"{nested}</node></hierarchy>"
    )


class InspectionBudgetGuardTests(unittest.TestCase):
    def test_default_artifact_budget_reserves_sixty_four_mib_for_faults(self):
        guard = BudgetGuard()
        self.assertEqual(guard.max_artifact_bytes, 448 * 1024 * 1024)
        self.assertEqual(guard.max_fault_artifact_bytes, 64 * 1024 * 1024)

    def test_aliases_and_all_budget_dimensions_stop_before_overshoot(self):
        guard = BudgetGuard(
            {
                "duration_seconds": 30,
                "max_actions": 99,
                "max_device_actions": 1,
                "max_states": 1,
                "max_observations": 1,
                "max_artifact_bytes": 4,
                "no_new_coverage_limit": 2,
            }
        )
        guard.before_device_interaction("first", mutating=True)
        with self.assertRaisesRegex(BudgetExceeded, "动作预算"):
            guard.before_device_interaction("second", mutating=True)

        guard.reserve_persistence(
            new_state=True,
            observation=True,
            artifact_bytes=4,
        )
        with self.assertRaisesRegex(BudgetExceeded, "状态预算"):
            guard.reserve_persistence(
                new_state=True,
                observation=False,
            )
        with self.assertRaisesRegex(BudgetExceeded, "采集预算"):
            guard.reserve_observation(0)

        guard.record_coverage(discovered=False)
        with self.assertRaisesRegex(BudgetExceeded, "连续动作无新状态"):
            guard.record_coverage(discovered=False)

    def test_zero_scroll_budget_enumerates_no_scroll_action(self):
        capture = _capture(
            "<hierarchy>"
            '<node package="com.demo" class="android.widget.ScrollView" '
            'scrollable="true" enabled="true" bounds="[0,0][100,100]"/>'
            "</hierarchy>",
            activity=".ListActivity",
        )
        actions = _state_actions(
            capture,
            screen_size=(100, 100),
            safety_rules=[],
            input_rules=[],
            max_scrolls=0,
            depth=0,
        )
        self.assertNotIn("scroll", [item.action_type for item in actions])

    def test_recovery_action_consumes_device_budget_before_invocation(self):
        source = _capture(_product_xml())
        target = _capture(_product_xml(1), color="black")
        action = InspectionAction(
            action_type="click",
            action_key="return-source",
            locator_candidates=[{"by": "description", "selector": "返回"}],
            target_meta={},
        )
        parent = StateWork(
            state_id=1,
            state_key=source.model.state_key,
            cluster_key=source.model.cluster_key,
            replay_key=source.model.replay_key,
            package_name=source.package_name,
            activity=source.activity,
            screenshot_sha=source.screenshot_sha,
            depth=0,
            path=[],
            actions=[],
            semantic_key=source.model.semantic_key,
        )
        entry = NavigationEntry(
            group_key="tabs",
            state_id=1,
            action=action,
            target_path=(),
        )
        performer = Mock()
        with patch("backend.inspection.engine.perform_action", performer):
            with self.assertRaisesRegex(BudgetExceeded, "动作预算"):
                _restore_parent_after_transition(
                    device=Mock(),
                    parent=parent,
                    target_capture=target,
                    relation_type="PEER",
                    navigation_group_key="tabs",
                    navigation_entries=[entry],
                    branch_config={},
                    device_serial="device-1",
                    package_name="com.demo",
                    abort_event=threading.Event(),
                    input_rules=[],
                    dynamic_patterns=[],
                    stable_wait_seconds=1.0,
                    secret_values=[],
                    budget_guard=BudgetGuard({"max_device_actions": 0}),
                )
        performer.assert_not_called()

    def test_branch_views_share_fair_quota_and_release_unused_capacity(self):
        guard = BudgetGuard(
            {
                "max_states": 6,
                "max_device_actions": 6,
                "max_observations": 6,
                "max_artifact_bytes": 6000,
                "fault_artifact_bytes": 0,
            }
        )
        first = guard.for_branch(2)
        self.assertEqual(first.max_states, 3)
        self.assertLess(first.deadline, guard.deadline)
        first.reserve_persistence(
            new_state=True,
            observation=True,
            artifact_bytes=100,
        )
        first.before_device_interaction("first", mutating=True)

        second = guard.for_branch(1)
        self.assertEqual(second.max_states, 5)
        self.assertEqual(second.max_device_actions, 5)
        for _ in range(5):
            second.reserve_persistence(
                new_state=True,
                observation=True,
                artifact_bytes=100,
            )
            second.before_device_interaction("later", mutating=True)
        with self.assertRaisesRegex(BudgetExceeded, "状态预算"):
            second.reserve_persistence(
                new_state=True,
                observation=False,
            )

    def test_entry_case_charges_each_runner_step_without_case_surcharge(self):
        test_engine = create_engine(
            "sqlite://",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        SQLModel.metadata.create_all(test_engine)
        self.addCleanup(test_engine.dispose)
        with Session(test_engine) as session:
            case = TestCase(name="entry")
            session.add(case)
            session.commit()
            session.refresh(case)
            case_id = int(case.id)

        observed_actions = []

        def run_two_steps(**kwargs):
            callback = kwargs["before_device_step"]
            callback("click")
            observed_actions.append("click")
            callback("back")
            observed_actions.append("back")
            return {"success": True}

        guard = BudgetGuard({"max_device_actions": 1})
        with patch(
            "backend.inspection.engine.engine",
            test_engine,
        ), patch(
            "backend.inspection.engine.run_case_with_standard_runner",
            side_effect=run_two_steps,
        ):
            with self.assertRaisesRegex(BudgetExceeded, "动作预算"):
                _run_case(
                    case_id=case_id,
                    device_serial="device-1",
                    env_id=None,
                    abort_event=threading.Event(),
                    budget_guard=guard,
                )

        self.assertEqual(observed_actions, ["click"])
        self.assertEqual(guard.snapshot()["device_actions"], 1)

        empty_guard = BudgetGuard({"max_device_actions": 0})
        with patch(
            "backend.inspection.engine.engine",
            test_engine,
        ), patch(
            "backend.inspection.engine.run_case_with_standard_runner",
            return_value={"success": True},
        ):
            self.assertTrue(
                _run_case(
                    case_id=case_id,
                    device_serial="device-1",
                    env_id=None,
                    abort_event=threading.Event(),
                    budget_guard=empty_guard,
                )
            )
        self.assertEqual(empty_guard.snapshot()["device_actions"], 0)

    def test_exploration_share_stops_internal_actions_without_spending_reserve(self):
        task_guard = BudgetGuard(
            {
                "duration_seconds": 60,
                "max_device_actions": 10,
            }
        )
        branch_guard = task_guard.for_branch(1)
        exploration = ExplorationBudgetView(
            branch_guard,
            deadline=branch_guard.deadline - 1,
            max_device_actions=9,
        )

        for index in range(9):
            exploration.before_device_interaction(
                f"internal-recovery-{index}",
                mutating=True,
            )
        with self.assertRaises(ExplorationBudgetExceeded):
            exploration.before_device_interaction(
                "internal-recovery-overflow",
                mutating=True,
            )

        self.assertEqual(exploration.device_actions, 9)
        self.assertEqual(task_guard.snapshot()["device_actions"], 9)
        branch_guard.before_device_interaction("reserved-verification", mutating=True)
        self.assertEqual(task_guard.snapshot()["device_actions"], 10)


class InspectionReplayExpectationTests(unittest.TestCase):
    def setUp(self):
        self.source = _capture(_product_xml())
        self.target = _capture(_product_xml(1), color="blue")
        self.action = InspectionAction(
            action_type="click",
            action_key="next",
            locator_candidates=[{"by": "description", "selector": "下一页"}],
            target_meta={},
        )

    def _replay(self, payload, captures, performer):
        with patch(
            "backend.inspection.engine._try_run_case",
            return_value=True,
        ), patch(
            "backend.inspection.engine.ready_assertion_exists",
            return_value=True,
        ), patch(
            "backend.inspection.engine.wait_for_stable_page",
            side_effect=captures,
        ), patch(
            "backend.inspection.engine.perform_action",
            performer,
        ):
            return _replay_path(
                device=Mock(),
                path=[payload],
                branch_config={},
                device_serial="device-1",
                package_name="com.demo",
                abort_event=threading.Event(),
                input_rules=[],
                dynamic_patterns=[],
                stable_wait_seconds=1.0,
                secret_values=[],
            )

    def test_source_divergence_stops_before_action(self):
        performer = Mock()
        payload = _serialize_action(
            self.action,
            expected_source_semantic_key="unexpected-source",
        )
        with self.assertRaisesRegex(PathDiverged, "PATH_DIVERGED"):
            self._replay(payload, [self.source, self.source], performer)
        performer.assert_not_called()

    def test_transient_source_divergence_is_cleared_by_second_stable_sample(self):
        changed = _capture(_product_xml(checked=True), color="red")
        performer = Mock(return_value="description")
        payload = _serialize_action(
            self.action,
            expected_source_semantic_key=self.source.model.semantic_key,
            expected_target_semantic_key=changed.model.semantic_key,
        )

        capture, unique = self._replay(
            payload,
            [changed, self.source, changed],
            performer,
        )

        self.assertIs(capture, changed)
        self.assertTrue(unique)
        performer.assert_called_once()

    def test_dynamic_semantic_change_accepts_conservative_replay_signature(self):
        stable_xml = _product_xml().replace(
            "</node></hierarchy>",
            '<node package="com.demo" class="android.widget.TextView" '
            'text="旗舰机型" enabled="true" bounds="[0,1120][90,1160]"/>'
            "</node></hierarchy>",
        )
        expected_source = _capture(stable_xml)
        dynamic_source = _capture(stable_xml)
        dynamic_source.model.semantic_key = "dynamic-semantic-key"
        performer = Mock(return_value="description")
        payload = _serialize_action(
            self.action,
            expected_source_semantic_key=expected_source.model.semantic_key,
            expected_target_semantic_key=expected_source.model.semantic_key,
            expected_source_signature=_replay_model_expectation(
                expected_source.model
            ),
            expected_target_signature=_replay_model_expectation(
                expected_source.model
            ),
        )

        capture, unique = self._replay(
            payload,
            [dynamic_source, expected_source],
            performer,
        )

        self.assertIs(capture, expected_source)
        self.assertTrue(unique)
        performer.assert_called_once()

    def test_control_state_change_still_diverges_with_replay_signature(self):
        changed = _capture(_product_xml(checked=True))
        performer = Mock()
        payload = _serialize_action(
            self.action,
            expected_source_semantic_key=self.source.model.semantic_key,
            expected_source_signature=_replay_model_expectation(self.source.model),
        )

        with self.assertRaisesRegex(PathDiverged, "PATH_DIVERGED"):
            self._replay(payload, [changed, changed], performer)

        performer.assert_not_called()

    def test_target_divergence_is_reported_after_action(self):
        performer = Mock(return_value="description")
        payload = _serialize_action(
            self.action,
            expected_source_semantic_key=self.source.model.semantic_key,
            expected_target_semantic_key="unexpected-target",
        )
        with self.assertRaisesRegex(PathDiverged, "PATH_DIVERGED"):
            self._replay(payload, [self.source, self.target, self.target], performer)
        performer.assert_called_once()

    def test_transient_target_divergence_is_cleared_by_second_stable_sample(self):
        changed = _capture(_product_xml(checked=True), color="red")
        performer = Mock(return_value="description")
        payload = _serialize_action(
            self.action,
            expected_source_semantic_key=self.source.model.semantic_key,
            expected_target_semantic_key=changed.model.semantic_key,
        )

        capture, unique = self._replay(
            payload,
            [self.source, self.source, changed],
            performer,
        )

        self.assertIs(capture, changed)
        self.assertTrue(unique)
        performer.assert_called_once()


class InspectionIdentityPersistenceTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine(
            "sqlite://",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        SQLModel.metadata.create_all(self.engine)
        with Session(self.engine) as session:
            run = InspectionRun(
                name="identity",
                package_name="com.demo",
                device_serial="device-1",
            )
            session.add(run)
            session.flush()
            session.add(
                SystemSetting(
                    key="content_addressed_assets",
                    value="true",
                )
            )
            branches = []
            for key in ("off", "on", "limited"):
                branch = InspectionBranchRun(
                    run_id=run.id,
                    branch_key=key,
                    branch_name=key,
                )
                session.add(branch)
                session.flush()
                branches.append(branch)
            session.commit()
            self.run_id = int(run.id)
            self.branches = {
                item.branch_key: InspectionBranchRun(
                    id=item.id,
                    run_id=run.id,
                    branch_key=item.branch_key,
                    branch_name=item.branch_name,
                )
                for item in branches
            }
        self.directory = TemporaryDirectory()
        self.root = Path(self.directory.name).resolve()
        self.patches = [
            patch("backend.inspection.engine.engine", self.engine),
            patch(
                "backend.inspection.engine._reports_root",
                return_value=(self.root / "reports").resolve(),
            ),
            patch(
                "backend.artifact_store.project_path",
                side_effect=lambda *parts: self.root.joinpath(*parts),
            ),
        ]
        for item in self.patches:
            item.start()

    def tearDown(self):
        for item in reversed(self.patches):
            item.stop()
        self.directory.cleanup()
        self.engine.dispose()

    def _persist(
        self,
        branch_key,
        capture,
        *,
        convergence=False,
        guard=None,
        root=False,
        capture_kind="DISCOVERY",
    ):
        return _persist_state(
            run_id=self.run_id,
            branch_run=self.branches[branch_key],
            capture=capture,
            depth=0,
            parent_state_id=None,
            path=[],
            sanitizer=InspectionArtifactSanitizer(),
            screen_size=(100, 100),
            safety_rules=[],
            input_rules=[],
            max_scrolls=0,
            max_variants=5,
            identity_v2=True,
            similarity_convergence=convergence,
            budget_guard=guard,
            mark_branch_root=root,
            capture_kind=capture_kind,
        )

    def test_fault_assets_are_pinned_until_fault_owner_is_deleted(self):
        fault_dir = self.root / "reports" / "inspection" / str(self.run_id) / "faults" / "1"
        fault_dir.mkdir(parents=True)
        log_path = fault_dir / "device.log"
        log_path.write_text("redacted crash log", encoding="utf-8")
        with Session(self.engine) as session:
            fault = InspectionFault(
                run_id=self.run_id,
                branch_run_id=self.branches["off"].id,
                fault_type="CRASH",
                signature="crash",
                full_log_path=log_path.relative_to(self.root / "reports").as_posix(),
            )
            session.add(fault)
            session.commit()
            session.refresh(fault)
            fault_id = int(fault.id)

        _persist_fault_assets(fault_id)

        with Session(self.engine) as session:
            references = session.exec(
                select(AssetReference).where(
                    AssetReference.owner_type == "inspection_fault",
                    AssetReference.owner_id == fault_id,
                    AssetReference.released_at == None,  # noqa: E711
                )
            ).all()
        self.assertEqual([item.role for item in references], ["full_log"])
        self.assertEqual({item.retention_class for item in references}, {"PINNED"})

    def test_visual_change_reuses_state_and_retains_two_observations(self):
        first_capture = _capture(_product_xml(), color="red", phash="0" * 16)
        second_capture = _capture(_product_xml(), color="blue", phash="f" * 16)
        guard = BudgetGuard(
            {
                "max_states": 5,
                "max_observations": 5,
                "max_artifact_bytes": 10_000_000,
            }
        )
        first = self._persist("off", first_capture, guard=guard, root=True)
        second = self._persist("off", second_capture, guard=guard)

        self.assertEqual(first.work.state_id, second.work.state_id)
        self.assertNotEqual(first.observation_id, second.observation_id)
        self.assertEqual(guard.snapshot()["states"], 1)
        self.assertEqual(guard.snapshot()["observations"], 2)
        with Session(self.engine) as session:
            states = session.exec(
                select(InspectionState).where(
                    InspectionState.branch_run_id == self.branches["off"].id
                )
            ).all()
            observations = session.exec(
                select(InspectionObservation)
                .where(InspectionObservation.state_id == first.work.state_id)
                .order_by(InspectionObservation.sequence)
            ).all()
        self.assertEqual(len(states), 1)
        self.assertEqual(len(observations), 2)
        self.assertEqual(observations[1].capture_kind, "REVISIT")
        self.assertEqual(states[0].screenshot_sha, first_capture.screenshot_sha)
        self.assertEqual(observations[0].xml_asset_id, observations[1].xml_asset_id)
        self.assertNotEqual(
            observations[0].screenshot_asset_id,
            observations[1].screenshot_asset_id,
        )
        for observation in observations:
            self.assertTrue(observation.screenshot_asset_id)
            self.assertTrue(observation.xml_asset_id)
            self.assertTrue(observation.thumbnail_asset_id)
            self.assertTrue(observation.action_map_asset_id)

    def test_exceptional_observations_do_not_consume_three_ordinary_samples(self):
        self._persist(
            "off",
            _capture(_product_xml(), color="red", phash="0" * 16),
            root=True,
        )
        for index, phash in enumerate(("1" * 16, "2" * 16, "3" * 16)):
            self._persist(
                "off",
                _capture(_product_xml(), color="blue", phash=phash),
                capture_kind="CYCLE",
            )
        for color, phash in (("green", "f" * 16), ("yellow", "a" * 16), ("purple", "5" * 16)):
            self._persist(
                "off",
                _capture(_product_xml(), color=color, phash=phash),
            )

        with Session(self.engine) as session:
            observations = session.exec(
                select(InspectionObservation).where(
                    InspectionObservation.branch_run_id == self.branches["off"].id
                )
            ).all()
        cycles = [item for item in observations if item.capture_kind == "CYCLE"]
        ordinary = [item for item in observations if item.capture_kind != "CYCLE"]
        self.assertEqual(len(cycles), 3)
        self.assertTrue(all(not item.metadata_only for item in cycles))
        self.assertEqual(sum(not item.metadata_only for item in ordinary), 3)

    def test_similarity_shadow_only_converges_when_flag_is_enabled(self):
        base = _capture(_product_depth_xml(), color="red")
        variant = _capture(_product_depth_xml(1), color="blue")
        similarity = compare_page_models(base.model, variant.model)
        self.assertTrue(similarity.equivalent)
        self.assertNotEqual(base.model.semantic_key, variant.model.semantic_key)

        off_first = self._persist("off", base, root=True)
        off_variant = self._persist("off", variant, convergence=False)
        self.assertNotEqual(off_first.work.state_id, off_variant.work.state_id)
        self.assertEqual(
            off_variant.match_evidence["match_type"],
            "SIMILARITY_SHADOW",
        )

        on_first = self._persist("on", base, root=True)
        on_variant = self._persist("on", variant, convergence=True)
        self.assertEqual(on_first.work.state_id, on_variant.work.state_id)
        self.assertEqual(
            on_variant.match_evidence["match_type"],
            "SIMILARITY_CONVERGED",
        )
        with Session(self.engine) as session:
            on_states = session.exec(
                select(InspectionState).where(
                    InspectionState.branch_run_id == self.branches["on"].id
                )
            ).all()
            on_observations = session.exec(
                select(InspectionObservation).where(
                    InspectionObservation.branch_run_id == self.branches["on"].id
                )
            ).all()
        self.assertEqual(len(on_states), 1)
        self.assertEqual(len(on_observations), 2)

    def test_redundant_observation_is_metadata_only_and_full_hot_samples_cap_at_three(self):
        captures = [
            _capture(_product_xml(), color="red", phash="0" * 16),
            _capture(_product_xml(), color="blue", phash="0" * 15 + "1"),
            _capture(_product_xml(), color="green", phash="f" * 16),
            _capture(_product_xml(), color="yellow", phash="f" * 8 + "0" * 8),
            _capture(_product_xml(), color="purple", phash="0" * 8 + "f" * 8),
        ]
        for index, capture in enumerate(captures):
            self._persist("off", capture, root=index == 0)

        with Session(self.engine) as session:
            observations = session.exec(
                select(InspectionObservation)
                .where(
                    InspectionObservation.branch_run_id
                    == self.branches["off"].id
                )
                .order_by(InspectionObservation.sequence)
            ).all()
        self.assertEqual(len(observations), 5)
        self.assertTrue(observations[1].metadata_only)
        self.assertIsNone(observations[1].screenshot_asset_id)
        self.assertLessEqual(
            sum(not item.metadata_only for item in observations),
            3,
        )

    def test_state_budget_is_checked_before_database_persistence(self):
        capture = _capture(_product_xml())
        guard = BudgetGuard({"max_states": 0, "max_observations": 5})
        with self.assertRaisesRegex(BudgetExceeded, "状态预算"):
            self._persist("limited", capture, guard=guard, root=True)
        with Session(self.engine) as session:
            states = session.exec(
                select(InspectionState).where(
                    InspectionState.branch_run_id == self.branches["limited"].id
                )
            ).all()
        self.assertEqual(states, [])

    def test_cas_flag_off_keeps_legacy_state_without_creating_asset_ids(self):
        with Session(self.engine) as session:
            setting = session.exec(
                select(SystemSetting).where(
                    SystemSetting.key == "content_addressed_assets"
                )
            ).one()
            setting.value = "false"
            session.add(setting)
            session.commit()

        persisted = self._persist(
            "limited",
            _capture(_product_xml(), color="red"),
            root=True,
        )
        with Session(self.engine) as session:
            state = session.get(InspectionState, persisted.work.state_id)
            observation = session.get(
                InspectionObservation,
                persisted.observation_id,
            )
        self.assertEqual(observation.asset_status, "LEGACY")
        self.assertTrue(observation.metadata_only)
        self.assertIsNone(observation.screenshot_asset_id)
        self.assertIsNone(observation.xml_asset_id)
        self.assertTrue(state.screenshot_path)
        self.assertTrue(state.xml_path)


if __name__ == "__main__":
    unittest.main()
