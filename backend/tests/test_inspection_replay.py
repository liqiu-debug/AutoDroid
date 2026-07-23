import json
import threading
import unittest
from unittest.mock import Mock, patch

from sqlmodel import SQLModel, Session, create_engine

from backend.inspection.device import CapturedPage, LocatorDrift
from backend.inspection.replay import (
    REPLAY_SCOPE_FULL_PATH,
    REPLAY_SCOPE_SAFETY_PREFIX,
    REACHABILITY_OBSERVED_ONCE,
    ReplayPlanError,
    build_replay_plan,
    derive_replay_eligibility,
    evaluate_reachability,
    execute_replay_chain,
    normalise_terminal_outcome,
    rebind_replay_action,
    state_reachability_evidence,
    terminal_boundaries_for_state,
)
from backend.inspection.semantics import (
    build_page_model,
    derive_instance_anchor,
    enumerate_actions,
)
from backend.models import (
    InspectionBranchRun,
    InspectionExplorationFamily,
    InspectionFault,
    InspectionObservation,
    InspectionPageTemplate,
    InspectionRun,
    InspectionState,
    InspectionTransition,
)


PACKAGE = "com.demo.mall"


def _signature(page, *, anchor=None):
    return {
        "version": 1,
        "package": page.package_name,
        "activity_family": page.activity_family,
        "role": page.role,
        "instance_anchor": anchor or derive_instance_anchor(page),
        "content_anchor": derive_instance_anchor(page),
        "structure_tokens": list(page.template_tokens),
        "action_tokens": list(page.action_tokens),
        "control_tokens": list(page.control_tokens),
        "risk_tokens": list(page.risk_tokens),
    }


def _serialize_action(action, source, target):
    return {
        "action_type": action.action_type,
        "action_key": action.action_key,
        "locator_candidates": [dict(item) for item in action.locator_candidates],
        "target_meta": dict(action.target_meta),
        "coordinate_only": action.coordinate_only,
        "replayable": action.replayable,
        "risk_type": action.risk_type,
        "blocked_reason": action.blocked_reason,
        "input_rule_id": action.input_rule_id,
        "input_variable_key": action.input_variable_key,
        "action_role": action.action_role,
        "action_role_key": action.action_role_key,
        "action_anchor_key": action.action_anchor_key,
        "action_group_key": action.action_group_key,
        "action_instance_key": action.action_instance_key,
        "sample_policy": action.sample_policy,
        "expected_source_semantic_key": source.semantic_key,
        "expected_target_semantic_key": target.semantic_key,
        "expected_target_role": target.role,
        "expected_target_template_key": target.template_key,
        "expected_source_signature": _signature(source),
        "expected_target_signature": _signature(target),
    }


def _capture(page):
    return CapturedPage(
        package_name=page.package_name,
        activity=page.activity,
        xml=page.xml,
        screenshot_png=b"png",
        screenshot_sha="sha",
        perceptual_hash="0" * 16,
        model=page,
        stable_by="exact",
    )


class ReplayPlanTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine(
            "sqlite://",
            connect_args={"check_same_thread": False},
        )
        SQLModel.metadata.create_all(self.engine)
        self.session = Session(self.engine)
        self.run = InspectionRun(
            name="source",
            package_name=PACKAGE,
            device_serial="device-1",
            status="WARNING",
            selected_branches=["authenticated"],
            profile_snapshot={
                "effective_features": {"inspection_identity_v2": True}
            },
        )
        self.session.add(self.run)
        self.session.flush()
        self.branch = InspectionBranchRun(
            run_id=self.run.id,
            branch_key="authenticated",
            branch_name="Authenticated",
            status="WARNING",
        )
        self.session.add(self.branch)
        self.session.flush()
        self.root = self._state(
            semantic_key="home",
            subtype="HOME",
            role="HOME",
            path=[],
            stable_status="UNVERIFIED",
        )
        self.branch.root_state_id = self.root.id
        self.session.add(self.branch)
        self.session.commit()

    def tearDown(self):
        self.session.close()

    def _state(
        self,
        *,
        semantic_key,
        subtype,
        role,
        path,
        stable_status="UNVERIFIED",
        expansion_status="EXPANDED",
        opaque=False,
    ):
        template = InspectionPageTemplate(
            package_name=PACKAGE,
            activity=".MainActivity",
            activity_family="main",
            page_role=role,
            template_key=f"template-{semantic_key}",
            structure_signature=[f"structure-{semantic_key}"],
            action_signature=[f"action-{semantic_key}"],
            anchor_signature=[f"anchor-{semantic_key}"],
        )
        self.session.add(template)
        self.session.flush()
        family = InspectionExplorationFamily(
            run_id=self.run.id,
            branch_run_id=self.branch.id,
            family_key=f"family-{semantic_key}",
            page_role=role,
            activity_family="main",
        )
        self.session.add(family)
        self.session.flush()
        state = InspectionState(
            run_id=self.run.id,
            branch_run_id=self.branch.id,
            branch_key=self.branch.branch_key,
            cluster_key=f"cluster-{semantic_key}",
            state_key=f"state-{semantic_key}",
            semantic_key=semantic_key,
            identity_version=2,
            instance_anchor=f"instance-{semantic_key}",
            template_id=template.id,
            exploration_family_id=family.id,
            activity=".MainActivity",
            foreground_package=PACKAGE,
            page_subtype=subtype,
            expansion_status=expansion_status,
            stable_status=stable_status,
            first_path=path,
            is_opaque=opaque,
        )
        self.session.add(state)
        self.session.flush()
        observation = InspectionObservation(
            run_id=self.run.id,
            branch_run_id=self.branch.id,
            state_id=state.id,
            template_id=template.id,
            sequence=state.id,
            package_name=PACKAGE,
            exact_cluster_key=state.cluster_key,
            exact_replay_key=semantic_key,
            exact_state_key=state.state_key,
            is_representative=True,
        )
        self.session.add(observation)
        self.session.flush()
        state.representative_observation_id = observation.id
        family.representative_state_id = state.id
        self.session.add(state)
        self.session.add(family)
        return state

    def _step(self, *, source, target, action_key, role="COMMAND:OPEN", risk=None):
        return {
            "action_type": "click",
            "action_key": action_key,
            "locator_candidates": [
                {
                    "by": "description",
                    "selector": action_key,
                    "expected_class": "android.widget.Button",
                }
            ],
            "target_meta": {
                "class": "android.widget.Button",
                "content_desc": action_key,
                "ancestor_semantic": "content",
                "relative_bucket": "r1c1",
                "bounds": [100, 100, 300, 200],
                "screen_size": [1080, 1920],
                "enabled": True,
                "checked": False,
                "selected": False,
            },
            "coordinate_only": False,
            "replayable": True,
            "risk_type": risk,
            "blocked_reason": "blocked" if risk else None,
            "action_role": role,
            "action_role_key": f"role-{action_key}",
            "action_anchor_key": f"anchor-{action_key}",
            "action_group_key": f"group-{action_key}",
            "expected_source_semantic_key": source,
            "expected_target_semantic_key": target,
            "expected_target_role": "LIST",
            "expected_source_signature": {
                "package": PACKAGE,
                "activity_family": "main",
                "role": "HOME",
                "instance_anchor": f"instance-{source}",
                "content_anchor": f"instance-{source}",
                "structure_tokens": [source],
                "action_tokens": [action_key],
                "control_tokens": [],
                "risk_tokens": [],
            },
            "expected_target_signature": {
                "package": PACKAGE,
                "activity_family": "main",
                "role": "LIST",
                "instance_anchor": f"instance-{target}",
                "content_anchor": f"instance-{target}",
                "structure_tokens": [target],
                "action_tokens": [],
                "control_tokens": [],
                "risk_tokens": [],
            },
        }

    def _connect(self, source, target, step, status="PASS"):
        transition = InspectionTransition(
            run_id=self.run.id,
            branch_run_id=self.branch.id,
            from_state_id=source.id,
            to_state_id=target.id,
            action_type=step["action_type"],
            action_key=step["action_key"],
            locator_candidates=step["locator_candidates"],
            target_meta=step["target_meta"],
            action_role=step["action_role"],
            action_role_key=step["action_role_key"],
            status=status,
            replayable=True,
        )
        self.session.add(transition)
        self.session.flush()
        target.incoming_transition_id = transition.id
        target.parent_state_id = source.id
        self.session.add(target)
        self.session.commit()

    def test_run_without_stable_paths_builds_observed_once_chain(self):
        category_step = self._step(
            source="home",
            target="category",
            action_key="category",
            role="NAV:CATEGORY",
        )
        category = self._state(
            semantic_key="category",
            subtype="CATALOG_CATEGORY",
            role="LIST",
            path=[category_step],
        )
        self._connect(self.root, category, category_step)
        product_step = self._step(
            source="category",
            target="product",
            action_key="product",
            role="ITEM_OPEN:collection",
        )
        product = self._state(
            semantic_key="product",
            subtype="PRODUCT_DETAIL",
            role="PRODUCT_DETAIL",
            path=[category_step, product_step],
        )
        self._connect(category, product, product_step)

        plan = build_replay_plan(
            self.session,
            self.run.id,
            "authenticated",
        )

        self.assertTrue(plan["chains"])
        self.assertEqual(len(plan["chains"]), 1)
        self.assertEqual(plan["chains"][0]["endpoint_state_id"], product.id)
        self.assertEqual(plan["chains"][0]["display_label"], "P003")
        self.assertEqual(plan["chains"][0]["display_index"], 3)
        self.assertEqual(plan["chains"][0]["source_observation_index"], 1)
        self.assertEqual(
            [item["display_label"] for item in plan["chains"][0]["checkpoints"]],
            ["P001", "P002", "P003"],
        )
        self.assertEqual(len(plan["prefix_tree"]["nodes"]), 3)
        self.assertEqual(
            {chain["evidence_level"] for chain in plan["chains"]},
            {"OBSERVED_ONCE"},
        )
        self.assertIn("HOME", plan["summary"]["covered_subtypes"])
        self.assertIn("CATALOG_CATEGORY", plan["summary"]["covered_subtypes"])
        self.assertIn("PRODUCT_DETAIL", plan["summary"]["covered_subtypes"])
        self.assertEqual(len(plan["digest"]), 64)

    def test_stable_state_is_verified_twice(self):
        step = self._step(source="home", target="stable", action_key="stable")
        state = self._state(
            semantic_key="stable",
            subtype="SETTINGS",
            role="SETTINGS",
            path=[step],
            stable_status="STABLE",
        )
        self._connect(self.root, state, step)

        plan = build_replay_plan(self.session, self.run.id, "authenticated")

        stable_chain = next(
            chain for chain in plan["chains"] if chain["endpoint_state_id"] == state.id
        )
        self.assertEqual(stable_chain["evidence_level"], "VERIFIED_TWICE")

    def test_blocked_edge_keeps_safe_prefix_and_unexpanded_observation(self):
        category_step = self._step(
            source="home",
            target="category",
            action_key="category",
            role="NAV:CATEGORY",
        )
        category = self._state(
            semantic_key="category",
            subtype="CATALOG_CATEGORY",
            role="LIST",
            path=[category_step],
            expansion_status="QUEUED",
        )
        self._connect(self.root, category, category_step)
        blocked = InspectionTransition(
            run_id=self.run.id,
            branch_run_id=self.branch.id,
            from_state_id=category.id,
            action_type="click",
            action_key="pay",
            action_role="PAYMENT:SUBMIT",
            status="BLOCKED",
            risk_type="PAYMENT",
            reason="支付动作已拦截",
            replayable=True,
        )
        self.session.add(blocked)
        self.session.commit()

        plan = build_replay_plan(self.session, self.run.id, "authenticated")
        chain = next(
            item for item in plan["chains"]
            if item["endpoint_state_id"] == category.id
        )
        self.assertEqual(plan["plan_version"], 3)
        self.assertEqual(chain["replay_eligibility"], "SAFE_PREFIX")
        self.assertEqual(chain["depth"], 1)
        self.assertIn("SAFETY_BLOCKED", {
            item["boundary_type"] for item in chain["terminal_boundaries"]
        })
        self.assertNotIn("STATE_NOT_EXPANDED", plan["excluded"]["by_reason"])

    def test_executed_payment_edge_is_not_a_safety_boundary(self):
        category_step = self._step(
            source="home",
            target="category",
            action_key="category",
            role="NAV:CATEGORY",
        )
        category = self._state(
            semantic_key="category",
            subtype="CATALOG_CATEGORY",
            role="LIST",
            path=[category_step],
        )
        self._connect(self.root, category, category_step)
        executed = InspectionTransition(
            run_id=self.run.id,
            branch_run_id=self.branch.id,
            from_state_id=category.id,
            action_type="click",
            action_key="checkout",
            action_role="NAV:CHECKOUT",
            status="PASS",
            risk_type="PAYMENT",
            execution_disposition="EXECUTED",
            replayable=True,
        )
        self.session.add(executed)
        self.session.commit()

        boundaries = terminal_boundaries_for_state(
            category.id,
            transitions=[executed],
        )

        self.assertEqual(boundaries, [])
        plan = build_replay_plan(self.session, self.run.id, "authenticated")
        chain = next(
            item for item in plan["chains"]
            if item["endpoint_state_id"] == category.id
        )
        self.assertEqual(chain["replay_scope"], REPLAY_SCOPE_FULL_PATH)

        checkout_step = self._step(
            source="category",
            target="checkout",
            action_key="checkout",
            role="NAV:CHECKOUT",
            risk="PAYMENT",
        )
        checkout_step.update({
            "status": "PASS",
            "execution_disposition": "EXECUTED",
            "blocked_reason": None,
        })
        checkout = self._state(
            semantic_key="checkout",
            subtype="CHECKOUT",
            role="CHECKOUT",
            path=[category_step, checkout_step],
        )
        self._connect(category, checkout, checkout_step)

        plan = build_replay_plan(self.session, self.run.id, "authenticated")
        checkout_chain = next(
            item for item in plan["chains"]
            if item["endpoint_state_id"] == checkout.id
        )
        self.assertEqual(checkout_chain["depth"], 2)
        self.assertEqual(checkout_chain["replay_scope"], REPLAY_SCOPE_FULL_PATH)

    def test_fault_action_keeps_reachable_path_without_claiming_safe_prefix(self):
        first = self._step(source="home", target="category", action_key="category")
        category = self._state(
            semantic_key="category",
            subtype="CATALOG_CATEGORY",
            role="LIST",
            path=[first],
        )
        self._connect(self.root, category, first)
        second = self._step(source="category", target="product", action_key="product")
        product = self._state(
            semantic_key="product",
            subtype="PRODUCT_DETAIL",
            role="PRODUCT_DETAIL",
            path=[first, second],
        )
        self._connect(category, product, second)
        fault = InspectionFault(
            run_id=self.run.id,
            branch_run_id=self.branch.id,
            state_id=category.id,
            fault_type="CRASH",
            signature="category-product-crash",
            summary="商品入口触发崩溃",
            details={"current_action": dict(second)},
        )
        self.session.add(fault)
        self.session.commit()

        plan = build_replay_plan(self.session, self.run.id, "authenticated")
        chain = next(
            item for item in plan["chains"]
            if item["endpoint_state_id"] == category.id
        )
        self.assertEqual(chain["first_path"], [first])
        self.assertEqual(chain["replay_scope"], REPLAY_SCOPE_FULL_PATH)
        self.assertEqual(chain["replay_eligibility"], "FULL")
        self.assertTrue(any(
            item.get("terminal_outcome") == "APP_FAULT"
            for item in chain["terminal_boundaries"]
        ))

    def test_root_marked_stable_without_two_observations_remains_observed_once(self):
        self.root.stable_status = "STABLE"
        self.root.observation_count = 1
        self.session.add(self.root)
        self.session.commit()

        plan = build_replay_plan(self.session, self.run.id, "authenticated")

        root_chain = next(item for item in plan["chains"] if item["depth"] == 0)
        self.assertEqual(
            root_chain["reachability_evidence"],
            REACHABILITY_OBSERVED_ONCE,
        )
        self.assertEqual(
            state_reachability_evidence(self.root, has_observation=True),
            REACHABILITY_OBSERVED_ONCE,
        )

    def test_failed_outgoing_branch_does_not_downgrade_reached_state(self):
        step = self._step(source="home", target="category", action_key="category")
        category = self._state(
            semantic_key="category",
            subtype="CATALOG_CATEGORY",
            role="LIST",
            path=[step],
        )
        self._connect(self.root, category, step)
        transition = InspectionTransition(
            run_id=self.run.id,
            branch_run_id=self.branch.id,
            from_state_id=category.id,
            action_type="click",
            action_key="later",
            action_role="ITEM_OPEN:catalog",
            status="QUEUE_TRUNCATED",
            failure_type="PATH_DIVERGED_CASCADE",
            execution_disposition="NOT_REACHED",
        )
        self.session.add(transition)
        self.session.commit()

        boundaries = terminal_boundaries_for_state(
            category.id,
            transitions=[transition],
        )
        scope, _ = derive_replay_eligibility(category, boundaries)

        self.assertEqual(boundaries[0]["terminal_outcome"], "LOCATOR_FAILED")
        self.assertEqual(scope, REPLAY_SCOPE_FULL_PATH)
        self.assertNotEqual(scope, REPLAY_SCOPE_SAFETY_PREFIX)

    def test_unstable_state_is_not_in_default_replay_plan(self):
        step = self._step(source="home", target="unstable", action_key="unstable")
        unstable = self._state(
            semantic_key="unstable",
            subtype="PRODUCT_DETAIL",
            role="PRODUCT_DETAIL",
            path=[step],
            stable_status="UNSTABLE",
        )
        self._connect(self.root, unstable, step)

        plan = build_replay_plan(self.session, self.run.id, "authenticated")

        self.assertNotIn(
            unstable.id,
            {item["endpoint_state_id"] for item in plan["chains"]},
        )
        self.assertEqual(plan["summary"]["diagnostic_only_count"], 1)

    def test_terminal_outcomes_are_orthogonal(self):
        self.assertEqual(normalise_terminal_outcome("CRASH"), "APP_FAULT")
        self.assertEqual(
            normalise_terminal_outcome("ACTION_ERROR"),
            "AUTOMATION_FAILED",
        )
        self.assertEqual(
            normalise_terminal_outcome("EXTERNAL_APP"),
            "EXTERNAL_NAVIGATION",
        )
        self.assertEqual(
            normalise_terminal_outcome("DEVICE_DISCONNECTED"),
            "INFRA_FAULT",
        )

    def test_unsafe_path_is_excluded_and_digest_is_deterministic(self):
        safe_step = self._step(source="home", target="safe", action_key="safe")
        safe = self._state(
            semantic_key="safe",
            subtype="CART",
            role="LIST",
            path=[safe_step],
        )
        self._connect(self.root, safe, safe_step)
        risky_step = self._step(
            source="home",
            target="risky",
            action_key="pay",
            risk="PAYMENT",
        )
        risky = self._state(
            semantic_key="risky",
            subtype="CASHIER",
            role="CHECKOUT",
            path=[risky_step],
        )
        self._connect(self.root, risky, risky_step)

        first = build_replay_plan(self.session, self.run.id, "authenticated")
        second = build_replay_plan(self.session, self.run.id, "authenticated")

        self.assertEqual(first["digest"], second["digest"])
        self.assertNotIn(
            risky.id,
            {chain["endpoint_state_id"] for chain in first["chains"]},
        )
        self.assertEqual(first["excluded"]["by_reason"]["HISTORICAL_RISK"], 1)

    def test_failed_source_run_is_a_completed_replay_source(self):
        self.run.status = "FAIL"
        self.session.add(self.run)
        self.session.commit()

        plan = build_replay_plan(self.session, self.run.id, "authenticated")

        self.assertIn("chains", plan)

    def test_invalid_run_and_chain_limit_have_stable_error_codes(self):
        with self.assertRaises(ReplayPlanError) as missing:
            build_replay_plan(self.session, 99999, "authenticated")
        self.assertEqual(missing.exception.code, "RUN_NOT_FOUND")
        with self.assertRaises(ReplayPlanError) as limit:
            build_replay_plan(self.session, self.run.id, "authenticated", 21)
        self.assertEqual(limit.exception.code, "INVALID_MAX_CHAINS")


