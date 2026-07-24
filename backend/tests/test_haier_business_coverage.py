import copy
import unittest

from backend.inspection.haier_business_coverage import (
    CoverageGoalTracker,
    HAIER_MALL_V2_MANIFEST,
    SEARCH_INPUT_RULE_ID,
    coverage_state_assignments,
    freeze_manifest,
    manifest_hash,
    evaluate_haier_business_coverage,
)
from backend.inspection.haier_coverage import StateEvidence, TransitionEvidence


def _state(
    state_id,
    subtype,
    *,
    branch="authenticated",
    xml="page",
    stable=True,
    stable_status=None,
):
    return StateEvidence(
        id=state_id,
        run_id=9,
        branch_run_id=1 if branch == "authenticated" else 2,
        branch_key=branch,
        page_subtype=subtype,
        xml_text=xml,
        stable_status=stable_status or ("STABLE" if stable else "UNVERIFIED"),
        screenshot_path=f"inspection/9/{branch}/{state_id}/screen.png",
    )


def _edge(
    edge_id,
    source,
    target,
    *,
    role="COMMAND",
    branch="authenticated",
    input_rule_id="",
    input_length=None,
    status="PASS",
):
    return TransitionEvidence(
        id=edge_id,
        run_id=9,
        branch_run_id=1 if branch == "authenticated" else 2,
        from_state_id=source,
        to_state_id=target,
        action_role=role,
        execution_disposition="EXECUTED",
        status=status,
        input_rule_id=input_rule_id,
        input_length=input_length,
    )


