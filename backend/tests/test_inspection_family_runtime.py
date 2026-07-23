import io
import threading
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock, patch

from PIL import Image
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, select

from backend.inspection.action_map import build_action_map
from backend.inspection.device import CapturedPage
from backend.inspection.engine import (
    PersistedState,
    StateWork,
    TransitionBuffer,
    _execute_branch,
    _family_action_pair,
    _overlay_return_owner,
    _persist_state,
    _serialize_action,
    _state_actions,
    _transition_payload,
)
from backend.inspection.sanitizer import InspectionArtifactSanitizer
from backend.inspection.semantics import InspectionAction, build_page_model
from backend.models import (
    InspectionBranchRun,
    InspectionCoverageContract,
    InspectionExplorationFamily,
    InspectionFamilyActionCoverage,
    InspectionObservation,
    InspectionPageTemplate,
    InspectionRun,
    InspectionState,
    InspectionTransition,
)


def _png(color: str = "white") -> bytes:
    output = io.BytesIO()
    Image.new("RGB", (120, 200), color).save(output, format="PNG")
    return output.getvalue()


def _page_xml() -> str:
    return (
        '<hierarchy rotation="0">'
        '<node package="com.demo" class="android.widget.FrameLayout" '
        'bounds="[0,0][120,200]" enabled="true">'
        '<node package="com.demo" class="android.widget.TextView" '
        'text="商品详情" bounds="[5,10][115,50]" enabled="true"/>'
        '<node package="com.demo" class="android.widget.Button" '
        'content-desc="立即购买" clickable="true" enabled="true" '
        'bounds="[5,120][115,185]"/>'
        "</node></hierarchy>"
    )


def _capture() -> CapturedPage:
    xml = _page_xml()
    model = build_page_model(
        xml,
        package_name="com.demo",
        activity=".ProductDetailActivity",
    )
    return CapturedPage(
        package_name="com.demo",
        activity=".ProductDetailActivity",
        xml=xml,
        screenshot_png=_png(),
        screenshot_sha="same-page-sha",
        perceptual_hash="0" * 16,
        model=model,
        stable_by="exact",
    )


def _filter_capture() -> CapturedPage:
    xml = (
        '<hierarchy rotation="0">'
        '<node package="com.demo" class="android.widget.FrameLayout" '
        'bounds="[0,0][120,200]" enabled="true">'
        '<node package="com.demo" class="android.widget.TextView" '
        'text="全部筛选" bounds="[30,10][90,30]" enabled="true"/>'
        '<node package="com.demo" class="android.widget.TextView" '
        'text="商品" bounds="[5,40][40,60]" enabled="true"/>'
        '<node package="com.demo" class="android.widget.TextView" '
        'text="价格" bounds="[5,65][40,85]" enabled="true"/>'
        '<node package="com.demo" class="android.widget.TextView" '
        'text="尺寸" bounds="[5,90][40,110]" enabled="true"/>'
        '<node package="com.demo" class="android.widget.Button" '
        'text="重置" clickable="true" bounds="[5,160][55,190]" enabled="true"/>'
        '<node package="com.demo" class="android.widget.Button" '
        'text="确定" clickable="true" bounds="[65,160][115,190]" enabled="true"/>'
        "</node></hierarchy>"
    )
    model = build_page_model(
        xml,
        package_name="com.demo",
        activity=".ProductDetailActivity",
    )
    return CapturedPage(
        package_name="com.demo",
        activity=".ProductDetailActivity",
        xml=xml,
        screenshot_png=_png("gray"),
        screenshot_sha="filter-panel-sha",
        perceptual_hash="1" * 16,
        model=model,
        stable_by="exact",
    )


def _action(
    key: str,
    *,
    role: str,
    role_key: str,
    label: str = "立即购买",
    group_key: str | None = None,
    sample_policy: str = "ALL",
) -> InspectionAction:
    return InspectionAction(
        action_type="click",
        action_key=key,
        locator_candidates=[{"by": "description", "selector": label}],
        target_meta={"content_desc": label},
        action_role=role,
        action_role_key=role_key,
        action_anchor_key=f"anchor-{key}",
        action_group_key=group_key,
        sample_policy=sample_policy,
    )


class InspectionFamilyPersistenceTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine(
            "sqlite://",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        SQLModel.metadata.create_all(self.engine)
        with Session(self.engine) as session:
            run = InspectionRun(
                name="family persistence",
                package_name="com.demo",
                device_serial="android-1",
            )
            session.add(run)
            session.flush()
            branch = InspectionBranchRun(
                run_id=run.id,
                branch_key="guest",
                branch_name="Guest",
            )
            session.add(branch)
            session.commit()
            session.refresh(run)
            session.refresh(branch)
            self.run_id = int(run.id)
            self.branch = InspectionBranchRun(
                id=branch.id,
                run_id=run.id,
                branch_key=branch.branch_key,
                branch_name=branch.branch_name,
            )
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

    def _persist(self, capture: CapturedPage, incoming_label: str):
        incoming = _action(
            f"open-{incoming_label}",
            role=f"NAV:{incoming_label}",
            role_key=f"nav-role-{incoming_label}",
            label=incoming_label,
        )
        return _persist_state(
            run_id=self.run_id,
            branch_run=self.branch,
            capture=capture,
            depth=1,
            parent_state_id=None,
            path=[_serialize_action(incoming)],
            sanitizer=InspectionArtifactSanitizer(),
            screen_size=(120, 200),
            safety_rules=[],
            input_rules=[],
            max_scrolls=0,
            max_variants=5,
            identity_v2=True,
            similarity_convergence=False,
            family_convergence=True,
        )

    def test_same_semantic_instances_share_family_but_keep_independent_states(self):
        capture = _capture()

        first = self._persist(capture, "商品 A")
        second = self._persist(capture, "商品 B")

        self.assertTrue(first.is_new)
        self.assertTrue(second.is_new)
        self.assertEqual(first.work.semantic_key, second.work.semantic_key)
        self.assertNotEqual(first.work.instance_anchor, second.work.instance_anchor)
        self.assertNotEqual(first.work.state_id, second.work.state_id)
        self.assertEqual(
            first.work.exploration_family_id,
            second.work.exploration_family_id,
        )
        self.assertEqual(first.work.exploration_mode, "FULL")
        self.assertEqual(second.work.exploration_mode, "DELTA_ONLY")

        with Session(self.engine) as session:
            states = session.exec(select(InspectionState).order_by(InspectionState.id)).all()
            families = session.exec(select(InspectionExplorationFamily)).all()
        self.assertEqual(len(states), 2)
        self.assertEqual(len(families), 1)
        self.assertEqual(families[0].member_count, 2)
        self.assertEqual(families[0].representative_state_id, states[0].id)
        self.assertEqual(families[0].fingerprint_version, 2)
        self.assertIsNone(states[0].family_match_confidence)

        revisit = self._persist(capture, "商品 A")
        self.assertFalse(revisit.is_new)
        with Session(self.engine) as session:
            families = session.exec(select(InspectionExplorationFamily)).all()
            persisted = session.get(InspectionState, first.work.state_id)
        self.assertEqual(len(families), 1)
        self.assertEqual(families[0].member_count, 2)
        self.assertEqual(persisted.exploration_family_id, families[0].id)

    def test_scroll_viewport_inherits_family_without_rematching_visible_modules(self):
        first = self._persist(_capture(), "商品 A")
        viewport_xml = _page_xml().replace(
            "</node></hierarchy>",
            '<node package="com.demo" class="android.widget.EditText" '
            'content-desc="服务备注" clickable="true" enabled="true" '
            'bounds="[5,60][115,110]"/></node></hierarchy>',
        )
        viewport_model = build_page_model(
            viewport_xml,
            package_name="com.demo",
            activity=".ProductDetailActivity",
        )
        viewport_capture = CapturedPage(
            package_name="com.demo",
            activity=".ProductDetailActivity",
            xml=viewport_xml,
            screenshot_png=_png("gray"),
            screenshot_sha="viewport-sha",
            perceptual_hash="1" * 16,
            model=viewport_model,
            stable_by="exact",
        )
        scroll = InspectionAction(
            action_type="scroll",
            action_key="scroll-down",
            locator_candidates=[],
            target_meta={"direction": "down"},
            action_role="SCROLL:vertical:down",
            action_role_key="scroll-role",
        )
        viewport = _persist_state(
            run_id=self.run_id,
            branch_run=self.branch,
            capture=viewport_capture,
            depth=1,
            parent_state_id=first.work.state_id,
            path=[_serialize_action(scroll)],
            sanitizer=InspectionArtifactSanitizer(),
            screen_size=(120, 200),
            safety_rules=[],
            input_rules=[],
            max_scrolls=0,
            max_variants=5,
            identity_v2=True,
            similarity_convergence=False,
            family_convergence=True,
        )

        self.assertTrue(viewport.is_new)
        self.assertEqual(
            viewport.work.exploration_family_id,
            first.work.exploration_family_id,
        )
        self.assertEqual(
            viewport.work.family_match_confidence,
            1.0,
        )
        with Session(self.engine) as session:
            families = session.exec(select(InspectionExplorationFamily)).all()
        self.assertEqual(len(families), 1)
        self.assertEqual(families[0].member_count, 2)

    def test_coverage_scheduler_stores_scroll_as_viewport_observation_on_source_state(self):
        first = self._persist(_capture(), "商品 A")
        viewport_xml = _page_xml().replace(
            "</node></hierarchy>",
            '<node package="com.demo" class="android.widget.Button" '
            'content-desc="加入购物车" clickable="true" enabled="true" '
            'bounds="[5,60][115,110]"/></node></hierarchy>',
        )
        viewport_model = build_page_model(
            viewport_xml,
            package_name="com.demo",
            activity=".ProductDetailActivity",
        )
        viewport_model.page_subtype = "SERVICE_LIST"
        viewport_capture = CapturedPage(
            package_name="com.demo",
            activity=".ProductDetailActivity",
            xml=viewport_xml,
            screenshot_png=_png("gray"),
            screenshot_sha="coverage-viewport-sha",
            perceptual_hash="2" * 16,
            model=viewport_model,
            stable_by="exact",
        )
        scroll = InspectionAction(
            action_type="scroll",
            action_key="coverage-scroll-down",
            locator_candidates=[],
            target_meta={"direction": "down"},
            action_role="SCROLL:vertical:down",
            action_role_key="coverage-scroll-role",
        )
        viewport = _persist_state(
            run_id=self.run_id,
            branch_run=self.branch,
            capture=viewport_capture,
            depth=1,
            parent_state_id=first.work.state_id,
            path=[_serialize_action(scroll)],
            sanitizer=InspectionArtifactSanitizer(),
            screen_size=(120, 200),
            safety_rules=[],
            input_rules=[],
            max_scrolls=0,
            max_variants=5,
            identity_v2=True,
            similarity_convergence=False,
            family_convergence=True,
            coverage_scheduler=True,
            preferred_state_id=first.work.state_id,
        )

        self.assertFalse(viewport.is_new)
        self.assertEqual(viewport.work.state_id, first.work.state_id)
        self.assertEqual(
            viewport.match_evidence["match_type"],
            "VIEWPORT_OBSERVATION",
        )
        self.assertTrue(
            any(
                action.target_meta.get("content_desc") == "加入购物车"
                for action in viewport.work.actions
            )
        )
        with Session(self.engine) as session:
            states = session.exec(select(InspectionState)).all()
            observations = session.exec(
                select(InspectionObservation).order_by(InspectionObservation.id)
            ).all()
        self.assertEqual(len(states), 1)
        self.assertEqual([item.capture_kind for item in observations], ["DISCOVERY", "VIEWPORT"])

    def test_bottom_navigation_destination_retains_business_actions(self):
        navigation = InspectionAction(
            action_type="click",
            action_key="bottom-profile",
            locator_candidates=[{"by": "description", "selector": "我的"}],
            target_meta={
                "content_desc": "我的",
                "navigation": {
                    "group_key": "bottom-nav",
                    "group_region": "bottom",
                    "member_key": "profile",
                },
            },
            action_role="NAV:profile",
            action_role_key="nav-profile-role",
        )
        persisted = _persist_state(
            run_id=self.run_id,
            branch_run=self.branch,
            capture=_capture(),
            depth=1,
            parent_state_id=None,
            path=[_serialize_action(navigation)],
            sanitizer=InspectionArtifactSanitizer(),
            screen_size=(120, 200),
            safety_rules=[],
            input_rules=[],
            max_scrolls=3,
            max_variants=5,
            identity_v2=True,
            similarity_convergence=False,
            family_convergence=True,
            coverage_scheduler=True,
        )

        self.assertTrue(persisted.is_new)
        self.assertTrue(
            any(
                action.target_meta.get("content_desc") == "立即购买"
                for action in persisted.work.actions
            )
        )
        self.assertTrue(
            any(
                item.get("label") == "立即购买"
                for item in persisted.work.action_map["actions"]
            )
        )

    def test_home_bottom_navigation_precedes_page_specific_entries(self):
        labels = ("首页", "分类", "许愿池", "购物车", "我的")
        navigation = "".join(
            '<node package="com.demo" class="android.view.ViewGroup" '
            f'content-desc="{label}" clickable="true" enabled="true" '
            f'selected="{str(index == 0).lower()}" '
            f'bounds="[{index * 24},180][{(index + 1) * 24},198]"/>'
            for index, label in enumerate(labels)
        )
        xml = (
            '<hierarchy rotation="0">'
            '<node package="com.demo" class="android.widget.FrameLayout" '
            'bounds="[0,0][120,200]" enabled="true">'
            '<node package="com.demo" class="android.widget.TextView" '
            'text="首页推荐" bounds="[5,10][115,30]" enabled="true"/>'
            '<node package="com.demo" class="android.widget.Button" '
            'content-desc="冰箱" clickable="true" enabled="true" '
            'bounds="[5,50][55,90]"/>'
            '<node package="com.demo" class="android.view.ViewGroup" '
            f'enabled="true" bounds="[0,178][120,200]">{navigation}</node>'
            "</node></hierarchy>"
        )
        capture = CapturedPage(
            package_name="com.demo",
            activity=".MainActivity",
            xml=xml,
            screenshot_png=_png(),
            screenshot_sha="home-with-tabs",
            perceptual_hash="0" * 16,
            model=build_page_model(
                xml,
                package_name="com.demo",
                activity=".MainActivity",
            ),
            stable_by="exact",
        )

        actions = _state_actions(
            capture,
            screen_size=(120, 200),
            safety_rules=[],
            input_rules=[],
            max_scrolls=0,
            depth=0,
            coverage_scheduler=True,
        )
        action_labels = [
            action.target_meta.get("content_desc")
            or action.target_meta.get("text")
            for action in actions
        ]

        self.assertEqual(action_labels[:4], list(labels[1:]))
        self.assertLess(action_labels.index("我的"), action_labels.index("冰箱"))

    def test_filter_close_restores_owner_state_without_new_instance_anchor(self):
        owner = self._persist(_capture(), "商品 A")
        open_filter = InspectionAction(
            action_type="click",
            action_key="open-filter",
            locator_candidates=[{"by": "text", "selector": "筛选"}],
            target_meta={"text": "筛选"},
            action_role="FILTER_OPEN",
            action_role_key="filter-open-role",
            action_group_key="filter-open-group",
        )
        overlay = _persist_state(
            run_id=self.run_id,
            branch_run=self.branch,
            capture=_filter_capture(),
            depth=owner.work.depth + 1,
            parent_state_id=owner.work.state_id,
            path=[*owner.work.path, _serialize_action(open_filter)],
            sanitizer=InspectionArtifactSanitizer(),
            screen_size=(120, 200),
            safety_rules=[],
            input_rules=[],
            max_scrolls=0,
            max_variants=5,
            identity_v2=True,
            similarity_convergence=False,
            family_convergence=True,
            coverage_scheduler=True,
            instance_anchor_override=owner.work.instance_anchor,
        )
        self.assertEqual(overlay.work.page_subtype, "FILTER_PANEL")
        close_filter = InspectionAction(
            action_type="back",
            action_key="close-filter",
            locator_candidates=[],
            target_meta={"text": "关闭筛选"},
            action_role="FILTER_CLOSE",
            action_role_key="filter-close-role",
            action_group_key="filter-close-group",
            sample_policy="PAGE_ONE",
        )
        tracked = {
            owner.work.state_id: owner.work,
            overlay.work.state_id: overlay.work,
        }

        resolved_owner = _overlay_return_owner(
            parent=overlay.work,
            action=close_filter,
            capture=_capture(),
            tracked_work=tracked,
        )

        self.assertIsNotNone(resolved_owner)
        restored = _persist_state(
            run_id=self.run_id,
            branch_run=self.branch,
            capture=_capture(),
            depth=resolved_owner.depth,
            parent_state_id=resolved_owner.parent_state_id,
            path=[*overlay.work.path, _serialize_action(close_filter)],
            sanitizer=InspectionArtifactSanitizer(),
            screen_size=(120, 200),
            safety_rules=[],
            input_rules=[],
            max_scrolls=0,
            max_variants=5,
            identity_v2=True,
            similarity_convergence=False,
            family_convergence=True,
            coverage_scheduler=True,
            instance_anchor_override=resolved_owner.instance_anchor,
            preferred_state_id=resolved_owner.state_id,
            preferred_match_type="OVERLAY_RETURN",
        )

        self.assertFalse(restored.is_new)
        self.assertEqual(restored.work.state_id, owner.work.state_id)
        self.assertEqual(restored.work.instance_anchor, owner.work.instance_anchor)
        self.assertEqual(restored.match_evidence["match_type"], "OVERLAY_RETURN")
        with Session(self.engine) as session:
            states = session.exec(select(InspectionState)).all()
        self.assertEqual(len(states), 2)

    def test_coverage_contract_requires_two_distinct_instance_anchors(self):
        first = self._persist(_capture(), "商品 A")
        second = self._persist(_capture(), "商品 B")
        action = InspectionAction(
            action_type="click",
            action_key="sample-product",
            locator_candidates=[{"by": "text", "selector": "商品"}],
            target_meta={
                "text": "商品",
                "enabled": True,
                "checked": False,
                "selected": False,
            },
            action_role="ITEM_OPEN:collection",
            action_role_key="sample-product-role",
            action_group_key="sample-product-group",
            sample_policy="FAMILY_TWO_SAMPLES",
        )
        buffer = TransitionBuffer(
            self.run_id,
            int(self.branch.id),
            coverage_scheduler=True,
        )
        buffer.append(
            _transition_payload(
                from_state_id=first.work.state_id,
                to_state_id=first.work.state_id,
                sequence=1,
                action=action,
                status="PASS",
                sampling_disposition="CONTRACT_SAMPLE",
            ),
            first.work.state_id,
        )
        buffer.flush()
        with Session(self.engine) as session:
            contract = session.exec(select(InspectionCoverageContract)).one()
            self.assertEqual(contract.status, "PROVISIONAL")
            self.assertEqual(contract.success_count, 1)

        buffer.append(
            _transition_payload(
                from_state_id=second.work.state_id,
                to_state_id=second.work.state_id,
                sequence=2,
                action=action,
                status="PASS",
                sampling_disposition="CONTRACT_SAMPLE",
            ),
            second.work.state_id,
        )
        buffer.flush()
        with Session(self.engine) as session:
            contract = session.exec(select(InspectionCoverageContract)).one()
            transitions = session.exec(
                select(InspectionTransition).order_by(InspectionTransition.sequence)
            ).all()
        self.assertEqual(contract.status, "VERIFIED")
        self.assertEqual(contract.success_count, 2)
        self.assertEqual(len(contract.source_instance_anchors), 2)
        self.assertTrue(all(item.coverage_contract_id == contract.id for item in transitions))

        with Session(self.engine) as session:
            template = InspectionPageTemplate(
                package_name="com.demo",
                activity=".OrderActivity",
                fingerprint_version=2,
                template_key="order-template",
                page_role="ORDER",
                activity_family="order",
            )
            session.add(template)
            session.flush()
            conflicting_target = InspectionState(
                run_id=self.run_id,
                branch_run_id=int(self.branch.id),
                branch_key=self.branch.branch_key,
                cluster_key="order-cluster",
                state_key="order-state",
                semantic_key="order-semantic",
                identity_version=2,
                instance_anchor="order-anchor",
                template_id=template.id,
                exploration_family_id=first.work.exploration_family_id,
                activity=".OrderActivity",
                foreground_package="com.demo",
            )
            session.add(conflicting_target)
            session.commit()
            session.refresh(conflicting_target)
            conflicting_target_id = int(conflicting_target.id)
        buffer.append(
            _transition_payload(
                from_state_id=first.work.state_id,
                to_state_id=conflicting_target_id,
                sequence=3,
                action=action,
                status="PASS",
                sampling_disposition="CONTRACT_SAMPLE",
            ),
            conflicting_target_id,
        )
        buffer.flush()
        with Session(self.engine) as session:
            contract = session.exec(select(InspectionCoverageContract)).one()
        self.assertEqual(contract.status, "CONFLICT")
        self.assertEqual(contract.failure_count, 1)

    def test_scroll_viewport_crossing_a_role_boundary_does_not_inherit_family(self):
        first = self._persist(_capture(), "商品 A")
        dialog_xml = (
            '<hierarchy rotation="0">'
            '<node package="com.demo" class="android.widget.FrameLayout" '
            'bounds="[0,0][120,200]" enabled="true">'
            '<node package="com.demo" class="android.app.Dialog" text="选择规格" '
            'bounds="[5,20][115,180]" enabled="true"/>'
            '</node></hierarchy>'
        )
        dialog_model = build_page_model(
            dialog_xml,
            package_name="com.demo",
            activity=".ProductDetailActivity",
        )
        dialog_capture = CapturedPage(
            package_name="com.demo",
            activity=".ProductDetailActivity",
            xml=dialog_xml,
            screenshot_png=_png("gray"),
            screenshot_sha="dialog-sha",
            perceptual_hash="2" * 16,
            model=dialog_model,
            stable_by="exact",
        )
        scroll = InspectionAction(
            action_type="scroll",
            action_key="scroll-dialog",
            locator_candidates=[],
            target_meta={"direction": "down"},
            action_role="SCROLL:vertical:down",
            action_role_key="scroll-role",
        )

        dialog = _persist_state(
            run_id=self.run_id,
            branch_run=self.branch,
            capture=dialog_capture,
            depth=1,
            parent_state_id=first.work.state_id,
            path=[_serialize_action(scroll)],
            sanitizer=InspectionArtifactSanitizer(),
            screen_size=(120, 200),
            safety_rules=[],
            input_rules=[],
            max_scrolls=0,
            max_variants=5,
            identity_v2=True,
            similarity_convergence=False,
            family_convergence=True,
        )

        self.assertEqual(dialog_capture.model.role, "DIALOG")
        self.assertNotEqual(
            dialog.work.exploration_family_id,
            first.work.exploration_family_id,
        )
        with Session(self.engine) as session:
            persisted_dialog = session.get(InspectionState, dialog.work.state_id)
        self.assertEqual(
            persisted_dialog.family_match_evidence.get("match_type"),
            "NEW_FAMILY",
        )


class InspectionFamilyRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine(
            "sqlite://",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        SQLModel.metadata.create_all(self.engine)
        self.capture = _capture()
        with Session(self.engine) as session:
            run = InspectionRun(
                name="family runtime",
                package_name="com.demo",
                device_serial="android-1",
                profile_snapshot={},
                selected_branches=["guest"],
            )
            session.add(run)
            session.flush()
            branch = InspectionBranchRun(
                run_id=run.id,
                branch_key="guest",
                branch_name="Guest",
            )
            session.add(branch)
            session.flush()
            family = InspectionExplorationFamily(
                run_id=run.id,
                branch_run_id=branch.id,
                family_key="product-family",
                page_role=self.capture.model.role,
                activity_family=self.capture.model.activity_family,
            )
            session.add(family)
            session.commit()
            session.refresh(run)
            session.refresh(branch)
            session.refresh(family)
            self.run_id = int(run.id)
            self.branch_id = int(branch.id)
            self.family_id = int(family.id)

    def tearDown(self):
        self.engine.dispose()

    def _seed_works(self, action_groups):
        works = []
        with Session(self.engine) as session:
            family = session.get(InspectionExplorationFamily, self.family_id)
            for index, actions in enumerate(action_groups):
                state = InspectionState(
                    run_id=self.run_id,
                    branch_run_id=self.branch_id,
                    branch_key="guest",
                    cluster_key=f"product-{index}",
                    state_key=f"product-{index}",
                    semantic_key=self.capture.model.semantic_key,
                    identity_version=2,
                    instance_anchor=f"sku-{index}",
                    exploration_family_id=self.family_id,
                    exploration_mode="FULL" if index == 0 else "DELTA_ONLY",
                    expansion_status="DISCOVERED",
                    pending_action_count=len(actions),
                    activity=self.capture.activity,
                    foreground_package=self.capture.package_name,
                    screenshot_sha=self.capture.screenshot_sha,
                    stable_status="STABLE",
                )
                session.add(state)
                session.flush()
                action_map = build_action_map(
                    run_id=self.run_id,
                    branch_key="guest",
                    state_id=int(state.id),
                    activity=self.capture.activity,
                    screen_size=(120, 200),
                    actions=actions,
                )
                works.append(
                    StateWork(
                        state_id=int(state.id),
                        state_key=state.state_key,
                        cluster_key=state.cluster_key,
                        replay_key=self.capture.model.replay_key,
                        package_name=self.capture.package_name,
                        activity=self.capture.activity,
                        screenshot_sha=self.capture.screenshot_sha,
                        depth=index,
                        path=[],
                        actions=actions,
                        action_map=action_map,
                        semantic_key=self.capture.model.semantic_key,
                        ancestry_state_ids=(int(state.id),),
                        instance_anchor=state.instance_anchor,
                        exploration_family_id=self.family_id,
                        exploration_mode=state.exploration_mode,
                    )
                )
                if index == 0:
                    family.representative_state_id = state.id
            family.member_count = len(works)
            session.add(family)
            session.commit()
        return works

    def _run_branch(
        self,
        works,
        persist_results,
        wait_captures,
        performer,
        *,
        coverage_scheduler=False,
    ):
        device = Mock()
        device.window_size.return_value = (120, 200)
        capture_by_state = {work.state_id: self.capture for work in works}
        publisher = Mock()

        def ensure_parent(**kwargs):
            return capture_by_state[kwargs["parent"].state_id]

        with (
            patch(
                "backend.inspection.engine.engine",
                self.engine,
            ),
            patch(
                "backend.inspection.engine._prepare_branch",
            ),
            patch(
                "backend.inspection.engine.wait_for_stable_page",
                side_effect=wait_captures,
            ),
            patch(
                "backend.inspection.engine._ensure_parent",
                side_effect=ensure_parent,
            ),
            patch(
                "backend.inspection.engine._persist_state",
                side_effect=persist_results,
            ),
            patch(
                "backend.inspection.engine._restore_parent_after_transition",
                return_value=self.capture,
            ),
            patch(
                "backend.inspection.engine.perform_action",
                side_effect=performer,
            ) as perform_mock,
            patch(
                "backend.inspection.engine._probe_ui_automation_responsive",
                return_value=True,
            ),
            patch(
                "backend.inspection.engine.is_white_screen",
                return_value=False,
            ),
            patch(
                "backend.inspection.engine._verify_stable_paths",
                return_value=1,
            ),
            patch(
                "backend.inspection.engine._persist_work_action_map",
            ),
            patch(
                "backend.inspection.engine._publish_live",
                publisher,
            ),
        ):
            outcome = _execute_branch(
                run_id=self.run_id,
                branch_run_id=self.branch_id,
                device=device,
                device_serial="android-1",
                package_name="com.demo",
                profile={
                    "inspection_identity_v2": True,
                    "inspection_similarity_convergence": False,
                    "inspection_exploration_family_convergence": True,
                    "inspection_coverage_scheduler_v2": coverage_scheduler,
                    "budgets": {
                        "duration_seconds": 30,
                        "max_states": 20,
                        "max_actions": 50,
                        "max_depth": 10,
                    },
                },
                branch_config={},
                abort_event=threading.Event(),
                monitor=None,
            )
        return outcome, perform_mock, publisher

    def test_successful_role_is_covered_on_later_member_without_device_call(self):
        role_key = "role-buy"
        first_action = _action(
            "buy-a",
            role="COMMAND:BUY",
            role_key=role_key,
        )
        second_action = _action(
            "buy-b",
            role="COMMAND:BUY",
            role_key=role_key,
        )
        first, second = self._seed_works([[first_action], [second_action]])

        outcome, performer, publisher = self._run_branch(
            [first, second],
            [
                PersistedState(work=first, is_new=True),
                PersistedState(work=second, is_new=True),
            ],
            [self.capture, self.capture],
            lambda *_args, **_kwargs: "description",
            coverage_scheduler=True,
        )

        self.assertIn(outcome.status, {"PASS", "WARNING"})
        self.assertEqual(performer.call_count, 1)
        self.assertTrue(
            any(
                call.args[1] == "ACTION_COVERED_BY_FAMILY"
                for call in publisher.call_args_list
            )
        )
        with Session(self.engine) as session:
            transitions = session.exec(select(InspectionTransition).order_by(InspectionTransition.sequence)).all()
            coverage = session.exec(
                select(InspectionFamilyActionCoverage).where(
                    InspectionFamilyActionCoverage.family_id == self.family_id,
                    InspectionFamilyActionCoverage.action_role_key == role_key,
                )
            ).one()
            second_state = session.get(InspectionState, second.state_id)
        self.assertEqual(
            [item.status for item in transitions],
            ["PASS", "COVERED_BY_FAMILY"],
        )
        self.assertEqual(transitions[1].execution_disposition, "FAMILY_REUSED")
        self.assertEqual(
            transitions[1].coverage_source_transition_id,
            transitions[0].id,
        )
        self.assertEqual(coverage.status, "SUCCESS")
        self.assertEqual(coverage.source_transition_id, transitions[0].id)
        self.assertEqual(second_state.expansion_status, "EXPANDED")
        self.assertIsNone(second_state.queued_at)

    def test_success_without_transition_evidence_is_executed_again(self):
        role_key = "role-without-evidence"
        action = _action(
            "buy-without-evidence",
            role="COMMAND:BUY",
            role_key=role_key,
        )
        (work,) = self._seed_works([[action]])
        with Session(self.engine) as session:
            session.add(
                InspectionFamilyActionCoverage(
                    family_id=self.family_id,
                    action_role_key=role_key,
                    action_role=action.action_role,
                    status="SUCCESS",
                    source_state_id=work.state_id,
                    source_transition_id=None,
                    max_attempts=2,
                )
            )
            session.commit()

        outcome, performer, _publisher = self._run_branch(
            [work],
            [
                PersistedState(work=work, is_new=True),
                PersistedState(work=work, is_new=False),
            ],
            [self.capture, self.capture],
            lambda *_args, **_kwargs: "description",
        )

        self.assertIn(outcome.status, {"PASS", "WARNING"})
        performer.assert_called_once()
        with Session(self.engine) as session:
            transition = session.exec(select(InspectionTransition)).one()
            coverage = session.exec(
                select(InspectionFamilyActionCoverage).where(
                    InspectionFamilyActionCoverage.family_id == self.family_id,
                    InspectionFamilyActionCoverage.action_role_key == role_key,
                )
            ).one()
        self.assertNotEqual(transition.status, "COVERED_BY_FAMILY")
        self.assertEqual(coverage.source_transition_id, transition.id)

    def test_category_tab_self_loop_does_not_cover_later_member(self):
        role_key = "role-category-tab"
        first_action = _action(
            "category-a",
            role="CATEGORY_TAB:top",
            role_key=role_key,
            label="分类 A",
        )
        second_action = _action(
            "category-b",
            role="CATEGORY_TAB:top",
            role_key=role_key,
            label="分类 B",
        )
        discover_second = _action(
            "discover-second",
            role="NAV:DISCOVER_SECOND",
            role_key="role-discover-second",
        )
        first, second = self._seed_works([[first_action, discover_second], [second_action]])

        outcome, performer, _publisher = self._run_branch(
            [first, second],
            [
                PersistedState(work=first, is_new=True),
                PersistedState(work=first, is_new=False),
                PersistedState(work=second, is_new=True),
                PersistedState(work=second, is_new=False),
            ],
            [self.capture, self.capture, self.capture, self.capture],
            lambda *_args, **_kwargs: "description",
        )

        self.assertIn(outcome.status, {"PASS", "WARNING"})
        self.assertEqual(performer.call_count, 3)
        with Session(self.engine) as session:
            transitions = session.exec(select(InspectionTransition).order_by(InspectionTransition.sequence)).all()
            coverage = session.exec(
                select(InspectionFamilyActionCoverage).where(
                    InspectionFamilyActionCoverage.family_id == self.family_id,
                    InspectionFamilyActionCoverage.action_role_key == role_key,
                )
            ).one()
        self.assertEqual(
            [item.status for item in transitions],
            ["SELF_LOOP", "PASS", "SELF_LOOP"],
        )
        self.assertNotIn("COVERED_BY_FAMILY", {item.status for item in transitions})
        self.assertEqual(coverage.status, "FAILED")
        self.assertEqual(coverage.attempt_count, 2)

    def test_family_one_samples_one_state_changing_member_per_page(self):
        group_key = "category-tab-group"
        actions = [
            _action(
                f"category-{suffix}",
                role="CATEGORY_TAB:top",
                role_key="role-category-tab",
                label=f"分类 {suffix}",
                group_key=group_key,
                sample_policy="FAMILY_ONE",
            )
            for suffix in ("当前", "目标", "多余")
        ]
        first, target = self._seed_works([actions, []])

        outcome, performer, _publisher = self._run_branch(
            [first, target],
            [
                PersistedState(work=first, is_new=True),
                PersistedState(work=first, is_new=False),
                PersistedState(work=target, is_new=True),
            ],
            [self.capture, self.capture, self.capture],
            lambda *_args, **_kwargs: "description",
            coverage_scheduler=True,
        )

        self.assertIn(outcome.status, {"PASS", "WARNING"})
        self.assertEqual(performer.call_count, 2)
        with Session(self.engine) as session:
            transitions = session.exec(
                select(InspectionTransition).order_by(
                    InspectionTransition.sequence
                )
            ).all()
        self.assertEqual(
            [item.status for item in transitions],
            ["SELF_LOOP", "PASS", "SAMPLED_OUT"],
        )
        self.assertEqual(transitions[-1].execution_disposition, "SAMPLED_OUT")

    def test_family_one_sampling_survives_same_instance_state_variant(self):
        group_key = "side-category-group"
        first_action = _action(
            "category-washer",
            role="CATEGORY_TAB:side",
            role_key="role-category-washer",
            label="洗衣机",
            group_key=group_key,
            sample_policy="FAMILY_ONE",
        )
        variant_action = _action(
            "category-air-conditioner",
            role="CATEGORY_TAB:side",
            role_key="role-category-air-conditioner",
            label="空调",
            group_key=group_key,
            sample_policy="FAMILY_ONE",
        )
        first, variant = self._seed_works([[first_action], [variant_action]])
        variant.instance_anchor = first.instance_anchor
        variant.semantic_key = f"{first.semantic_key}-category-variant"
        with Session(self.engine) as session:
            variant_state = session.get(InspectionState, variant.state_id)
            variant_state.semantic_key = variant.semantic_key
            variant_state.instance_anchor = first.instance_anchor
            session.add(variant_state)
            session.commit()

        outcome, performer, _publisher = self._run_branch(
            [first, variant],
            [
                PersistedState(work=first, is_new=True),
                PersistedState(work=variant, is_new=True),
            ],
            [self.capture, self.capture],
            lambda *_args, **_kwargs: "description",
            coverage_scheduler=True,
        )

        self.assertIn(outcome.status, {"PASS", "WARNING"})
        self.assertEqual(performer.call_count, 1)
        with Session(self.engine) as session:
            transitions = session.exec(
                select(InspectionTransition).order_by(
                    InspectionTransition.sequence
                )
            ).all()
        self.assertEqual(
            [item.status for item in transitions],
            ["PASS", "SAMPLED_OUT"],
        )

    def test_page_one_sampling_survives_same_instance_state_variant(self):
        group_key = "filter-open-group"
        first_action = _action(
            "filter-before-sort",
            role="FILTER_OPEN",
            role_key="role-filter-before-sort",
            label="筛选",
            group_key=group_key,
            sample_policy="PAGE_ONE",
        )
        variant_action = _action(
            "filter-after-sort",
            role="FILTER_OPEN",
            role_key="role-filter-after-sort",
            label="筛选",
            group_key=group_key,
            sample_policy="PAGE_ONE",
        )
        first, variant = self._seed_works([[first_action], [variant_action]])
        variant.instance_anchor = first.instance_anchor
        variant.semantic_key = f"{first.semantic_key}-filter-variant"
        with Session(self.engine) as session:
            variant_state = session.get(InspectionState, variant.state_id)
            variant_state.semantic_key = variant.semantic_key
            variant_state.instance_anchor = first.instance_anchor
            session.add(variant_state)
            session.commit()

        outcome, performer, _publisher = self._run_branch(
            [first, variant],
            [
                PersistedState(work=first, is_new=True),
                PersistedState(work=variant, is_new=True),
            ],
            [self.capture, self.capture],
            lambda *_args, **_kwargs: "description",
            coverage_scheduler=True,
        )

        self.assertIn(outcome.status, {"PASS", "WARNING"})
        self.assertEqual(performer.call_count, 1)
        with Session(self.engine) as session:
            transitions = session.exec(
                select(InspectionTransition).order_by(
                    InspectionTransition.sequence
                )
            ).all()
        self.assertEqual(
            [item.status for item in transitions],
            ["PASS", "SAMPLED_OUT"],
        )

    def test_overlay_cleanup_actions_are_never_sampled_out(self):
        group_key = "dialog-close-group"
        first_action = _action(
            "close-dialog-first",
            role="DIALOG_CLOSE",
            role_key="role-dialog-close",
            label="关闭弹窗",
            group_key=group_key,
            sample_policy="PAGE_ONE",
        )
        variant_action = _action(
            "close-dialog-variant",
            role="DIALOG_CLOSE",
            role_key="role-dialog-close",
            label="关闭弹窗",
            group_key=group_key,
            sample_policy="PAGE_ONE",
        )
        (work,) = self._seed_works([[first_action, variant_action]])

        outcome, performer, _publisher = self._run_branch(
            [work],
            [
                PersistedState(work=work, is_new=True),
                PersistedState(work=work, is_new=False),
                PersistedState(work=work, is_new=False),
            ],
            [self.capture, self.capture, self.capture],
            lambda *_args, **_kwargs: "description",
            coverage_scheduler=True,
        )

        self.assertIn(outcome.status, {"PASS", "WARNING"})
        self.assertEqual(performer.call_count, 2)
        with Session(self.engine) as session:
            transitions = session.exec(
                select(InspectionTransition).order_by(
                    InspectionTransition.sequence
                )
            ).all()
        self.assertEqual(
            [item.status for item in transitions],
            ["SELF_LOOP", "SELF_LOOP"],
        )

    def test_page_one_stops_after_two_failed_candidates_in_same_group(self):
        actions = [
            _action(
                f"appointment-{index}",
                role="STORE_APPOINTMENT",
                role_key="store-appointment-role",
                label="立即预约",
                group_key="store-appointment-group",
                sample_policy="PAGE_ONE",
            )
            for index in range(4)
        ]
        (work,) = self._seed_works([actions])

        outcome, performer, _publisher = self._run_branch(
            [work],
            [PersistedState(work=work, is_new=True)],
            [self.capture],
            lambda *_args, **_kwargs: (_ for _ in ()).throw(
                RuntimeError("locator unavailable")
            ),
            coverage_scheduler=True,
        )

        self.assertEqual(outcome.status, "WARNING")
        self.assertEqual(performer.call_count, 2)
        with Session(self.engine) as session:
            transitions = session.exec(
                select(InspectionTransition).order_by(
                    InspectionTransition.sequence
                )
            ).all()
        self.assertEqual(len(transitions), 4)
        self.assertEqual(
            [transition.status for transition in transitions[-2:]],
            ["SAMPLED_OUT", "SAMPLED_OUT"],
        )

    def test_failed_role_executes_on_at_most_two_family_members(self):
        shared_role_key = "role-unavailable"
        nav_first = _action(
            "discover-b",
            role="NAV:DISCOVER_B",
            role_key="role-discover-b",
        )
        nav_second = _action(
            "discover-c",
            role="NAV:DISCOVER_C",
            role_key="role-discover-c",
        )
        fail_actions = [
            _action(
                f"unavailable-{suffix}",
                role="COMMAND:UNAVAILABLE",
                role_key=shared_role_key,
            )
            for suffix in ("a", "b", "c")
        ]
        first, second, third = self._seed_works(
            [
                [nav_first, fail_actions[0]],
                [nav_second, fail_actions[1]],
                [fail_actions[2]],
            ]
        )

        def perform(_device, action, **_kwargs):
            if action.action_role_key == shared_role_key:
                raise RuntimeError("locator temporarily unavailable")
            return "description"

        outcome, performer, _publisher = self._run_branch(
            [first, second, third],
            [
                PersistedState(work=first, is_new=True),
                PersistedState(work=second, is_new=True),
                PersistedState(work=third, is_new=True),
            ],
            [self.capture, self.capture, self.capture],
            perform,
        )

        self.assertEqual(outcome.status, "WARNING")
        shared_calls = [call for call in performer.call_args_list if call.args[1].action_role_key == shared_role_key]
        self.assertEqual(len(shared_calls), 2)
        with Session(self.engine) as session:
            transitions = session.exec(select(InspectionTransition).order_by(InspectionTransition.sequence)).all()
            coverage = session.exec(
                select(InspectionFamilyActionCoverage).where(
                    InspectionFamilyActionCoverage.family_id == self.family_id,
                    InspectionFamilyActionCoverage.action_role_key == shared_role_key,
                )
            ).one()
        shared_transitions = [item for item in transitions if item.action_role_key == shared_role_key]
        self.assertEqual(
            [item.status for item in shared_transitions],
            ["ACTION_ERROR", "ACTION_ERROR", "COVERAGE_EXHAUSTED"],
        )
        self.assertEqual(shared_transitions[-1].execution_disposition, "SKIPPED")
        self.assertEqual(shared_transitions[-1].failure_type, "COVERAGE_EXHAUSTED")
        self.assertEqual(coverage.status, "FAILED")
        self.assertEqual(coverage.attempt_count, 2)

    def test_external_app_result_exhausts_after_two_family_members(self):
        shared_role_key = "role-external"
        nav_first = _action(
            "discover-external-b",
            role="NAV:DISCOVER_EXTERNAL_B",
            role_key="role-discover-external-b",
        )
        nav_second = _action(
            "discover-external-c",
            role="NAV:DISCOVER_EXTERNAL_C",
            role_key="role-discover-external-c",
        )
        external_actions = [
            _action(
                f"external-{suffix}",
                role="COMMAND:EXTERNAL",
                role_key=shared_role_key,
            )
            for suffix in ("a", "b", "c")
        ]
        first, second, third = self._seed_works(
            [
                [nav_first, external_actions[0]],
                [nav_second, external_actions[1]],
                [external_actions[2]],
            ]
        )
        external_xml = _page_xml().replace("com.demo", "com.external")
        external_capture = CapturedPage(
            package_name="com.external",
            activity=".ExternalActivity",
            xml=external_xml,
            screenshot_png=_png("black"),
            screenshot_sha="external-sha",
            perceptual_hash="f" * 16,
            model=build_page_model(
                external_xml,
                package_name="com.external",
                activity=".ExternalActivity",
            ),
            stable_by="exact",
        )
        next_capture = self.capture

        def perform(_device, action, **_kwargs):
            nonlocal next_capture
            next_capture = (
                external_capture
                if action.action_role_key == shared_role_key
                else self.capture
            )
            return "description"

        def wait_for_capture(*_args, **_kwargs):
            return next_capture

        outcome, performer, _publisher = self._run_branch(
            [first, second, third],
            [
                PersistedState(work=first, is_new=True),
                PersistedState(work=second, is_new=True),
                PersistedState(work=third, is_new=True),
            ],
            wait_for_capture,
            perform,
        )

        self.assertEqual(outcome.status, "WARNING")
        shared_calls = [
            call
            for call in performer.call_args_list
            if call.args[1].action_role_key == shared_role_key
        ]
        self.assertEqual(len(shared_calls), 2)
        with Session(self.engine) as session:
            transitions = session.exec(
                select(InspectionTransition).order_by(InspectionTransition.sequence)
            ).all()
            coverage = session.exec(
                select(InspectionFamilyActionCoverage).where(
                    InspectionFamilyActionCoverage.family_id == self.family_id,
                    InspectionFamilyActionCoverage.action_role_key == shared_role_key,
                )
            ).one()
        shared_transitions = [
            item for item in transitions if item.action_role_key == shared_role_key
        ]
        self.assertEqual(
            [item.status for item in shared_transitions],
            ["EXTERNAL_APP", "EXTERNAL_APP", "COVERAGE_EXHAUSTED"],
        )
        self.assertEqual(coverage.status, "FAILED")
        self.assertEqual(coverage.attempt_count, 2)

    def test_instance_entry_actions_are_not_reused_across_family(self):
        role_key = "role-item-open"
        first_action = _action(
            "open-item-a",
            role="ITEM_OPEN:collection",
            role_key=role_key,
            label="商品 A",
        )
        second_action = _action(
            "open-item-b",
            role="ITEM_OPEN:collection",
            role_key=role_key,
            label="商品 B",
        )
        first, second = self._seed_works([[first_action], [second_action]])

        outcome, performer, _publisher = self._run_branch(
            [first, second],
            [
                PersistedState(work=first, is_new=True),
                PersistedState(work=second, is_new=True),
                PersistedState(work=second, is_new=False),
            ],
            [self.capture, self.capture, self.capture],
            lambda *_args, **_kwargs: "description",
        )

        self.assertIn(outcome.status, {"PASS", "WARNING"})
        self.assertEqual(performer.call_count, 2)
        with Session(self.engine) as session:
            coverage = session.exec(
                select(InspectionFamilyActionCoverage).where(
                    InspectionFamilyActionCoverage.family_id == self.family_id,
                    InspectionFamilyActionCoverage.action_role_key == role_key,
                )
            ).first()
            transitions = session.exec(select(InspectionTransition)).all()
        self.assertIsNone(coverage)
        self.assertNotIn(
            "COVERED_BY_FAMILY",
            {transition.status for transition in transitions},
        )

    def test_scroll_actions_are_scanned_per_member_but_remain_cycle_evidence(self):
        (work,) = self._seed_works([[]])
        scroll = InspectionAction(
            action_type="scroll",
            action_key="scroll-down",
            locator_candidates=[],
            target_meta={"direction": "down"},
            action_role="SCROLL:vertical:down",
            action_role_key="scroll-role",
        )

        self.assertIsNone(_family_action_pair(work, scroll))
        self.assertEqual(
            _family_action_pair(work, scroll, include_scroll=True),
            (self.family_id, "scroll-role"),
        )

    def test_duplicate_bad_locator_counts_once_per_family_member(self):
        role_key = "role-shared-locator"
        actions = [
            _action(
                f"bad-locator-{index}",
                role="COMMAND:DETAIL",
                role_key=role_key,
                label="自营",
            )
            for index in range(2)
        ]
        actions[1].locator_candidates[0]["selector"] = "权益"
        (work,) = self._seed_works([actions])

        outcome, performer, _publisher = self._run_branch(
            [work],
            [PersistedState(work=work, is_new=True)],
            [self.capture],
            lambda *_args, **_kwargs: (_ for _ in ()).throw(
                RuntimeError("locator unavailable")
            ),
        )

        self.assertEqual(outcome.status, "WARNING")
        self.assertEqual(performer.call_count, 2)
        with Session(self.engine) as session:
            coverage = session.exec(
                select(InspectionFamilyActionCoverage).where(
                    InspectionFamilyActionCoverage.family_id == self.family_id,
                    InspectionFamilyActionCoverage.action_role_key == role_key,
                )
            ).one()
        self.assertEqual(coverage.status, "FAILED")
        self.assertEqual(coverage.attempt_count, 1)


if __name__ == "__main__":
    unittest.main()
