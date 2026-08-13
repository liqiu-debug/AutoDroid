import io
import threading
import unittest
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock, patch

from PIL import Image
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, select

from backend.inspection.action_map import build_action_map
from backend.inspection.device import CapturedPage, InspectionAborted, LocatorDrift
from backend.inspection.engine import (
    BudgetExceeded,
    PersistedState,
    StateWork,
    _execute_branch,
    _family_action_cycle_period,
    _capture_matches_parent,
    _is_unambiguous_active_navigation_action,
    _persist_state,
    _serialize_action,
    _work_page_logical_key,
    _work_logical_key,
    execute_inspection_run,
)
from backend.inspection.live import InspectionLiveRegistry
from backend.inspection.sanitizer import InspectionArtifactSanitizer
from backend.inspection.semantics import (
    InspectionAction,
    build_page_model,
    enumerate_actions,
)
from backend.models import (
    InspectionBranchRun,
    InspectionExplorationFamily,
    InspectionFamilyActionCoverage,
    InspectionRun,
    InspectionState,
    InspectionTransition,
)


def _page(body: str) -> str:
    return (
        '<hierarchy rotation="0">'
        '<node package="com.demo" class="android.widget.FrameLayout" '
        'bounds="[0,0][1080,2400]" enabled="true">'
        f"{body}"
        "</node>"
        "</hierarchy>"
    )


def _capture(xml: str, *, screenshot_sha: str = "root-sha") -> CapturedPage:
    model = build_page_model(
        xml,
        package_name="com.demo",
        activity=".MainActivity",
    )
    return CapturedPage(
        package_name="com.demo",
        activity=".MainActivity",
        xml=xml,
        screenshot_png=b"not-used-by-mocked-persistence",
        screenshot_sha=screenshot_sha,
        perceptual_hash="0000000000000000",
        model=model,
        stable_by="exact",
    )


def _coordinate_click_action(
    key: str,
    *,
    bounds: tuple[int, int, int, int],
) -> InspectionAction:
    return InspectionAction(
        action_type="click",
        action_key=key,
        locator_candidates=[],
        target_meta={
            "class": "android.widget.Button",
            "bounds": list(bounds),
            "screen_size": [1080, 2400],
            "coordinate_authorized": True,
        },
        coordinate_only=True,
        replayable=False,
        action_role="COMMAND:coordinate-test",
        action_role_key=f"coordinate-role-{key}",
    )


class InspectionLiveEngineTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine(
            "sqlite://",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        SQLModel.metadata.create_all(self.engine)
        with Session(self.engine) as session:
            run = InspectionRun(
                name="live engine",
                package_name="com.demo",
                device_serial="android-1",
                profile_snapshot={},
                selected_branches=["authenticated"],
            )
            session.add(run)
            session.commit()
            session.refresh(run)
            branch = InspectionBranchRun(
                run_id=run.id,
                branch_key="authenticated",
                branch_name="已登录",
            )
            session.add(branch)
            session.commit()
            session.refresh(branch)
            state = InspectionState(
                run_id=run.id,
                branch_run_id=branch.id,
                branch_key=branch.branch_key,
                cluster_key="root-cluster",
                state_key="root-state",
                activity=".MainActivity",
                foreground_package="com.demo",
                screenshot_sha="root-sha",
                stable_status="STABLE",
                depth=0,
            )
            session.add(state)
            session.commit()
            session.refresh(state)
            self.run_id = run.id
            self.branch_id = branch.id
            self.state_id = state.id

    def tearDown(self):
        self.engine.dispose()

    def _work(
        self,
        capture: CapturedPage,
        actions: list[InspectionAction],
    ) -> StateWork:
        action_map = build_action_map(
            run_id=self.run_id,
            branch_key="authenticated",
            state_id=self.state_id,
            activity=capture.activity,
            screen_size=(1080, 2400),
            actions=actions,
            screenshot_path=(
                f"inspection/{self.run_id}/authenticated/"
                f"{self.state_id}/screenshot.png"
            ),
        )
        return StateWork(
            state_id=self.state_id,
            state_key=capture.model.state_key,
            cluster_key=capture.model.cluster_key,
            replay_key=capture.model.replay_key,
            package_name=capture.package_name,
            activity=capture.activity,
            screenshot_sha=capture.screenshot_sha,
            depth=0,
            path=[],
            actions=actions,
            action_map=action_map,
        )

    def _stored_work(
        self,
        capture: CapturedPage,
        actions: list[InspectionAction],
        *,
        depth: int,
        path: list[dict],
        parent_state_id: int | None,
    ) -> StateWork:
        with Session(self.engine) as session:
            state = InspectionState(
                run_id=self.run_id,
                branch_run_id=self.branch_id,
                branch_key="authenticated",
                cluster_key=capture.model.cluster_key,
                state_key=capture.model.state_key,
                semantic_key=capture.model.semantic_key,
                activity=capture.activity,
                foreground_package=capture.package_name,
                screenshot_sha=capture.screenshot_sha,
                stable_status="UNVERIFIED",
                expansion_status="DISCOVERED",
                depth=depth,
                parent_state_id=parent_state_id,
                first_path=list(path),
            )
            session.add(state)
            session.commit()
            session.refresh(state)
            state_id = int(state.id)
        return StateWork(
            state_id=state_id,
            state_key=capture.model.state_key,
            cluster_key=capture.model.cluster_key,
            replay_key=capture.model.replay_key,
            package_name=capture.package_name,
            activity=capture.activity,
            screenshot_sha=capture.screenshot_sha,
            depth=depth,
            path=list(path),
            actions=list(actions),
            action_map=build_action_map(
                run_id=self.run_id,
                branch_key="authenticated",
                state_id=state_id,
                activity=capture.activity,
                screen_size=(1080, 2400),
                actions=actions,
            ),
            parent_state_id=parent_state_id,
            semantic_key=capture.model.semantic_key,
        )

    def _run_branch(
        self,
        *,
        capture: CapturedPage,
        work: StateWork,
        persist_results: list[PersistedState],
        publish_mock,
        perform_mock,
        wait_captures: list[CapturedPage],
        device=None,
        ensure_captures=None,
        prepare_mock=None,
        max_actions=10,
        verify_mock=None,
        family_convergence=False,
        coverage_scheduler=None,
        business_coverage=False,
        branch_config=None,
    ):
        device = device or Mock()
        device.window_size.return_value = (1080, 2400)
        abort_event = Mock()
        abort_event.is_set.return_value = False
        abort_event.wait.return_value = False
        ensure_parent = Mock(return_value=capture)
        if ensure_captures is not None:
            ensure_parent.side_effect = ensure_captures
        prepare = prepare_mock or Mock()
        stable_verifier = verify_mock or Mock(return_value=1)
        profile = {
            "inspection_identity_v2": (
                family_convergence or bool(coverage_scheduler)
            ),
            "inspection_similarity_convergence": False,
            "inspection_exploration_family_convergence": (
                family_convergence or bool(coverage_scheduler)
            ),
            "budgets": {
                "duration_seconds": 30,
                "max_states": 10,
                "max_actions": max_actions,
                "max_depth": 3,
            },
        }
        if coverage_scheduler is not None:
            profile["inspection_coverage_scheduler_v2"] = bool(
                coverage_scheduler
            )
            profile["inspection_visual_home_actions"] = False
        if business_coverage:
            profile["inspection_business_coverage_v2"] = True
            profile["coverage_manifest"] = {"journeys": []}
        with patch(
            "backend.inspection.engine.engine",
            self.engine,
        ), patch(
            "backend.inspection.engine._prepare_branch",
            prepare,
        ), patch(
            "backend.inspection.engine.wait_for_stable_page",
            side_effect=wait_captures,
        ), patch(
            "backend.inspection.engine._ensure_parent",
            ensure_parent,
        ), patch(
            "backend.inspection.engine.exact_parent_matches",
            return_value=True,
        ), patch(
            "backend.inspection.engine._persist_state",
            side_effect=persist_results,
        ), patch(
            "backend.inspection.engine.perform_action",
            perform_mock,
        ), patch(
            "backend.inspection.engine.is_white_screen",
            return_value=False,
        ), patch(
            "backend.inspection.engine._verify_stable_paths",
            stable_verifier,
        ), patch(
            "backend.inspection.engine._persist_work_action_map",
        ), patch(
            "backend.inspection.engine._publish_live",
            publish_mock,
        ):
            return _execute_branch(
                run_id=self.run_id,
                branch_run_id=self.branch_id,
                device=device,
                device_serial="android-1",
                package_name="com.demo",
                profile=profile,
                branch_config=branch_config or {},
                abort_event=abort_event,
                monitor=None,
            )

    def _transitions(self):
        with Session(self.engine) as session:
            return session.exec(
                select(InspectionTransition)
                .where(InspectionTransition.run_id == self.run_id)
                .order_by(InspectionTransition.sequence)
            ).all()

    def test_family_action_cycle_requires_two_multi_edge_rounds(self):
        self.assertIsNone(
            _family_action_cycle_period(
                [(1, "open", True), (1, "open", False)]
            )
        )
        self.assertEqual(
            _family_action_cycle_period(
                [
                    (1, "open", True),
                    (2, "checkout", True),
                    (1, "open", False),
                    (2, "checkout", False),
                ]
            ),
            2,
        )
        self.assertIsNone(
            _family_action_cycle_period(
                [
                    (1, "open", True),
                    (2, "checkout", True),
                    (1, "open", False),
                    (2, "checkout", True),
                ]
            )
        )
        six_step_cycle = [
            (index, f"role-{index}", True) for index in range(6)
        ] + [
            (index, f"role-{index}", False) for index in range(6)
        ]
        self.assertEqual(_family_action_cycle_period(six_step_cycle), 6)

    def test_full_family_member_executes_duplicate_role_actions_locally(self):
        capture = _capture(
            _page(
                '<node package="com.demo" class="android.widget.TextView" '
                'text="完整探索页" enabled="true" bounds="[0,100][1080,300]"/>'
            )
        )
        actions = [
            InspectionAction(
                action_type="click",
                action_key=f"full-local-{index}",
                locator_candidates=[{"by": "description", "selector": label}],
                target_meta={"content_desc": label},
                action_role="NAV:shared-role",
                action_role_key="shared-family-role",
            )
            for index, label in enumerate(("入口一", "入口二"), 1)
        ]
        work = replace(
            self._work(capture, actions),
            semantic_key=capture.model.semantic_key,
            exploration_family_id=1,
            exploration_mode="FULL",
        )
        with Session(self.engine) as session:
            session.add(
                InspectionExplorationFamily(
                    id=1,
                    run_id=self.run_id,
                    branch_run_id=self.branch_id,
                    family_key="full-member-family",
                )
            )
            state = session.get(InspectionState, self.state_id)
            state.exploration_family_id = 1
            state.exploration_mode = "FULL"
            session.add(state)
            session.commit()

        performer = Mock(return_value="description")
        self._run_branch(
            capture=capture,
            work=work,
            persist_results=[
                PersistedState(work=work, is_new=True),
                PersistedState(work=work, is_new=False),
                PersistedState(work=work, is_new=False),
            ],
            publish_mock=Mock(),
            perform_mock=performer,
            wait_captures=[capture, capture, capture],
            family_convergence=True,
        )

        self.assertEqual(
            [call.args[1].action_key for call in performer.call_args_list],
            [item.action_key for item in actions],
        )
        with Session(self.engine) as session:
            transitions = session.exec(
                select(InspectionTransition)
                .where(InspectionTransition.run_id == self.run_id)
                .order_by(InspectionTransition.sequence)
            ).all()
        self.assertEqual(
            [item.status for item in transitions],
            ["SELF_LOOP", "SELF_LOOP"],
        )

    def test_instance_anchor_scopes_attempted_action_identity(self):
        capture = _capture(
            _page(
                '<node package="com.demo" class="android.widget.Button" '
                'content-desc="打开" clickable="true" enabled="true" '
                'bounds="[10,20][300,140]"/>'
            )
        )
        action = enumerate_actions(capture.model, screen_size=(1080, 2400))[0]
        first = replace(
            self._work(capture, [action]),
            semantic_key="same-semantic",
            instance_anchor="instance-a",
        )
        second = replace(first, state_id=first.state_id + 1, instance_anchor="instance-b")
        revisit = replace(first, screenshot_sha="another-observation")
        changed_state = replace(
            first,
            state_id=first.state_id + 2,
            semantic_key="changed-control-state",
        )

        self.assertNotEqual(_work_logical_key(first), _work_logical_key(second))
        self.assertEqual(_work_logical_key(first), _work_logical_key(revisit))
        self.assertNotEqual(_work_logical_key(first), _work_logical_key(changed_state))
        self.assertEqual(
            _work_page_logical_key(first),
            first.semantic_key,
        )

    def test_viewport_parent_match_accepts_family_delta_but_rejects_conflicts(self):
        expected_capture = _capture(
            _page(
                '<node package="com.demo" class="android.widget.TextView" '
                'text="Viewport expected" enabled="true" '
                'bounds="[0,100][1080,300]"/>'
            )
        )
        actual_capture = _capture(
            _page(
                '<node package="com.demo" class="android.widget.TextView" '
                'text="Viewport dynamic module" enabled="true" '
                'bounds="[0,100][1080,300]"/>'
            ),
            screenshot_sha="viewport-dynamic",
        )
        scroll = InspectionAction(
            action_type="scroll",
            action_key="viewport-recovery-scroll",
            locator_candidates=[],
            target_meta={"direction": "up"},
        )
        parent = replace(
            self._work(expected_capture, []),
            semantic_key="stored-viewport-semantic",
            replay_key="stored-viewport-replay",
            path=[_serialize_action(scroll)],
            instance_anchor="",
        )
        compatible = Mock(
            equivalent=False,
            score=0.907,
            evidence={"control_conflicts": []},
        )
        page_compatible = Mock(
            score=0.94,
            evidence={
                "structure_similarity": 0.9677,
                "action_similarity": 0.9167,
                "anchor_similarity": 1.0,
                "landmark_similarity": 1.0,
                "risk_signature_match": True,
            },
        )

        with patch(
            "backend.inspection.engine.engine",
            self.engine,
        ), patch(
            "backend.inspection.engine._observation_candidate_model",
            return_value=expected_capture.model,
        ), patch(
            "backend.inspection.engine.compare_exploration_families",
            return_value=compatible,
        ), patch(
            "backend.inspection.engine.compare_page_models",
            return_value=page_compatible,
        ):
            self.assertTrue(_capture_matches_parent(actual_capture, parent))

            page_compatible.evidence = {
                **page_compatible.evidence,
                "risk_signature_match": False,
            }
            self.assertFalse(_capture_matches_parent(actual_capture, parent))

            page_compatible.evidence = {
                **page_compatible.evidence,
                "risk_signature_match": True,
            }
            compatible.evidence = {
                **compatible.evidence,
                "control_conflicts": ["selected:home-tab"],
            }
            self.assertFalse(_capture_matches_parent(actual_capture, parent))

            compatible.evidence = {
                **compatible.evidence,
                "control_conflicts": [],
            }
            role_conflict = replace(
                actual_capture,
                model=replace(actual_capture.model, role="ORDER"),
            )
            self.assertFalse(_capture_matches_parent(role_conflict, parent))

    def test_profile_parent_match_tolerates_dynamic_counters(self):
        def profile_page(*, coupon_count: int, order_count: int) -> str:
            labels = (
                "我的社区",
                "消息",
                "客服",
                "设置",
                f", {coupon_count}, 张, 优惠券",
                f"待付款, {order_count}",
                "商品收藏, 37",
                "家电维修",
            )
            return _page(
                "".join(
                    '<node package="com.demo" class="android.view.ViewGroup" '
                    'content-desc="{}" clickable="true" enabled="true" '
                    'bounds="[40,{}][1040,{}]"/>'.format(
                        label,
                        220 + index * 180,
                        360 + index * 180,
                    )
                    for index, label in enumerate(labels)
                )
            )

        expected_capture = _capture(
            profile_page(coupon_count=0, order_count=21),
        )
        actual_capture = _capture(
            profile_page(coupon_count=2, order_count=22),
            screenshot_sha="profile-dynamic",
        )
        parent = replace(
            self._work(expected_capture, []),
            semantic_key="stored-profile-semantic",
            instance_anchor="profile-instance",
            role=expected_capture.model.role,
            page_subtype=expected_capture.model.page_subtype,
        )

        with patch(
            "backend.inspection.engine.engine",
            self.engine,
        ), patch(
            "backend.inspection.engine._observation_candidate_model",
            return_value=expected_capture.model,
        ):
            self.assertTrue(_capture_matches_parent(actual_capture, parent))

            conflicting = replace(
                actual_capture,
                model=replace(actual_capture.model, page_subtype="ORDER"),
            )
            self.assertFalse(_capture_matches_parent(conflicting, parent))

    def test_exact_state_key_stays_anchor_scoped_except_known_same_page(self):
        def png_bytes(color: str) -> bytes:
            buffer = io.BytesIO()
            Image.new("RGB", (1080, 2400), color=color).save(buffer, format="PNG")
            return buffer.getvalue()

        first_capture = replace(
            _capture(
                _page(
                    '<node package="com.demo" class="android.widget.TextView" '
                    'text="同一个首页" enabled="true" bounds="[0,100][1080,300]"/>'
                    '<node package="com.demo" class="android.view.ViewGroup" '
                    'enabled="true" bounds="[0,2100][1080,2320]">'
                    '<node package="com.demo" class="android.view.ViewGroup" '
                    'content-desc="首页" clickable="true" selected="true" '
                    'enabled="true" bounds="[0,2120][360,2300]"/>'
                    '<node package="com.demo" class="android.view.ViewGroup" '
                    'content-desc="附近门店" clickable="true" selected="false" '
                    'enabled="true" bounds="[360,2120][720,2300]"/>'
                    '<node package="com.demo" class="android.view.ViewGroup" '
                    'content-desc="我的" clickable="true" selected="false" '
                    'enabled="true" bounds="[720,2120][1080,2300]"/>'
                    "</node>"
                ),
                screenshot_sha="same-state-first",
            ),
            screenshot_png=png_bytes("red"),
        )
        different_capture = replace(
            _capture(
                _page(
                    '<node package="com.demo" class="android.widget.TextView" '
                    'text="确实不同的页面" enabled="true" bounds="[0,100][1080,300]"/>'
                ),
                screenshot_sha="different-state",
            ),
            screenshot_png=png_bytes("blue"),
        )
        incoming = InspectionAction(
            action_type="click",
            action_key="dynamic-entry",
            locator_candidates=[{"by": "description", "selector": "动态入口"}],
            target_meta={"content_desc": "动态入口"},
            action_role="COMMAND:dynamic-entry",
            action_role_key="dynamic-entry-role",
        )
        with Session(self.engine) as session:
            row = session.get(InspectionBranchRun, self.branch_id)
            branch = InspectionBranchRun(
                id=row.id,
                run_id=row.run_id,
                branch_key=row.branch_key,
                branch_name=row.branch_name,
            )

        with TemporaryDirectory() as directory, patch(
            "backend.inspection.engine.engine",
            self.engine,
        ), patch(
            "backend.inspection.engine._reports_root",
            return_value=(Path(directory) / "reports").resolve(),
        ):
            common = {
                "run_id": self.run_id,
                "branch_run": branch,
                "sanitizer": InspectionArtifactSanitizer(),
                "screen_size": (1080, 2400),
                "safety_rules": [],
                "input_rules": [],
                "max_scrolls": 0,
                "max_variants": 5,
                "identity_v2": True,
            }
            first = _persist_state(
                **common,
                capture=first_capture,
                depth=0,
                parent_state_id=None,
                path=[],
                mark_branch_root=True,
            )
            cross_anchor = _persist_state(
                **common,
                capture=first_capture,
                depth=1,
                parent_state_id=first.work.state_id,
                path=[_serialize_action(incoming)],
            )
            same_page = _persist_state(
                **common,
                capture=first_capture,
                depth=1,
                parent_state_id=first.work.state_id,
                path=[_serialize_action(incoming)],
                instance_anchor_override=first.work.instance_anchor,
                preferred_state_id=first.work.state_id,
            )
            different = _persist_state(
                **common,
                capture=different_capture,
                depth=1,
                parent_state_id=first.work.state_id,
                path=[_serialize_action(incoming)],
            )

        self.assertNotEqual(cross_anchor.work.state_id, first.work.state_id)
        self.assertEqual(same_page.work.state_id, first.work.state_id)
        self.assertNotIn(
            "首页",
            {item.target_meta.get("content_desc") for item in first.work.actions},
        )
        self.assertIn(
            "首页",
            {
                item.target_meta.get("content_desc")
                for item in first.work.recovery_navigation_actions
            },
        )
        self.assertTrue(
            {
                item.action_key
                for item in first.work.recovery_navigation_actions
                if item.target_meta.get("content_desc") == "首页"
            }.isdisjoint(
                {
                    item.get("action_key")
                    for item in first.work.action_map.get("actions") or []
                }
            )
        )
        self.assertEqual(
            same_page.match_evidence["match_type"],
            "SOURCE_EXACT_STATE",
        )
        self.assertNotEqual(different.work.state_id, first.work.state_id)

    def _run_lost_frontier_scenario(self, *, drop_every_time: bool):
        root_capture = _capture(
            _page(
                '<node package="com.demo" class="android.widget.Button" '
                'content-desc="进入 B" clickable="true" enabled="true" '
                'bounds="[10,20][300,140]"/>'
            ),
            screenshot_sha="frontier-root",
        )
        child_capture = _capture(
            _page(
                '<node package="com.demo" class="android.widget.Button" '
                'content-desc="刷新 B" clickable="true" enabled="true" '
                'bounds="[10,20][300,140]"/>'
            ),
            screenshot_sha="frontier-child",
        )
        root_action = enumerate_actions(
            root_capture.model,
            screen_size=(1080, 2400),
        )[0]
        child_action = enumerate_actions(
            child_capture.model,
            screen_size=(1080, 2400),
        )[0]
        root_work = self._work(root_capture, [root_action])
        with Session(self.engine) as session:
            child_state = InspectionState(
                run_id=self.run_id,
                branch_run_id=self.branch_id,
                branch_key="authenticated",
                cluster_key=child_capture.model.cluster_key,
                state_key=child_capture.model.state_key,
                semantic_key=child_capture.model.semantic_key,
                activity=child_capture.activity,
                foreground_package=child_capture.package_name,
                screenshot_sha=child_capture.screenshot_sha,
                stable_status="STABLE",
                depth=1,
                parent_state_id=self.state_id,
            )
            session.add(child_state)
            session.commit()
            session.refresh(child_state)
            child_state_id = int(child_state.id)
        child_work = StateWork(
            state_id=child_state_id,
            state_key=child_capture.model.state_key,
            cluster_key=child_capture.model.cluster_key,
            replay_key=child_capture.model.replay_key,
            package_name=child_capture.package_name,
            activity=child_capture.activity,
            screenshot_sha=child_capture.screenshot_sha,
            depth=1,
            path=[_serialize_action(root_action)],
            actions=[child_action],
            action_map=build_action_map(
                run_id=self.run_id,
                branch_key="authenticated",
                state_id=child_state_id,
                activity=child_capture.activity,
                screen_size=(1080, 2400),
                actions=[child_action],
            ),
            parent_state_id=self.state_id,
        )
        pop_count = 0

        def lose_queued_child(queue, _current_path):
            nonlocal pop_count
            pop_count += 1
            if pop_count >= 2 and (drop_every_time or pop_count == 2):
                queue.popleft()
                return root_work
            return queue.popleft()

        publisher = Mock()
        performer = Mock(return_value="description")
        verifier = Mock(return_value=1)
        persist_results = [
            PersistedState(work=root_work, is_new=True),
            PersistedState(work=child_work, is_new=True),
        ]
        wait_captures = [root_capture, child_capture, root_capture]
        ensure_captures = [root_capture]
        if not drop_every_time:
            persist_results.append(PersistedState(work=child_work, is_new=False))
            wait_captures.append(child_capture)
            ensure_captures.append(child_capture)
        with patch(
            "backend.inspection.engine._pop_most_local",
            side_effect=lose_queued_child,
        ):
            outcome = self._run_branch(
                capture=root_capture,
                work=root_work,
                persist_results=persist_results,
                publish_mock=publisher,
                perform_mock=performer,
                wait_captures=wait_captures,
                ensure_captures=ensure_captures,
                verify_mock=verifier,
            )
        return outcome, child_work, child_state_id, performer, publisher, verifier

    def test_lost_queue_entry_is_reconciled_before_verification(self):
        outcome, child, state_id, performer, publisher, verifier = (
            self._run_lost_frontier_scenario(drop_every_time=False)
        )

        self.assertEqual(outcome.stop_reason, "队列自然耗尽")
        self.assertEqual(
            [call.args[1].action_key for call in performer.call_args_list],
            [
                child.path[0]["action_key"],
                child.actions[0].action_key,
            ],
        )
        verifier.assert_called_once()
        self.assertTrue(
            any(
                call.args[1] == "FRONTIER_UPDATED"
                and call.kwargs.get("current_stage") == "重建丢失的探索前沿"
                for call in publisher.call_args_list
            )
        )
        frontier_events = [
            call.kwargs["frontier"]
            for call in publisher.call_args_list
            if call.args[1] == "FRONTIER_UPDATED"
            and "frontier" in call.kwargs
        ]
        self.assertTrue(frontier_events)
        self.assertTrue(
            all(
                {
                    "queued_count",
                    "deferred_count",
                    "pending_action_count",
                    "expanding_count",
                }
                <= set(frontier)
                for frontier in frontier_events
            )
        )
        with Session(self.engine) as session:
            state = session.get(InspectionState, state_id)
        self.assertEqual(state.expansion_status, "EXPANDED")

    def test_second_lost_queue_entry_is_terminally_truncated(self):
        outcome, child, state_id, performer, _publisher, verifier = (
            self._run_lost_frontier_scenario(drop_every_time=True)
        )

        self.assertEqual(outcome.status, "WARNING")
        self.assertIn("FRONTIER_INCOMPLETE", outcome.stop_reason)
        self.assertEqual(performer.call_count, 1)
        verifier.assert_not_called()
        self.assertEqual(
            child.action_map["actions"][0]["status"],
            "QUEUE_TRUNCATED",
        )
        with Session(self.engine) as session:
            state = session.get(InspectionState, state_id)
        self.assertEqual(state.expansion_status, "ABORTED")
        self.assertEqual(state.pending_action_count, 0)

    def test_success_event_order_and_overlay_clear_after_device_invocation(self):
        xml = _page(
            '<node package="com.demo" class="android.widget.Button" '
            'content-desc="下一页" clickable="true" enabled="true" '
            'bounds="[10,20][300,140]"/>'
        )
        capture = _capture(xml)
        action = enumerate_actions(
            capture.model,
            screen_size=(1080, 2400),
        )[0]
        work = self._work(capture, [action])
        publisher = Mock()
        performer = Mock(return_value="description")

        outcome = self._run_branch(
            capture=capture,
            work=work,
            persist_results=[
                PersistedState(work=work, is_new=True),
                PersistedState(work=work, is_new=False),
            ],
            publish_mock=publisher,
            perform_mock=performer,
            wait_captures=[capture, capture],
        )

        event_calls = [
            (call.args[1], call.kwargs)
            for call in publisher.call_args_list
        ]
        event_types = [event_type for event_type, _ in event_calls]
        started_index = event_types.index("ACTION_STARTED")
        invoked_index = event_types.index("ACTION_INVOKED")
        finished_index = event_types.index("ACTION_FINISHED")
        page_index = max(
            index
            for index, event_type in enumerate(event_types[:started_index])
            if event_type == "PAGE_ACTIONS"
        )
        clear_index = next(
            index
            for index in range(invoked_index + 1, finished_index)
            if event_types[index] == "OVERLAY_CLEAR"
        )

        self.assertLess(page_index, started_index)
        self.assertLess(started_index, invoked_index)
        self.assertLess(invoked_index, clear_index)
        self.assertLess(clear_index, finished_index)
        self.assertEqual(
            event_calls[started_index][1]["current_action"]["status"],
            "ACTIVE",
        )
        self.assertFalse(
            event_calls[started_index][1]["current_action"]["invoked"]
        )
        self.assertEqual(
            event_calls[invoked_index][1]["current_action"]["status"],
            "INVOKED",
        )
        self.assertTrue(
            event_calls[invoked_index][1]["current_action"]["invoked"]
        )
        self.assertEqual(
            event_calls[finished_index][1]["current_action"]["status"],
            "SELF_LOOP",
        )
        self.assertTrue(
            event_calls[finished_index][1]["current_action"]["invoked"]
        )
        self.assertIn(outcome.status, {"PASS", "WARNING"})
        performer.assert_called_once()

    def test_same_page_transition_keeps_source_anchor_and_state_preference(self):
        capture = _capture(
            _page(
                '<node package="com.demo" class="android.widget.Button" '
                'content-desc="延保" clickable="true" enabled="true" '
                'bounds="[10,20][300,140]"/>'
            )
        )
        action = enumerate_actions(capture.model, screen_size=(1080, 2400))[0]
        work = replace(
            self._work(capture, [action]),
            semantic_key=capture.model.semantic_key,
            instance_anchor="stable-home-anchor",
        )
        calls = []
        results = iter(
            [
                PersistedState(work=work, is_new=True),
                PersistedState(work=work, is_new=False),
            ]
        )

        def persist_state(**kwargs):
            calls.append(kwargs)
            return next(results)

        self._run_branch(
            capture=capture,
            work=work,
            persist_results=persist_state,
            publish_mock=Mock(),
            perform_mock=Mock(return_value="description"),
            wait_captures=[capture, capture],
            family_convergence=True,
        )

        self.assertEqual(calls[1]["preferred_state_id"], self.state_id)
        self.assertEqual(
            calls[1]["instance_anchor_override"],
            "stable-home-anchor",
        )
        with Session(self.engine) as session:
            transition = session.exec(
                select(InspectionTransition).where(
                    InspectionTransition.run_id == self.run_id
                )
            ).one()
        self.assertEqual(transition.topology_type, "SELF_LOOP")

    def test_locator_rebound_hides_overlay_until_action_map_is_recaptured(self):
        original_capture = _capture(
            _page(
                '<node package="com.demo" class="android.widget.Button" '
                'content-desc="下一页" clickable="true" enabled="true" '
                'bounds="[10,20][300,140]"/>'
            ),
            screenshot_sha="before-rebind",
        )
        fresh_capture = _capture(
            _page(
                '<node package="com.demo" class="android.widget.Button" '
                'content-desc="下一页" clickable="true" enabled="true" '
                'bounds="[40,80][360,220]"/>'
            ),
            screenshot_sha="after-rebind",
        )
        action = enumerate_actions(
            original_capture.model,
            screen_size=(1080, 2400),
        )[0]
        work = self._work(original_capture, [action])
        publisher = Mock()
        performer = Mock(side_effect=[LocatorDrift("stale locator"), "description"])

        self._run_branch(
            capture=original_capture,
            work=work,
            persist_results=[
                PersistedState(work=work, is_new=True),
                PersistedState(work=work, is_new=False),
            ],
            publish_mock=publisher,
            perform_mock=performer,
            wait_captures=[original_capture, fresh_capture, fresh_capture],
        )

        rebound = next(
            call
            for call in publisher.call_args_list
            if call.args[1] == "ACTION_REBOUND"
        )
        self.assertFalse(rebound.kwargs["overlay_visible"])
        self.assertFalse(rebound.kwargs["canvas_matches_panel"])
        self.assertFalse(
            rebound.kwargs["action_panel"]["canvas_matches_panel"]
        )
        self.assertEqual(
            rebound.kwargs["current_action"]["bounds"],
            [10, 20, 300, 140],
        )
        self.assertEqual(performer.call_count, 2)
        self.assertEqual(
            list(performer.call_args_list[1].args[1].target_meta["bounds"]),
            [40, 80, 360, 220],
        )
        finished = next(
            call
            for call in publisher.call_args_list
            if call.args[1] == "ACTION_FINISHED"
        )
        self.assertFalse(finished.kwargs["overlay_visible"])
        self.assertFalse(finished.kwargs["canvas_matches_panel"])

        with Session(self.engine) as session:
            transition = session.exec(
                select(InspectionTransition).where(
                    InspectionTransition.run_id == self.run_id
                )
            ).one()
        self.assertEqual(transition.status, "SELF_LOOP")

    def test_fresh_xml_preflight_skips_missing_followup_without_device_call(self):
        original_capture = _capture(
            _page(
                '<node package="com.demo" class="android.widget.TextView" '
                'text="动态列表" enabled="true" bounds="[0,100][1080,240]"/>'
                '<node package="com.demo" class="android.widget.Button" '
                'content-desc="动作一" clickable="true" enabled="true" '
                'bounds="[10,300][300,440]"/>'
                '<node package="com.demo" class="android.widget.Button" '
                'content-desc="动作二" clickable="true" enabled="true" '
                'bounds="[10,500][300,640]"/>'
            ),
            screenshot_sha="before-missing-actions",
        )
        fresh_capture = _capture(
            _page(
                '<node package="com.demo" class="android.widget.TextView" '
                'text="动态列表" enabled="true" bounds="[0,100][1080,240]"/>'
            ),
            screenshot_sha="after-missing-actions",
        )
        actions = enumerate_actions(
            original_capture.model,
            screen_size=(1080, 2400),
        )
        self.assertEqual(len(actions), 2)
        work = self._work(original_capture, actions)
        performer = Mock(side_effect=LocatorDrift("stale locator"))

        with patch(
            "backend.inspection.engine._capture_matches_parent",
            return_value=True,
        ):
            self._run_branch(
                capture=original_capture,
                work=work,
                persist_results=[PersistedState(work=work, is_new=True)],
                publish_mock=Mock(),
                perform_mock=performer,
                wait_captures=[original_capture, fresh_capture],
            )

        performer.assert_called_once()
        with Session(self.engine) as session:
            transitions = session.exec(
                select(InspectionTransition)
                .where(InspectionTransition.run_id == self.run_id)
                .order_by(InspectionTransition.sequence)
            ).all()
        self.assertEqual(
            [transition.status for transition in transitions],
            ["LOCATOR_NOT_FOUND", "LOCATOR_NOT_FOUND"],
        )
        self.assertEqual(
            [transition.execution_disposition for transition in transitions],
            ["FAILED", "SKIPPED"],
        )
        self.assertEqual(
            transitions[1].reason,
            "最新采集未暴露该动作，跳过设备调用",
        )

    def test_stale_action_map_keeps_started_and_family_covered_overlay_hidden(self):
        original_xml = _page(
            '<node package="com.demo" class="android.widget.Button" '
            'content-desc="动作一" clickable="true" enabled="true" '
            'bounds="[10,20][300,140]"/>'
            '<node package="com.demo" class="android.widget.Button" '
            'content-desc="动作二" clickable="true" enabled="true" '
            'bounds="[10,180][300,300]"/>'
            '<node package="com.demo" class="android.widget.Button" '
            'content-desc="动作三" clickable="true" enabled="true" '
            'bounds="[10,340][300,460]"/>'
        )
        fresh_xml = original_xml
        original_capture = _capture(original_xml, screenshot_sha="before-rebind")
        fresh_capture = _capture(fresh_xml, screenshot_sha="after-rebind")
        actions = enumerate_actions(
            original_capture.model,
            screen_size=(1080, 2400),
        )
        self.assertEqual(len(actions), 3)
        work = self._work(original_capture, actions)

        with Session(self.engine) as session:
            family = InspectionExplorationFamily(
                run_id=self.run_id,
                branch_run_id=self.branch_id,
                family_key="live-stale-map-family",
                page_role=original_capture.model.role,
                activity_family=original_capture.model.activity_family,
            )
            session.add(family)
            session.flush()
            state = session.get(InspectionState, self.state_id)
            state.exploration_family_id = family.id
            state.exploration_mode = "DELTA_ONLY"
            session.add(state)
            session.add(
                InspectionFamilyActionCoverage(
                    family_id=family.id,
                    action_role_key=actions[2].action_role_key,
                    action_role=actions[2].action_role,
                    status="SUCCESS",
                    source_state_id=None,
                    source_transition_id=1,
                )
            )
            session.commit()
            work.exploration_family_id = family.id
            work.exploration_mode = "DELTA_ONLY"

        publisher = Mock()
        performer = Mock(
            side_effect=[LocatorDrift("stale locator"), "description", "description"]
        )
        outcome = self._run_branch(
            capture=original_capture,
            work=work,
            persist_results=[
                PersistedState(work=work, is_new=True),
                PersistedState(work=work, is_new=False),
                PersistedState(work=work, is_new=False),
            ],
            publish_mock=publisher,
            perform_mock=performer,
            wait_captures=[
                original_capture,
                fresh_capture,
                fresh_capture,
                fresh_capture,
            ],
            family_convergence=True,
        )

        started = [
            call
            for call in publisher.call_args_list
            if call.args[1] == "ACTION_STARTED"
        ]
        covered = next(
            call
            for call in publisher.call_args_list
            if call.args[1] == "ACTION_COVERED_BY_FAMILY"
        )

        self.assertEqual(len(started), 2)
        self.assertTrue(started[0].kwargs["overlay_visible"])
        self.assertFalse(
            started[1].kwargs["action_panel"]["canvas_matches_panel"],
            [
                (
                    call.args[1],
                    (call.kwargs.get("current_action") or {}).get("action_key"),
                    call.kwargs.get("canvas_matches_panel"),
                )
                for call in publisher.call_args_list
            ],
        )
        self.assertFalse(started[1].kwargs["overlay_visible"])
        self.assertFalse(
            covered.kwargs["action_panel"]["canvas_matches_panel"]
        )
        self.assertFalse(covered.kwargs["overlay_visible"])
        self.assertIn(outcome.status, {"PASS", "WARNING"})

    def test_blocked_is_not_called_and_coordinate_no_effect_finishes_once(self):
        xml = _page(
            '<node package="com.demo" class="android.widget.EditText" '
            'content-desc="Search" clickable="true" enabled="true" '
            'bounds="[10,20][300,140]"/>'
            '<node package="com.demo" class="android.widget.Button" '
            'text="订单号 A12345678" clickable="true" enabled="true" '
            'bounds="[10,180][500,300]"/>'
        )
        capture = _capture(xml)
        actions = enumerate_actions(
            capture.model,
            screen_size=(1080, 2400),
        )
        actions.append(
            _coordinate_click_action(
                "authorized-coordinate",
                bounds=(10, 180, 500, 300),
            )
        )
        self.assertEqual(
            {item.risk_type for item in actions if item.risk_type},
            {"UNMAPPED_INPUT"},
        )
        self.assertEqual(
            len([item for item in actions if item.coordinate_only]),
            1,
        )
        work = self._work(capture, actions)
        publisher = Mock()
        performer = Mock(return_value="coordinate")

        outcome = self._run_branch(
            capture=capture,
            work=work,
            persist_results=[PersistedState(work=work, is_new=True)],
            publish_mock=publisher,
            perform_mock=performer,
            wait_captures=[capture, capture],
        )

        event_calls = [
            (call.args[1], call.kwargs)
            for call in publisher.call_args_list
        ]
        event_types = [event_type for event_type, _ in event_calls]
        finished = [
            payload
            for event_type, payload in event_calls
            if event_type == "ACTION_FINISHED"
        ]

        self.assertEqual(event_types.count("ACTION_STARTED"), 1)
        self.assertEqual(event_types.count("ACTION_INVOKED"), 1)
        self.assertEqual(
            [item["current_action"]["status"] for item in finished],
            ["BLOCKED", "NO_EFFECT"],
        )
        self.assertFalse(finished[0]["current_action"]["invoked"])
        self.assertTrue(finished[1]["current_action"]["invoked"])
        self.assertIsNone(finished[0]["current_action"]["display_order"])
        self.assertEqual(finished[1]["current_action"]["display_order"], 1)
        performer.assert_called_once()
        called_action = performer.call_args.args[1]
        self.assertTrue(called_action.coordinate_only)
        self.assertIsNone(called_action.risk_type)
        self.assertTrue(
            performer.call_args.kwargs["allow_coordinate_discovery"]
        )
        self.assertEqual(outcome.status, "WARNING")

        with Session(self.engine) as session:
            transitions = session.exec(
                select(InspectionTransition)
                .where(InspectionTransition.run_id == self.run_id)
                .order_by(InspectionTransition.sequence)
            ).all()
        self.assertEqual(
            [item.status for item in transitions],
            ["BLOCKED", "NO_EFFECT"],
        )
        self.assertEqual(transitions[0].execution_disposition, "SKIPPED")
        self.assertEqual(transitions[0].failure_type, "SAFETY_BLOCKED")
        coordinate_transition = transitions[1]
        self.assertTrue(coordinate_transition.coordinate_only)
        self.assertFalse(coordinate_transition.replayable)
        self.assertIsNone(coordinate_transition.to_state_id)
        self.assertIn("不再重试", coordinate_transition.reason)

    def test_exploration_action_limit_preserves_verification_turn(self):
        capture = _capture(
            _page(
                '<node package="com.demo" class="android.widget.Button" '
                'content-desc="Open" clickable="true" enabled="true" '
                'bounds="[10,180][500,300]"/>'
            )
        )
        action = next(
            item
            for item in enumerate_actions(capture.model, screen_size=(1080, 2400))
            if item.action_type == "click"
        )
        work = self._work(capture, [action])
        verify = Mock(return_value=0)
        performer = Mock()

        outcome = self._run_branch(
            capture=capture,
            work=work,
            persist_results=[PersistedState(work=work, is_new=True)],
            publish_mock=Mock(),
            perform_mock=performer,
            wait_captures=[capture],
            max_actions=1,
            verify_mock=verify,
        )

        performer.assert_not_called()
        verify.assert_called_once()
        verification_guard = verify.call_args.kwargs["budget_guard"]
        self.assertEqual(verification_guard.device_actions, 0)
        self.assertEqual(verification_guard.max_device_actions, 1)
        self.assertEqual(outcome.status, "WARNING")
        self.assertEqual(
            outcome.stop_reason,
            "探索阶段 90% 动作预算已用完",
        )

    def test_business_coverage_shadow_mode_does_not_change_scheduler_budget(self):
        capture = _capture(
            _page(
                '<node package="com.demo" class="android.widget.Button" '
                'content-desc="Open" clickable="true" enabled="true" '
                'bounds="[10,180][500,300]"/>'
            )
        )
        action = next(
            item
            for item in enumerate_actions(capture.model, screen_size=(1080, 2400))
            if item.action_type == "click"
        )
        work = self._work(capture, [action])
        verify = Mock(return_value=0)

        outcome = self._run_branch(
            capture=capture,
            work=work,
            persist_results=[PersistedState(work=work, is_new=True)],
            publish_mock=Mock(),
            perform_mock=Mock(),
            wait_captures=[capture],
            max_actions=1,
            verify_mock=verify,
            coverage_scheduler=False,
            business_coverage=True,
        )

        self.assertEqual(outcome.stop_reason, "探索阶段 90% 动作预算已用完")
        self.assertIsNone(verify.call_args.kwargs["max_paths"])
        self.assertFalse(verify.call_args.kwargs["representative_only"])
        self.assertFalse(verify.call_args.kwargs["coverage_reverify_once"])

    def test_business_coverage_directed_mode_reserves_fifteen_percent(self):
        capture = _capture(
            _page(
                '<node package="com.demo" class="android.widget.Button" '
                'content-desc="Open" clickable="true" enabled="true" '
                'bounds="[10,180][500,300]"/>'
            )
        )
        action = next(
            item
            for item in enumerate_actions(capture.model, screen_size=(1080, 2400))
            if item.action_type == "click"
        )
        work = self._work(capture, [action])
        verify = Mock(return_value=0)

        outcome = self._run_branch(
            capture=capture,
            work=work,
            persist_results=[PersistedState(work=work, is_new=True)],
            publish_mock=Mock(),
            perform_mock=Mock(),
            wait_captures=[capture],
            max_actions=1,
            verify_mock=verify,
            coverage_scheduler=True,
            business_coverage=True,
        )

        self.assertEqual(outcome.stop_reason, "探索阶段 85% 动作预算已用完")
        self.assertEqual(verify.call_args.kwargs["max_paths"], 24)
        self.assertTrue(verify.call_args.kwargs["representative_only"])
        self.assertTrue(verify.call_args.kwargs["coverage_reverify_once"])

    def test_navigation_is_not_precovered_when_active_indicator_is_ambiguous(self):
        action = InspectionAction(
            action_type="click",
            action_key="open-category",
            locator_candidates=[],
            target_meta={
                "navigation": {
                    "member_index": 1,
                    "active_member_indices": [0, 1, 2, 3, 4],
                }
            },
            action_group_key="bottom-category",
        )

        self.assertFalse(_is_unambiguous_active_navigation_action(action))
        action.target_meta["navigation"]["active_member_indices"] = [1]
        self.assertTrue(_is_unambiguous_active_navigation_action(action))

    def test_duplicate_coordinate_point_is_not_invoked_after_no_effect(self):
        xml = _page(
            '<node package="com.demo" class="android.widget.Button" '
            'text="订单号 A12345678" clickable="true" enabled="true" '
            'bounds="[10,180][500,300]"/>'
        )
        capture = _capture(xml)
        action = _coordinate_click_action(
            "duplicate-coordinate",
            bounds=(10, 180, 500, 300),
        )
        duplicate = InspectionAction(
            **{
                **action.__dict__,
                "action_key": f"{action.action_key}-duplicate",
            }
        )
        work = self._work(capture, [action, duplicate])
        performer = Mock(return_value="coordinate")

        self._run_branch(
            capture=capture,
            work=work,
            persist_results=[PersistedState(work=work, is_new=True)],
            publish_mock=Mock(),
            perform_mock=performer,
            wait_captures=[capture, capture],
        )

        performer.assert_called_once()
        with Session(self.engine) as session:
            transitions = session.exec(
                select(InspectionTransition)
                .where(InspectionTransition.run_id == self.run_id)
                .order_by(InspectionTransition.sequence)
            ).all()
        self.assertEqual(
            [item.status for item in transitions],
            ["NO_EFFECT", "SKIPPED"],
        )
        self.assertIn("同一物理坐标", transitions[1].reason)

    def test_coordinate_click_rejects_stale_parent_screenshot(self):
        xml = _page(
            '<node package="com.demo" class="android.widget.Button" '
            'text="订单号 A12345678" clickable="true" enabled="true" '
            'bounds="[10,180][500,300]"/>'
        )
        saved_capture = _capture(xml, screenshot_sha="saved-sha")
        current_capture = _capture(xml, screenshot_sha="current-sha")
        action = _coordinate_click_action(
            "stale-coordinate",
            bounds=(10, 180, 500, 300),
        )
        work = self._work(saved_capture, [action])
        performer = Mock(return_value="coordinate")

        self._run_branch(
            capture=current_capture,
            work=work,
            persist_results=[PersistedState(work=work, is_new=True)],
            publish_mock=Mock(),
            perform_mock=performer,
            wait_captures=[saved_capture],
        )

        performer.assert_not_called()
        with Session(self.engine) as session:
            transition = session.exec(
                select(InspectionTransition).where(
                    InspectionTransition.run_id == self.run_id
                )
            ).one()
        self.assertEqual(transition.status, "COORDINATE_STALE")
        self.assertIn("页面像素已变化", transition.reason)

    def test_coordinate_click_change_immediately_expands_new_full_child(self):
        root_xml = _page(
            '<node package="com.demo" class="android.widget.Button" '
            'text="订单号 A12345678" clickable="true" enabled="true" '
            'bounds="[100,200][500,400]"/>'
        )
        child_xml = _page(
            '<node package="com.demo" class="android.widget.TextView" '
            'content-desc="坐标目标页" enabled="true" '
            'bounds="[10,20][500,140]"/>'
        )
        root_capture = _capture(root_xml)
        child_capture = _capture(child_xml, screenshot_sha="child-sha")
        action = _coordinate_click_action(
            "child-coordinate",
            bounds=(100, 200, 500, 400),
        )
        self.assertTrue(action.coordinate_only)
        self.assertFalse(action.replayable)
        root_work = self._work(root_capture, [action])
        child_action = InspectionAction(
            action_type="click",
            action_key="child-safe-boundary",
            locator_candidates=[],
            target_meta={"content_desc": "领取权益"},
            risk_type="TEST_BLOCKED",
            blocked_reason="测试安全边界",
            action_role="COMMAND:CLAIM",
            action_role_key="command-claim",
        )
        child_work = StateWork(
            state_id=self.state_id + 1000,
            state_key=child_capture.model.state_key,
            cluster_key=child_capture.model.cluster_key,
            replay_key=child_capture.model.replay_key,
            package_name=child_capture.package_name,
            activity=child_capture.activity,
            screenshot_sha=child_capture.screenshot_sha,
            depth=1,
            path=[],
            actions=[child_action],
            action_map=build_action_map(
                run_id=self.run_id,
                branch_key="authenticated",
                state_id=self.state_id + 1000,
                activity=child_capture.activity,
                screen_size=(1080, 2400),
                actions=[child_action],
            ),
            exploration_mode="FULL",
        )
        publisher = Mock()
        performer = Mock(return_value="coordinate")

        outcome = self._run_branch(
            capture=root_capture,
            work=root_work,
            persist_results=[
                PersistedState(work=root_work, is_new=True),
                PersistedState(work=child_work, is_new=True),
            ],
            publish_mock=publisher,
            perform_mock=performer,
            wait_captures=[root_capture, child_capture, root_capture],
            coverage_scheduler=True,
        )

        performer.assert_called_once()
        self.assertTrue(
            performer.call_args.kwargs["allow_coordinate_discovery"]
        )
        self.assertEqual(outcome.status, "WARNING")
        with Session(self.engine) as session:
            transitions = session.exec(
                select(InspectionTransition).where(
                    InspectionTransition.run_id == self.run_id
                ).order_by(InspectionTransition.sequence)
            ).all()
        self.assertEqual([item.status for item in transitions], ["PASS", "BLOCKED"])
        self.assertEqual(transitions[0].to_state_id, child_work.state_id)
        self.assertTrue(transitions[0].coordinate_only)
        self.assertFalse(transitions[0].replayable)
        self.assertEqual(transitions[1].from_state_id, child_work.state_id)
        self.assertEqual(child_work.frontier_priority, 20)
        self.assertEqual(
            child_work.frontier_reason,
            "COORDINATE_DISCOVERY_HANDOFF",
        )

    def test_coordinate_click_change_resumes_existing_full_child(self):
        """A queued/revisited coordinate page must keep its live capture handoff."""
        root_capture = _capture(
            _page(
                '<node package="com.demo" class="android.widget.Button" '
                'text="订单号 B12345678" clickable="true" enabled="true" '
                'bounds="[100,200][500,400]"/>'
            )
        )
        child_capture = _capture(
            _page(
                '<node package="com.demo" class="android.widget.TextView" '
                'content-desc="已排队坐标页" enabled="true" '
                'bounds="[10,20][500,140]"/>'
            ),
            screenshot_sha="queued-child-sha",
        )
        action = _coordinate_click_action(
            "queued-child-coordinate",
            bounds=(100, 200, 500, 400),
        )
        root_work = self._work(root_capture, [action])
        child_action = InspectionAction(
            action_type="click",
            action_key="queued-child-safe-boundary",
            locator_candidates=[],
            target_meta={"content_desc": "领取权益"},
            risk_type="TEST_BLOCKED",
            blocked_reason="测试安全边界",
            action_role="COMMAND:CLAIM",
            action_role_key="command-claim",
        )
        child_work = StateWork(
            state_id=self.state_id + 1100,
            state_key=child_capture.model.state_key,
            cluster_key=child_capture.model.cluster_key,
            replay_key=child_capture.model.replay_key,
            package_name=child_capture.package_name,
            activity=child_capture.activity,
            screenshot_sha=child_capture.screenshot_sha,
            depth=1,
            path=[],
            actions=[child_action],
            action_map=build_action_map(
                run_id=self.run_id,
                branch_key="authenticated",
                state_id=self.state_id + 1100,
                activity=child_capture.activity,
                screen_size=(1080, 2400),
                actions=[child_action],
            ),
            exploration_mode="FULL",
        )
        performer = Mock(return_value="coordinate")

        outcome = self._run_branch(
            capture=root_capture,
            work=root_work,
            persist_results=[
                PersistedState(work=root_work, is_new=True),
                # The target already exists in the run, but still has pending
                # actions. The current coordinate capture must be trusted.
                PersistedState(work=child_work, is_new=False),
            ],
            publish_mock=Mock(),
            perform_mock=performer,
            wait_captures=[root_capture, child_capture, root_capture],
            coverage_scheduler=True,
        )

        self.assertIn(outcome.status, {"PASS", "WARNING"})
        self.assertEqual(
            [item.status for item in self._transitions()],
            ["PASS", "BLOCKED"],
        )
        performer.assert_called_once()
        self.assertEqual(
            self._transitions()[1].from_state_id,
            child_work.state_id,
        )

    def test_child_finish_stays_clear_until_back_restores_parent_page(self):
        root_xml = _page(
            '<node package="com.demo" class="android.widget.Button" '
            'content-desc="进入详情" clickable="true" enabled="true" '
            'bounds="[10,20][300,140]"/>'
            '<node package="com.demo" class="android.widget.Button" '
            'content-desc="刷新首页" clickable="true" enabled="true" '
            'bounds="[10,180][300,300]"/>'
        )
        child_xml = _page(
            '<node package="com.demo" class="android.widget.TextView" '
            'content-desc="详情页" enabled="true" '
            'bounds="[10,20][300,140]"/>'
        )
        root_capture = _capture(root_xml)
        child_capture = _capture(child_xml, screenshot_sha="child-sha")
        actions = enumerate_actions(
            root_capture.model,
            screen_size=(1080, 2400),
        )
        root_work = self._work(root_capture, actions)
        child_work = StateWork(
            state_id=self.state_id + 1000,
            state_key=child_capture.model.state_key,
            cluster_key=child_capture.model.cluster_key,
            # A distinct State may intentionally share a replay structure.
            # Its controls must still never be drawn over the parent's canvas.
            replay_key=root_work.replay_key,
            semantic_key="distinct-child-state",
            package_name=child_capture.package_name,
            activity=child_capture.activity,
            screenshot_sha=child_capture.screenshot_sha,
            depth=1,
            path=[],
            actions=[],
            action_map=build_action_map(
                run_id=self.run_id,
                branch_key="authenticated",
                state_id=self.state_id + 1000,
                activity=child_capture.activity,
                screen_size=(1080, 2400),
                actions=[],
            ),
        )
        self.assertEqual(root_work.replay_key, child_work.replay_key)
        timeline = []
        registry = InspectionLiveRegistry()
        registry.start_run(self.run_id, "android-1", "RUNNING")
        snapshots = []

        def publish_snapshot(run_id, event_type, **event_patch):
            timeline.append(f"EVENT:{event_type}")
            snapshots.append(
                registry.publish(run_id, event_type, **event_patch)
            )

        publisher = Mock(
            side_effect=publish_snapshot
        )
        device = Mock()
        device.press.side_effect = lambda key: timeline.append(
            f"DEVICE:{str(key).upper()}"
        )

        self._run_branch(
            capture=root_capture,
            work=root_work,
            persist_results=[
                PersistedState(work=root_work, is_new=True),
                PersistedState(work=child_work, is_new=False),
                PersistedState(work=root_work, is_new=False),
            ],
            publish_mock=publisher,
            perform_mock=Mock(return_value="description"),
            wait_captures=[
                root_capture,
                child_capture,
                root_capture,
                root_capture,
            ],
            device=device,
        )

        finished_calls = [
            call
            for call in publisher.call_args_list
            if call.args[1] == "ACTION_FINISHED"
        ]
        self.assertEqual(len(finished_calls), 2)
        first_finished = finished_calls[0]
        self.assertEqual(
            first_finished.kwargs["current_action"]["status"],
            "PASS",
        )
        self.assertFalse(first_finished.kwargs["overlay_visible"])

        first_finished_index = timeline.index("EVENT:ACTION_FINISHED")
        back_index = timeline.index("DEVICE:BACK", first_finished_index + 1)
        restored_page_index = timeline.index(
            "EVENT:PAGE_ACTIONS",
            back_index + 1,
        )
        second_started_index = timeline.index(
            "EVENT:ACTION_STARTED",
            restored_page_index + 1,
        )
        self.assertLess(first_finished_index, back_index)
        self.assertLess(back_index, restored_page_index)
        self.assertLess(restored_page_index, second_started_index)
        restored_page_calls = [
            call
            for call in publisher.call_args_list
            if call.args[1] == "PAGE_ACTIONS"
        ]
        self.assertTrue(restored_page_calls[-1].kwargs["overlay_visible"])
        self.assertTrue(
            all(
                "page" not in call.kwargs
                and "actions" not in call.kwargs
                and "current_action" not in call.kwargs
                and "overlay_visible" not in call.kwargs
                for call in publisher.call_args_list
                if call.args[1] == "FRONTIER_UPDATED"
            )
        )

        owner_snapshots = [
            item
            for item in snapshots
            if item.get("action_panel") is not None
        ]
        self.assertTrue(owner_snapshots)
        first_child_index = next(
            index
            for index, item in enumerate(owner_snapshots)
            if item["action_panel"]["state_id"] == child_work.state_id
        )
        self.assertTrue(
            all(
                item["action_panel"]["state_id"] == root_work.state_id
                for item in owner_snapshots[:first_child_index]
            )
        )
        self.assertEqual(
            owner_snapshots[first_child_index]["event_type"],
            "PAGE_ACTIONS",
        )
        self.assertGreater(
            owner_snapshots[first_child_index]["expansion_epoch"],
            owner_snapshots[0]["expansion_epoch"],
        )

    def test_confirmed_tab_switch_persists_peer_relation_and_same_depth(self):
        navigation = "".join(
            f'<node package="com.demo" class="android.view.ViewGroup" '
            f'content-desc="{label}" clickable="true" enabled="true" '
            f'bounds="[{index * 216},2166][{(index + 1) * 216},2364]"/>'
            for index, label in enumerate(
                ("首页", "分类", "许愿池", "购物车", "我的")
            )
        )
        root_capture = _capture(
            _page(
                '<node package="com.demo" class="android.widget.TextView" '
                'text="首页内容" enabled="true" bounds="[0,200][1080,400]"/>'
                f'<node package="com.demo" class="android.view.ViewGroup" '
                f'enabled="true" bounds="[0,2166][1080,2364]">{navigation}</node>'
            )
        )
        peer_capture = _capture(
            _page(
                '<node package="com.demo" class="android.widget.TextView" '
                'text="许愿池内容" enabled="true" bounds="[0,200][1080,400]"/>'
                f'<node package="com.demo" class="android.view.ViewGroup" '
                f'enabled="true" bounds="[0,2166][1080,2364]">{navigation}</node>'
            ),
            screenshot_sha="peer-sha",
        )
        action = next(
            item
            for item in enumerate_actions(
                root_capture.model,
                screen_size=(1080, 2400),
            )
            if item.target_meta.get("content_desc") == "许愿池"
        )
        self.assertIn("navigation", action.target_meta)
        root_work = self._work(root_capture, [action])
        peer_work = StateWork(
            state_id=self.state_id + 1000,
            state_key=peer_capture.model.state_key,
            cluster_key=peer_capture.model.cluster_key,
            replay_key=peer_capture.model.replay_key,
            package_name=peer_capture.package_name,
            activity=peer_capture.activity,
            screenshot_sha=peer_capture.screenshot_sha,
            depth=0,
            path=[_serialize_action(action)],
            actions=[],
            parent_state_id=None,
        )
        persisted_calls = []
        persisted_results = iter(
            [
                PersistedState(work=root_work, is_new=True),
                PersistedState(
                    work=peer_work,
                    is_new=True,
                    assign_incoming=True,
                ),
            ]
        )

        def persist_state(**kwargs):
            persisted_calls.append(kwargs)
            return next(persisted_results)

        device = Mock()
        outcome = self._run_branch(
            capture=root_capture,
            work=root_work,
            persist_results=persist_state,
            publish_mock=Mock(),
            perform_mock=Mock(return_value="description"),
            wait_captures=[root_capture, peer_capture, root_capture],
            device=device,
        )

        self.assertIn(outcome.status, {"PASS", "WARNING"})
        self.assertEqual(persisted_calls[1]["depth"], 0)
        self.assertIsNone(persisted_calls[1]["parent_state_id"])
        self.assertTrue(persisted_calls[1]["prefer_hierarchy"])
        device.press.assert_called_once_with("back")
        with Session(self.engine) as session:
            transition = session.exec(
                select(InspectionTransition).where(
                    InspectionTransition.run_id == self.run_id
                )
            ).one()
        self.assertEqual(transition.relation_type, "PEER")
        self.assertGreaterEqual(transition.relation_confidence, 0.85)
        self.assertTrue(
            transition.target_meta["navigation"]["confirmation"]["matched"]
        )

    def test_root_bottom_tabs_are_all_captured_before_target_expansion(self):
        labels = ("首页", "分类", "许愿池", "购物车", "我的")

        def page(active_label: str, content: str) -> CapturedPage:
            navigation = "".join(
                f'<node package="com.demo" class="android.view.ViewGroup" '
                f'content-desc="{label}" clickable="true" enabled="true" '
                f'selected="{str(label == active_label).lower()}" '
                f'bounds="[{index * 216},2166]'
                f'[{(index + 1) * 216},2364]"/>'
                for index, label in enumerate(labels)
            )
            return _capture(
                _page(
                    '<node package="com.demo" class="android.widget.TextView" '
                    f'text="{content}" enabled="true" '
                    'bounds="[0,200][1080,400]"/>'
                    '<node package="com.demo" class="android.view.ViewGroup" '
                    'enabled="true" bounds="[0,2166][1080,2364]">'
                    f"{navigation}</node>"
                ),
                screenshot_sha=f"tab-{active_label}",
            )

        root_capture = page("首页", "首页内容")
        category_capture = page("分类", "分类内容")
        wish_capture = page("许愿池", "许愿池内容")
        root_navigation = enumerate_actions(
            root_capture.model,
            screen_size=(1080, 2400),
            coverage_scheduler_v2=True,
            include_current_navigation=True,
        )
        root_actions = [
            action
            for action in root_navigation
            if action.target_meta.get("content_desc") in {"分类", "许愿池"}
        ]
        home_action = next(
            action
            for action in root_navigation
            if action.target_meta.get("content_desc") == "首页"
        )
        root_work = replace(
            self._work(root_capture, root_actions),
            semantic_key=root_capture.model.semantic_key,
            role="HOME",
            page_subtype="HOME",
            exploration_mode="FULL",
            recovery_navigation_actions=[home_action],
        )

        def blocked_action(key: str, label: str) -> InspectionAction:
            return InspectionAction(
                action_type="click",
                action_key=key,
                locator_candidates=[
                    {"by": "description", "selector": label}
                ],
                target_meta={"content_desc": label},
                replayable=True,
                risk_type="TEST_BLOCKED",
                blocked_reason="测试安全拦截",
                action_role=f"COMMAND:{key}",
                action_role_key=f"role-{key}",
            )

        def peer_work(
            capture: CapturedPage,
            state_id: int,
            navigation_action: InspectionAction,
        ) -> StateWork:
            actions = [
                blocked_action(
                    f"business-{state_id}",
                    f"页面动作-{state_id}",
                )
            ]
            return StateWork(
                state_id=state_id,
                state_key=capture.model.state_key,
                cluster_key=capture.model.cluster_key,
                replay_key=capture.model.replay_key,
                semantic_key=capture.model.semantic_key,
                package_name=capture.package_name,
                activity=capture.activity,
                screenshot_sha=capture.screenshot_sha,
                depth=0,
                path=[_serialize_action(navigation_action)],
                actions=actions,
                action_map=build_action_map(
                    run_id=self.run_id,
                    branch_key="authenticated",
                    state_id=state_id,
                    activity=capture.activity,
                    screen_size=(1080, 2400),
                    actions=actions,
                ),
                role=capture.model.role,
                page_subtype=capture.model.page_subtype,
                exploration_mode="FULL",
            )

        category_action = next(
            action
            for action in root_actions
            if action.target_meta.get("content_desc") == "分类"
        )
        wish_action = next(
            action
            for action in root_actions
            if action.target_meta.get("content_desc") == "许愿池"
        )
        category_work = peer_work(
            category_capture,
            self.state_id + 1000,
            category_action,
        )
        wish_work = peer_work(
            wish_capture,
            self.state_id + 2000,
            wish_action,
        )

        def ensure_parent(**kwargs):
            state_id = kwargs["parent"].state_id
            return {
                root_work.state_id: root_capture,
                category_work.state_id: category_capture,
                wish_work.state_id: wish_capture,
            }[state_id]

        performed_labels = []

        def perform_action(_device, action, **_kwargs):
            performed_labels.append(action.target_meta.get("content_desc"))
            return "description"

        performer = Mock(side_effect=perform_action)
        persisted_state_ids = set()

        def persist_state(**kwargs):
            capture = kwargs["capture"]
            work = {
                root_capture.screenshot_sha: root_work,
                category_capture.screenshot_sha: category_work,
                wish_capture.screenshot_sha: wish_work,
            }[capture.screenshot_sha]
            is_new = work.state_id not in persisted_state_ids
            persisted_state_ids.add(work.state_id)
            return PersistedState(
                work=work,
                is_new=is_new,
                assign_incoming=is_new and work.state_id != root_work.state_id,
            )

        def wait_capture(*_args, **_kwargs):
            if not performed_labels:
                return root_capture
            return {
                "分类": category_capture,
                "许愿池": wish_capture,
            }.get(performed_labels[-1], root_capture)

        publisher = Mock()
        outcome = self._run_branch(
            capture=root_capture,
            work=root_work,
            persist_results=persist_state,
            publish_mock=publisher,
            perform_mock=performer,
            wait_captures=wait_capture,
            ensure_captures=ensure_parent,
            family_convergence=True,
            coverage_scheduler=True,
        )

        self.assertIn(outcome.status, {"PASS", "WARNING"})
        self.assertEqual(
            performed_labels,
            ["分类", "首页", "许愿池", "首页"],
        )
        with Session(self.engine) as session:
            transitions = session.exec(
                select(InspectionTransition)
                .where(InspectionTransition.run_id == self.run_id)
                .order_by(InspectionTransition.sequence)
            ).all()
        self.assertEqual(
            [transition.status for transition in transitions],
            ["PASS", "PASS", "BLOCKED", "BLOCKED"],
        )
        self.assertEqual(
            {transition.from_state_id for transition in transitions[2:]},
            {category_work.state_id, wish_work.state_id},
        )
        self.assertEqual(category_work.frontier_priority, 40)
        self.assertEqual(category_work.frontier_reason, "PRIMARY_ENTRY_SURFACE")
        self.assertEqual(wish_work.frontier_priority, 40)
        self.assertEqual(wish_work.frontier_reason, "PRIMARY_ENTRY_SURFACE")
        self.assertTrue(
            any(
                call.args[1] == "PHASE_CHANGED"
                and call.kwargs.get("phase") == "coverage_explore"
                for call in publisher.call_args_list
            )
        )

    def test_scroll_limit_is_shared_across_viewport_local_action_keys(self):
        captures = [
            _capture(
                _page(
                    f'<node package="com.demo" class="android.widget.TextView" '
                    f'text="{label}" enabled="true" bounds="[0,100][1080,300]"/>'
                ),
                screenshot_sha=f"scroll-{index}",
            )
            for index, label in enumerate(
                ("Root alpha", "Viewport bravo", "Viewport charlie", "Viewport delta")
            )
        ]

        def scroll_action(index: int) -> InspectionAction:
            return InspectionAction(
                action_type="scroll",
                action_key=f"viewport-local-scroll-{index}",
                locator_candidates=[],
                target_meta={
                    "direction": "up",
                    "class": "androidx.recyclerview.widget.RecyclerView",
                    "relative_bucket": "content:c0",
                    "bounds": [0, 100, 1080, 2200],
                    "screen_size": [1080, 2400],
                },
                coordinate_only=True,
                replayable=False,
                action_role="SCROLL:vertical:up",
                action_role_key=f"viewport-local-role-{index}",
            )

        actions = [scroll_action(index) for index in range(4)]
        second_region_action = replace(
            scroll_action(99),
            action_key="independent-scroll-region",
            target_meta={
                **scroll_action(99).target_meta,
                "relative_bucket": "content:c2",
                "bounds": [720, 100, 1080, 2200],
            },
        )
        root_work = replace(
            self._work(captures[0], [actions[0]]),
            semantic_key=captures[0].model.semantic_key,
        )
        viewport_works = []
        parent_state_id = self.state_id
        for index in range(1, 4):
            work = self._stored_work(
                captures[index],
                [
                    actions[index],
                    *([second_region_action] if index == 3 else []),
                ],
                depth=0,
                path=[_serialize_action(actions[index - 1])],
                parent_state_id=parent_state_id,
            )
            viewport_works.append(work)
            parent_state_id = work.state_id

        performer = Mock(return_value="scroll:up:coordinate")
        self._run_branch(
            capture=captures[0],
            work=root_work,
            persist_results=[
                PersistedState(work=root_work, is_new=True),
                *(
                    PersistedState(work=work, is_new=True)
                    for work in viewport_works
                ),
                PersistedState(work=viewport_works[-1], is_new=False),
            ],
            publish_mock=Mock(),
            perform_mock=performer,
            wait_captures=[*captures, captures[-1]],
            ensure_captures=[captures[0]],
            max_actions=10,
        )

        self.assertEqual(
            [call.args[1].action_key for call in performer.call_args_list],
            [
                *[action.action_key for action in actions[:3]],
                second_region_action.action_key,
            ],
        )
        with Session(self.engine) as session:
            transitions = session.exec(
                select(InspectionTransition)
                .where(InspectionTransition.run_id == self.run_id)
                .order_by(InspectionTransition.sequence)
            ).all()
        self.assertEqual(
            [item.status for item in transitions],
            ["PASS", "PASS", "PASS", "SKIPPED", "SELF_LOOP"],
        )
        self.assertEqual(transitions[-2].reason, "达到该滚动方向次数上限")

    def test_viewport_handoff_yields_to_discovered_business_page(self):
        root_capture = _capture(
            _page(
                '<node package="com.demo" class="android.widget.TextView" '
                'text="Root alpha" enabled="true" bounds="[0,100][1080,300]"/>'
            ),
            screenshot_sha="fair-root",
        )
        child_capture = _capture(
            _page(
                '<node package="com.demo" class="android.widget.TextView" '
                'text="Business child" enabled="true" bounds="[0,100][1080,300]"/>'
            ),
            screenshot_sha="fair-child",
        )
        viewport_captures = [
            _capture(
                _page(
                    f'<node package="com.demo" class="android.widget.TextView" '
                    f'text="Viewport {label}" enabled="true" '
                    'bounds="[0,100][1080,300]"/>'
                ),
                screenshot_sha=f"fair-viewport-{index}",
            )
            for index, label in enumerate(("bravo", "charlie", "delta"), 1)
        ]
        open_child = InspectionAction(
            action_type="click",
            action_key="open-business-child",
            locator_candidates=[{"by": "description", "selector": "业务页"}],
            target_meta={"content_desc": "业务页"},
        )
        child_action = InspectionAction(
            action_type="click",
            action_key="business-child-action",
            locator_candidates=[{"by": "description", "selector": "刷新业务页"}],
            target_meta={"content_desc": "刷新业务页"},
        )

        def scroll_action(index: int) -> InspectionAction:
            return InspectionAction(
                action_type="scroll",
                action_key=f"fair-scroll-{index}",
                locator_candidates=[],
                target_meta={
                    "direction": "up",
                    "bounds": [0, 100, 1080, 2200],
                    "screen_size": [1080, 2400],
                },
                coordinate_only=True,
                replayable=False,
                action_role="SCROLL:vertical:up",
                action_role_key=f"fair-scroll-role-{index}",
            )

        scroll_actions = [scroll_action(index) for index in range(4)]
        root_work = replace(
            self._work(root_capture, [open_child, scroll_actions[0]]),
            semantic_key=root_capture.model.semantic_key,
        )
        child_work = self._stored_work(
            child_capture,
            [child_action],
            depth=1,
            path=[_serialize_action(open_child)],
            parent_state_id=self.state_id,
        )
        viewport_works = []
        parent_state_id = self.state_id
        for index, capture in enumerate(viewport_captures, 1):
            work = self._stored_work(
                capture,
                [scroll_actions[index]],
                depth=0,
                path=[_serialize_action(scroll_actions[index - 1])],
                parent_state_id=parent_state_id,
            )
            viewport_works.append(work)
            parent_state_id = work.state_id

        performer = Mock(
            side_effect=lambda _device, action, **_kwargs: (
                "scroll:up:coordinate"
                if action.action_type == "scroll"
                else "description"
            )
        )
        with patch(
            "backend.inspection.engine._restore_parent_after_transition",
            return_value=root_capture,
        ):
            self._run_branch(
                capture=root_capture,
                work=root_work,
                persist_results=[
                    PersistedState(work=root_work, is_new=True),
                    PersistedState(work=child_work, is_new=True),
                    *(
                        PersistedState(work=work, is_new=True)
                        for work in viewport_works
                    ),
                    PersistedState(work=child_work, is_new=False),
                ],
                publish_mock=Mock(),
                perform_mock=performer,
                wait_captures=[
                    root_capture,
                    child_capture,
                    *viewport_captures,
                    child_capture,
                ],
                ensure_captures=[
                    root_capture,
                    child_capture,
                    viewport_captures[-1],
                ],
                max_actions=10,
            )

        self.assertEqual(
            [call.args[1].action_key for call in performer.call_args_list],
            [
                open_child.action_key,
                scroll_actions[0].action_key,
                scroll_actions[1].action_key,
                scroll_actions[2].action_key,
                child_action.action_key,
            ],
        )
        with Session(self.engine) as session:
            transitions = session.exec(
                select(InspectionTransition)
                .where(InspectionTransition.run_id == self.run_id)
                .order_by(InspectionTransition.sequence)
            ).all()
        self.assertEqual(
            [item.action_key for item in transitions],
            [
                open_child.action_key,
                scroll_actions[0].action_key,
                scroll_actions[1].action_key,
                scroll_actions[2].action_key,
                child_action.action_key,
                scroll_actions[3].action_key,
            ],
        )
        self.assertEqual(transitions[-1].status, "SKIPPED")

    def test_tab_first_seen_after_scroll_uses_business_owner_hierarchy(self):
        def navigation(active_label: str) -> str:
            return "".join(
                f'<node package="com.demo" class="android.view.ViewGroup" '
                f'content-desc="{label}" clickable="true" enabled="true" '
                f'selected="{str(label == active_label).lower()}" '
                f'bounds="[{index * 216},2166][{(index + 1) * 216},2364]"/>'
                for index, label in enumerate(
                    ("首页", "分类", "许愿池", "购物车", "我的")
                )
            )
        root_capture = _capture(
            _page(
                '<node package="com.demo" class="android.widget.ScrollView" '
                'scrollable="true" enabled="true" bounds="[0,100][1080,2200]">'
                '<node package="com.demo" class="android.widget.TextView" '
                'text="首页上半屏" enabled="true" bounds="[0,200][1080,400]"/>'
                "</node>"
            )
        )
        viewport_capture = _capture(
            _page(
                '<node package="com.demo" class="android.widget.TextView" '
                'text="首页下半屏" enabled="true" bounds="[0,200][1080,400]"/>'
                '<node package="com.demo" class="android.view.ViewGroup" '
                f'enabled="true" bounds="[0,2166][1080,2364]">'
                f'{navigation("首页")}</node>'
            ),
            screenshot_sha="viewport-sha",
        )
        peer_capture = _capture(
            _page(
                '<node package="com.demo" class="android.widget.TextView" '
                'text="许愿池内容" enabled="true" bounds="[0,200][1080,400]"/>'
                '<node package="com.demo" class="android.view.ViewGroup" '
                f'enabled="true" bounds="[0,2166][1080,2364]">'
                f'{navigation("许愿池")}</node>'
            ),
            screenshot_sha="peer-after-scroll-sha",
        )
        scroll_action = InspectionAction(
            action_type="scroll",
            action_key="root-scroll-down",
            locator_candidates=[],
            target_meta={"direction": "down", "bounds": [0, 100, 1080, 2200]},
        )
        tab_action = next(
            item
            for item in enumerate_actions(
                viewport_capture.model,
                screen_size=(1080, 2400),
                include_current_navigation=True,
            )
            if item.target_meta.get("content_desc") == "许愿池"
        )
        home_action = next(
            item
            for item in enumerate_actions(
                viewport_capture.model,
                screen_size=(1080, 2400),
                include_current_navigation=True,
            )
            if item.target_meta.get("content_desc") == "首页"
        )
        scroll_path = [_serialize_action(scroll_action)]
        root_work = self._work(root_capture, [scroll_action])
        viewport_work = StateWork(
            state_id=self.state_id + 1000,
            state_key=viewport_capture.model.state_key,
            cluster_key=viewport_capture.model.cluster_key,
            replay_key=viewport_capture.model.replay_key,
            package_name=viewport_capture.package_name,
            activity=viewport_capture.activity,
            screenshot_sha=viewport_capture.screenshot_sha,
            depth=0,
            path=scroll_path,
            actions=[tab_action],
            recovery_navigation_actions=[home_action],
            action_map=build_action_map(
                run_id=self.run_id,
                branch_key="authenticated",
                state_id=self.state_id + 1000,
                activity=viewport_capture.activity,
                screen_size=(1080, 2400),
                actions=[tab_action],
            ),
            parent_state_id=self.state_id,
        )
        peer_work = StateWork(
            state_id=self.state_id + 2000,
            state_key=peer_capture.model.state_key,
            cluster_key=peer_capture.model.cluster_key,
            replay_key=peer_capture.model.replay_key,
            package_name=peer_capture.package_name,
            activity=peer_capture.activity,
            screenshot_sha=peer_capture.screenshot_sha,
            depth=0,
            path=[*scroll_path, _serialize_action(tab_action)],
            actions=[],
            action_map=build_action_map(
                run_id=self.run_id,
                branch_key="authenticated",
                state_id=self.state_id + 2000,
                activity=peer_capture.activity,
                screen_size=(1080, 2400),
                actions=[],
            ),
            parent_state_id=None,
        )
        persisted_calls = []
        persisted_results = iter(
            [
                PersistedState(work=root_work, is_new=True),
                PersistedState(work=viewport_work, is_new=True),
                PersistedState(work=peer_work, is_new=True),
            ]
        )

        def persist_state(**kwargs):
            persisted_calls.append(kwargs)
            return next(persisted_results)

        device = Mock()
        performer = Mock(return_value="description")
        prepare = Mock()
        outcome = self._run_branch(
            capture=root_capture,
            work=root_work,
            persist_results=persist_state,
            publish_mock=Mock(),
            perform_mock=performer,
            wait_captures=[
                root_capture,
                viewport_capture,
                peer_capture,
                root_capture,
                viewport_capture,
            ],
            ensure_captures=[root_capture, viewport_capture, peer_capture],
            prepare_mock=prepare,
            # Four exploration actions plus the reserved validation share.
            max_actions=5,
            device=device,
        )

        self.assertIn(outcome.status, {"PASS", "WARNING"})
        self.assertEqual(persisted_calls[1]["depth"], 0)
        self.assertEqual(persisted_calls[1]["parent_state_id"], self.state_id)
        self.assertEqual(persisted_calls[2]["depth"], 0)
        self.assertIsNone(persisted_calls[2]["parent_state_id"])
        self.assertTrue(persisted_calls[2]["prefer_hierarchy"])
        self.assertEqual(
            [call.args[1].action_key for call in performer.call_args_list],
            [
                scroll_action.action_key,
                tab_action.action_key,
                home_action.action_key,
                scroll_action.action_key,
            ],
        )
        prepare.assert_called_once()
        device.press.assert_not_called()
        with Session(self.engine) as session:
            transitions = session.exec(
                select(InspectionTransition)
                .where(InspectionTransition.run_id == self.run_id)
                .order_by(InspectionTransition.sequence)
            ).all()
        self.assertEqual(
            [item.relation_type for item in transitions],
            ["VIEWPORT", "PEER"],
        )
        self.assertEqual([item.status for item in transitions], ["PASS", "PASS"])

    def test_path_divergence_is_single_failure_and_does_not_poison_family(self):
        capture = _capture(
            _page(
                '<node package="com.demo" class="android.widget.TextView" '
                'text="动态首页" enabled="true" bounds="[0,100][1080,300]"/>'
            )
        )
        actions = [
            InspectionAction(
                action_type="click",
                action_key=f"path-action-{index}",
                locator_candidates=[{"by": "description", "selector": label}],
                target_meta={"content_desc": label},
                action_role=f"NAV:{index}",
                action_role_key=f"family-role-{index}",
            )
            for index, label in enumerate(("动作一", "动作二", "动作三"), 1)
        ]
        work = replace(
            self._work(capture, actions),
            recovery_status="PATH_DIVERGED",
            exploration_family_id=1,
        )
        with Session(self.engine) as session:
            family = InspectionExplorationFamily(
                id=1,
                run_id=self.run_id,
                branch_run_id=self.branch_id,
                family_key="home-family",
                page_role="HOME",
            )
            session.add(family)
            state = session.get(InspectionState, self.state_id)
            state.exploration_family_id = 1
            for action in actions:
                session.add(
                    InspectionFamilyActionCoverage(
                        family_id=1,
                        action_role_key=action.action_role_key,
                        action_role=action.action_role,
                        status="SUCCESS",
                        source_state_id=self.state_id,
                    )
                )
            session.commit()

        outcome = self._run_branch(
            capture=capture,
            work=work,
            persist_results=[PersistedState(work=work, is_new=True)],
            publish_mock=Mock(),
            perform_mock=Mock(),
            wait_captures=[capture],
            ensure_captures=[None],
            family_convergence=True,
        )

        self.assertEqual(outcome.status, "WARNING")
        with Session(self.engine) as session:
            transitions = session.exec(
                select(InspectionTransition)
                .where(InspectionTransition.run_id == self.run_id)
                .order_by(InspectionTransition.sequence)
            ).all()
            coverage = session.exec(
                select(InspectionFamilyActionCoverage).order_by(
                    InspectionFamilyActionCoverage.id
                )
            ).all()
            state = session.get(InspectionState, self.state_id)
        self.assertEqual(
            [item.status for item in transitions],
            ["PATH_DIVERGED", "QUEUE_TRUNCATED", "QUEUE_TRUNCATED"],
        )
        self.assertEqual(
            sum(item.status == "PATH_DIVERGED" for item in transitions),
            1,
        )
        self.assertTrue(
            all("重试已耗尽" not in str(item.reason or "") for item in transitions)
        )
        self.assertEqual(
            [item.recovery_attempt_count for item in transitions],
            [0, 0, 0],
        )
        self.assertEqual({item.status for item in coverage}, {"SUCCESS"})
        self.assertEqual(state.recovery_retry_count, 0)

    def test_successful_deferred_recovery_resets_retry_counter(self):
        capture = _capture(
            _page(
                '<node package="com.demo" class="android.widget.Button" '
                'content-desc="刷新" clickable="true" enabled="true" '
                'bounds="[10,20][300,140]"/>'
            )
        )
        action = enumerate_actions(capture.model, screen_size=(1080, 2400))[0]
        work = self._work(capture, [action])
        publisher = Mock()
        performer = Mock(return_value="description")

        self._run_branch(
            capture=capture,
            work=work,
            persist_results=[
                PersistedState(work=work, is_new=True),
                PersistedState(work=work, is_new=False),
            ],
            publish_mock=publisher,
            perform_mock=performer,
            wait_captures=[capture, capture],
            ensure_captures=[None, capture],
        )

        performer.assert_called_once()
        event_types = [call.args[1] for call in publisher.call_args_list]
        self.assertIn("ACTION_DEFERRED", event_types)
        self.assertIn("ACTION_RESUMED", event_types)
        with Session(self.engine) as session:
            state = session.get(InspectionState, self.state_id)
        self.assertEqual(state.recovery_retry_count, 0)
        self.assertEqual(state.expansion_status, "EXPANDED")

    def test_failed_parent_does_not_stop_viewport_handoff_or_continuation(self):
        root_capture = _capture(
            _page(
                '<node package="com.demo" class="android.widget.TextView" '
                'text="Root" enabled="true" bounds="[0,100][1080,300]"/>'
            )
        )
        failed_capture = _capture(
            _page(
                '<node package="com.demo" class="android.widget.TextView" '
                'text="A" enabled="true" bounds="[0,100][1080,300]"/>'
            ),
            screenshot_sha="failed-sha",
        )
        parent_capture = _capture(
            _page(
                '<node package="com.demo" class="android.widget.ScrollView" '
                'scrollable="true" enabled="true" bounds="[0,100][1080,2200]">'
                '<node package="com.demo" class="android.widget.TextView" '
                'text="B" enabled="true" bounds="[0,200][1080,400]"/>'
                "</node>"
            ),
            screenshot_sha="parent-sha",
        )
        viewport_capture = _capture(
            _page(
                '<node package="com.demo" class="android.widget.ScrollView" '
                'scrollable="true" enabled="true" bounds="[0,100][1080,2200]">'
                '<node package="com.demo" class="android.widget.TextView" '
                'text="C" enabled="true" bounds="[0,1500][1080,1700]"/>'
                "</node>"
            ),
            screenshot_sha="viewport-sha",
        )

        def click_action(key, label, *, coordinate_only=False):
            return InspectionAction(
                action_type="click",
                action_key=key,
                locator_candidates=(
                    []
                    if coordinate_only
                    else [{"by": "description", "selector": label}]
                ),
                target_meta={
                    "content_desc": label,
                    "bounds": [20, 200, 400, 360],
                    "screen_size": [1080, 2400],
                    "coordinate_authorized": coordinate_only,
                },
                coordinate_only=coordinate_only,
                replayable=not coordinate_only,
            )

        to_failed = click_action("to-a", "A")
        to_parent = click_action("to-b", "B")
        failed_action = click_action("a-action", "A action")
        parent_scroll = InspectionAction(
            action_type="scroll",
            action_key="b-scroll",
            locator_candidates=[],
            target_meta={
                "direction": "up",
                "bounds": [0, 100, 1080, 2200],
                "screen_size": [1080, 2400],
            },
            coordinate_only=True,
            replayable=False,
        )
        parent_remaining = click_action(
            "b-remaining",
            "B remaining",
            coordinate_only=True,
        )
        viewport_action = click_action(
            "c-action",
            "C action",
            coordinate_only=True,
        )

        with Session(self.engine) as session:
            def insert_state(capture, *, depth, parent_state_id):
                state = InspectionState(
                    run_id=self.run_id,
                    branch_run_id=self.branch_id,
                    branch_key="authenticated",
                    cluster_key=capture.model.cluster_key,
                    state_key=capture.model.state_key,
                    activity=capture.activity,
                    foreground_package=capture.package_name,
                    screenshot_sha=capture.screenshot_sha,
                    stable_status="UNVERIFIED",
                    depth=depth,
                    parent_state_id=parent_state_id,
                )
                session.add(state)
                session.flush()
                return int(state.id)

            failed_state_id = insert_state(
                failed_capture,
                depth=1,
                parent_state_id=self.state_id,
            )
            parent_state_id = insert_state(
                parent_capture,
                depth=1,
                parent_state_id=self.state_id,
            )
            viewport_state_id = insert_state(
                viewport_capture,
                depth=1,
                parent_state_id=parent_state_id,
            )
            session.commit()

        def make_work(
            state_id,
            capture,
            actions,
            *,
            depth,
            path,
            parent_state_id,
        ):
            return StateWork(
                state_id=state_id,
                state_key=capture.model.state_key,
                cluster_key=capture.model.cluster_key,
                replay_key=capture.model.replay_key,
                package_name=capture.package_name,
                activity=capture.activity,
                screenshot_sha=capture.screenshot_sha,
                depth=depth,
                path=list(path),
                actions=list(actions),
                action_map=build_action_map(
                    run_id=self.run_id,
                    branch_key="authenticated",
                    state_id=state_id,
                    activity=capture.activity,
                    screen_size=(1080, 2400),
                    actions=actions,
                ),
                parent_state_id=parent_state_id,
            )

        root_work = self._work(root_capture, [to_failed, to_parent])
        failed_work = make_work(
            failed_state_id,
            failed_capture,
            [failed_action],
            depth=1,
            path=[_serialize_action(to_failed)],
            parent_state_id=self.state_id,
        )
        parent_work = make_work(
            parent_state_id,
            parent_capture,
            [parent_scroll, parent_remaining],
            depth=1,
            path=[_serialize_action(to_parent)],
            parent_state_id=self.state_id,
        )
        viewport_work = make_work(
            viewport_state_id,
            viewport_capture,
            [viewport_action],
            depth=1,
            path=[
                _serialize_action(to_parent),
                _serialize_action(parent_scroll),
            ],
            parent_state_id=parent_state_id,
        )

        ensure_state_ids = []
        ensure_budget_guards = []

        def ensure_parent(**kwargs):
            state_id = kwargs["parent"].state_id
            ensure_state_ids.append(state_id)
            ensure_budget_guards.append(kwargs.get("budget_guard"))
            if state_id == self.state_id:
                return root_capture
            if state_id == failed_state_id:
                return None
            if state_id == parent_state_id:
                return parent_capture
            self.fail("viewport must reuse the handed-off capture")

        source_state_by_action = {
            to_failed.action_key: self.state_id,
            to_parent.action_key: self.state_id,
            parent_scroll.action_key: parent_state_id,
            parent_remaining.action_key: parent_state_id,
            viewport_action.action_key: viewport_state_id,
        }

        def perform_action_side_effect(_device, action, **_kwargs):
            with Session(self.engine) as session:
                source = session.get(
                    InspectionState,
                    source_state_by_action[action.action_key],
                )
                self.assertIsNone(source.expanded_at)
            return (
                "scroll:up:coordinate"
                if action.action_type == "scroll"
                else "coordinate"
                if action.coordinate_only
                else "description"
            )

        performer = Mock(side_effect=perform_action_side_effect)
        publisher = Mock()
        with patch(
            "backend.inspection.engine._restore_parent_after_transition",
            return_value=root_capture,
        ):
            outcome = self._run_branch(
                capture=root_capture,
                work=root_work,
                persist_results=[
                    PersistedState(work=root_work, is_new=True),
                    PersistedState(work=failed_work, is_new=True),
                    PersistedState(work=parent_work, is_new=True),
                    PersistedState(work=viewport_work, is_new=True),
                ],
                publish_mock=publisher,
                perform_mock=performer,
                wait_captures=[
                    root_capture,
                    failed_capture,
                    parent_capture,
                    viewport_capture,
                    viewport_capture,
                    parent_capture,
                ],
                ensure_captures=ensure_parent,
                max_actions=10,
            )

        self.assertEqual(outcome.status, "WARNING")
        self.assertEqual(outcome.stop_reason, "队列自然耗尽")
        event_types = [call.args[1] for call in publisher.call_args_list]
        self.assertIn("FRONTIER_UPDATED", event_types)
        self.assertIn("PHASE_CHANGED", event_types)
        self.assertIn("ACTION_DEFERRED", event_types)
        self.assertIn("ACTION_RESUMED", event_types)
        self.assertEqual(
            [call.args[1].action_key for call in performer.call_args_list],
            [
                to_failed.action_key,
                to_parent.action_key,
                parent_scroll.action_key,
                viewport_action.action_key,
                parent_remaining.action_key,
            ],
        )
        self.assertEqual(
            ensure_state_ids,
            [
                self.state_id,
                failed_state_id,
                parent_state_id,
                parent_state_id,
                failed_state_id,
            ],
        )
        self.assertTrue(all(guard is not None for guard in ensure_budget_guards))
        self.assertEqual(len({id(guard) for guard in ensure_budget_guards}), 1)

        with Session(self.engine) as session:
            transitions = session.exec(
                select(InspectionTransition)
                .where(InspectionTransition.run_id == self.run_id)
                .order_by(InspectionTransition.sequence)
            ).all()
            states = {
                state_id: session.get(InspectionState, state_id)
                for state_id in (
                    self.state_id,
                    failed_state_id,
                    parent_state_id,
                    viewport_state_id,
                )
            }

        self.assertEqual(
            [item.status for item in transitions],
            [
                "PASS",
                "PASS",
                "PASS",
                "NO_EFFECT",
                "NO_EFFECT",
                "PARENT_RECOVERY_FAILED",
            ],
        )
        self.assertEqual(
            [item.from_state_id for item in transitions],
            [
                self.state_id,
                self.state_id,
                parent_state_id,
                viewport_state_id,
                parent_state_id,
                failed_state_id,
            ],
        )
        self.assertEqual(transitions[2].relation_type, "VIEWPORT")
        self.assertEqual(transitions[2].to_state_id, viewport_state_id)

        for state_id, work in (
            (self.state_id, root_work),
            (failed_state_id, failed_work),
            (parent_state_id, parent_work),
            (viewport_state_id, viewport_work),
        ):
            state = states[state_id]
            self.assertIsNotNone(state.expanded_at)
            state_transitions = [
                item for item in transitions if item.from_state_id == state_id
            ]
            self.assertGreaterEqual(
                state.expanded_at,
                max(item.created_at for item in state_transitions),
            )
            self.assertFalse(
                {
                    str(item.get("status") or "").upper()
                    for item in work.action_map.get("actions") or []
                }
                & {"PENDING", "ACTIVE", "NOT_REACHED"}
            )

    def test_initial_active_indicator_learns_direct_tab_return(self):
        def top_tabs(indicator_x: int) -> str:
            return (
                '<node package="com.demo" class="android.view.ViewGroup" '
                'enabled="true" bounds="[180,100][900,260]">'
                '<node package="com.demo" class="android.view.ViewGroup" '
                'content-desc="首页" clickable="true" enabled="true" '
                'bounds="[180,120][540,240]"/>'
                f'<node package="com.demo" class="android.view.ViewGroup" '
                f'enabled="true" bounds="[{indicator_x},225][{indicator_x + 120},233]"/>'
                '<node package="com.demo" class="android.view.ViewGroup" '
                'content-desc="附近门店" clickable="true" enabled="true" '
                'bounds="[540,120][900,240]"/>'
                "</node>"
            )

        root_capture = _capture(
            _page(
                '<node package="com.demo" class="android.widget.TextView" '
                'text="首页内容" enabled="true" bounds="[0,400][1080,600]"/>'
                f"{top_tabs(300)}"
            )
        )
        peer_capture = _capture(
            _page(
                '<node package="com.demo" class="android.widget.TextView" '
                'text="门店内容" enabled="true" bounds="[0,400][1080,600]"/>'
                f"{top_tabs(660)}"
            ),
            screenshot_sha="nearby-sha",
        )
        actions = enumerate_actions(
            root_capture.model,
            screen_size=(1080, 2400),
            include_current_navigation=True,
        )
        action = next(
            item
            for item in actions
            if item.target_meta.get("content_desc") == "附近门店"
        )
        home_action = next(
            item
            for item in actions
            if item.target_meta.get("content_desc") == "首页"
        )
        self.assertTrue(
            home_action.target_meta["navigation"]["member"]["active"]
        )
        root_work = replace(
            self._work(root_capture, [action]),
            recovery_navigation_actions=[home_action],
        )
        peer_work = StateWork(
            state_id=self.state_id + 1000,
            state_key=peer_capture.model.state_key,
            cluster_key=peer_capture.model.cluster_key,
            replay_key=peer_capture.model.replay_key,
            package_name=peer_capture.package_name,
            activity=peer_capture.activity,
            screenshot_sha=peer_capture.screenshot_sha,
            depth=0,
            path=[_serialize_action(action)],
            actions=[],
        )
        persisted_results = iter(
            [
                PersistedState(work=root_work, is_new=True),
                PersistedState(work=peer_work, is_new=True),
            ]
        )

        def persist_state(**_kwargs):
            return next(persisted_results)

        performer = Mock(return_value="description")
        device = Mock()
        self._run_branch(
            capture=root_capture,
            work=root_work,
            persist_results=persist_state,
            publish_mock=Mock(),
            perform_mock=performer,
            wait_captures=[
                root_capture,
                peer_capture,
                root_capture,
            ],
            device=device,
        )

        self.assertEqual(performer.call_count, 2)
        self.assertEqual(performer.call_args_list[-1].args[1].action_key, home_action.action_key)
        device.press.assert_not_called()

    def test_three_page_cycle_records_cycle_back_without_reexpanding_root(self):
        captures = []
        actions = []
        for label, target in (("A", "B"), ("B", "C"), ("C", "A")):
            capture = _capture(
                _page(
                    '<node package="com.demo" class="android.widget.Button" '
                    f'content-desc="{label} to {target}" clickable="true" '
                    'enabled="true" bounds="[10,20][400,160]"/>'
                ),
                screenshot_sha=f"sha-{label}",
            )
            captures.append(capture)
            actions.append(
                enumerate_actions(capture.model, screen_size=(1080, 2400))[0]
            )

        root_work = self._work(captures[0], [actions[0]])

        def child_work(index, state_id, path):
            return StateWork(
                state_id=state_id,
                state_key=captures[index].model.state_key,
                cluster_key=captures[index].model.cluster_key,
                replay_key=captures[index].model.replay_key,
                package_name=captures[index].package_name,
                activity=captures[index].activity,
                screenshot_sha=captures[index].screenshot_sha,
                depth=index,
                path=list(path),
                actions=[actions[index]],
                action_map=build_action_map(
                    run_id=self.run_id,
                    branch_key="authenticated",
                    state_id=state_id,
                    activity=captures[index].activity,
                    screen_size=(1080, 2400),
                    actions=[actions[index]],
                ),
                parent_state_id=(
                    self.state_id if index == 1 else self.state_id + 1000
                ),
            )

        b_work = child_work(
            1,
            self.state_id + 1000,
            [_serialize_action(actions[0])],
        )
        c_work = child_work(
            2,
            self.state_id + 2000,
            [_serialize_action(actions[0]), _serialize_action(actions[1])],
        )
        performer = Mock(return_value="description")

        outcome = self._run_branch(
            capture=captures[0],
            work=root_work,
            persist_results=[
                PersistedState(work=root_work, is_new=True),
                PersistedState(work=b_work, is_new=True),
                PersistedState(work=c_work, is_new=True),
                PersistedState(work=root_work, is_new=False),
            ],
            publish_mock=Mock(),
            perform_mock=performer,
            wait_captures=[
                captures[0],
                captures[1],
                captures[0],
                captures[2],
                captures[1],
                captures[0],
                captures[2],
            ],
            ensure_captures=captures,
            device=Mock(),
            max_actions=10,
        )

        self.assertIn(outcome.status, {"PASS", "WARNING"})
        self.assertEqual(performer.call_count, 3)
        with Session(self.engine) as session:
            transitions = session.exec(
                select(InspectionTransition)
                .where(InspectionTransition.run_id == self.run_id)
                .order_by(InspectionTransition.sequence)
            ).all()
        self.assertEqual(
            [item.topology_type for item in transitions],
            ["TREE", "TREE", "CYCLE_BACK"],
        )
        self.assertTrue(transitions[-1].target_was_existing)
        self.assertEqual(transitions[-1].to_state_id, root_work.state_id)

    def test_repeated_two_edge_family_cycle_converges_target_frontier(self):
        root_capture = _capture(
            _page(
                '<node package="com.demo" class="android.widget.Button" '
                'content-desc="继续" clickable="true" enabled="true" '
                'bounds="[10,20][400,160]"/>'
            ),
            screenshot_sha="cycle-root",
        )
        target_capture = _capture(
            _page(
                '<node package="com.demo" class="android.widget.Button" '
                'content-desc="返回" clickable="true" enabled="true" '
                'bounds="[10,20][400,160]"/>'
            ),
            screenshot_sha="cycle-target",
        )
        root_action = replace(
            enumerate_actions(root_capture.model, screen_size=(1080, 2400))[0],
            action_role="NAV:cycle-b",
            action_role_key="cycle-b",
        )
        root_work = replace(
            self._work(root_capture, [root_action]),
            exploration_family_id=2,
            family_action_trail=(
                (1, "cycle-a", True),
                (2, "cycle-b", True),
                (1, "cycle-a", False),
            ),
        )
        with Session(self.engine) as session:
            target_state = InspectionState(
                run_id=self.run_id,
                branch_run_id=self.branch_id,
                branch_key="authenticated",
                cluster_key=target_capture.model.cluster_key,
                state_key=target_capture.model.state_key,
                semantic_key=target_capture.model.semantic_key,
                activity=target_capture.activity,
                foreground_package=target_capture.package_name,
                screenshot_sha=target_capture.screenshot_sha,
                stable_status="STABLE",
                expansion_status="EXPANDED",
                depth=1,
                parent_state_id=self.state_id,
            )
            session.add(target_state)
            session.commit()
            session.refresh(target_state)
            target_state_id = int(target_state.id)
        target_work = StateWork(
            state_id=target_state_id,
            state_key=target_capture.model.state_key,
            cluster_key=target_capture.model.cluster_key,
            replay_key=target_capture.model.replay_key,
            package_name=target_capture.package_name,
            activity=target_capture.activity,
            screenshot_sha=target_capture.screenshot_sha,
            depth=1,
            path=[_serialize_action(root_action)],
            actions=[
                replace(
                    root_action,
                    action_key="new-delta-action",
                    action_role="NAV:new-delta",
                    action_role_key="new-delta",
                )
            ],
            action_map=build_action_map(
                run_id=self.run_id,
                branch_key="authenticated",
                state_id=target_state_id,
                activity=target_capture.activity,
                screen_size=(1080, 2400),
                actions=[
                    replace(
                        root_action,
                        action_key="new-delta-action",
                        action_role="NAV:new-delta",
                        action_role_key="new-delta",
                    )
                ],
            ),
            parent_state_id=self.state_id,
            exploration_family_id=1,
        )
        performer = Mock(return_value="description")

        outcome = self._run_branch(
            capture=root_capture,
            work=root_work,
            persist_results=[
                PersistedState(work=root_work, is_new=True),
                PersistedState(work=target_work, is_new=True),
            ],
            publish_mock=Mock(),
            perform_mock=performer,
            wait_captures=[root_capture, target_capture, root_capture],
            ensure_captures=[root_capture],
        )

        self.assertIn(outcome.status, {"PASS", "WARNING"})
        performer.assert_called_once()
        with Session(self.engine) as session:
            transition = session.exec(
                select(InspectionTransition).where(
                    InspectionTransition.run_id == self.run_id
                )
            ).one()
            target_state = session.get(InspectionState, target_state_id)
        self.assertEqual(transition.status, "CYCLE_CONVERGED")
        self.assertEqual(transition.topology_type, "TREE")
        self.assertEqual(target_state.expansion_status, "ABORTED")
        self.assertEqual(
            target_work.action_map["actions"][0]["status"],
            "QUEUE_TRUNCATED",
        )

    def test_existing_state_is_requeued_only_for_new_action_keys(self):
        xml = _page(
            '<node package="com.demo" class="android.widget.Button" '
            'content-desc="刷新" clickable="true" enabled="true" '
            'bounds="[10,20][300,140]"/>'
            '<node package="com.demo" class="android.widget.Button" '
            'content-desc="新入口" clickable="true" enabled="true" '
            'bounds="[10,180][300,300]"/>'
        )
        capture = _capture(xml)
        all_actions = enumerate_actions(
            capture.model,
            screen_size=(1080, 2400),
        )
        root_work = self._work(capture, [all_actions[0]])
        updated_work = self._work(capture, all_actions)
        performer = Mock(return_value="description")

        self._run_branch(
            capture=capture,
            work=root_work,
            persist_results=[
                PersistedState(work=root_work, is_new=True),
                PersistedState(work=updated_work, is_new=False),
                PersistedState(work=updated_work, is_new=False),
            ],
            publish_mock=Mock(),
            perform_mock=performer,
            wait_captures=[capture, capture, capture],
            ensure_captures=[capture, capture],
            max_actions=10,
        )

        self.assertEqual(
            [call.args[1].action_key for call in performer.call_args_list],
            [all_actions[0].action_key, all_actions[1].action_key],
        )
        with Session(self.engine) as session:
            transitions = session.exec(
                select(InspectionTransition)
                .where(InspectionTransition.run_id == self.run_id)
                .order_by(InspectionTransition.sequence)
            ).all()
        self.assertEqual(
            [item.topology_type for item in transitions],
            ["SELF_LOOP", "SELF_LOOP"],
        )

    def test_persistence_budget_exhaustion_records_terminal_transition(self):
        xml = _page(
            '<node package="com.demo" class="android.widget.Button" '
            'content-desc="下一页" clickable="true" enabled="true" '
            'bounds="[10,20][300,140]"/>'
        )
        capture = _capture(xml)
        action = enumerate_actions(
            capture.model,
            screen_size=(1080, 2400),
        )[0]
        work = self._work(capture, [action])

        outcome = self._run_branch(
            capture=capture,
            work=work,
            persist_results=[
                PersistedState(work=work, is_new=True),
                BudgetExceeded("STATES"),
            ],
            publish_mock=Mock(),
            perform_mock=Mock(return_value="description"),
            wait_captures=[capture, capture],
        )

        self.assertEqual(outcome.status, "WARNING")
        self.assertEqual(outcome.stop_reason, "达到状态预算")
        with Session(self.engine) as session:
            transition = session.exec(
                select(InspectionTransition).where(
                    InspectionTransition.run_id == self.run_id
                )
            ).one()
        self.assertEqual(transition.status, "BUDGET_LIMIT")
        self.assertEqual(transition.topology_type, "TERMINAL")
        self.assertIsNone(transition.to_state_id)

    def test_budget_finalizes_remaining_actions_and_state_frontier(self):
        capture = _capture(
            _page(
                '<node package="com.demo" class="android.widget.Button" '
                'content-desc="动作一" clickable="true" enabled="true" '
                'bounds="[10,20][300,140]"/>'
                '<node package="com.demo" class="android.widget.Button" '
                'content-desc="动作二" clickable="true" enabled="true" '
                'bounds="[10,180][300,300]"/>'
            )
        )
        actions = enumerate_actions(capture.model, screen_size=(1080, 2400))
        work = self._work(capture, actions)

        outcome = self._run_branch(
            capture=capture,
            work=work,
            persist_results=[
                PersistedState(work=work, is_new=True),
                BudgetExceeded("STATES"),
            ],
            publish_mock=Mock(),
            perform_mock=Mock(return_value="description"),
            wait_captures=[capture, capture],
        )

        self.assertEqual(outcome.stop_reason, "达到状态预算")
        self.assertEqual(
            [item["status"] for item in work.action_map["actions"]],
            ["BUDGET_LIMIT", "BUDGET_NOT_REACHED"],
        )
        with Session(self.engine) as session:
            state = session.get(InspectionState, self.state_id)
        self.assertEqual(state.expansion_status, "BUDGET_SKIPPED")
        self.assertEqual(state.pending_action_count, 0)

    def test_single_page_scope_prunes_cross_page_targets_but_keeps_viewports(self):
        root_capture = _capture(
            _page(
                '<node package="com.demo" class="android.widget.Button" '
                'content-desc="进入详情" clickable="true" enabled="true" '
                'bounds="[10,20][300,140]"/>'
            ),
            screenshot_sha="scope-root",
        )
        child_capture = _capture(
            _page(
                '<node package="com.demo" class="android.widget.Button" '
                'content-desc="详情页动作" clickable="true" enabled="true" '
                'bounds="[10,20][300,140]"/>'
            ),
            screenshot_sha="scope-child",
        )
        viewport_capture = _capture(
            _page(
                '<node package="com.demo" class="android.widget.TextView" '
                'text="第二屏" enabled="true" bounds="[0,100][1080,300]"/>'
            ),
            screenshot_sha="scope-viewport",
        )
        open_child = InspectionAction(
            action_type="click",
            action_key="scope-open-child",
            locator_candidates=[{"by": "description", "selector": "进入详情"}],
            target_meta={"content_desc": "进入详情"},
        )
        child_action = InspectionAction(
            action_type="click",
            action_key="scope-child-action",
            locator_candidates=[{"by": "description", "selector": "详情页动作"}],
            target_meta={"content_desc": "详情页动作"},
        )

        def scope_scroll(index: int) -> InspectionAction:
            return InspectionAction(
                action_type="scroll",
                action_key=f"scope-scroll-{index}",
                locator_candidates=[],
                target_meta={
                    "direction": "up",
                    "bounds": [0, 100, 1080, 2200],
                    "screen_size": [1080, 2400],
                },
                coordinate_only=True,
                replayable=False,
                action_role="SCROLL:vertical:up",
                action_role_key=f"scope-scroll-role-{index}",
            )

        scroll_root = scope_scroll(0)
        scroll_viewport = scope_scroll(1)
        root_work = replace(
            self._work(root_capture, [open_child, scroll_root]),
            semantic_key=root_capture.model.semantic_key,
        )
        child_work = self._stored_work(
            child_capture,
            [child_action],
            depth=1,
            path=[_serialize_action(open_child)],
            parent_state_id=self.state_id,
        )
        viewport_work = self._stored_work(
            viewport_capture,
            [scroll_viewport],
            depth=0,
            path=[_serialize_action(scroll_root)],
            parent_state_id=self.state_id,
        )
        performer = Mock(
            side_effect=lambda _device, action, **_kwargs: (
                "scroll:up:coordinate"
                if action.action_type == "scroll"
                else "description"
            )
        )

        with patch(
            "backend.inspection.engine._restore_parent_after_transition",
            return_value=root_capture,
        ):
            outcome = self._run_branch(
                capture=root_capture,
                work=root_work,
                persist_results=[
                    PersistedState(work=root_work, is_new=True),
                    PersistedState(work=child_work, is_new=True),
                    PersistedState(work=viewport_work, is_new=True),
                    PersistedState(work=viewport_work, is_new=False),
                ],
                publish_mock=Mock(),
                perform_mock=performer,
                wait_captures=[
                    root_capture,
                    child_capture,
                    viewport_capture,
                    viewport_capture,
                ],
                branch_config={"scope": "single_page"},
            )

        self.assertEqual(outcome.stop_reason, "队列自然耗尽")
        # The cross-page child is captured but never dequeued: its own action
        # must not run, while same-page viewport scrolling keeps expanding.
        self.assertEqual(
            [call.args[1].action_key for call in performer.call_args_list],
            [
                open_child.action_key,
                scroll_root.action_key,
                scroll_viewport.action_key,
            ],
        )
        self.assertEqual(
            [item["status"] for item in child_work.action_map["actions"]],
            ["OUT_OF_SCOPE"],
        )
        with Session(self.engine) as session:
            child_state = session.get(InspectionState, child_work.state_id)
        self.assertEqual(child_state.expansion_status, "SCOPE_SKIPPED")
        self.assertEqual(child_state.pending_action_count, 0)

    def test_cancel_finalizes_active_and_remaining_actions(self):
        capture = _capture(
            _page(
                '<node package="com.demo" class="android.widget.Button" '
                'content-desc="动作一" clickable="true" enabled="true" '
                'bounds="[10,20][300,140]"/>'
                '<node package="com.demo" class="android.widget.Button" '
                'content-desc="动作二" clickable="true" enabled="true" '
                'bounds="[10,180][300,300]"/>'
            )
        )
        actions = enumerate_actions(capture.model, screen_size=(1080, 2400))
        work = self._work(capture, actions)

        with self.assertRaises(InspectionAborted):
            self._run_branch(
                capture=capture,
                work=work,
                persist_results=[PersistedState(work=work, is_new=True)],
                publish_mock=Mock(),
                perform_mock=Mock(
                    side_effect=InspectionAborted("inspection cancelled")
                ),
                wait_captures=[capture],
            )

        self.assertEqual(
            [item["status"] for item in work.action_map["actions"]],
            ["CANCELLED", "CANCELLED"],
        )
        with Session(self.engine) as session:
            transition = session.exec(
                select(InspectionTransition).where(
                    InspectionTransition.run_id == self.run_id
                )
            ).one()
            state = session.get(InspectionState, self.state_id)
        self.assertEqual(transition.status, "CANCELLED")
        self.assertEqual(transition.topology_type, "TERMINAL")
        self.assertEqual(state.expansion_status, "ABORTED")
        self.assertEqual(state.pending_action_count, 0)

    def test_execute_run_always_publishes_terminal_snapshot_when_cancelled(self):
        abort_event = threading.Event()
        abort_event.set()
        registry = InspectionLiveRegistry()

        with patch(
            "backend.inspection.engine.engine",
            self.engine,
        ), patch(
            "backend.inspection.engine.inspection_live_registry",
            registry,
        ):
            execute_inspection_run(self.run_id, abort_event=abort_event)

        snapshot = registry.snapshot(self.run_id)
        self.assertIsNotNone(snapshot)
        self.assertTrue(snapshot["terminal"])
        self.assertFalse(snapshot["overlay_visible"])
        self.assertEqual(snapshot["event_type"], "TERMINAL")
        self.assertEqual(snapshot["run_status"], "ABORTED")
        self.assertEqual(snapshot["current_stage"], "已取消")
        self.assertEqual(snapshot["recent_events"][-1]["type"], "TERMINAL")
        with Session(self.engine) as session:
            run = session.get(InspectionRun, self.run_id)
        self.assertEqual(run.status, "ABORTED")


if __name__ == "__main__":
    unittest.main()
