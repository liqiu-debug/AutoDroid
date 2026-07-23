import io
import threading
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock, patch

from PIL import Image
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine

from backend.inspection.device import CapturedPage, LocatorDrift
from backend.inspection.engine import (
    NavigationEntry,
    StateWork,
    TransitionBuffer,
    _persist_state,
    _restore_parent_after_transition,
    _serialize_action,
    _transition_payload,
)
from backend.inspection.sanitizer import InspectionArtifactSanitizer
from backend.inspection.semantics import InspectionAction, build_page_model
from backend.models import (
    InspectionBranchRun,
    InspectionRun,
    InspectionState,
    InspectionTransition,
)


def _capture(
    label: str,
    *,
    activity: str = ".Main",
    screenshot_png: bytes = b"unused",
) -> CapturedPage:
    xml = (
        '<hierarchy rotation="0">'
        '<node package="com.demo" class="android.widget.FrameLayout" '
        'enabled="true" bounds="[0,0][1080,2400]">'
        f'<node package="com.demo" class="android.widget.TextView" text="{label}" '
        'enabled="true" bounds="[20,100][800,220]"/>'
        "</node></hierarchy>"
    )
    model = build_page_model(
        xml,
        package_name="com.demo",
        activity=activity,
    )
    return CapturedPage(
        package_name="com.demo",
        activity=activity,
        xml=xml,
        screenshot_png=screenshot_png,
        screenshot_sha=f"sha-{label}",
        perceptual_hash="0" * 16,
        model=model,
        stable_by="exact",
    )


def _png_bytes() -> bytes:
    output = io.BytesIO()
    Image.new("RGB", (8, 8), "white").save(output, format="PNG")
    return output.getvalue()


def _action(key: str, label: str) -> InspectionAction:
    return InspectionAction(
        action_type="click",
        action_key=key,
        locator_candidates=[{"by": "description", "selector": label}],
        target_meta={"content_desc": label},
    )


def _work(capture: CapturedPage, *, path=None) -> StateWork:
    return StateWork(
        state_id=10,
        state_key=capture.model.state_key,
        cluster_key=capture.model.cluster_key,
        replay_key=capture.model.replay_key,
        package_name=capture.package_name,
        activity=capture.activity,
        screenshot_sha=capture.screenshot_sha,
        depth=0,
        path=list(path or []),
        actions=[],
    )