class HaierBusinessCoverageTests(unittest.TestCase):
    def test_runtime_manifest_uses_explicit_haier_endpoint_subtypes(self):
        journeys = {
            str(item["key"]): item for item in HAIER_MALL_V2_MANIFEST["journeys"]
        }

        self.assertEqual(
            journeys["wish_pool_content"]["stages"][-1]["subtypes"],
            ["COMMUNITY_DETAIL"],
        )
        self.assertEqual(
            journeys["member_benefits_flow"]["stages"][-1]["subtypes"],
            ["MEMBER_BENEFITS"],
        )
        self.assertEqual(
            journeys["favorites_flow"]["stages"][-1]["subtypes"],
            ["FAVORITES"],
        )
        self.assertEqual(
            journeys["history_flow"]["stages"][-1]["subtypes"],
            ["BROWSING_HISTORY"],
        )
        self.assertEqual(
            journeys["category_search_product_flow"]["edge_role_patterns"][0],
            "COMMAND:SEARCH",
        )
        self.assertEqual(
            [
                stage["subtypes"]
                for stage in journeys["category_search_product_flow"]["stages"]
            ],
            [
                ["CATALOG_CATEGORY"],
                ["SEARCH"],
                ["PRODUCT_LIST"],
                ["PRODUCT_DETAIL"],
            ],
        )
        self.assertTrue(
            journeys["product_orders_flow"]["action_hints"][0]["label_patterns"]
        )

    def test_manifest_snapshot_is_deterministic_and_run_scoped(self):
        first = freeze_manifest(
            "com.ehaier.zgq.shop.mall", ["authenticated", "guest"]
        )
        second = freeze_manifest(
            "com.ehaier.zgq.shop.mall", ["guest", "authenticated"]
        )

        self.assertIsNotNone(first)
        self.assertEqual(first, second)
        frozen_hash = first.pop("hash")
        self.assertEqual(frozen_hash, manifest_hash(first))
        self.assertIsNone(freeze_manifest("example.invalid", ["guest"]))

    def test_fixed_search_requires_haier_keyword_input_transition(self):
        states = [
            _state(1, "CATALOG_CATEGORY"),
            _state(2, "SEARCH"),
            _state(3, "SEARCH"),
            _state(4, "PRODUCT_LIST"),
            _state(
                5,
                "PRODUCT_DETAIL",
                xml="Haier/海尔 商品参数 正品保障",
            ),
        ]
        base_edges = [
            _edge(10, 1, 2, role="COMMAND:SEARCH"),
            _edge(12, 3, 4, role="SEARCH_SUBMIT"),
            _edge(13, 4, 5, role="ITEM_OPEN:collection"),
        ]
        wrong_input = _edge(
            11,
            2,
            3,
            role="INPUT",
            input_rule_id="random_hot_word",
            input_length=2,
        )
        fixed_input = _edge(
            14,
            2,
            3,
            role="INPUT",
            input_rule_id=SEARCH_INPUT_RULE_ID,
            input_length=2,
        )

        wrong = evaluate_haier_business_coverage(
            states=states,
            transitions=[*base_edges, wrong_input],
            selected_branches=["authenticated"],
        )
        accepted = evaluate_haier_business_coverage(
            states=states,
            transitions=[*base_edges, fixed_input],
            selected_branches=["authenticated"],
        )

        wrong_item = next(
            item
            for branch in wrong["branches"]
            for item in branch["items"]
            if branch["branch_key"] == "authenticated"
            and item["key"] == "category_search_product_flow"
        )
        accepted_item = next(
            item
            for branch in accepted["branches"]
            for item in branch["items"]
            if branch["branch_key"] == "authenticated"
            and item["key"] == "category_search_product_flow"
        )
        self.assertNotEqual(wrong_item["status"], "COVERED")
        self.assertEqual(accepted_item["status"], "COVERED")
        self.assertEqual(accepted_item["evidence_transition_ids"], [10, 14, 12, 13])

    def test_fixed_search_accepts_audited_input_self_loop(self):
        states = [
            _state(1, "CATALOG_CATEGORY"),
            _state(2, "SEARCH"),
            _state(3, "PRODUCT_LIST"),
            _state(4, "PRODUCT_DETAIL", xml="Haier/海尔 商品参数 正品保障"),
        ]
        assessment = evaluate_haier_business_coverage(
            states=states,
            transitions=[
                _edge(10, 1, 2, role="COMMAND:SEARCH"),
                _edge(
                    11,
                    2,
                    2,
                    role="INPUT",
                    input_rule_id=SEARCH_INPUT_RULE_ID,
                    input_length=2,
                    status="SELF_LOOP",
                ),
                _edge(12, 2, 3, role="SEARCH_SUBMIT"),
                _edge(13, 3, 4, role="ITEM_OPEN:collection"),
            ],
            selected_branches=["authenticated"],
        )

        item = next(
            item
            for branch in assessment["branches"]
            for item in branch["items"]
            if branch["branch_key"] == "authenticated"
            and item["key"] == "category_search_product_flow"
        )
        self.assertEqual(item["status"], "COVERED")
        self.assertEqual(item["evidence_state_ids"], [1, 2, 2, 3, 4])
        self.assertEqual(item["evidence_transition_ids"], [10, 11, 12, 13])

    def test_legacy_five_stage_search_snapshot_keeps_original_semantics(self):
        manifest = copy.deepcopy(HAIER_MALL_V2_MANIFEST)
        search = next(
            item
            for item in manifest["journeys"]
            if item["key"] == "category_search_product_flow"
        )
        search["stages"].insert(2, copy.deepcopy(search["stages"][1]))
        states = [
            _state(1, "CATALOG_CATEGORY"),
            _state(2, "SEARCH"),
            _state(3, "PRODUCT_LIST"),
            _state(4, "PRODUCT_DETAIL", xml="Haier/海尔 商品参数 正品保障"),
        ]
        assessment = evaluate_haier_business_coverage(
            states=states,
            transitions=[
                _edge(10, 1, 2, role="COMMAND:SEARCH"),
                _edge(
                    11,
                    2,
                    2,
                    role="INPUT",
                    input_rule_id=SEARCH_INPUT_RULE_ID,
                    input_length=2,
                    status="SELF_LOOP",
                ),
                _edge(12, 2, 3, role="SEARCH_SUBMIT"),
                _edge(13, 3, 4, role="ITEM_OPEN:collection"),
            ],
            selected_branches=["authenticated"],
            manifest=manifest,
        )

        item = next(
            item
            for branch in assessment["branches"]
            for item in branch["items"]
            if branch["branch_key"] == "authenticated"
            and item["key"] == "category_search_product_flow"
        )
        self.assertEqual(item["status"], "COVERED")
        self.assertEqual(item["deepest_stage"], 5)

    def test_fixed_search_accepts_input_that_auto_submits_to_results(self):
        states = [
            _state(1, "CATALOG_CATEGORY"),
            _state(2, "SEARCH"),
            _state(3, "PRODUCT_LIST"),
            _state(4, "PRODUCT_DETAIL", xml="Haier/海尔 商品参数 正品保障"),
        ]
        assessment = evaluate_haier_business_coverage(
            states=states,
            transitions=[
                _edge(10, 1, 2, role="COMMAND:SEARCH"),
                _edge(
                    11,
                    2,
                    3,
                    role="INPUT",
                    input_rule_id=SEARCH_INPUT_RULE_ID,
                    input_length=2,
                ),
                _edge(12, 3, 4, role="ITEM_OPEN:collection"),
            ],
            selected_branches=["authenticated"],
        )

        item = next(
            item
            for branch in assessment["branches"]
            for item in branch["items"]
            if branch["branch_key"] == "authenticated"
            and item["key"] == "category_search_product_flow"
        )
        self.assertEqual(item["status"], "COVERED")
        self.assertEqual(item["evidence_state_ids"], [1, 2, 3, 4])
        self.assertEqual(item["evidence_transition_ids"], [10, 11, 12])

    def test_fixed_search_rejects_direct_detail_and_disconnected_result(self):
        states = [
            _state(1, "CATALOG_CATEGORY"),
            _state(2, "SEARCH"),
            _state(3, "PRODUCT_DETAIL", xml="Haier/海尔 商品参数 正品保障"),
            _state(4, "PRODUCT_LIST"),
            _state(5, "PRODUCT_DETAIL", xml="Haier/海尔 商品参数 正品保障"),
        ]
        assessment = evaluate_haier_business_coverage(
            states=states,
            transitions=[
                _edge(10, 1, 2, role="COMMAND:SEARCH"),
                _edge(
                    11,
                    2,
                    3,
                    role="INPUT",
                    input_rule_id=SEARCH_INPUT_RULE_ID,
                    input_length=2,
                ),
                _edge(12, 4, 5, role="ITEM_OPEN:collection"),
            ],
            selected_branches=["authenticated"],
        )

        item = next(
            item
            for branch in assessment["branches"]
            for item in branch["items"]
            if branch["branch_key"] == "authenticated"
            and item["key"] == "category_search_product_flow"
        )
        self.assertNotEqual(item["status"], "COVERED")
        self.assertEqual(item["deepest_stage"], 2)
        self.assertEqual(item["evidence_transition_ids"], [10])

    def test_reached_endpoint_without_reverification_is_inconclusive(self):
        manifest = copy.deepcopy(HAIER_MALL_V2_MANIFEST)
        manifest["journeys"] = [
            {
                "key": "store",
                "label": "store",
                "branches": ["guest"],
                "required": True,
                "kind": "path",
                "stages": [
                    {"subtypes": ["STORE_LIST"]},
                    {"subtypes": ["STORE_DETAIL"]},
                ],
                "edge_role_patterns": ["STORE_OPEN"],
                "action_hints": [],
            }
        ]
        assessment = evaluate_haier_business_coverage(
            states=[
                _state(1, "STORE_LIST", branch="guest"),
                _state(2, "STORE_DETAIL", branch="guest", stable=False),
            ],
            transitions=[_edge(10, 1, 2, role="STORE_OPEN", branch="guest")],
            selected_branches=["guest"],
            manifest=manifest,
        )

        item = assessment["branches"][0]["items"][0]
        self.assertEqual(item["status"], "INCONCLUSIVE")
        self.assertEqual(item["reason_code"], "ENDPOINT_NOT_REVERIFIED")
        self.assertEqual(assessment["selected_scope_verdict"], "INCONCLUSIVE")
        self.assertEqual(assessment["full_app_verdict"], "INCOMPLETE")
        self.assertTrue(
            any(
                blind_spot["type"] == "REVERIFY_FAILED"
                and blind_spot["count"] == 1
                and blind_spot["severity"] == "HIGH"
                for blind_spot in assessment["blind_spots"]
            )
        )

    def test_optional_reverification_failure_is_not_reported_as_required(self):
        manifest = {
            "id": "haier-mall-v2",
            "version": 2,
            "journeys": [
                {
                    "key": "required_home",
                    "label": "required home",
                    "branches": ["authenticated"],
                    "required": True,
                    "kind": "state",
                    "stages": [{"subtypes": ["HOME"]}],
                },
                {
                    "key": "optional_orders",
                    "label": "optional orders",
                    "branches": ["authenticated"],
                    "required": False,
                    "kind": "state",
                    "stages": [{"subtypes": ["ORDER"]}],
                },
            ],
        }
        assessment = evaluate_haier_business_coverage(
            states=[
                _state(1, "HOME", stable_status="REVERIFIED_ONCE"),
                _state(2, "ORDER", stable=False),
            ],
            transitions=[],
            selected_branches=["authenticated"],
            manifest=manifest,
        )

        self.assertEqual(assessment["selected_scope_verdict"], "COMPLETE")
        self.assertFalse(
            any(
                blind_spot["type"] == "REVERIFY_FAILED"
                for blind_spot in assessment["blind_spots"]
            )
        )
        optional_blind_spot = next(
            blind_spot
            for blind_spot in assessment["blind_spots"]
            if blind_spot["type"] == "OPTIONAL_REVERIFY_FAILED"
        )
        self.assertEqual(optional_blind_spot["count"], 1)
        self.assertEqual(optional_blind_spot["severity"], "MEDIUM")
        self.assertEqual(optional_blind_spot["message"], "存在终点复验失败的可选旅程")

    def test_single_business_endpoint_reverification_is_covered(self):
        manifest = copy.deepcopy(HAIER_MALL_V2_MANIFEST)
        manifest["journeys"] = [
            {
                "key": "store",
                "label": "store",
                "branches": ["guest"],
                "required": True,
                "kind": "path",
                "stages": [
                    {"subtypes": ["STORE_LIST"]},
                    {"subtypes": ["STORE_DETAIL"]},
                ],
                "edge_role_patterns": ["STORE_OPEN"],
                "action_hints": [],
            }
        ]
        assessment = evaluate_haier_business_coverage(
            states=[
                _state(1, "STORE_LIST", branch="guest"),
                _state(
                    2,
                    "STORE_DETAIL",
                    branch="guest",
                    stable_status="REVERIFIED_ONCE",
                ),
            ],
            transitions=[_edge(10, 1, 2, role="STORE_OPEN", branch="guest")],
            selected_branches=["guest"],
            manifest=manifest,
        )

        item = assessment["branches"][0]["items"][0]
        self.assertEqual(item["status"], "COVERED")
        self.assertEqual(assessment["selected_scope_verdict"], "COMPLETE")

    def test_complete_transition_path_requires_readable_xml_for_every_state(self):
        manifest = {
            "id": "haier-mall-v2",
            "version": 2,
            "journeys": [
                {
                    "key": "store",
                    "label": "store",
                    "branches": ["guest"],
                    "required": True,
                    "kind": "path",
                    "stages": [
                        {"subtypes": ["STORE_LIST"]},
                        {"subtypes": ["STORE_DETAIL"]},
                    ],
                    "edge_role_patterns": ["STORE_OPEN"],
                    "action_hints": [],
                }
            ],
        }
        assessment = evaluate_haier_business_coverage(
            states=[
                _state(1, "STORE_LIST", branch="guest", xml=None),
                _state(2, "STORE_DETAIL", branch="guest"),
            ],
            transitions=[_edge(10, 1, 2, role="STORE_OPEN", branch="guest")],
            selected_branches=["guest"],
            manifest=manifest,
        )

        item = assessment["branches"][0]["items"][0]
        self.assertEqual(item["status"], "INCONCLUSIVE")
        self.assertEqual(item["reason_code"], "XML_MISSING")

    def test_budget_stop_keeps_missing_journey_inconclusive_and_visible(self):
        manifest = {
            "id": "haier-mall-v2",
            "version": 2,
            "journeys": [
                {
                    "key": "store",
                    "label": "store",
                    "branches": ["guest"],
                    "required": True,
                    "kind": "path",
                    "stages": [
                        {"subtypes": ["STORE_LIST"]},
                        {"subtypes": ["STORE_DETAIL"]},
                    ],
                    "edge_role_patterns": ["STORE_OPEN"],
                    "action_hints": [],
                }
            ],
        }
        stop_reason = "探索阶段 85% 时间预算已用完"
        assessment = evaluate_haier_business_coverage(
            states=[_state(1, "STORE_LIST", branch="guest")],
            transitions=[],
            selected_branches=["guest"],
            branch_statuses={"guest": "WARNING"},
            branch_stop_reasons={"guest": stop_reason},
            run_stop_reason=stop_reason,
            manifest=manifest,
        )

        item = assessment["branches"][0]["items"][0]
        self.assertEqual(item["status"], "INCONCLUSIVE")
        self.assertEqual(item["reason_code"], "EXECUTION_INCOMPLETE")
        self.assertEqual(assessment["summary"]["scope_branches_selected"], 1)
        self.assertEqual(assessment["summary"]["scope_branches_covered"], 0)
        self.assertTrue(
            any(
                blind_spot["type"] == "BUDGET_STOP"
                and blind_spot["severity"] == "HIGH"
                for blind_spot in assessment["blind_spots"]
            )
        )

    def test_payment_requires_explicit_blocked_payment_boundary(self):
        states = [
            _state(1, "PRODUCT_DETAIL", xml="Haier/海尔 商品参数 正品保障"),
            _state(2, "PURCHASE_OPTIONS"),
            _state(3, "CHECKOUT", xml="提交订单"),
            _state(4, "CASHIER", xml="海尔收银台"),
        ]
        chain = [
            _edge(10, 1, 2, role="BUY_NOW"),
            _edge(11, 2, 3, role="CHECKOUT"),
            _edge(12, 3, 4, role="PLACE_ORDER"),
        ]
        missing = evaluate_haier_business_coverage(
            states=states,
            transitions=chain,
            selected_branches=["authenticated"],
        )
        boundary = TransitionEvidence(
            id=13,
            run_id=9,
            branch_run_id=1,
            from_state_id=4,
            to_state_id=None,
            execution_disposition="SKIPPED",
            status="BLOCKED",
            failure_type="SAFETY_BLOCKED",
            risk_type="PAYMENT",
        )
        covered = evaluate_haier_business_coverage(
            states=states,
            transitions=[*chain, boundary],
            selected_branches=["authenticated"],
        )

        def payment(assessment):
            return next(
                item
                for branch in assessment["branches"]
                for item in branch["items"]
                if branch["branch_key"] == "authenticated"
                and item["key"] == "physical_checkout_safety_flow"
            )

        self.assertEqual(payment(missing)["reason_code"], "PAYMENT_BOUNDARY_MISSING")
        self.assertEqual(payment(covered)["status"], "COVERED")
        self.assertIn(13, payment(covered)["evidence_transition_ids"])

    def test_payment_rejects_boundary_recorded_before_the_physical_cashier_path(self):
        states = [
            _state(1, "PRODUCT_DETAIL", xml="Haier/海尔 商品参数 正品保障"),
            _state(2, "PURCHASE_OPTIONS"),
            _state(3, "CHECKOUT", xml="提交订单"),
            _state(4, "CASHIER", xml="海尔收银台"),
        ]
        earlier_order_boundary = TransitionEvidence(
            id=9,
            run_id=9,
            branch_run_id=1,
            from_state_id=4,
            to_state_id=None,
            execution_disposition="SKIPPED",
            status="BLOCKED",
            failure_type="SAFETY_BLOCKED",
            risk_type="PAYMENT",
        )
        physical_chain = [
            _edge(10, 1, 2, role="OPTION_SELECT"),
            _edge(11, 2, 3, role="BUY_NOW"),
            _edge(12, 3, 4, role="PLACE_ORDER"),
        ]

        assessment = evaluate_haier_business_coverage(
            states=states,
            transitions=[earlier_order_boundary, *physical_chain],
            selected_branches=["authenticated"],
        )
        payment = next(
            item
            for branch in assessment["branches"]
            for item in branch["items"]
            if branch["branch_key"] == "authenticated"
            and item["key"] == "physical_checkout_safety_flow"
        )

        self.assertEqual(payment["status"], "MISSING")
        self.assertEqual(payment["reason_code"], "PAYMENT_BOUNDARY_MISSING")
        self.assertNotIn(9, payment["evidence_transition_ids"])

    def test_payment_accepts_default_spec_side_branch_from_same_product(self):
        states = [
            _state(1, "PRODUCT_DETAIL", xml="Haier/海尔 商品参数 正品保障"),
            _state(2, "PURCHASE_OPTIONS"),
            _state(3, "CHECKOUT", xml="提交订单"),
            _state(4, "CASHIER", xml="海尔收银台"),
        ]
        transitions = [
            _edge(10, 1, 2, role="OPTION_SELECT"),
            _edge(11, 2, 1, role="DIALOG_CLOSE"),
            _edge(12, 1, 3, role="BUY_NOW"),
            _edge(13, 3, 4, role="PLACE_ORDER"),
            TransitionEvidence(
                id=14,
                run_id=9,
                branch_run_id=1,
                from_state_id=4,
                to_state_id=None,
                execution_disposition="SKIPPED",
                status="BLOCKED",
                failure_type="SAFETY_BLOCKED",
                risk_type="PAYMENT",
            ),
        ]

        assessment = evaluate_haier_business_coverage(
            states=states,
            transitions=transitions,
            selected_branches=["authenticated"],
        )
        payment = next(
            item
            for branch in assessment["branches"]
            for item in branch["items"]
            if branch["branch_key"] == "authenticated"
            and item["key"] == "physical_checkout_safety_flow"
        )

        self.assertEqual(payment["status"], "COVERED")
        self.assertEqual(payment["evidence_state_ids"], [1, 2, 3, 4])
        self.assertEqual(payment["evidence_transition_ids"], [10, 11, 12, 13, 14])

    def test_payment_accepts_add_cart_as_spec_entry_but_not_checkout(self):
        states = [
            _state(1, "PRODUCT_DETAIL", xml="Haier/海尔 商品参数 正品保障"),
            _state(2, "PURCHASE_OPTIONS"),
            _state(3, "CHECKOUT", xml="提交订单"),
            _state(4, "CASHIER", xml="海尔收银台"),
        ]
        boundary = TransitionEvidence(
            id=14,
            run_id=9,
            branch_run_id=1,
            from_state_id=4,
            to_state_id=None,
            execution_disposition="SKIPPED",
            status="BLOCKED",
            failure_type="SAFETY_BLOCKED",
            risk_type="PAYMENT",
        )
        valid = evaluate_haier_business_coverage(
            states=states,
            transitions=[
                _edge(10, 1, 2, role="ADD_CART"),
                _edge(11, 2, 1, role="DIALOG_CLOSE"),
                _edge(12, 1, 3, role="BUY_NOW"),
                _edge(13, 3, 4, role="PLACE_ORDER"),
                boundary,
            ],
            selected_branches=["authenticated"],
        )
        invalid = evaluate_haier_business_coverage(
            states=states,
            transitions=[
                _edge(10, 1, 2, role="ADD_CART"),
                _edge(12, 1, 3, role="ADD_CART"),
                _edge(13, 3, 4, role="PLACE_ORDER"),
                boundary,
            ],
            selected_branches=["authenticated"],
        )

        def payment(assessment):
            return next(
                item
                for branch in assessment["branches"]
                for item in branch["items"]
                if branch["branch_key"] == "authenticated"
                and item["key"] == "physical_checkout_safety_flow"
            )

        self.assertEqual(payment(valid)["status"], "COVERED")
        self.assertNotEqual(payment(invalid)["status"], "COVERED")

    def test_payment_path_accepts_haier_checkout_confirmation_prompt(self):
        states = [
            _state(1, "PRODUCT_DETAIL", xml="Haier/海尔 商品参数 正品保障"),
            _state(2, "PURCHASE_OPTIONS"),
            _state(3, "CHECKOUT", xml="提交订单"),
            _state(4, "CHECKOUT_CONFIRMATION", xml="权益选择提醒 直接提交"),
            _state(5, "CASHIER", xml="海尔收银台"),
        ]
        transitions = [
            _edge(10, 1, 2, role="OPTION_SELECT"),
            _edge(11, 2, 3, role="BUY_NOW"),
            _edge(12, 3, 4, role="PLACE_ORDER"),
            _edge(13, 4, 5, role="PLACE_ORDER"),
            TransitionEvidence(
                id=14,
                run_id=9,
                branch_run_id=1,
                from_state_id=5,
                to_state_id=None,
                execution_disposition="SKIPPED",
                status="BLOCKED",
                failure_type="SAFETY_BLOCKED",
                risk_type="PAYMENT",
            ),
        ]

        assessment = evaluate_haier_business_coverage(
            states=states,
            transitions=transitions,
            selected_branches=["authenticated"],
        )
        payment = next(
            item
            for branch in assessment["branches"]
            for item in branch["items"]
            if branch["branch_key"] == "authenticated"
            and item["key"] == "physical_checkout_safety_flow"
        )

        self.assertEqual(payment["status"], "COVERED")
        self.assertEqual(payment["evidence_state_ids"], [1, 2, 3, 5])
        self.assertEqual(payment["evidence_transition_ids"], [10, 11, 12, 13, 14])

    def test_xml_blind_spot_counts_state_without_xml_path(self):
        assessment = evaluate_haier_business_coverage(
            states=[_state(1, "HOME", xml=None)],
            transitions=[],
            selected_branches=["authenticated"],
        )

        blind_spot = next(
            item for item in assessment["blind_spots"] if item["type"] == "XML_MISSING"
        )
        self.assertEqual(blind_spot["count"], 1)
        self.assertEqual(assessment["summary"]["evidence_quality"], "LOW")

    def test_manifest_hash_mismatch_prevents_complete_verdict(self):
        manifest = {
            "id": "haier-mall-v2",
            "version": 2,
            "hash": "not-the-manifest-hash",
            "journeys": [
                {
                    "key": "home",
                    "label": "home",
                    "branches": ["authenticated"],
                    "required": True,
                    "kind": "state",
                    "stages": [{"subtypes": ["HOME"]}],
                }
            ],
        }

        assessment = evaluate_haier_business_coverage(
            states=[_state(1, "HOME")],
            transitions=[],
            selected_branches=["authenticated"],
            manifest=manifest,
        )

        self.assertEqual(assessment["selected_scope_verdict"], "INCONCLUSIVE")
        self.assertEqual(assessment["full_app_verdict"], "INCOMPLETE")
        self.assertFalse(assessment["manifest"]["hash_valid"])
        self.assertEqual(assessment["summary"]["evidence_quality"], "LOW")
        self.assertTrue(
            any(
                item["type"] == "MANIFEST_HASH_MISMATCH"
                for item in assessment["blind_spots"]
            )
        )

    def test_complete_selected_branch_keeps_full_app_incomplete(self):
        manifest = {
            "id": "haier-mall-v2",
            "version": 2,
            "journeys": [
                {
                    "key": "authenticated_home",
                    "label": "authenticated home",
                    "branches": ["authenticated"],
                    "required": True,
                    "kind": "state",
                    "stages": [{"subtypes": ["HOME"]}],
                }
            ],
        }

        assessment = evaluate_haier_business_coverage(
            states=[_state(1, "HOME")],
            transitions=[],
            selected_branches=["authenticated"],
            manifest=manifest,
        )

        self.assertEqual(assessment["selected_scope_verdict"], "COMPLETE")
        self.assertEqual(assessment["full_app_verdict"], "INCOMPLETE")
        self.assertEqual(assessment["coverage_verdict"], "INCOMPLETE")
        self.assertEqual(assessment["summary"]["scope_branches_selected"], 1)

    def test_guest_auth_gates_cover_selected_scope_but_not_full_app(self):
        manifest = copy.deepcopy(HAIER_MALL_V2_MANIFEST)
        manifest["journeys"] = [
            item
            for item in manifest["journeys"]
            if item["key"] in {"profile_auth_gate", "purchase_auth_gate"}
        ]
        states = [
            _state(1, "PROFILE", branch="guest", xml="我的 立即登录"),
            _state(2, "AUTH_GATE", branch="guest", xml="请先登录"),
            _state(
                3,
                "PRODUCT_DETAIL",
                branch="guest",
                xml="Haier/海尔 商品参数 正品保障",
            ),
            _state(4, "AUTH_GATE", branch="guest", xml="登录后购买"),
        ]
        assessment = evaluate_haier_business_coverage(
            states=states,
            transitions=[
                _edge(10, 1, 2, role="COMMAND:LOGIN", branch="guest"),
                _edge(11, 3, 4, role="BUY_NOW", branch="guest"),
            ],
            selected_branches=["guest"],
            manifest=manifest,
        )

        guest = next(
            branch
            for branch in assessment["branches"]
            if branch["branch_key"] == "guest"
        )
        self.assertEqual(guest["covered_required"], 2)
        self.assertEqual(guest["total_required"], 2)
        self.assertTrue(all(item["status"] == "COVERED" for item in guest["items"]))
        self.assertEqual(assessment["selected_scope_verdict"], "COMPLETE")
        self.assertEqual(assessment["full_app_verdict"], "INCOMPLETE")

    def test_bottom_tabs_require_navigation_transitions(self):
        states = [
            _state(1, "HOME", xml="首页 分类 许愿池 购物车 我的"),
            _state(2, "CATALOG_CATEGORY"),
            _state(3, "COMMUNITY_FEED"),
            _state(4, "CART"),
            _state(5, "PROFILE"),
        ]
        wrong = evaluate_haier_business_coverage(
            states=states,
            transitions=[
                _edge(10 + index, 1, target, role="COMMAND")
                for index, target in enumerate(range(2, 6))
            ],
            selected_branches=["authenticated"],
        )
        covered = evaluate_haier_business_coverage(
            states=states,
            transitions=[
                _edge(20 + index, 1, target, role=f"NAV:{index}")
                for index, target in enumerate(range(2, 6))
            ],
            selected_branches=["authenticated"],
        )

        def bottom_tabs(assessment):
            return next(
                item
                for branch in assessment["branches"]
                for item in branch["items"]
                if branch["branch_key"] == "authenticated"
                and item["key"] == "home_five_tabs"
            )

        self.assertNotEqual(bottom_tabs(wrong)["status"], "COVERED")
        self.assertEqual(bottom_tabs(covered)["status"], "COVERED")

    def test_state_assignment_keeps_strongest_evidence_status(self):
        assessment = {
            "branches": [
                {
                    "items": [
                        {
                            "required": True,
                            "status": "COVERED",
                            "evidence_state_ids": [1],
                        },
                        {
                            "required": True,
                            "status": "MISSING",
                            "evidence_state_ids": [1, 2],
                        },
                        {
                            "required": False,
                            "status": "COVERED",
                            "evidence_state_ids": [3],
                        },
                    ]
                }
            ]
        }

        assignments = coverage_state_assignments(assessment, [1, 2, 3, 4])

        self.assertEqual(assignments[1], "REQUIRED_EVIDENCE")
        self.assertEqual(assignments[2], "INCOMPLETE")
        self.assertEqual(assignments[3], "OPTIONAL_EVIDENCE")
        self.assertEqual(assignments[4], "EXPLORED")

    def test_goal_tracker_prioritizes_unresolved_required_action(self):
        tracker = CoverageGoalTracker("authenticated")
        tracker.refresh([], [])

        class Action:
            def __init__(self, role):
                self.action_role = role
                self.target_meta = {}
                self.risk_type = None

        actions = [Action("SCROLL:vertical:up"), Action("BUY_NOW")]
        ordered = tracker.prioritize_actions("PRODUCT_DETAIL", actions)

        self.assertEqual(ordered[0].action_role, "BUY_NOW")
        self.assertEqual(tracker.frontier_priority("PRODUCT_DETAIL", actions), 10)

    def test_goal_tracker_prioritizes_bottom_navigation_when_branch_starts_on_profile(self):
        tracker = CoverageGoalTracker("authenticated")

        class Action:
            def __init__(self, role, label=""):
                self.action_role = role
                self.target_meta = {"content_desc": label}
                self.risk_type = None

        settings = Action("COMMAND:SETTINGS", "设置")
        home = Action("NAV:home", "首页")
        ordered = tracker.prioritize_actions("PROFILE", [settings, home])

        self.assertTrue(tracker.action_is_required("PROFILE", home.action_role))
        self.assertEqual(ordered[0].action_role, "NAV:home")
        self.assertEqual(tracker.frontier_priority("PROFILE", [home]), 10)

    def test_goal_tracker_prioritizes_haier_service_entry_from_profile(self):
        tracker = CoverageGoalTracker("authenticated")

        class Action:
            action_role = "COMMAND:cleaning"
            target_meta = {"content_desc": "清洗服务"}
            risk_type = None

        action = Action()
        self.assertTrue(
            tracker.action_is_required(
                "PROFILE",
                action.action_role,
                label=action.target_meta["content_desc"],
            )
        )
        self.assertEqual(tracker.frontier_priority("PROFILE", [action]), 10)

    def test_goal_tracker_ignores_edges_before_a_journey_starts(self):
        tracker = CoverageGoalTracker("authenticated")
        tracker.observe_state(1, "HOME")
        tracker.observe_state(2, "CATALOG_CATEGORY")

        tracker.observe_transition(
            from_state_id=1,
            to_state_id=2,
            action_role="NAV:catalog",
            status="PASS",
            execution_disposition="EXECUTED",
        )

        progress = {item.key: item for item in tracker.unresolved}
        self.assertEqual(progress["product_orders_flow"].deepest_stage, 0)
        self.assertEqual(progress["settings_address_flow"].deepest_stage, 0)

    def test_goal_tracker_rejects_disconnected_generic_journey_edge(self):
        tracker = CoverageGoalTracker("authenticated")
        tracker.observe_state(1, "STORE_LIST")
        tracker.observe_state(2, "HOME")
        tracker.observe_state(3, "STORE_DETAIL")

        tracker.observe_transition(
            from_state_id=2,
            to_state_id=3,
            action_role="STORE_OPEN",
            status="PASS",
            execution_disposition="EXECUTED",
        )

        progress = {item.key: item for item in tracker.unresolved}
        self.assertEqual(progress["store_detail_flow"].deepest_stage, 1)

        tracker.observe_transition(
            from_state_id=1,
            to_state_id=3,
            action_role="STORE_OPEN",
            status="PASS",
            execution_disposition="EXECUTED",
        )

        self.assertNotIn(
            "store_detail_flow",
            {item.key for item in tracker.unresolved},
        )

    def test_goal_tracker_tracks_both_fixed_search_submit_modes(self):
        def exercise(*, auto_submit):
            tracker = CoverageGoalTracker("authenticated")
            subtypes = {
                1: "CATALOG_CATEGORY",
                2: "SEARCH",
                3: "PRODUCT_LIST" if auto_submit else "SEARCH",
                4: "PRODUCT_DETAIL" if auto_submit else "PRODUCT_LIST",
                5: "PRODUCT_DETAIL",
            }
            for state_id, subtype in subtypes.items():
                tracker.observe_state(state_id, subtype)
            tracker.observe_transition(
                from_state_id=1,
                to_state_id=2,
                action_role="COMMAND:SEARCH",
                status="PASS",
                execution_disposition="EXECUTED",
            )
            tracker.observe_transition(
                from_state_id=2,
                to_state_id=3,
                action_role="INPUT",
                status="PASS" if auto_submit else "SELF_LOOP",
                execution_disposition="EXECUTED",
                input_rule_id=SEARCH_INPUT_RULE_ID,
                input_length=2,
            )
            if auto_submit:
                result_state_id = 3
                detail_state_id = 4
            else:
                tracker.observe_transition(
                    from_state_id=3,
                    to_state_id=4,
                    action_role="SEARCH_SUBMIT",
                    status="PASS",
                    execution_disposition="EXECUTED",
                )
                result_state_id = 4
                detail_state_id = 5
            tracker.observe_transition(
                from_state_id=result_state_id,
                to_state_id=detail_state_id,
                action_role="ITEM_OPEN:collection",
                status="PASS",
                execution_disposition="EXECUTED",
            )
            return tracker

        for auto_submit in (True, False):
            with self.subTest(auto_submit=auto_submit):
                unresolved = {
                    item.key for item in exercise(auto_submit=auto_submit).unresolved
                }
                self.assertNotIn("category_search_product_flow", unresolved)

    def test_goal_tracker_prioritizes_the_same_product_payment_graph(self):
        tracker = CoverageGoalTracker("authenticated")
        for state_id, subtype in {
            1: "PRODUCT_DETAIL",
            2: "PURCHASE_OPTIONS",
            3: "CHECKOUT",
            4: "CHECKOUT_CONFIRMATION",
            5: "CASHIER",
        }.items():
            tracker.observe_state(state_id, subtype)

        tracker.observe_transition(
            from_state_id=5,
            to_state_id=None,
            action_role="COMMAND:PAY",
            status="BLOCKED",
            execution_disposition="SKIPPED",
            risk_type="PAYMENT",
        )
        self.assertIn(
            "physical_checkout_safety_flow",
            {item.key for item in tracker.unresolved},
        )

        class Action:
            def __init__(self, role):
                self.action_role = role
                self.target_meta = {}
                self.risk_type = None

        ordered = tracker.prioritize_actions(
            "PRODUCT_DETAIL",
            [Action("BUY_NOW"), Action("ADD_CART"), Action("OPTION_SELECT")],
        )
        self.assertEqual(
            [action.action_role for action in ordered],
            ["OPTION_SELECT", "ADD_CART", "BUY_NOW"],
        )

        # Runtime can observe checkout before the spec side branch. The
        # tracker must promote that cached edge once same-detail spec evidence
        # arrives, matching the final graph evaluator's order independence.
        tracker.observe_transition(
            from_state_id=1,
            to_state_id=3,
            action_role="BUY_NOW",
            status="PASS",
            execution_disposition="EXECUTED",
        )
        tracker.observe_transition(
            from_state_id=1,
            to_state_id=2,
            action_role="OPTION_SELECT",
            status="PASS",
            execution_disposition="EXECUTED",
        )
        tracker.observe_transition(
            from_state_id=2,
            to_state_id=1,
            action_role="DIALOG_CLOSE",
            status="PASS",
            execution_disposition="EXECUTED",
        )
        tracker.observe_transition(
            from_state_id=3,
            to_state_id=4,
            action_role="PLACE_ORDER",
            status="PASS",
            execution_disposition="EXECUTED",
        )

        self.assertTrue(tracker.state_is_required_candidate("CHECKOUT_CONFIRMATION"))
        self.assertEqual(
            tracker.frontier_priority(
                "CHECKOUT_CONFIRMATION",
                [Action("PLACE_ORDER")],
            ),
            10,
        )

        tracker.observe_transition(
            from_state_id=4,
            to_state_id=5,
            action_role="PLACE_ORDER",
            status="PASS",
            execution_disposition="EXECUTED",
        )
        self.assertIn(
            "physical_checkout_safety_flow",
            {item.key for item in tracker.unresolved},
        )
        tracker.observe_transition(
            from_state_id=5,
            to_state_id=None,
            action_role="COMMAND:PAY",
            status="BLOCKED",
            execution_disposition="SKIPPED",
            risk_type="PAYMENT",
        )
        self.assertNotIn(
            "physical_checkout_safety_flow",
            {item.key for item in tracker.unresolved},
        )


if __name__ == "__main__":
    unittest.main()