class ReplayRuntimeTests(unittest.TestCase):
    def _pages(self):
        source_xml = (
            '<hierarchy rotation="0">'
            f'<node package="{PACKAGE}" class="android.widget.FrameLayout" '
            'bounds="[0,0][1080,1920]">'
            '<node package="com.demo.mall" class="android.widget.TextView" '
            'text="Product detail" bounds="[10,80][500,150]"/>'
            '<node package="com.demo.mall" class="android.widget.Button" '
            'content-desc="Buy now" clickable="true" enabled="true" '
            'bounds="[100,1600][980,1750]"/>'
            "</node></hierarchy>"
        )
        target_xml = (
            '<hierarchy rotation="0">'
            f'<node package="{PACKAGE}" class="android.widget.FrameLayout" '
            'bounds="[0,0][1080,1920]">'
            '<node package="com.demo.mall" class="android.widget.TextView" '
            'text="Shipping address" bounds="[10,80][500,150]"/>'
            '<node package="com.demo.mall" class="android.widget.Button" '
            'content-desc="Place order" clickable="true" enabled="true" '
            'bounds="[100,1600][980,1750]"/>'
            "</node></hierarchy>"
        )
        return (
            build_page_model(
                source_xml,
                package_name=PACKAGE,
                activity=".ProductActivity",
            ),
            build_page_model(
                target_xml,
                package_name=PACKAGE,
                activity=".CheckoutActivity",
            ),
        )

    def test_reachability_allows_structure_change_when_role_and_anchor_match(self):
        source, _ = self._pages()
        anchor = derive_instance_anchor(source)
        self.assertTrue(anchor)
        checkpoint = {
            "semantic_key": "old-semantic-key",
            "role": source.role,
            "page_subtype": source.page_subtype,
            "instance_anchor": anchor,
            "expectation": {
                **_signature(source),
                "structure_tokens": ["old-structure"],
            },
        }

        evidence = evaluate_reachability(
            checkpoint,
            source,
            expected_package=PACKAGE,
        )

        self.assertEqual(evidence["status"], "MATCH")
        self.assertIn("STRUCTURE_CHANGED", evidence["warnings"])
        wrong = {**checkpoint, "role": "ORDER"}
        self.assertEqual(
            evaluate_reachability(wrong, source, expected_package=PACKAGE)["status"],
            "MISMATCH",
        )

    def test_rebind_recomputes_current_risk(self):
        source, target = self._pages()
        action = next(
            item
            for item in enumerate_actions(source, screen_size=(1080, 1920))
            if item.action_type == "click"
        )
        step = _serialize_action(action, source, target)

        bound = rebind_replay_action(step, source)
        blocked = rebind_replay_action(
            step,
            source,
            safety_rules=[
                {
                    "pattern": "Buy now",
                    "risk_type": "DESTRUCTIVE",
                }
            ],
        )

        self.assertEqual(bound.status, "BOUND")
        self.assertEqual(blocked.status, "BLOCKED")
        self.assertEqual(blocked.risk_type, "DESTRUCTIVE")

    def test_safety_boundary_is_verified_without_invoking_action(self):
        source, target = self._pages()
        action = next(
            item
            for item in enumerate_actions(source, screen_size=(1080, 1920))
            if item.action_type == "click"
        )
        boundary = _serialize_action(action, source, target)
        boundary["risk_type"] = "DESTRUCTIVE"
        boundary["blocked_reason"] = "支付动作已拦截"
        # This is the shape persisted by a real BLOCKED inspection transition:
        # it is intentionally non-replayable and may be coordinate-only, but
        # compatibility verification must still probe it without clicking.
        boundary["replayable"] = False
        boundary["coordinate_only"] = True
        chain = {
            "first_path": [],
            "checkpoints": [
                {
                    "semantic_key": source.semantic_key,
                    "role": source.role,
                    "page_subtype": source.page_subtype,
                    "instance_anchor": derive_instance_anchor(source),
                    "expectation": _signature(source),
                }
            ],
            "terminal_boundaries": [
                {
                    "boundary_id": "pay-boundary",
                    "boundary_type": "SAFETY_BLOCKED",
                    "terminal_outcome": "SAFETY_BLOCKED",
                    "risk_type": "DESTRUCTIVE",
                    "action": boundary,
                }
            ],
        }
        with patch("backend.inspection.replay.perform_action") as perform:
            result = execute_replay_chain(
                Mock(),
                chain,
                package_name=PACKAGE,
                abort_event=threading.Event(),
                initial_capture=_capture(source),
                safety_rules=[{"pattern": "Buy now", "risk_type": "DESTRUCTIVE"}],
            )
        self.assertEqual(result.status, "PASS")
        self.assertEqual(result.boundary_evidence, "VERIFIED")
        self.assertEqual(result.trace[-1]["status"], "BOUNDARY_VERIFIED")
        self.assertEqual(result.trace[-1]["boundary_evidence"], "VERIFIED")
        perform.assert_not_called()

    def test_safety_boundary_without_unique_probe_is_not_verifiable(self):
        source, _ = self._pages()
        chain = {
            "first_path": [],
            "checkpoints": [
                {
                    "semantic_key": source.semantic_key,
                    "role": source.role,
                    "page_subtype": source.page_subtype,
                    "instance_anchor": derive_instance_anchor(source),
                    "expectation": _signature(source),
                }
            ],
            "terminal_boundaries": [
                {
                    "boundary_id": "coordinate-only-boundary",
                    "boundary_type": "SAFETY_BLOCKED",
                    "terminal_outcome": "SAFETY_BLOCKED",
                    "risk_type": "PAYMENT",
                    "action": {
                        "action_type": "click",
                        "action_key": "legacy-coordinate",
                        "action_role": "PAYMENT:SUBMIT",
                        "target_meta": {},
                        "locator_candidates": [],
                        "coordinate_only": True,
                        "replayable": False,
                        "risk_type": "PAYMENT",
                    },
                }
            ],
        }

        with patch("backend.inspection.replay.perform_action") as perform:
            result = execute_replay_chain(
                Mock(),
                chain,
                package_name=PACKAGE,
                abort_event=threading.Event(),
                initial_capture=_capture(source),
                safety_rules=[{"pattern": "Buy now", "risk_type": "PAYMENT"}],
            )

        self.assertEqual(result.status, "WARNING")
        self.assertEqual(result.boundary_evidence, "NOT_VERIFIABLE")
        self.assertEqual(result.trace[-1]["status"], "BOUNDARY_NOT_VERIFIABLE")
        self.assertEqual(
            result.trace[-1]["boundary_evidence"],
            "NOT_VERIFIABLE",
        )
        perform.assert_not_called()

    def test_execute_chain_emits_trace_without_locator_or_input_value(self):
        source, target = self._pages()
        action = next(
            item
            for item in enumerate_actions(source, screen_size=(1080, 1920))
            if item.action_type == "click"
        )
        step = _serialize_action(action, source, target)
        chain = {
            "first_path": [step],
            "checkpoints": [
                {
                    "semantic_key": source.semantic_key,
                    "role": source.role,
                    "page_subtype": source.page_subtype,
                    "instance_anchor": derive_instance_anchor(source),
                    "expectation": _signature(source),
                },
                {
                    "semantic_key": target.semantic_key,
                    "role": target.role,
                    "page_subtype": target.page_subtype,
                    "instance_anchor": derive_instance_anchor(
                        target,
                        incoming_action=action,
                        source_instance_anchor=derive_instance_anchor(source),
                    ),
                    "expectation": _signature(
                        target,
                        anchor=derive_instance_anchor(
                            target,
                            incoming_action=action,
                            source_instance_anchor=derive_instance_anchor(source),
                        ),
                    ),
                },
            ],
        }
        # A changed implementation is retained as diagnostics and does not
        # turn an otherwise compatible reachability result into WARNING.
        chain["checkpoints"][0]["expectation"]["structure_tokens"] = [
            "changed-implementation"
        ]
        device = Mock()

        with patch(
            "backend.inspection.replay.perform_action"
        ) as perform, patch(
            "backend.inspection.replay.wait_for_stable_page",
            return_value=_capture(target),
        ):
            result = execute_replay_chain(
                device,
                chain,
                package_name=PACKAGE,
                abort_event=threading.Event(),
                initial_capture=_capture(source),
            )

        self.assertEqual(result.status, "PASS")
        self.assertEqual(result.completed_checkpoints, 2)
        self.assertEqual(result.trace[0]["status"], "PASS")
        self.assertIn(
            "STRUCTURE_CHANGED",
            result.trace[0]["source"]["warnings"],
        )
        perform.assert_called_once()
        serialized_trace = json.dumps(result.trace)
        self.assertNotIn("selector", serialized_trace)
        self.assertNotIn("input_value", serialized_trace)

    def test_execution_exception_keeps_failed_step_trace_and_redacts_locator(self):
        source, target = self._pages()
        action = next(
            item
            for item in enumerate_actions(source, screen_size=(1080, 1920))
            if item.action_type == "click"
        )
        step = _serialize_action(action, source, target)
        chain = {
            "first_path": [step],
            "checkpoints": [
                {
                    "semantic_key": source.semantic_key,
                    "role": source.role,
                    "page_subtype": source.page_subtype,
                    "instance_anchor": derive_instance_anchor(source),
                    "expectation": _signature(source),
                },
                {
                    "semantic_key": target.semantic_key,
                    "role": target.role,
                    "page_subtype": target.page_subtype,
                    "instance_anchor": derive_instance_anchor(target),
                    "expectation": _signature(target),
                },
            ],
        }
        with patch(
            "backend.inspection.replay.perform_action",
            side_effect=LocatorDrift("selector=secret-value"),
        ):
            result = execute_replay_chain(
                Mock(),
                chain,
                package_name=PACKAGE,
                abort_event=threading.Event(),
                initial_capture=_capture(source),
            )

        self.assertEqual(result.status, "FAIL")
        self.assertEqual(result.failed_step_index, 0)
        self.assertEqual(result.completed_checkpoints, 1)
        self.assertEqual(result.trace[-1]["failure_type"], "LOCATOR_NOT_FOUND")
        self.assertNotIn("secret-value", json.dumps(result.to_dict()))

    def test_multistep_target_anchor_is_reused_for_next_source_check(self):
        source, middle = self._pages()
        final_xml = (
            '<hierarchy rotation="0">'
            f'<node package="{PACKAGE}" class="android.widget.FrameLayout" '
            'bounds="[0,0][1080,1920]">'
            '<node package="com.demo.mall" class="android.widget.TextView" '
            'text="Order detail" bounds="[10,80][500,150]"/>'
            "</node></hierarchy>"
        )
        final = build_page_model(
            final_xml,
            package_name=PACKAGE,
            activity=".OrderActivity",
        )
        first_action = next(
            item
            for item in enumerate_actions(source, screen_size=(1080, 1920))
            if item.action_type == "click"
        )
        second_action = next(
            item
            for item in enumerate_actions(middle, screen_size=(1080, 1920))
            if item.action_type == "click"
        )
        first_step = _serialize_action(first_action, source, middle)
        second_step = _serialize_action(second_action, middle, final)
        middle_anchor = derive_instance_anchor(
            middle,
            incoming_action=first_action,
            source_instance_anchor=derive_instance_anchor(source),
        )
        final_anchor = derive_instance_anchor(
            final,
            incoming_action=second_action,
            source_instance_anchor=middle_anchor,
        )
        chain = {
            "first_path": [first_step, second_step],
            "checkpoints": [
                {
                    "semantic_key": source.semantic_key,
                    "role": source.role,
                    "page_subtype": source.page_subtype,
                    "instance_anchor": derive_instance_anchor(source),
                    "expectation": _signature(source),
                },
                {
                    "semantic_key": middle.semantic_key,
                    "role": middle.role,
                    "page_subtype": middle.page_subtype,
                    "instance_anchor": middle_anchor,
                    "expectation": _signature(middle, anchor=middle_anchor),
                },
                {
                    "semantic_key": final.semantic_key,
                    "role": final.role,
                    "page_subtype": final.page_subtype,
                    "instance_anchor": final_anchor,
                    "expectation": _signature(final, anchor=final_anchor),
                },
            ],
        }
        with patch(
            "backend.inspection.replay.perform_action"
        ) as perform, patch(
            "backend.inspection.replay.wait_for_stable_page",
            side_effect=[_capture(middle), _capture(final)],
        ):
            result = execute_replay_chain(
                Mock(),
                chain,
                package_name=PACKAGE,
                abort_event=threading.Event(),
                initial_capture=_capture(source),
            )

        self.assertEqual(result.status, "PASS")
        self.assertEqual(result.completed_checkpoints, 3)
        self.assertEqual(perform.call_count, 2)


if __name__ == "__main__":
    unittest.main()