class InspectionPeerRecoveryTests(unittest.TestCase):
    def _restore(self, *, parent, target, entries):
        return _restore_parent_after_transition(
            device=self.device,
            parent=parent,
            target_capture=target,
            relation_type="PEER",
            navigation_group_key="main-tabs",
            navigation_entries=entries,
            branch_config={},
            device_serial="android-1",
            package_name="com.demo",
            abort_event=threading.Event(),
            input_rules=[],
            dynamic_patterns=[],
            stable_wait_seconds=2.0,
            secret_values=[],
        )

    def setUp(self):
        self.device = Mock()

    def _branch_database(self):
        test_engine = create_engine(
            "sqlite://",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        SQLModel.metadata.create_all(test_engine)
        with Session(test_engine) as session:
            run = InspectionRun(
                name="incoming",
                package_name="com.demo",
                device_serial="android-1",
            )
            session.add(run)
            session.flush()
            branch = InspectionBranchRun(
                run_id=run.id,
                branch_key="authenticated",
                branch_name="已登录",
            )
            session.add(branch)
            session.flush()
            root = InspectionState(
                run_id=run.id,
                branch_run_id=branch.id,
                branch_key=branch.branch_key,
                cluster_key="root-cluster",
                state_key="root-state",
                depth=0,
            )
            session.add(root)
            session.flush()
            branch.root_state_id = root.id
            session.add(branch)
            session.commit()
            run_id = run.id
            branch_id = branch.id
            root_id = root.id
        branch_ref = InspectionBranchRun(
            id=branch_id,
            run_id=run_id,
            branch_key="authenticated",
            branch_name="已登录",
            root_state_id=root_id,
        )
        self.addCleanup(test_engine.dispose)
        return test_engine, run_id, branch_ref, root_id

    def _persist_existing(
        self,
        *,
        test_engine,
        run_id,
        branch,
        capture,
        depth,
        parent_state_id,
        path,
        prefer_hierarchy=False,
    ):
        action_map_path = Mock()
        action_map_path.exists.return_value = True
        with patch("backend.inspection.engine.engine", test_engine), patch(
            "backend.inspection.engine._state_action_map_path",
            return_value=action_map_path,
        ), patch(
            "backend.inspection.engine.read_action_map",
            return_value={},
        ):
            return _persist_state(
                run_id=run_id,
                branch_run=branch,
                capture=capture,
                depth=depth,
                parent_state_id=parent_state_id,
                path=path,
                sanitizer=Mock(rules=[]),
                screen_size=(1080, 2400),
                safety_rules=[],
                input_rules=[],
                max_scrolls=3,
                max_variants=5,
                prefer_hierarchy=prefer_hierarchy,
            )

    def test_known_peer_uses_source_tab_without_back_or_full_replay(self):
        parent_capture = _capture("首页")
        target_capture = _capture("购物车")
        home = _action("home", "首页")
        parent = _work(parent_capture)
        entry = NavigationEntry(
            group_key="main-tabs",
            state_id=parent.state_id,
            action=home,
            target_path=(),
        )

        with patch(
            "backend.inspection.engine.perform_action",
            return_value="description",
        ) as perform, patch(
            "backend.inspection.engine.wait_for_stable_page",
            return_value=parent_capture,
        ), patch(
            "backend.inspection.engine._replay_path",
        ) as replay:
            restored = self._restore(
                parent=parent,
                target=target_capture,
                entries=[entry],
            )

        self.assertIs(restored, parent_capture)
        perform.assert_called_once()
        self.device.press.assert_not_called()
        replay.assert_not_called()

    def test_nested_peer_replays_only_path_suffix(self):
        base_capture = _capture("许愿池")
        parent_capture = _capture("许愿详情", activity=".Detail")
        target_capture = _capture("购物车")
        wish = _action("wish", "许愿池")
        detail = _action("detail", "查看详情")
        wish_payload = _serialize_action(wish)
        parent = _work(
            parent_capture,
            path=[wish_payload, _serialize_action(detail)],
        )
        entry = NavigationEntry(
            group_key="main-tabs",
            state_id=8,
            action=wish,
            target_path=(wish_payload,),
        )

        with patch(
            "backend.inspection.engine.perform_action",
            side_effect=["description", "description"],
        ) as perform, patch(
            "backend.inspection.engine.wait_for_stable_page",
            side_effect=[base_capture, parent_capture],
        ), patch(
            "backend.inspection.engine._replay_path",
        ) as replay:
            restored = self._restore(
                parent=parent,
                target=target_capture,
                entries=[entry],
            )

        self.assertIs(restored, parent_capture)
        self.assertEqual(perform.call_count, 2)
        self.assertEqual(perform.call_args_list[1].args[1].action_key, "detail")
        self.device.press.assert_not_called()
        replay.assert_not_called()

    def test_locator_drift_falls_back_to_one_verified_back(self):
        parent_capture = _capture("首页")
        target_capture = _capture("购物车")
        parent = _work(parent_capture)
        entry = NavigationEntry(
            group_key="main-tabs",
            state_id=parent.state_id,
            action=_action("home", "首页"),
            target_path=(),
        )

        with patch(
            "backend.inspection.engine.perform_action",
            side_effect=LocatorDrift("changed"),
        ), patch(
            "backend.inspection.engine.wait_for_stable_page",
            side_effect=[target_capture, parent_capture],
        ), patch(
            "backend.inspection.engine._replay_path",
        ) as replay:
            restored = self._restore(
                parent=parent,
                target=target_capture,
                entries=[entry],
            )

        self.assertIs(restored, parent_capture)
        self.device.press.assert_called_once_with("back")
        replay.assert_not_called()

    def test_locator_drift_unknown_page_skips_back_and_uses_full_replay(self):
        parent_capture = _capture("首页")
        target_capture = _capture("购物车")
        unknown_capture = _capture("未知中间页", activity=".Unknown")
        parent = _work(parent_capture)
        entry = NavigationEntry(
            group_key="main-tabs",
            state_id=parent.state_id,
            action=_action("home", "首页"),
            target_path=(),
        )

        with patch(
            "backend.inspection.engine.perform_action",
            side_effect=LocatorDrift("changed"),
        ), patch(
            "backend.inspection.engine.wait_for_stable_page",
            return_value=unknown_capture,
        ), patch(
            "backend.inspection.engine._replay_path",
            return_value=(parent_capture, True),
        ) as replay:
            restored = self._restore(
                parent=parent,
                target=target_capture,
                entries=[entry],
            )

        self.assertIs(restored, parent_capture)
        self.device.press.assert_not_called()
        replay.assert_called_once()

    def test_failed_back_uses_full_path_replay_once(self):
        parent_capture = _capture("首页")
        target_capture = _capture("购物车")
        parent = _work(parent_capture)

        with patch(
            "backend.inspection.engine.wait_for_stable_page",
            return_value=target_capture,
        ), patch(
            "backend.inspection.engine._replay_path",
            return_value=(parent_capture, True),
        ) as replay:
            restored = self._restore(
                parent=parent,
                target=target_capture,
                entries=[],
            )

        self.assertIs(restored, parent_capture)
        self.device.press.assert_called_once_with("back")
        replay.assert_called_once()

    def test_full_replay_exception_returns_recovery_failure(self):
        parent_capture = _capture("首页")
        target_capture = _capture("购物车")

        with patch(
            "backend.inspection.engine.wait_for_stable_page",
            return_value=target_capture,
        ), patch(
            "backend.inspection.engine._replay_path",
            side_effect=RuntimeError("replay failed"),
        ) as replay:
            restored = self._restore(
                parent=_work(parent_capture),
                target=target_capture,
                entries=[],
            )

        self.assertIsNone(restored)
        self.device.press.assert_called_once_with("back")
        replay.assert_called_once()

    def test_shorter_child_path_does_not_replace_peer_canonical_hierarchy(self):
        test_engine, run_id, branch, root_id = self._branch_database()
        capture = _capture("许愿池")
        first = _serialize_action(_action("open-list", "进入列表"))
        second = _serialize_action(_action("wish-tab", "许愿池"))
        with Session(test_engine) as session:
            state = InspectionState(
                run_id=run_id,
                branch_run_id=branch.id,
                branch_key=branch.branch_key,
                cluster_key=capture.model.cluster_key,
                state_key=capture.model.state_key,
                activity=capture.activity,
                foreground_package=capture.package_name,
                depth=2,
                parent_state_id=root_id,
                first_path=[first, second],
            )
            session.add(state)
            session.flush()
            incoming = InspectionTransition(
                run_id=run_id,
                branch_run_id=branch.id,
                from_state_id=root_id,
                to_state_id=state.id,
                sequence=1,
                action_type="click",
                action_key="wish-tab",
                status="PASS",
                relation_type="PEER",
            )
            session.add(incoming)
            session.flush()
            state.incoming_transition_id = incoming.id
            session.add(state)
            session.commit()
            state_id = state.id
            incoming_id = incoming.id

        persisted = self._persist_existing(
            test_engine=test_engine,
            run_id=run_id,
            branch=branch,
            capture=capture,
            depth=1,
            parent_state_id=root_id,
            path=[second],
        )

        self.assertFalse(persisted.assign_incoming)
        self.assertEqual(persisted.work.path, [second])
        self.assertEqual(persisted.work.depth, 2)
        with Session(test_engine) as session:
            state = session.get(InspectionState, state_id)
            self.assertEqual(state.depth, 2)
            self.assertEqual(state.parent_state_id, root_id)
            self.assertEqual(state.first_path, [second])
            self.assertEqual(state.incoming_transition_id, incoming_id)

    def test_same_hierarchy_child_canonical_can_upgrade_to_peer(self):
        test_engine, run_id, branch, root_id = self._branch_database()
        capture = _capture("附近门店")
        tab = _serialize_action(_action("nearby-tab", "附近门店"))
        with Session(test_engine) as session:
            state = InspectionState(
                run_id=run_id,
                branch_run_id=branch.id,
                branch_key=branch.branch_key,
                cluster_key=capture.model.cluster_key,
                state_key=capture.model.state_key,
                activity=capture.activity,
                foreground_package=capture.package_name,
                depth=1,
                parent_state_id=root_id,
                first_path=[tab],
            )
            session.add(state)
            session.flush()
            child_transition = InspectionTransition(
                run_id=run_id,
                branch_run_id=branch.id,
                from_state_id=root_id,
                to_state_id=state.id,
                sequence=1,
                action_type="click",
                action_key="nearby-child",
                status="PASS",
                relation_type="CHILD",
            )
            session.add(child_transition)
            session.flush()
            state.incoming_transition_id = child_transition.id
            session.add(state)
            session.commit()
            state_id = state.id

        persisted = self._persist_existing(
            test_engine=test_engine,
            run_id=run_id,
            branch=branch,
            capture=capture,
            depth=1,
            parent_state_id=root_id,
            path=[tab],
            prefer_hierarchy=True,
        )
        self.assertTrue(persisted.assign_incoming)

        action = _action("nearby-tab", "附近门店")
        with patch("backend.inspection.engine.engine", test_engine):
            buffer = TransitionBuffer(run_id, branch.id)
            buffer.append(
                _transition_payload(
                    from_state_id=root_id,
                    to_state_id=state_id,
                    sequence=2,
                    action=action,
                    status="PASS",
                    relation_type="PEER",
                    relation_confidence=0.98,
                ),
                state_id,
                assign_incoming=persisted.assign_incoming,
            )
            self.assertEqual(buffer.items, [])

        with Session(test_engine) as session:
            state = session.get(InspectionState, state_id)
            incoming = session.get(
                InspectionTransition,
                state.incoming_transition_id,
            )
            self.assertEqual(incoming.relation_type, "PEER")

    def test_existing_non_root_without_canonical_incoming_requests_assignment(self):
        test_engine, run_id, branch, root_id = self._branch_database()
        capture = _capture("分类")
        tab = _serialize_action(_action("category-tab", "分类"))
        with Session(test_engine) as session:
            state = InspectionState(
                run_id=run_id,
                branch_run_id=branch.id,
                branch_key=branch.branch_key,
                cluster_key=capture.model.cluster_key,
                state_key=capture.model.state_key,
                activity=capture.activity,
                foreground_package=capture.package_name,
                depth=1,
                parent_state_id=root_id,
                first_path=[tab],
            )
            session.add(state)
            session.commit()

        persisted = self._persist_existing(
            test_engine=test_engine,
            run_id=run_id,
            branch=branch,
            capture=capture,
            depth=1,
            parent_state_id=root_id,
            path=[tab],
        )

        self.assertTrue(persisted.assign_incoming)

    def test_rejected_deeper_candidate_cannot_replace_or_fill_canonical(self):
        for existing_relation in (None, "CHILD"):
            with self.subTest(existing_relation=existing_relation):
                test_engine, run_id, branch, root_id = self._branch_database()
                capture = _capture(f"浅层页面-{existing_relation or 'missing'}")
                action = _serialize_action(_action("open", "打开页面"))
                with Session(test_engine) as session:
                    state = InspectionState(
                        run_id=run_id,
                        branch_run_id=branch.id,
                        branch_key=branch.branch_key,
                        cluster_key=capture.model.cluster_key,
                        state_key=capture.model.state_key,
                        activity=capture.activity,
                        foreground_package=capture.package_name,
                        depth=0,
                        parent_state_id=None,
                        first_path=[action],
                    )
                    session.add(state)
                    session.flush()
                    incoming_id = None
                    if existing_relation is not None:
                        incoming = InspectionTransition(
                            run_id=run_id,
                            branch_run_id=branch.id,
                            from_state_id=root_id,
                            to_state_id=state.id,
                            sequence=1,
                            action_type="click",
                            action_key="existing-child",
                            status="PASS",
                            relation_type=existing_relation,
                        )
                        session.add(incoming)
                        session.flush()
                        incoming_id = incoming.id
                        state.incoming_transition_id = incoming_id
                        session.add(state)
                    session.commit()
                    state_id = state.id

                persisted = self._persist_existing(
                    test_engine=test_engine,
                    run_id=run_id,
                    branch=branch,
                    capture=capture,
                    depth=1,
                    parent_state_id=root_id,
                    path=[action, action],
                    prefer_hierarchy=existing_relation is not None,
                )

                self.assertFalse(persisted.assign_incoming)
                with Session(test_engine) as session:
                    state = session.get(InspectionState, state_id)
                    self.assertEqual(state.depth, 0)
                    self.assertIsNone(state.parent_state_id)
                    self.assertEqual(state.incoming_transition_id, incoming_id)

    def test_initial_root_state_and_branch_pointer_are_persisted_together(self):
        test_engine = create_engine(
            "sqlite://",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        SQLModel.metadata.create_all(test_engine)
        self.addCleanup(test_engine.dispose)
        with Session(test_engine) as session:
            run = InspectionRun(
                name="root",
                package_name="com.demo",
                device_serial="android-1",
            )
            session.add(run)
            session.flush()
            branch = InspectionBranchRun(
                run_id=run.id,
                branch_key="authenticated",
                branch_name="已登录",
            )
            session.add(branch)
            session.commit()
            run_id = run.id
            branch_id = branch.id
        branch_ref = InspectionBranchRun(
            id=branch_id,
            run_id=run_id,
            branch_key="authenticated",
            branch_name="已登录",
        )
        capture = _capture("首页", screenshot_png=_png_bytes())

        with TemporaryDirectory() as directory, patch(
            "backend.inspection.engine.engine",
            test_engine,
        ), patch(
            "backend.inspection.engine._reports_root",
            return_value=Path(directory).resolve(),
        ):
            persisted = _persist_state(
                run_id=run_id,
                branch_run=branch_ref,
                capture=capture,
                depth=0,
                parent_state_id=None,
                path=[],
                sanitizer=InspectionArtifactSanitizer(),
                screen_size=(1080, 2400),
                safety_rules=[],
                input_rules=[],
                max_scrolls=3,
                max_variants=5,
                mark_branch_root=True,
            )

        self.assertTrue(persisted.is_new)
        self.assertFalse(persisted.assign_incoming)
        self.assertEqual(branch_ref.root_state_id, persisted.work.state_id)
        with Session(test_engine) as session:
            branch = session.get(InspectionBranchRun, branch_id)
            root = session.get(InspectionState, persisted.work.state_id)
            self.assertEqual(branch.root_state_id, root.id)
            self.assertIsNone(root.incoming_transition_id)

    def test_transition_buffer_only_assigns_explicit_canonical_incoming(self):
        test_engine = create_engine(
            "sqlite://",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        SQLModel.metadata.create_all(test_engine)
        with Session(test_engine) as session:
            run = InspectionRun(
                name="incoming",
                package_name="com.demo",
                device_serial="android-1",
            )
            session.add(run)
            session.flush()
            branch = InspectionBranchRun(
                run_id=run.id,
                branch_key="authenticated",
                branch_name="已登录",
            )
            session.add(branch)
            session.flush()
            root = InspectionState(
                run_id=run.id,
                branch_run_id=branch.id,
                branch_key=branch.branch_key,
                cluster_key="root-cluster",
                state_key="root-state",
            )
            child = InspectionState(
                run_id=run.id,
                branch_run_id=branch.id,
                branch_key=branch.branch_key,
                cluster_key="child-cluster",
                state_key="child-state",
                depth=1,
            )
            session.add(root)
            session.add(child)
            session.flush()
            branch.root_state_id = root.id
            session.add(branch)
            session.commit()
            root_id = root.id
            child_id = child.id
            run_id = run.id
            branch_id = branch.id

        action = _action("tab", "首页")
        with patch("backend.inspection.engine.engine", test_engine):
            buffer = TransitionBuffer(run_id, branch_id)
            buffer.append(
                _transition_payload(
                    from_state_id=child_id,
                    to_state_id=root_id,
                    sequence=1,
                    action=action,
                    status="PASS",
                    relation_type="PEER",
                    relation_confidence=0.95,
                ),
                root_id,
                assign_incoming=True,
            )
            self.assertEqual(buffer.items, [])
            buffer.append(
                _transition_payload(
                    from_state_id=root_id,
                    to_state_id=child_id,
                    sequence=2,
                    action=action,
                    status="PASS",
                    relation_type="CHILD",
                    relation_confidence=1.0,
                ),
                child_id,
                assign_incoming=True,
            )
            self.assertEqual(buffer.items, [])

        with Session(test_engine) as session:
            root = session.get(InspectionState, root_id)
            child = session.get(InspectionState, child_id)
            transition = session.get(InspectionTransition, child.incoming_transition_id)
            self.assertIsNone(root.incoming_transition_id)
            self.assertEqual(transition.to_state_id, child_id)
        test_engine.dispose()


if __name__ == "__main__":
    unittest.main()
