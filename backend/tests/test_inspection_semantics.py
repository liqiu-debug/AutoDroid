import io
import tempfile
import threading
import time
import unittest
from collections import deque
from dataclasses import replace
from pathlib import Path
from unittest.mock import Mock, patch

from PIL import Image
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, select

from backend.inspection.device import (
    CapturedPage,
    LocatorAmbiguous,
    LocatorDrift,
    perform_action,
    wait_for_stable_page,
)
from backend.inspection.engine import (
    PathDiverged,
    PersistedState,
    StateWork,
    _capture_matches_parent,
    _coverage_endpoint_reverify_matches,
    _business_goal_priority_override,
    _consecutive_scroll_repetitions,
    _environment_secret_values,
    _ensure_parent,
    _execute_branch,
    _finish_active_branches,
    _path_score,
    _pop_most_local,
    _probe_ui_automation_responsive,
    _redact,
    _replay_path,
    _serialize_action,
    _state_actions,
    _transition_payload,
    _validated_fault_artifact,
    _validated_replay_artifact,
    _verify_stable_paths,
)
from backend.inspection.sanitizer import InspectionArtifactSanitizer
from backend.inspection.haier_business_coverage import CoverageGoalTracker
from backend.inspection.semantics import (
    InspectionAction,
    build_page_model,
    compare_exploration_families,
    compare_page_models,
    derive_instance_anchor,
    enumerate_actions,
    exploration_family_signature,
    is_stable_semantic_text,
    locator_match_count,
    locator_unique_bounds,
    visual_locator_matches,
)
from backend.models import (
    Environment,
    GlobalVariable,
    InspectionBranchRun,
    InspectionFault,
    InspectionRun,
    InspectionState,
    InspectionTransition,
)


def _page(body: str) -> str:
    return (
        '<hierarchy rotation="0">'
        '<node class="android.widget.FrameLayout" bounds="[0,0][1080,2400]" '
        'enabled="true">'
        f"{body}"
        "</node>"
        "</hierarchy>"
    )


class InspectionSemanticsTests(unittest.TestCase):
    def test_business_goal_priority_overrides_new_family_priority(self):
        tracker = CoverageGoalTracker("authenticated")
        tracker.refresh([], [])
        action = Mock(
            action_role="BUY_NOW",
            target_meta={"content_desc": "立即购买", "text": ""},
            risk_type=None,
        )

        priority, reason = _business_goal_priority_override(
            tracker,
            "PRODUCT_DETAIL",
            [action],
            200,
            "NEW_FAMILY_REPRESENTATIVE",
        )
        already_higher, higher_reason = _business_goal_priority_override(
            tracker,
            "PRODUCT_DETAIL",
            [action],
            5,
            "DIRECT_HANDOFF",
        )

        self.assertEqual((priority, reason), (10, "BUSINESS_COVERAGE_GOAL"))
        self.assertEqual((already_higher, higher_reason), (5, "DIRECT_HANDOFF"))

    def test_description_is_preferred_without_resource_id(self):
        xml = _page(
            '<node class="android.widget.Button" content-desc="购物车" text="购物车" '
            'clickable="true" enabled="true" bounds="[10,10][200,100]"/>'
        )
        model = build_page_model(
            xml,
            package_name="com.demo",
            activity=".MainActivity",
        )
        actions = enumerate_actions(model, screen_size=(1080, 2400))
        self.assertEqual(actions[0].locator_candidates[0]["by"], "description")
        self.assertNotIn("resource_id", actions[0].action_key)

    def test_text_is_used_when_description_is_missing(self):
        xml = _page(
            '<node class="android.widget.Button" text="去结算页面" '
            'clickable="true" enabled="true" bounds="[10,10][200,100]"/>'
        )
        model = build_page_model(xml, package_name="com.demo", activity=".Main")
        actions = enumerate_actions(model, screen_size=(1080, 2400))
        self.assertEqual(actions[0].locator_candidates[0]["by"], "text")

    def test_duplicate_description_and_text_generate_constrained_xpaths(self):
        xml = _page(
            '<node class="android.widget.LinearLayout" content-desc="商品 A" '
            'bounds="[0,0][1080,300]" enabled="true">'
            '<node class="android.widget.Button" content-desc="查看" text="查看" '
            'clickable="true" enabled="true" bounds="[10,10][200,100]"/>'
            "</node>"
            '<node class="android.widget.LinearLayout" content-desc="商品 B" '
            'bounds="[0,300][1080,600]" enabled="true">'
            '<node class="android.widget.Button" content-desc="查看" text="查看" '
            'clickable="true" enabled="true" bounds="[10,310][200,400]"/>'
            "</node>"
        )
        model = build_page_model(xml, package_name="com.demo", activity=".List")
        actions = enumerate_actions(model, screen_size=(1080, 2400))
        self.assertEqual(len(actions), 2)
        for action in actions:
            self.assertTrue(
                all(candidate["by"] == "xpath" for candidate in action.locator_candidates)
            )
            self.assertTrue(
                any(candidate.get("bounds_constrained") for candidate in action.locator_candidates)
            )
            anchored = next(
                candidate
                for candidate in action.locator_candidates
                if not candidate.get("bounds_constrained")
            )
            self.assertIn("anchor", anchored)

    def test_dynamic_text_never_becomes_locator(self):
        self.assertFalse(is_stable_semantic_text("2026-07-17 12:30"))
        self.assertFalse(is_stable_semantic_text("订单号 A12345678"))
        self.assertFalse(is_stable_semantic_text("￥19.99"))
        self.assertFalse(is_stable_semantic_text("活动页710"))
        self.assertFalse(is_stable_semantic_text("购物车, 20"))
        self.assertTrue(
            is_stable_semantic_text(
                "\uFFFC海尔 法式四门336升 一级能耗三档变温风冷无霜冰箱"
            )
        )
        self.assertFalse(is_stable_semantic_text("海尔冰箱\uFFFD"))
        xml = _page(
            '<node class="android.widget.Button" text="订单号 A12345678" '
            'clickable="true" enabled="true" bounds="[10,10][200,100]"/>'
        )
        model = build_page_model(xml, package_name="com.demo", activity=".Main")
        self.assertEqual(
            enumerate_actions(model, screen_size=(1080, 2400)),
            [],
        )

    def test_checked_selected_enabled_are_state_identity(self):
        base = (
            '<node class="android.widget.CheckBox" content-desc="接收通知" '
            'clickable="true" enabled="{enabled}" checked="{checked}" '
            'selected="{selected}" bounds="[10,10][200,100]"/>'
        )
        first = build_page_model(
            _page(base.format(enabled="true", checked="false", selected="false")),
            package_name="com.demo",
            activity=".Settings",
        )
        second = build_page_model(
            _page(base.format(enabled="true", checked="true", selected="false")),
            package_name="com.demo",
            activity=".Settings",
        )
        self.assertEqual(first.cluster_key, second.cluster_key)
        self.assertNotEqual(first.replay_key, second.replay_key)
        self.assertNotEqual(first.state_key, second.state_key)
        self.assertEqual(first.template_key, second.template_key)
        self.assertNotEqual(first.semantic_key, second.semantic_key)

    def test_screenshot_variant_keeps_structural_replay_identity(self):
        xml = _page(
            '<node class="android.widget.Button" content-desc="首页" '
            'clickable="true" enabled="true" bounds="[10,10][200,100]"/>'
        )
        first = build_page_model(
            xml,
            package_name="com.demo",
            activity=".Main",
            screenshot_phash="0000000000000000",
        )
        second = build_page_model(
            xml,
            package_name="com.demo",
            activity=".Main",
            screenshot_phash="ffffffffffffffff",
        )
        self.assertEqual(first.cluster_key, second.cluster_key)
        self.assertEqual(first.replay_key, second.replay_key)
        self.assertEqual(first.template_key, second.template_key)
        self.assertEqual(first.semantic_key, second.semantic_key)
        self.assertNotEqual(first.state_key, second.state_key)

    def test_product_instances_share_screenshot_free_semantic_identity(self):
        def product(title: str) -> str:
            return _page(
                f'<node class="android.widget.TextView" text="{title}" '
                'enabled="true" bounds="[10,10][900,100]"/>'
                '<node class="android.widget.Button" text="加入购物车" '
                'clickable="true" enabled="true" bounds="[10,120][400,220]"/>'
            )

        first = build_page_model(
            product("海尔冰箱 336L"),
            package_name="com.demo",
            activity=".ProductDetailActivity",
            screenshot_phash="0000000000000000",
        )
        second = build_page_model(
            product("索尼电视 65 英寸"),
            package_name="com.demo",
            activity="com.demo.ProductDetailActivity",
            screenshot_phash="ffffffffffffffff",
        )

        self.assertEqual(first.role, "PRODUCT_DETAIL")
        self.assertEqual(first.activity_family, "product_detail")
        self.assertEqual(first.template_key, second.template_key)
        self.assertEqual(first.semantic_key, second.semantic_key)
        self.assertNotEqual(first.state_key, second.state_key)
        similarity = compare_page_models(first, second)
        self.assertTrue(similarity.equivalent)
        self.assertTrue(similarity.same_template)
        self.assertEqual(similarity.score, 1.0)

        serialized_signature = repr(first.signature)
        serialized_evidence = repr(similarity.to_dict())
        self.assertNotIn("海尔冰箱", serialized_signature)
        self.assertNotIn("索尼电视", serialized_evidence)

    def test_business_roles_are_hard_identity_boundaries(self):
        def role_page(label: str, action: str) -> str:
            return _page(
                f'<node class="android.widget.TextView" text="{label}" '
                'enabled="true" bounds="[10,10][900,100]"/>'
                f'<node class="android.widget.Button" text="{action}" '
                'clickable="true" enabled="true" bounds="[10,120][400,220]"/>'
            )

        product = build_page_model(
            role_page("商品详情", "立即购买"),
            package_name="com.demo",
            activity=".MainActivity",
        )
        checkout = build_page_model(
            role_page("确认订单", "提交订单"),
            package_name="com.demo",
            activity=".MainActivity",
        )
        order = build_page_model(
            role_page("订单详情", "查看订单"),
            package_name="com.demo",
            activity=".MainActivity",
        )

        self.assertEqual(
            [product.role, checkout.role, order.role],
            ["PRODUCT_DETAIL", "CHECKOUT", "ORDER"],
        )
        self.assertEqual(len({product.semantic_key, checkout.semantic_key, order.semantic_key}), 3)
        cross_role = compare_page_models(product, checkout)
        self.assertFalse(cross_role.equivalent)
        self.assertEqual(cross_role.score, 0.0)
        self.assertFalse(cross_role.evidence["role_match"])

    def test_unknown_pages_require_exact_landmark_identity(self):
        first = build_page_model(
            _page(
                '<node class="android.widget.TextView" text="会员权益" '
                'enabled="true" bounds="[10,10][900,100]"/>'
            ),
            package_name="com.demo",
            activity=".AccountActivity",
        )
        second = build_page_model(
            _page(
                '<node class="android.widget.TextView" text="隐私政策" '
                'enabled="true" bounds="[10,10][900,100]"/>'
            ),
            package_name="com.demo",
            activity=".AccountActivity",
        )

        self.assertEqual(first.role, "UNKNOWN")
        self.assertEqual(first.template_key, second.template_key)
        self.assertNotEqual(first.semantic_key, second.semantic_key)
        similarity = compare_page_models(first, second)
        self.assertFalse(similarity.equivalent)
        self.assertTrue(similarity.same_template)
        self.assertTrue(similarity.evidence["exact_only_role"])
        self.assertNotIn("会员权益", repr(similarity.to_dict()))
        self.assertNotIn("隐私政策", repr(similarity.to_dict()))

    def test_similarity_uses_required_weights_and_keeps_gray_zone_as_evidence(self):
        page = build_page_model(
            _page(
                '<node class="android.widget.TextView" text="商品详情" '
                'enabled="true" bounds="[10,10][900,100]"/>'
                '<node class="android.widget.Button" text="立即购买" '
                'clickable="true" enabled="true" bounds="[10,120][400,220]"/>'
            ),
            package_name="com.demo",
            activity=".ProductDetailActivity",
        )
        self.assertGreaterEqual(len(page.landmark_keys), 2)
        anchor_variant = replace(
            page,
            semantic_key="different-semantic-key",
            landmark_keys=page.landmark_keys[:1],
        )

        similarity = compare_page_models(page, anchor_variant)
        self.assertAlmostEqual(similarity.score, 0.90)
        self.assertFalse(similarity.equivalent)
        self.assertEqual(similarity.evidence["confidence_band"], "GRAY")
        self.assertEqual(similarity.evidence["structure_similarity"], 1.0)
        self.assertEqual(similarity.evidence["action_similarity"], 1.0)
        self.assertEqual(similarity.evidence["anchor_similarity"], 0.5)
        self.assertEqual(similarity.evidence["control_similarity"], 1.0)
        self.assertTrue(similarity.evidence["control_state_match"])
        self.assertEqual(similarity.evidence["high_confidence_threshold"], 0.92)
        self.assertEqual(similarity.evidence["gray_zone_threshold"], 0.82)

    def test_control_state_changes_are_similarity_hard_negatives(self):
        def product_control(*, checked: bool, selected: bool, enabled: bool):
            return build_page_model(
                _page(
                    '<node class="android.widget.TextView" text="商品详情" '
                    'enabled="true" bounds="[10,10][900,100]"/>'
                    '<node class="android.widget.CheckBox" content-desc="收藏" '
                    f'clickable="true" enabled="{str(enabled).lower()}" '
                    f'checked="{str(checked).lower()}" '
                    f'selected="{str(selected).lower()}" '
                    'bounds="[10,120][300,220]"/>'
                    '<node class="android.widget.Button" text="立即购买" '
                    'clickable="true" enabled="true" bounds="[10,240][400,340]"/>'
                ),
                package_name="com.demo",
                activity=".ProductDetailActivity",
            )

        baseline = product_control(checked=False, selected=False, enabled=True)
        variants = {
            "checked": product_control(checked=True, selected=False, enabled=True),
            "selected": product_control(checked=False, selected=True, enabled=True),
            "enabled": product_control(checked=False, selected=False, enabled=False),
        }
        for state_name, variant in variants.items():
            with self.subTest(state=state_name):
                self.assertEqual(baseline.template_key, variant.template_key)
                self.assertNotEqual(baseline.semantic_key, variant.semantic_key)
                similarity = compare_page_models(baseline, variant)
                self.assertFalse(similarity.equivalent)
                self.assertEqual(similarity.score, 0.0)
                self.assertFalse(similarity.evidence["control_state_match"])
                self.assertFalse(similarity.evidence["hard_gates_passed"])

    def test_dynamic_list_item_count_converges_but_unique_action_does_not(self):
        def list_page(item_count: int, *, include_search: bool = False):
            items = "".join(
                '<node class="android.view.ViewGroup" '
                f'content-desc="商品 {index + 1}" clickable="true" enabled="true" '
                f'bounds="[10,{100 + index * 120}][900,{200 + index * 120}]"/>'
                for index in range(item_count)
            )
            search = (
                '<node class="android.view.ViewGroup" content-desc="搜索" '
                'clickable="true" enabled="true" bounds="[10,1800][900,1900]"/>'
                if include_search
                else ""
            )
            return build_page_model(
                _page(items + search),
                package_name="com.demo",
                activity=".ListActivity",
            )

        one_item = list_page(1)
        three_items = list_page(3)
        self.assertEqual(one_item.role, "LIST")
        self.assertEqual(one_item.template_key, three_items.template_key)
        self.assertEqual(one_item.semantic_key, three_items.semantic_key)
        self.assertEqual(len(one_item.action_tokens), 1)
        self.assertEqual(len(three_items.action_tokens), 1)
        self.assertTrue(compare_page_models(one_item, three_items).equivalent)

        with_search = list_page(3, include_search=True)
        self.assertEqual(three_items.template_key, with_search.template_key)
        self.assertNotEqual(three_items.semantic_key, with_search.semantic_key)
        self.assertEqual(len(with_search.action_tokens), 2)
        similarity = compare_page_models(three_items, with_search)
        self.assertFalse(similarity.equivalent)
        self.assertFalse(similarity.evidence["control_state_match"])

    def test_category_instances_share_exploration_family_but_not_instance_anchor(self):
        def category_page(tab_labels, *, clipped=False):
            tabs = "".join(
                '<node class="android.view.ViewGroup" '
                f'content-desc="{label}" clickable="true" enabled="true" '
                f'bounds="[{index * 250},120][{(index + 1) * 250},300]"/>'
                for index, label in enumerate(tab_labels)
            )
            clipped_node = (
                '<node class="android.view.ViewGroup" clickable="true" '
                'enabled="true" bounds="[0,2396][1080,2400]"/>'
                if clipped
                else ""
            )
            return build_page_model(
                _page(
                    '<node class="android.widget.TextView" text="分类" '
                    'enabled="true" bounds="[400,20][680,110]"/>'
                    + tabs
                    + '<node class="android.view.ViewGroup" content-desc="综合" '
                    'clickable="true" enabled="true" bounds="[0,320][270,440]"/>'
                    '<node class="android.view.ViewGroup" content-desc="销量" '
                    'clickable="true" enabled="true" bounds="[270,320][540,440]"/>'
                    '<node class="android.view.ViewGroup" content-desc="价格" '
                    'clickable="true" enabled="true" bounds="[540,320][810,440]"/>'
                    '<node class="android.view.ViewGroup" content-desc="筛选" '
                    'clickable="true" enabled="true" bounds="[810,320][1080,440]"/>'
                    '<node class="android.view.ViewGroup" clickable="true" '
                    'enabled="true" bounds="[0,460][1080,1050]">'
                    '<node class="android.widget.TextView" text="自营" '
                    'enabled="true" bounds="[400,500][500,560]"/>'
                    '<node class="android.widget.TextView" text="商品标题" '
                    'enabled="true" bounds="[400,580][950,680]"/>'
                    "</node>"
                    + clipped_node
                ),
                package_name="com.demo",
                activity=".MainActivity",
            )

        fridge = category_page(["多门", "T型", "三门", "对开门"])
        washer = category_page(["滚筒", "波轮", "干衣机"], clipped=True)
        similarity = compare_exploration_families(
            fridge,
            washer,
            left_screen_size=(1080, 2400),
            right_screen_size=(1080, 2400),
        )
        self.assertTrue(similarity.equivalent, similarity.to_dict())
        self.assertGreaterEqual(similarity.score, 0.93)
        self.assertEqual(
            len(exploration_family_signature(washer)["action_role_tokens"]),
            len(exploration_family_signature(fridge)["action_role_tokens"]),
        )

        fridge_entry = InspectionAction(
            action_type="click",
            action_key="fridge",
            locator_candidates=[],
            target_meta={"content_desc": "冰箱"},
            action_role="NAV:fridge",
        )
        washer_entry = replace(
            fridge_entry,
            action_key="washer",
            target_meta={"content_desc": "洗衣机"},
            action_role="NAV:washer",
        )
        self.assertNotEqual(
            derive_instance_anchor(fridge, incoming_action=fridge_entry),
            derive_instance_anchor(washer, incoming_action=washer_entry),
        )

    def test_instance_anchor_uses_stable_primary_entry_label(self):
        page = build_page_model(
            _page(
                '<node class="android.widget.TextView" text="商品详情" '
                'enabled="true" bounds="[40,40][1040,160]"/>'
                '<node class="android.widget.Button" text="立即购买" '
                'clickable="true" enabled="true" bounds="[600,2050][1040,2180]"/>'
            ),
            package_name="com.ehaier.zgq.shop.mall",
            activity="com.ehaier.mall.MainActivity",
        )
        title_with_artifacts = "洗\u200b衣\u200b机\u200b干\u200b衣\u200b机\u200b通\u200b用"
        plain_title = "洗衣机干衣机通用"
        full_card = InspectionAction(
            action_type="click",
            action_key="full-card",
            locator_candidates=[],
            target_meta={
                "content_desc": f"{title_with_artifacts}, 原厂配件 官方质保, ￥299",
                "text": "",
            },
            action_role="COMMAND:full-card",
        )
        title_only = replace(
            full_card,
            action_key="title-only",
            target_meta={"content_desc": "", "text": plain_title},
            action_role="COMMAND:title-only",
        )
        explicit_text_wins = replace(
            full_card,
            action_key="explicit-text",
            target_meta={
                "content_desc": "营销活动, 与实例标题无关",
                "text": plain_title,
            },
        )
        different_title = replace(
            title_only,
            action_key="different-title",
            target_meta={"content_desc": "", "text": "冰箱冷柜通用"},
        )

        expected = derive_instance_anchor(page, incoming_action=title_only)
        self.assertEqual(
            derive_instance_anchor(page, incoming_action=full_card),
            expected,
        )
        self.assertEqual(
            derive_instance_anchor(page, incoming_action=explicit_text_wins),
            expected,
        )
        self.assertNotEqual(
            derive_instance_anchor(page, incoming_action=different_title),
            expected,
        )

    def test_command_role_uses_same_primary_label_as_instance_anchor(self):
        def entry_page(*, title: str, full_description: bool):
            attribute = (
                f'content-desc="{title}, 原厂配件 官方质保"'
                if full_description
                else f'text="{title}"'
            )
            return build_page_model(
                _page(
                    f'<node class="android.view.ViewGroup" {attribute} '
                    'clickable="true" enabled="true" bounds="[50,500][1030,850]"/>'
                ),
                package_name="com.demo",
                activity=".Main",
            )

        title_with_artifacts = "冰\u200b箱\u200b冷\u200b柜\u200b通\u200b用"
        full_card = enumerate_actions(
            entry_page(title=title_with_artifacts, full_description=True),
            screen_size=(1080, 2400),
        )[0]
        title_only = enumerate_actions(
            entry_page(title="冰箱冷柜通用", full_description=False),
            screen_size=(1080, 2400),
        )[0]
        different_title = enumerate_actions(
            entry_page(title="洗衣机干衣机通用", full_description=False),
            screen_size=(1080, 2400),
        )[0]

        self.assertEqual(full_card.action_role, title_only.action_role)
        self.assertEqual(full_card.action_role_key, title_only.action_role_key)
        self.assertEqual(full_card.sample_policy, "PAGE_ONE")
        self.assertEqual(title_only.sample_policy, "PAGE_ONE")
        self.assertNotEqual(title_only.action_role, different_title.action_role)
        self.assertNotEqual(title_only.action_role_key, different_title.action_role_key)

    def test_partially_visible_second_campaign_card_establishes_list_family(self):
        def campaign_page(product_name: str):
            return build_page_model(
                _page(
                    '<node class="android.view.ViewGroup" clickable="true" '
                    'enabled="true" bounds="[35,1843][1045,2189]">'
                    f'<node class="android.widget.TextView" text="{product_name} A" '
                    'enabled="true" bounds="[80,1870][700,1960]"/>'
                    '<node class="android.widget.Button" text="立即购买" '
                    'clickable="true" enabled="true" bounds="[760,2020][1010,2140]"/>'
                    '</node>'
                    '<node class="android.view.ViewGroup" clickable="true" '
                    'enabled="true" bounds="[35,2209][1045,2364]">'
                    f'<node class="android.widget.TextView" text="{product_name} B" '
                    'enabled="true" bounds="[80,2220][700,2280]"/>'
                    '<node class="android.widget.Button" text="立即购买" '
                    'clickable="true" enabled="true" bounds="[760,2280][1010,2350]"/>'
                    '</node>'
                ),
                package_name="com.ehaier.zgq.shop.mall",
                activity="com.ehaier.mall.MainActivity",
            )

        fridge = campaign_page("滤芯")
        washer = campaign_page("洗护清洁")
        air_conditioner = campaign_page("空调清洗")

        self.assertEqual(
            [fridge.role, washer.role, air_conditioner.role],
            ["LIST", "LIST", "LIST"],
        )
        self.assertEqual(fridge.page_subtype, "CONSUMABLE_LIST")
        self.assertEqual(washer.page_subtype, "PRODUCT_LIST")
        self.assertEqual(air_conditioner.page_subtype, "SERVICE_LIST")
        for candidate in (washer, air_conditioner):
            similarity = compare_exploration_families(
                fridge,
                candidate,
                left_screen_size=(1080, 2400),
                right_screen_size=(1080, 2400),
            )
            self.assertFalse(similarity.equivalent, similarity.to_dict())
            self.assertFalse(similarity.evidence["page_subtype_match"])

    def test_clipped_action_is_filtered_and_list_actions_receive_roles(self):
        page = build_page_model(
            _page(
                '<node class="android.widget.TextView" text="分类" '
                'enabled="true" bounds="[400,20][680,110]"/>'
                '<node class="android.view.ViewGroup" content-desc="销量" '
                'clickable="true" enabled="true" bounds="[270,320][540,440]"/>'
                '<node class="android.view.ViewGroup" clickable="true" '
                'enabled="true" bounds="[0,460][1080,1050]">'
                '<node class="android.widget.TextView" text="自营" '
                'enabled="true" bounds="[400,500][500,560]"/>'
                "</node>"
                '<node class="android.view.ViewGroup" clickable="true" '
                'enabled="true" bounds="[0,2396][1080,2400]"/>'
            ),
            package_name="com.demo",
            activity=".MainActivity",
        )
        actions = enumerate_actions(page, screen_size=(1080, 2400))
        self.assertEqual(len(actions), 2)
        self.assertEqual(
            {action.action_role for action in actions},
            {"SORT:sales", "ITEM_OPEN:collection"},
        )
        self.assertTrue(all(action.action_role_key for action in actions))

    def test_family_control_conflict_is_hard_negative_only_for_shared_anchor(self):
        def category(*, selected: bool, add_unrelated=False):
            extra = (
                '<node class="android.widget.Button" content-desc="额外" '
                'clickable="true" enabled="true" bounds="[20,1900][220,2000]"/>'
                if add_unrelated
                else ""
            )
            return build_page_model(
                _page(
                    '<node class="android.widget.TextView" text="分类" '
                    'enabled="true" bounds="[400,20][680,110]"/>'
                    '<node class="android.view.ViewGroup" content-desc="销量" '
                    f'clickable="true" enabled="true" selected="{str(selected).lower()}" '
                    'bounds="[270,320][540,440]"/>'
                    + extra
                ),
                package_name="com.demo",
                activity=".MainActivity",
            )

        baseline = category(selected=False)
        unrelated = category(selected=False, add_unrelated=True)
        conflict = category(selected=True)
        self.assertTrue(compare_exploration_families(baseline, unrelated).evidence[
            "shared_control_state_match"
        ])
        conflict_result = compare_exploration_families(baseline, conflict)
        self.assertFalse(conflict_result.equivalent)
        self.assertFalse(conflict_result.evidence["shared_control_state_match"])

    def test_page_roles_are_limited_to_supported_vocabulary(self):
        list_page = build_page_model(
            _page(
                '<node class="android.widget.TextView" text="购物车" '
                'enabled="true" bounds="[10,10][900,100]"/>'
            ),
            package_name="com.demo",
            activity=".CartActivity",
        )
        dialog_page = build_page_model(
            _page(
                '<node class="android.app.Dialog" text="操作提示" '
                'enabled="true" bounds="[100,400][980,1200]"/>'
            ),
            package_name="com.demo",
            activity=".MainActivity",
        )
        opaque_page = build_page_model(
            _page(
                '<node class="android.webkit.WebView" '
                'enabled="true" bounds="[0,0][1080,2400]"/>'
            ),
            package_name="com.demo",
            activity=".MainActivity",
        )

        self.assertEqual(list_page.role, "LIST")
        self.assertEqual(dialog_page.role, "DIALOG")
        self.assertEqual(opaque_page.role, "OPAQUE")
        supported = {
            "PRODUCT_DETAIL",
            "CHECKOUT",
            "ORDER",
            "LIST",
            "HOME",
            "DIALOG",
            "OPAQUE",
            "UNKNOWN",
        }
        self.assertTrue(
            {list_page.role, dialog_page.role, opaque_page.role}.issubset(supported)
        )

    def test_target_identity_ignores_status_bar_and_vendor_overlay(self):
        def hierarchy(clock: str, network: str) -> str:
            return (
                '<hierarchy rotation="0">'
                '<node package="com.android.systemui" class="android.widget.FrameLayout" '
                'enabled="true" bounds="[0,0][1080,100]">'
                f'<node package="com.android.systemui" class="android.widget.TextView" '
                f'text="{clock}" content-desc="{network}" enabled="true" '
                'bounds="[0,0][200,100]"/>'
                "</node>"
                '<node package="com.demo" class="android.widget.FrameLayout" '
                'enabled="true" bounds="[0,100][1080,2400]">'
                '<node package="com.demo" class="android.widget.Button" '
                'content-desc="首页" clickable="true" enabled="true" '
                'bounds="[10,110][200,200]"/>'
                "</node>"
                '<node package="com.coloros.smartsidebar" class="android.view.ViewGroup" '
                'content-desc="智能侧边栏" clickable="true" enabled="true" '
                'bounds="[1060,200][1080,500]"/>'
                "</hierarchy>"
            )

        first = build_page_model(
            hierarchy("09:07", "7 KB/s"),
            package_name="com.demo",
            activity=".Main",
        )
        second = build_page_model(
            hierarchy("09:08", "21 KB/s"),
            package_name="com.demo",
            activity=".Main",
        )
        self.assertEqual(first.cluster_key, second.cluster_key)
        self.assertEqual(first.replay_key, second.replay_key)
        self.assertEqual(first.template_key, second.template_key)
        self.assertEqual(first.semantic_key, second.semantic_key)
        self.assertFalse(first.has_dynamic_text)

        actions = enumerate_actions(first, screen_size=(1080, 2400))
        sidebar = next(
            action
            for action in actions
            if action.target_meta.get("content_desc") == "智能侧边栏"
        )
        self.assertEqual(sidebar.risk_type, "SYSTEM_OR_EXTERNAL")
        self.assertFalse(sidebar.replayable)

    def test_parent_verification_does_not_restart_for_visual_only_change(self):
        xml = _page(
            '<node class="android.widget.Button" content-desc="首页" '
            'clickable="true" enabled="true" bounds="[10,10][200,100]"/>'
        )
        saved_model = build_page_model(
            xml,
            package_name="com.demo",
            activity=".Main",
            screenshot_phash="0000000000000000",
        )
        current_model = build_page_model(
            xml,
            package_name="com.demo",
            activity=".Main",
            screenshot_phash="ffffffffffffffff",
        )
        current_capture = CapturedPage(
            package_name="com.demo",
            activity=".Main",
            xml=xml,
            screenshot_png=b"png",
            screenshot_sha="current-sha",
            perceptual_hash="ffffffffffffffff",
            model=current_model,
            stable_by="perceptual",
        )
        parent = StateWork(
            state_id=1,
            state_key=saved_model.state_key,
            cluster_key=saved_model.cluster_key,
            replay_key=saved_model.replay_key,
            package_name="com.demo",
            activity=".Main",
            screenshot_sha="saved-sha",
            depth=0,
            path=[],
            actions=[],
        )

        with patch(
            "backend.inspection.engine.exact_parent_matches",
            return_value=False,
        ), patch(
            "backend.inspection.engine.wait_for_stable_page",
            return_value=current_capture,
        ), patch(
            "backend.inspection.engine._replay_path",
        ) as replay:
            result = _ensure_parent(
                device=Mock(),
                parent=parent,
                branch_config={},
                device_serial="android-1",
                package_name="com.demo",
                abort_event=threading.Event(),
                input_rules=[],
                dynamic_patterns=[],
                stable_wait_seconds=5.0,
                secret_values=[],
            )

        self.assertIs(result, current_capture)
        replay.assert_not_called()

    def test_root_without_real_replay_stays_observed_only(self):
        test_engine = create_engine(
            "sqlite://",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        SQLModel.metadata.create_all(test_engine)
        with Session(test_engine) as session:
            session.add(
                InspectionState(
                    run_id=1,
                    branch_run_id=1,
                    branch_key="authenticated",
                    cluster_key="root-cluster",
                    state_key="root-state",
                    depth=0,
                    stable_status="UNVERIFIED",
                    first_path=[],
                )
            )
            session.add(
                InspectionState(
                    run_id=1,
                    branch_run_id=1,
                    branch_key="authenticated",
                    cluster_key="viewport-cluster",
                    state_key="viewport-state",
                    depth=0,
                    stable_status="UNVERIFIED",
                    first_path=[
                        {
                            "action_type": "scroll",
                            "action_key": "viewport-up",
                            "coordinate_only": False,
                            "replayable": True,
                        }
                    ],
                )
            )
            session.commit()

        with patch(
            "backend.inspection.engine.engine",
            test_engine,
        ), patch(
            "backend.inspection.engine._replay_path",
        ) as replay:
            count = _verify_stable_paths(
                run_id=1,
                branch_run_id=1,
                device=Mock(),
                branch_config={},
                device_serial="android-1",
                package_name="com.demo",
                abort_event=threading.Event(),
                input_rules=[],
                dynamic_patterns=[],
                stable_wait_seconds=5.0,
                deadline=time.monotonic() + 10,
                secret_values=[],
            )

        self.assertEqual(count, 0)
        replay.assert_not_called()
        with Session(test_engine) as session:
            root_state = session.get(InspectionState, 1)
            viewport_state = session.get(InspectionState, 2)
            self.assertEqual(root_state.stable_status, "UNVERIFIED")
            self.assertFalse(root_state.selected_for_regression)
            self.assertEqual(viewport_state.stable_status, "VIEWPORT")
            self.assertFalse(viewport_state.selected_for_regression)
        test_engine.dispose()

    def test_stability_verification_marks_replayed_prefix_checkpoints(self):
        test_engine = create_engine(
            "sqlite://",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        SQLModel.metadata.create_all(test_engine)
        step_one = {
            "action_type": "click",
            "action_key": "open-list",
            "action_role": "NAV:LIST",
            "risk_type": "PAYMENT",
            "status": "PASS",
            "execution_disposition": "EXECUTED",
            "expected_source_semantic_key": "root",
            "expected_target_semantic_key": "middle",
            "replayable": True,
        }
        step_two = {
            "action_type": "click",
            "action_key": "open-detail",
            "action_role": "ITEM_OPEN:PRODUCT",
            "expected_source_semantic_key": "middle",
            "expected_target_semantic_key": "end",
            "replayable": True,
        }
        with Session(test_engine) as session:
            session.add_all(
                [
                    InspectionState(
                        id=1,
                        run_id=1,
                        branch_run_id=1,
                        branch_key="authenticated",
                        cluster_key="root",
                        state_key="root",
                        semantic_key="root",
                        instance_anchor="root",
                        page_subtype="HOME",
                        coverage_status="INCOMPLETE",
                        depth=0,
                        first_path=[],
                        representative_observation_id=101,
                    ),
                    InspectionState(
                        id=2,
                        run_id=1,
                        branch_run_id=1,
                        branch_key="authenticated",
                        cluster_key="middle",
                        state_key="middle",
                        semantic_key="middle",
                        instance_anchor="middle",
                        page_subtype="PRODUCT_LIST",
                        coverage_status="INCOMPLETE",
                        depth=1,
                        first_path=[step_one],
                        representative_observation_id=102,
                    ),
                    InspectionState(
                        id=3,
                        run_id=1,
                        branch_run_id=1,
                        branch_key="authenticated",
                        cluster_key="end",
                        state_key="end",
                        semantic_key="end",
                        instance_anchor="end",
                        page_subtype="CASHIER",
                        coverage_status="INCOMPLETE",
                        depth=2,
                        first_path=[step_one, step_two],
                        representative_observation_id=103,
                    ),
                ]
            )
            session.commit()

        def replay_result(*_args, **kwargs):
            path = kwargs.get("path") or []
            key = path[-1].get("expected_target_semantic_key") if path else "root"
            capture = Mock()
            capture.model.semantic_key = key
            capture.model.cluster_key = key
            return capture, True

        with patch("backend.inspection.engine.engine", test_engine), patch(
            "backend.inspection.engine._replay_path", side_effect=replay_result
        ) as replay, patch("backend.inspection.engine._pin_observation_assets") as pin:
            count = _verify_stable_paths(
                run_id=1,
                branch_run_id=1,
                device=Mock(),
                branch_config={},
                device_serial="android-1",
                package_name="com.demo",
                abort_event=threading.Event(),
                input_rules=[],
                dynamic_patterns=[],
                stable_wait_seconds=0.1,
                deadline=time.monotonic() + 10,
                secret_values=[],
                representative_only=True,
            )

        self.assertEqual(count, 3)
        with Session(test_engine) as session:
            states = session.exec(
                select(InspectionState).order_by(InspectionState.id)
            ).all()
        self.assertEqual(
            [item.stable_status for item in states],
            ["VERIFIED_TWICE", "STABLE", "STABLE"],
        )
        self.assertTrue(all(item.selected_for_regression for item in states))
        self.assertEqual(replay.call_count, 2)
        self.assertEqual(
            {call.args[0] for call in pin.call_args_list},
            {101, 102, 103},
        )
        test_engine.dispose()

    def test_business_coverage_endpoint_is_reverified_once_only(self):
        test_engine = create_engine(
            "sqlite://",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        SQLModel.metadata.create_all(test_engine)
        required_step = {
            "action_type": "click",
            "action_key": "open-required",
            "expected_target_semantic_key": "required-end",
            "replayable": True,
        }
        ordinary_step = {
            "action_type": "click",
            "action_key": "open-ordinary",
            "expected_target_semantic_key": "ordinary-end",
            "replayable": True,
        }
        with Session(test_engine) as session:
            session.add_all(
                [
                    InspectionState(
                        id=1,
                        run_id=1,
                        branch_run_id=1,
                        branch_key="authenticated",
                        cluster_key="required-end",
                        state_key="required-end",
                        semantic_key="required-end",
                        coverage_status="INCOMPLETE",
                        depth=1,
                        first_path=[required_step],
                    ),
                    InspectionState(
                        id=2,
                        run_id=1,
                        branch_run_id=1,
                        branch_key="authenticated",
                        cluster_key="ordinary-end",
                        state_key="ordinary-end",
                        semantic_key="ordinary-end",
                        coverage_status="EXPLORED",
                        depth=1,
                        first_path=[ordinary_step],
                    ),
                ]
            )
            session.commit()

        def replay_result(*_args, **kwargs):
            key = kwargs["path"][-1]["expected_target_semantic_key"]
            capture = Mock()
            capture.model.semantic_key = key
            capture.model.cluster_key = key
            return capture, True

        with patch("backend.inspection.engine.engine", test_engine), patch(
            "backend.inspection.engine._replay_path", side_effect=replay_result
        ) as replay:
            count = _verify_stable_paths(
                run_id=1,
                branch_run_id=1,
                device=Mock(),
                branch_config={},
                device_serial="android-1",
                package_name="com.demo",
                abort_event=threading.Event(),
                input_rules=[],
                dynamic_patterns=[],
                stable_wait_seconds=0.1,
                deadline=time.monotonic() + 10,
                secret_values=[],
                coverage_reverify_once=True,
            )

        self.assertEqual(count, 2)
        self.assertEqual(replay.call_count, 3)
        with Session(test_engine) as session:
            required = session.get(InspectionState, 1)
            ordinary = session.get(InspectionState, 2)
            self.assertEqual(required.stable_status, "REVERIFIED_ONCE")
            self.assertFalse(required.selected_for_regression)
            self.assertEqual(ordinary.stable_status, "STABLE")
            self.assertTrue(ordinary.selected_for_regression)
        test_engine.dispose()

    def test_business_coverage_reverify_accepts_only_exact_dynamic_endpoint(self):
        test_engine = create_engine(
            "sqlite://",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        SQLModel.metadata.create_all(test_engine)
        required_step = {
            "action_type": "click",
            "action_key": "open-favorites",
            "locator_candidates": [
                {"by": "description", "selector": "商品收藏, 40"}
            ],
            "target_meta": {"content_desc": "商品收藏, 40"},
            "expected_source_semantic_key": "profile",
            "expected_target_semantic_key": "frozen-favorites",
            "expected_source_signature": {"instance_anchor": "profile-instance"},
            "expected_target_signature": {
                "package": "com.demo",
                "activity_family": "main",
                "role": "LIST",
                "page_subtype": "FAVORITES",
                "content_anchor": "favorites-content",
                "instance_anchor": "favorites-instance",
            },
            "replayable": True,
        }
        ordinary_step = {
            **required_step,
            "action_key": "open-ordinary",
            "expected_target_semantic_key": "ordinary-end",
        }
        with Session(test_engine) as session:
            session.add_all(
                [
                    InspectionState(
                        id=1,
                        run_id=1,
                        branch_run_id=1,
                        branch_key="authenticated",
                        cluster_key="frozen-favorites",
                        state_key="frozen-favorites",
                        semantic_key="frozen-favorites",
                        page_subtype="FAVORITES",
                        coverage_status="INCOMPLETE",
                        depth=1,
                        first_path=[required_step],
                    ),
                    InspectionState(
                        id=2,
                        run_id=1,
                        branch_run_id=1,
                        branch_key="authenticated",
                        cluster_key="ordinary-end",
                        state_key="ordinary-end",
                        semantic_key="ordinary-end",
                        page_subtype="FAVORITES",
                        coverage_status="EXPLORED",
                        depth=1,
                        first_path=[ordinary_step],
                    ),
                ]
            )
            session.commit()

        model = Mock(
            semantic_key="dynamic-favorites",
            cluster_key="dynamic-favorites",
            package_name="com.demo",
            activity_family="main",
            role="LIST",
            page_subtype="FAVORITES",
        )
        capture = CapturedPage(
            package_name="com.demo",
            activity=".Main",
            xml='<hierarchy><node text="商品收藏" /></hierarchy>',
            screenshot_png=b"png",
            screenshot_sha="sha",
            perceptual_hash="phash",
            model=model,
            stable_by="timeout",
        )
        divergence = PathDiverged(
            phase="target_template",
            expected="frozen-template",
            actual="animated-template",
            step_index=0,
        )

        def anchor_for(_model, *, incoming_action=None, source_instance_anchor=None):
            if incoming_action is not None and source_instance_anchor:
                return "favorites-instance"
            return "favorites-content"

        with patch("backend.inspection.engine.engine", test_engine), patch(
            "backend.inspection.engine._replay_path",
            side_effect=divergence,
        ) as replay, patch(
            "backend.inspection.engine._budgeted_wait_for_stable_page",
            return_value=capture,
        ) as recapture, patch(
            "backend.inspection.engine.derive_instance_anchor",
            side_effect=anchor_for,
        ):
            count = _verify_stable_paths(
                run_id=1,
                branch_run_id=1,
                device=Mock(),
                branch_config={},
                device_serial="android-1",
                package_name="com.demo",
                abort_event=threading.Event(),
                input_rules=[],
                dynamic_patterns=[],
                stable_wait_seconds=0.1,
                deadline=time.monotonic() + 10,
                secret_values=[],
                coverage_reverify_once=True,
            )

        self.assertEqual(count, 1)
        self.assertEqual(replay.call_count, 3)
        recapture.assert_called_once()
        with Session(test_engine) as session:
            required = session.get(InspectionState, 1)
            ordinary = session.get(InspectionState, 2)
            self.assertEqual(required.stable_status, "REVERIFIED_ONCE")
            self.assertFalse(required.selected_for_regression)
            self.assertEqual(ordinary.stable_status, "UNSTABLE")
        test_engine.dispose()

    def test_dynamic_coverage_endpoint_rejects_unknown_or_wrong_identity(self):
        model = Mock(
            package_name="com.demo",
            activity_family="main",
            role="LIST",
            page_subtype="UNKNOWN",
        )
        capture = CapturedPage(
            package_name="com.demo",
            activity=".Main",
            xml="<hierarchy />",
            screenshot_png=b"png",
            screenshot_sha="sha",
            perceptual_hash="phash",
            model=model,
            stable_by="timeout",
        )
        path = [
            {
                "action_type": "click",
                "action_key": "open-favorites",
                "target_meta": {},
                "expected_source_signature": {
                    "instance_anchor": "profile-instance"
                },
                "expected_target_signature": {
                    "package": "com.demo",
                    "activity_family": "main",
                    "role": "LIST",
                    "page_subtype": "FAVORITES",
                    "content_anchor": "favorites-content",
                    "instance_anchor": "favorites-instance",
                },
            }
        ]
        self.assertFalse(
            _coverage_endpoint_reverify_matches(
                capture,
                path,
                expected_page_subtype="FAVORITES",
            )
        )

    def test_dynamic_favorites_reverify_allows_live_content_anchor_drift(self):
        model = Mock(
            package_name="com.demo",
            activity_family="main",
            role="LIST",
            page_subtype="FAVORITES",
        )
        capture = CapturedPage(
            package_name="com.demo",
            activity=".Main",
            xml='<hierarchy><node text="商品收藏" /></hierarchy>',
            screenshot_png=b"png",
            screenshot_sha="sha",
            perceptual_hash="phash",
            model=model,
            stable_by="timeout",
        )
        path = [
            {
                "action_type": "click",
                "action_key": "open-favorites",
                "target_meta": {},
                "expected_source_signature": {
                    "instance_anchor": "profile-instance"
                },
                "expected_target_signature": {
                    "package": "com.demo",
                    "activity_family": "main",
                    "role": "LIST",
                    "page_subtype": "FAVORITES",
                    "content_anchor": "stale-content-anchor",
                    "instance_anchor": "stale-instance-anchor",
                },
            }
        ]

        with patch(
            "backend.inspection.engine.derive_instance_anchor",
            return_value="live-anchor",
        ):
            self.assertTrue(
                _coverage_endpoint_reverify_matches(
                    capture,
                    path,
                    expected_page_subtype="FAVORITES",
                )
            )

        capture.xml = '<hierarchy><node text="个人中心" /></hierarchy>'
        self.assertFalse(
            _coverage_endpoint_reverify_matches(
                capture,
                path,
                expected_page_subtype="FAVORITES",
            )
        )

    def test_external_overlay_does_not_add_back_and_scrolls_are_last(self):
        xml = (
            '<hierarchy rotation="0">'
            '<node package="com.demo" class="android.widget.FrameLayout" '
            'enabled="true" bounds="[0,0][1080,2400]">'
            '<node package="com.demo" class="android.widget.Button" '
            'content-desc="分类" clickable="true" enabled="true" '
            'bounds="[10,10][200,100]"/>'
            '<node package="com.demo" '
            'class="androidx.recyclerview.widget.RecyclerView" '
            'scrollable="true" enabled="true" '
            'bounds="[0,200][1080,2200]"/>'
            "</node>"
            '<node package="com.coloros.smartsidebar" '
            'class="android.view.ViewGroup" content-desc="智能侧边栏" '
            'clickable="true" enabled="true" '
            'bounds="[1060,200][1080,500]"/>'
            "</hierarchy>"
        )
        model = build_page_model(
            xml,
            package_name="com.demo",
            activity=".Main",
        )
        capture = CapturedPage(
            package_name="com.demo",
            activity=".Main",
            xml=xml,
            screenshot_png=b"png",
            screenshot_sha="sha",
            perceptual_hash="phash",
            model=model,
            stable_by="exact",
        )

        actions = _state_actions(
            capture,
            screen_size=(1080, 2400),
            safety_rules=[],
            input_rules=[],
            max_scrolls=3,
            depth=2,
        )

        self.assertFalse(any(action.action_type == "back" for action in actions))
        first_scroll = next(
            index
            for index, action in enumerate(actions)
            if action.action_type == "scroll"
        )
        self.assertTrue(
            all(action.action_type != "scroll" for action in actions[:first_scroll])
        )
        self.assertTrue(
            all(action.action_type == "scroll" for action in actions[first_scroll:])
        )

    def test_dialog_adds_explicit_back_action(self):
        xml = _page(
            '<node class="android.app.Dialog" enabled="true" '
            'bounds="[100,100][900,1200]"/>'
        )
        model = build_page_model(
            xml,
            package_name="com.demo",
            activity=".Main",
        )
        capture = CapturedPage(
            package_name="com.demo",
            activity=".Main",
            xml=xml,
            screenshot_png=b"png",
            screenshot_sha="sha",
            perceptual_hash="phash",
            model=model,
            stable_by="exact",
        )
        actions = _state_actions(
            capture,
            screen_size=(1080, 2400),
            safety_rules=[],
            input_rules=[],
            max_scrolls=3,
            depth=0,
        )
        self.assertEqual(
            [action.action_type for action in actions],
            ["back"],
        )

    def test_parent_capture_match_is_none_safe(self):
        xml = _page(
            '<node class="android.widget.Button" content-desc="首页" '
            'clickable="true" enabled="true" bounds="[10,10][200,100]"/>'
        )
        model = build_page_model(
            xml,
            package_name="com.demo",
            activity=".Main",
        )
        parent = StateWork(
            state_id=1,
            state_key=model.state_key,
            cluster_key=model.cluster_key,
            replay_key=model.replay_key,
            package_name="com.demo",
            activity=".Main",
            screenshot_sha="sha",
            depth=0,
            path=[],
            actions=[],
        )
        self.assertFalse(_capture_matches_parent(None, parent))

    def test_coordinate_scroll_replay_is_only_allowed_for_discovery(self):
        xml = _page(
            '<node class="androidx.recyclerview.widget.RecyclerView" '
            'scrollable="true" enabled="true" bounds="[0,200][1000,2000]"/>'
        )
        model = build_page_model(
            xml,
            package_name="com.demo",
            activity=".List",
        )
        capture = CapturedPage(
            package_name="com.demo",
            activity=".List",
            xml=xml,
            screenshot_png=b"png",
            screenshot_sha="sha",
            perceptual_hash="phash",
            model=model,
            stable_by="exact",
        )
        scroll = next(
            action
            for action in enumerate_actions(
                model,
                screen_size=(1080, 2400),
            )
            if action.action_type == "scroll"
        )
        path = [_serialize_action(scroll)]
        common = {
            "device": Mock(),
            "path": path,
            "branch_config": {
                "entry_case_id": 1,
                "ready_assertion": {
                    "selector": "首页",
                    "by": "description",
                },
            },
            "device_serial": "android-1",
            "package_name": "com.demo",
            "abort_event": threading.Event(),
            "input_rules": [],
            "dynamic_patterns": [],
            "stable_wait_seconds": 5.0,
            "secret_values": [],
        }

        with patch(
            "backend.inspection.engine._try_run_case",
            return_value=True,
        ), patch(
            "backend.inspection.engine.ready_assertion_exists",
            return_value=True,
        ), patch(
            "backend.inspection.engine.wait_for_stable_page",
            return_value=capture,
        ), patch(
            "backend.inspection.engine.perform_action",
        ) as perform:
            stable_capture, stable_unique = _replay_path(**common)
            self.assertIsNone(stable_capture)
            self.assertFalse(stable_unique)
            perform.assert_not_called()

            discovery_capture, discovery_unique = _replay_path(
                **common,
                allow_discovery_scroll=True,
            )
            self.assertIs(discovery_capture, capture)
            self.assertTrue(discovery_unique)
            perform.assert_called_once()

    def test_scroll_expands_viewport_without_back_or_depth_increment(self):
        test_engine = create_engine(
            "sqlite://",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        SQLModel.metadata.create_all(test_engine)
        with Session(test_engine) as session:
            run = InspectionRun(
                name="scroll viewport",
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
            run_id = run.id
            branch_id = branch.id

        root_xml = _page(
            '<node class="androidx.recyclerview.widget.RecyclerView" '
            'scrollable="true" enabled="true" bounds="[0,200][1000,2000]"/>'
        )
        viewport_xml = _page(
            '<node class="androidx.recyclerview.widget.RecyclerView" '
            'scrollable="true" enabled="true" bounds="[0,200][1000,2000]">'
            '<node class="android.widget.TextView" content-desc="更多内容" '
            'enabled="true" bounds="[10,1500][500,1600]"/>'
            "</node>"
        )
        root_model = build_page_model(
            root_xml,
            package_name="com.demo",
            activity=".Main",
        )
        viewport_model = build_page_model(
            viewport_xml,
            package_name="com.demo",
            activity=".Main",
        )
        scroll = next(
            action
            for action in enumerate_actions(
                root_model,
                screen_size=(1080, 2400),
            )
            if action.action_type == "scroll"
            and action.target_meta.get("direction") == "up"
        )
        root_capture = CapturedPage(
            package_name="com.demo",
            activity=".Main",
            xml=root_xml,
            screenshot_png=b"root",
            screenshot_sha="root-sha",
            perceptual_hash="root-phash",
            model=root_model,
            stable_by="exact",
        )
        viewport_capture = CapturedPage(
            package_name="com.demo",
            activity=".Main",
            xml=viewport_xml,
            screenshot_png=b"viewport",
            screenshot_sha="viewport-sha",
            perceptual_hash="viewport-phash",
            model=viewport_model,
            stable_by="exact",
        )
        root_work = StateWork(
            state_id=101,
            state_key=root_model.state_key,
            cluster_key=root_model.cluster_key,
            replay_key=root_model.replay_key,
            package_name="com.demo",
            activity=".Main",
            screenshot_sha="root-sha",
            depth=0,
            path=[],
            actions=[scroll],
        )
        viewport_work = StateWork(
            state_id=102,
            state_key=viewport_model.state_key,
            cluster_key=viewport_model.cluster_key,
            replay_key=viewport_model.replay_key,
            package_name="com.demo",
            activity=".Main",
            screenshot_sha="viewport-sha",
            depth=0,
            path=[_serialize_action(scroll)],
            actions=[],
        )
        device = Mock()
        device.window_size.return_value = (1080, 2400)

        with patch(
            "backend.inspection.engine.engine",
            test_engine,
        ), patch(
            "backend.inspection.engine._prepare_branch",
        ), patch(
            "backend.inspection.engine.wait_for_stable_page",
            side_effect=[root_capture, viewport_capture],
        ), patch(
            "backend.inspection.engine._ensure_parent",
            side_effect=[root_capture, viewport_capture],
        ), patch(
            "backend.inspection.engine._persist_state",
            side_effect=[
                PersistedState(work=root_work, is_new=True),
                PersistedState(work=viewport_work, is_new=True),
            ],
        ) as persist_state, patch(
            "backend.inspection.engine.perform_action",
            return_value="scroll:up:coordinate",
        ), patch(
            "backend.inspection.engine.is_white_screen",
            return_value=False,
        ), patch(
            "backend.inspection.engine._verify_stable_paths",
            return_value=0,
        ):
            outcome = _execute_branch(
                run_id=run_id,
                branch_run_id=branch_id,
                device=device,
                device_serial="android-1",
                package_name="com.demo",
                profile={
                    "budgets": {
                        "duration_seconds": 30,
                        "max_states": 10,
                        "max_actions": 10,
                        "max_depth": 3,
                    }
                },
                branch_config={},
                abort_event=threading.Event(),
                monitor=None,
            )

        self.assertIn(outcome.status, {"PASS", "WARNING"})
        device.press.assert_not_called()
        self.assertEqual(persist_state.call_args_list[1].kwargs["depth"], 0)
        with Session(test_engine) as session:
            transition = session.exec(
                select(InspectionTransition).where(
                    InspectionTransition.run_id == run_id
                )
            ).one()
            self.assertEqual(transition.action_type, "scroll")
            self.assertEqual(transition.reason, "同页视口扩展")
        test_engine.dispose()

    def test_parent_recovery_failure_is_not_retried_for_every_action(self):
        test_engine = create_engine(
            "sqlite://",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        SQLModel.metadata.create_all(test_engine)
        with Session(test_engine) as session:
            run = InspectionRun(
                name="single parent recovery",
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
            run_id = run.id
            branch_id = branch.id

        root_xml = _page(
            '<node class="android.widget.Button" content-desc="入口一" '
            'clickable="true" enabled="true" bounds="[10,10][200,100]"/>'
            '<node class="android.widget.Button" content-desc="入口二" '
            'clickable="true" enabled="true" bounds="[10,110][200,200]"/>'
            '<node class="android.widget.Button" content-desc="入口三" '
            'clickable="true" enabled="true" bounds="[10,210][200,300]"/>'
        )
        child_xml = _page(
            '<node class="android.widget.TextView" content-desc="子页面" '
            'enabled="true" bounds="[10,10][400,100]"/>'
        )
        root_model = build_page_model(
            root_xml,
            package_name="com.demo",
            activity=".Main",
        )
        child_model = build_page_model(
            child_xml,
            package_name="com.demo",
            activity=".Child",
        )
        actions = enumerate_actions(root_model, screen_size=(1080, 2400))
        root_capture = CapturedPage(
            package_name="com.demo",
            activity=".Main",
            xml=root_xml,
            screenshot_png=b"root",
            screenshot_sha="root-sha",
            perceptual_hash="root-phash",
            model=root_model,
            stable_by="exact",
        )
        child_capture = CapturedPage(
            package_name="com.demo",
            activity=".Child",
            xml=child_xml,
            screenshot_png=b"child",
            screenshot_sha="child-sha",
            perceptual_hash="child-phash",
            model=child_model,
            stable_by="exact",
        )
        root_work = StateWork(
            state_id=201,
            state_key=root_model.state_key,
            cluster_key=root_model.cluster_key,
            replay_key=root_model.replay_key,
            package_name="com.demo",
            activity=".Main",
            screenshot_sha="root-sha",
            depth=0,
            path=[],
            actions=actions,
        )
        child_work = StateWork(
            state_id=202,
            state_key=child_model.state_key,
            cluster_key=child_model.cluster_key,
            replay_key=child_model.replay_key,
            package_name="com.demo",
            activity=".Child",
            screenshot_sha="child-sha",
            depth=1,
            path=[_serialize_action(actions[0])],
            actions=[],
        )
        device = Mock()
        device.window_size.return_value = (1080, 2400)

        with patch(
            "backend.inspection.engine.engine",
            test_engine,
        ), patch(
            "backend.inspection.engine._prepare_branch",
        ), patch(
            "backend.inspection.engine.wait_for_stable_page",
            side_effect=[root_capture, child_capture],
        ), patch(
            "backend.inspection.engine._ensure_parent",
            side_effect=[root_capture, child_capture],
        ) as ensure_parent, patch(
            "backend.inspection.engine._restore_parent_after_transition",
            return_value=None,
        ) as restore_parent, patch(
            "backend.inspection.engine._persist_state",
            side_effect=[
                PersistedState(work=root_work, is_new=True),
                PersistedState(work=child_work, is_new=True),
            ],
        ), patch(
            "backend.inspection.engine.perform_action",
            return_value="description",
        ) as perform_action_mock, patch(
            "backend.inspection.engine.exact_parent_matches",
            return_value=False,
        ), patch(
            "backend.inspection.engine.is_white_screen",
            return_value=False,
        ), patch(
            "backend.inspection.engine._verify_stable_paths",
            return_value=0,
        ):
            outcome = _execute_branch(
                run_id=run_id,
                branch_run_id=branch_id,
                device=device,
                device_serial="android-1",
                package_name="com.demo",
                profile={
                    "budgets": {
                        "duration_seconds": 30,
                        "max_states": 10,
                        "max_actions": 10,
                        "max_depth": 3,
                    }
                },
                branch_config={},
                abort_event=threading.Event(),
                monitor=None,
            )

        self.assertEqual(outcome.status, "WARNING")
        self.assertEqual(perform_action_mock.call_count, 1)
        # Root and queued child are each restored once. The exhausted root
        # recovery is not retried for every remaining root action.
        self.assertEqual(ensure_parent.call_count, 2)
        restore_parent.assert_called_once()
        with Session(test_engine) as session:
            transitions = session.exec(
                select(InspectionTransition)
                .where(InspectionTransition.run_id == run_id)
                .order_by(InspectionTransition.sequence)
            ).all()
            self.assertEqual(
                [item.status for item in transitions],
                [
                    "PASS",
                    "PARENT_RECOVERY_FAILED",
                    "PARENT_RECOVERY_FAILED",
                ],
            )
        test_engine.dispose()

    def test_abnormal_branch_finish_keeps_partial_counts(self):
        test_engine = create_engine(
            "sqlite://",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        SQLModel.metadata.create_all(test_engine)
        with Session(test_engine) as session:
            run = InspectionRun(
                name="partial error",
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
                status="RUNNING",
            )
            session.add(branch)
            session.commit()
            session.refresh(branch)
            state = InspectionState(
                run_id=run.id,
                branch_run_id=branch.id,
                branch_key="authenticated",
                cluster_key="cluster",
                state_key="state",
                stable_status="STABLE",
            )
            session.add(state)
            session.commit()
            session.refresh(state)
            session.add(
                InspectionTransition(
                    run_id=run.id,
                    branch_run_id=branch.id,
                    from_state_id=state.id,
                    sequence=1,
                    action_type="click",
                    action_key="blocked",
                    status="BLOCKED",
                )
            )
            session.add(
                InspectionFault(
                    run_id=run.id,
                    branch_run_id=branch.id,
                    state_id=state.id,
                    fault_type="UI_UNRESPONSIVE",
                    signature="fault",
                    occurrence_count=2,
                )
            )
            session.commit()
            branch_id = branch.id
            run_id = run.id

        with patch("backend.inspection.engine.engine", test_engine):
            _finish_active_branches(
                run_id,
                status="ERROR",
                reason="基础设施异常",
            )

        with Session(test_engine) as session:
            branch = session.get(InspectionBranchRun, branch_id)
            self.assertEqual(branch.status, "ERROR")
            self.assertEqual(branch.state_count, 1)
            self.assertEqual(branch.transition_count, 1)
            self.assertEqual(branch.blocked_count, 1)
            self.assertEqual(branch.stable_count, 1)
            self.assertEqual(branch.fault_count, 2)
        test_engine.dispose()

    def test_scrcpy_replay_path_is_canonicalized_to_inspection_asset(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            reports_root = Path(tmpdir) / "reports"
            replay = (
                reports_root
                / "inspection"
                / "42"
                / "monitor"
                / "replays"
                / "crash_100001_01.mp4"
            )
            replay.parent.mkdir(parents=True)
            replay.write_bytes(b"mp4")
            with patch(
                "backend.inspection.engine._reports_root",
                return_value=reports_root.resolve(),
            ):
                expected = (
                    "inspection/42/monitor/replays/crash_100001_01.mp4"
                )
                self.assertEqual(
                    _validated_fault_artifact(
                        f"reports/{expected}",
                        run_id=42,
                    ),
                    expected,
                )
                self.assertEqual(
                    _validated_replay_artifact(
                        {
                            "path": "",
                            "filename": "crash_100001_01.mp4",
                        },
                        run_id=42,
                    ),
                    expected,
                )
                self.assertIsNone(
                    _validated_replay_artifact(
                        {
                            "path": "",
                            "filename": "../crash_100001_01.mp4",
                        },
                        run_id=42,
                    )
                )

    def test_destructive_actions_remain_blocked_without_device_operation(self):
        xml = _page(
            '<node class="android.widget.Button" content-desc="Delete account" '
            'clickable="true" enabled="true" bounds="[10,10][200,100]"/>'
            '<node class="android.widget.Button" text="支付" '
            'clickable="true" enabled="true" bounds="[10,110][200,200]"/>'
        )
        model = build_page_model(xml, package_name="com.demo", activity=".Account")
        actions = enumerate_actions(model, screen_size=(1080, 2400))
        self.assertEqual(actions[0].risk_type, "DESTRUCTIVE")
        self.assertFalse(actions[0].replayable)
        self.assertIsNone(actions[1].risk_type)
        self.assertTrue(actions[1].replayable)

    def test_transaction_navigation_and_continuations_are_allowed(self):
        labels = (
            "立即购买",
            "去结算",
            "提交订单",
            "下一步",
            "继续",
            "确认",
        )

        def transaction_page(title: str) -> str:
            buttons = "".join(
                f'<node class="android.widget.Button" text="{label}" '
                f'clickable="true" enabled="true" bounds="[10,{120 + index * 110}]'
                f'[400,{220 + index * 110}]"/>'
                for index, label in enumerate(labels)
            )
            return _page(
                f'<node class="android.widget.TextView" text="{title}" '
                'enabled="true" bounds="[10,10][900,100]"/>'
                + buttons
            )

        for title, activity, expected_role in (
            ("确认订单", ".CheckoutActivity", "CHECKOUT"),
            ("订单详情", ".OrderDetailActivity", "ORDER"),
        ):
            with self.subTest(role=expected_role):
                page = build_page_model(
                    transaction_page(title),
                    package_name="com.demo",
                    activity=activity,
                )
                self.assertEqual(page.role, expected_role)
                self.assertEqual(page.risk_tokens, ())
                allowed = enumerate_actions(page, screen_size=(1080, 2400))
                self.assertEqual(len(allowed), len(labels))
                self.assertTrue(all(action.risk_type is None for action in allowed))
                self.assertTrue(all(action.replayable for action in allowed))

        unknown_page = build_page_model(
            _page(
                '<node class="android.widget.Button" text="下一步" '
                'clickable="true" enabled="true" bounds="[10,120][400,220]"/>'
            ),
            package_name="com.demo",
            activity=".WizardActivity",
        )
        self.assertEqual(unknown_page.role, "UNKNOWN")
        self.assertIsNone(
            enumerate_actions(unknown_page, screen_size=(1080, 2400))[0].risk_type
        )

    def test_risk_signature_conflict_is_a_similarity_hard_negative(self):
        def product(action: str) -> object:
            return build_page_model(
                _page(
                    '<node class="android.widget.TextView" text="商品详情" '
                    'enabled="true" bounds="[10,10][900,100]"/>'
                    f'<node class="android.widget.Button" text="{action}" '
                    'clickable="true" enabled="true" bounds="[10,120][400,220]"/>'
                ),
                package_name="com.demo",
                activity=".ProductDetailActivity",
            )

        navigation = product("立即购买")
        payment = product("确认支付")
        destructive = product("删除")
        self.assertEqual(navigation.risk_tokens, ())
        self.assertEqual(payment.risk_tokens, ("PAYMENT",))
        self.assertEqual(destructive.risk_tokens, ("DESTRUCTIVE",))
        self.assertNotIn("立即购买", repr(navigation.signature))

        similarity = compare_page_models(payment, destructive)
        self.assertFalse(similarity.equivalent)
        self.assertEqual(similarity.score, 0.0)
        self.assertFalse(similarity.evidence["risk_signature_match"])
        self.assertFalse(similarity.evidence["hard_gates_passed"])

    def test_run20_body_copy_does_not_taint_buy_now_or_coordinate_controls(self):
        xml = _page(
            '<node class="android.widget.TextView" '
            'text="如需上门服务，可在提交订单时自主勾选付费服务" '
            'enabled="true" bounds="[100,1200][900,1500]"/>'
            '<node class="android.widget.Button" content-desc="立即购买" '
            'clickable="true" enabled="true" bounds="[500,1700][900,1850]"/>'
            '<node class="android.widget.Button" text="确认" '
            'clickable="true" enabled="true" bounds="[100,1550][400,1680]"/>'
            '<node class="android.widget.Button" clickable="true" enabled="true" '
            'bounds="[500,1900][900,2050]"/>'
        )
        page = build_page_model(
            xml,
            package_name="com.ehaier.zgq.shop.mall",
            activity="com.ehaier.mall.MainActivity",
        )
        actions = enumerate_actions(page, screen_size=(1080, 2400))
        buy_now = next(
            item
            for item in actions
            if item.target_meta.get("content_desc") == "立即购买"
        )
        confirmation = next(
            item for item in actions if item.target_meta.get("text") == "确认"
        )

        self.assertIsNone(buy_now.risk_type)
        self.assertTrue(buy_now.replayable)
        self.assertIsNone(confirmation.risk_type)
        self.assertTrue(confirmation.replayable)
        self.assertFalse(any(item.coordinate_only for item in actions))

    def test_only_haier_cashier_final_payment_is_blocked(self):
        cashier_xml = _page(
            '<node class="android.widget.TextView" text="海尔收银台" '
            'enabled="true" bounds="[100,100][900,220]"/>'
            '<node class="android.widget.Button" content-desc="确认" '
            'clickable="true" enabled="true" bounds="[100,1500][900,1650]">'
            '<node class="android.widget.TextView" text="确认云闪付支付 ￥314.00" '
            'enabled="true" bounds="[150,1530][850,1620]"/>'
            "</node>"
            '<node class="android.widget.Button" text="支付方式" '
            'clickable="true" enabled="true" bounds="[100,1200][900,1350]"/>'
            '<node class="android.widget.Button" content-desc="返回" '
            'clickable="true" enabled="true" bounds="[20,100][100,200]"/>'
            '<node class="android.widget.Button" clickable="true" enabled="true" '
            'bounds="[100,1800][900,1950]"/>'
        )
        cashier = build_page_model(
            cashier_xml,
            package_name="com.ehaier.zgq.shop.mall",
            activity="com.ehaier.mall.MainActivity",
        )
        self.assertEqual(cashier.page_subtype, "CASHIER")
        actions = enumerate_actions(cashier, screen_size=(1080, 2400))
        payment = next(item for item in actions if item.risk_type == "PAYMENT")
        payment_method = next(
            item for item in actions if item.target_meta.get("text") == "支付方式"
        )
        back = next(
            item for item in actions if item.target_meta.get("content_desc") == "返回"
        )
        self.assertEqual(payment.risk_type, "PAYMENT")
        self.assertEqual(
            payment.blocked_reason,
            "海尔收银台最终付款安全规则命中: PAYMENT",
        )
        self.assertFalse(payment.replayable)
        self.assertFalse(payment.coordinate_only)
        self.assertEqual(payment.target_meta.get("content_desc"), "确认")
        self.assertNotIn("314.00", repr(payment.target_meta))
        self.assertIsNone(payment_method.risk_type)
        self.assertIsNone(back.risk_type)
        self.assertFalse(any(item.coordinate_only for item in actions))
        device = Mock()
        with self.assertRaises(PermissionError):
            perform_action(device, payment, current_xml=cashier_xml)
        device.click.assert_not_called()

        dynamic_direct_xml = _page(
            '<node class="android.widget.TextView" text="海尔收银台" '
            'enabled="true" bounds="[417,127][662,193]"/>'
            '<node class="android.view.ViewGroup" content-desc="订单详情" '
            'clickable="true" enabled="true" bounds="[889,133][1045,186]"/>'
            '<node class="android.view.ViewGroup" content-desc="微信支付" '
            'clickable="true" enabled="true" bounds="[46,546][1034,710]"/>'
            '<node class="android.view.ViewGroup" '
            'content-desc="确认微信支付 ￥2234.00" clickable="true" '
            'enabled="true" bounds="[35,2226][1045,2341]"/>'
        )
        dynamic_direct = build_page_model(
            dynamic_direct_xml,
            package_name="com.ehaier.zgq.shop.mall",
            activity="com.ehaier.mall.MainActivity",
        )
        self.assertEqual(dynamic_direct.page_subtype, "CASHIER")
        terminal_actions = enumerate_actions(
            dynamic_direct,
            screen_size=(1080, 2400),
            coverage_scheduler_v2=True,
        )
        self.assertEqual(len(terminal_actions), 1)
        self.assertEqual(terminal_actions[0].risk_type, "PAYMENT")
        self.assertTrue(terminal_actions[0].coordinate_only)
        self.assertFalse(terminal_actions[0].replayable)
        self.assertNotIn("2234", repr(terminal_actions[0].target_meta))

        for label in (
            "确认云闪付支付, ￥314.00",
            "确认云闪付支付，￥314.00",
            "确认支付(￥314.00)",
            "确认支付（￥314.00）",
            "确认支付订单",
            "确认支付并离开",
            "立即支付",
            "确认付款",
            "立即付款",
            "支付订单",
            "Pay now",
            "Confirm UnionPay payment, $314.00",
            "Complete payment",
            "Submit payment",
        ):
            with self.subTest(final_payment_label=label):
                xml = _page(
                    '<node class="android.widget.TextView" text="海尔收银台" '
                    'enabled="true" bounds="[100,100][900,220]"/>'
                    '<node class="android.widget.Button" content-desc="确认" '
                    'clickable="true" enabled="true" '
                    'bounds="[100,1500][900,1650]">'
                    f'<node class="android.widget.TextView" text="{label}" '
                    'enabled="true" bounds="[150,1530][850,1620]"/>'
                    "</node>"
                )
                page = build_page_model(
                    xml,
                    package_name="com.ehaier.zgq.shop.mall",
                    activity="com.ehaier.mall.MainActivity",
                )
                action = enumerate_actions(page, screen_size=(1080, 2400))[0]
                self.assertEqual(action.risk_type, "PAYMENT")

        for label in (
            "支付方式",
            "选择支付方式",
            "支付说明",
            "支付协议",
            "确认支付协议",
            "Payment method",
            "Confirm payment method",
        ):
            with self.subTest(non_commit_payment_label=label):
                xml = _page(
                    '<node class="android.widget.TextView" text="海尔收银台" '
                    'enabled="true" bounds="[100,100][900,220]"/>'
                    '<node class="android.widget.Button" content-desc="确认" '
                    'clickable="true" enabled="true" '
                    'bounds="[100,1500][900,1650]">'
                    f'<node class="android.widget.TextView" text="{label}" '
                    'enabled="true" bounds="[150,1530][850,1620]"/>'
                    "</node>"
                )
                page = build_page_model(
                    xml,
                    package_name="com.ehaier.zgq.shop.mall",
                    activity="com.ehaier.mall.MainActivity",
                )
                action = enumerate_actions(page, screen_size=(1080, 2400))[0]
                self.assertIsNone(action.risk_type)

        non_haier_cashier = build_page_model(
            cashier_xml,
            package_name="com.demo",
            activity="com.ehaier.mall.MainActivity",
        )
        action = enumerate_actions(
            non_haier_cashier, screen_size=(1080, 2400)
        )[0]
        self.assertIsNone(action.risk_type)
        self.assertFalse(action.coordinate_only)
        self.assertTrue(action.replayable)

        unanchored_dynamic_payment = build_page_model(
            _page(
                '<node class="android.widget.Button" '
                'text="确认云闪付支付 ￥314.00" clickable="true" '
                'enabled="true" bounds="[100,1500][900,1650]"/>'
            ),
            package_name="com.ehaier.zgq.shop.mall",
            activity="com.ehaier.mall.MainActivity",
        )
        self.assertEqual(
            enumerate_actions(
                unanchored_dynamic_payment,
                screen_size=(1080, 2400),
            ),
            [],
        )

    def test_checkout_immediate_payment_is_a_safe_place_order_transition(self):
        xml = _page(
            '<node class="android.widget.TextView" text="提交订单" '
            'enabled="true" bounds="[420,100][660,220]"/>'
            '<node class="android.view.ViewGroup" '
            'content-desc="支付方式 , 在线支付" clickable="true" '
            'enabled="true" bounds="[70,1900][1010,2050]"/>'
            '<node class="android.view.ViewGroup" content-desc="立即支付" '
            'clickable="true" enabled="true" '
            'bounds="[750,2200][1030,2340]"/>'
        )
        page = build_page_model(
            xml,
            package_name="com.ehaier.zgq.shop.mall",
            activity="com.ehaier.mall.MainActivity",
        )
        self.assertEqual(page.role, "CHECKOUT")

        actions = enumerate_actions(
            page,
            screen_size=(1080, 2400),
        )
        payment_method = next(
            item
            for item in actions
            if item.target_meta.get("content_desc") == "支付方式 , 在线支付"
        )
        place_order = next(
            item
            for item in actions
            if item.target_meta.get("content_desc") == "立即支付"
        )
        self.assertEqual(payment_method.action_role, "COMMAND:PAY")
        self.assertIsNone(payment_method.risk_type)
        self.assertEqual(place_order.action_role, "PLACE_ORDER")
        self.assertIsNone(place_order.risk_type)
        self.assertTrue(place_order.replayable)

        capture = CapturedPage(
            package_name=page.package_name,
            activity=page.activity,
            xml=page.xml,
            screenshot_png=b"png",
            screenshot_sha="sha",
            perceptual_hash="phash",
            model=page,
            stable_by="exact",
        )
        ordered = _state_actions(
            capture,
            screen_size=(1080, 2400),
            safety_rules=[],
            input_rules=[],
            max_scrolls=3,
            depth=3,
            coverage_scheduler=True,
        )
        self.assertEqual(len(ordered), 1)
        self.assertEqual(ordered[0].action_role, "PLACE_ORDER")

    def test_midpage_cashier_marketing_copy_is_not_a_strong_anchor(self):
        xml = _page(
            '<node class="android.widget.TextView" text="商品详情" '
            'enabled="true" bounds="[100,100][900,220]"/>'
            '<node class="android.widget.TextView" text="海尔收银台" '
            'enabled="true" bounds="[100,1000][900,1120]"/>'
            '<node class="android.widget.Button" text="立即支付" '
            'clickable="true" enabled="true" bounds="[100,1500][900,1650]"/>'
        )
        page = build_page_model(
            xml,
            package_name="com.ehaier.zgq.shop.mall",
            activity="com.ehaier.mall.MainActivity",
        )
        payment = enumerate_actions(page, screen_size=(1080, 2400))[0]

        self.assertIsNone(payment.risk_type)
        self.assertTrue(payment.replayable)

    def test_dangerous_page_context_does_not_change_labeled_action_behavior(self):
        xml = _page(
            '<node class="android.widget.TextView" text="删除账号" '
            'enabled="true" bounds="[100,200][900,400]"/>'
            '<node class="android.widget.Button" content-desc="返回" '
            'clickable="true" enabled="true" bounds="[100,500][400,650]"/>'
        )
        action = enumerate_actions(
            build_page_model(xml, package_name="com.demo", activity=".Account"),
            screen_size=(1080, 2400),
        )[0]
        self.assertFalse(action.coordinate_only)
        self.assertIsNone(action.risk_type)

    def test_unlabeled_coordinate_clicks_require_allow_and_are_deduplicated(self):
        xml = _page(
            '<node class="android.view.ViewGroup" enabled="true" '
            'bounds="[0,100][1080,600]">'
            '<node class="android.view.ViewGroup" clickable="true" enabled="true" '
            'bounds="[0,100][1080,600]"/>'
            '<node class="android.view.ViewGroup" clickable="true" enabled="true" '
            'bounds="[0,100][1080,600]"/>'
            '<node class="android.view.ViewGroup" clickable="true" enabled="true" '
            'bounds="[0,100][1080,600]"/>'
            "</node>"
        )
        page = build_page_model(xml, package_name="com.demo", activity=".Carousel")
        self.assertEqual(
            enumerate_actions(page, screen_size=(1080, 2400)),
            [],
        )
        actions = enumerate_actions(
            page,
            screen_size=(1080, 2400),
            safety_rules=[
                {
                    "id": "allow-carousel-coordinate",
                    "pattern": r"^android\.view\.ViewGroup$",
                    "risk_type": "ALLOW",
                    "allow": True,
                }
            ],
        )
        self.assertEqual(len(actions), 1)
        self.assertTrue(actions[0].coordinate_only)
        self.assertTrue(actions[0].target_meta["coordinate_authorized"])

    def test_locator_uniqueness_count(self):
        xml = _page(
            '<node class="android.widget.Button" content-desc="查看" '
            'clickable="true" enabled="true" bounds="[10,10][200,100]"/>'
            '<node class="android.widget.Button" content-desc="查看" '
            'clickable="true" enabled="true" bounds="[10,110][200,200]"/>'
        )
        self.assertEqual(
            locator_match_count(xml, {"selector": "查看", "by": "description"}),
            2,
        )

    def test_replay_never_clicks_first_when_locator_is_ambiguous(self):
        xml = _page(
            '<node class="android.widget.Button" content-desc="查看" '
            'clickable="true" enabled="true" bounds="[10,10][200,100]"/>'
            '<node class="android.widget.Button" content-desc="查看" '
            'clickable="true" enabled="true" bounds="[10,110][200,200]"/>'
        )
        action = InspectionAction(
            action_type="click",
            action_key="ambiguous",
            locator_candidates=[
                {"selector": "查看", "by": "description"},
            ],
            target_meta={},
        )
        device = Mock()
        with self.assertRaises(LocatorAmbiguous):
            perform_action(device, action, current_xml=xml)
        device.assert_not_called()

    def test_bounds_constrained_semantic_click_targets_foreground_duplicate(self):
        xml = _page(
            '<node class="android.view.ViewGroup" content-desc="立即购买" '
            'clickable="true" enabled="true" bounds="[730,2200][1040,2340]"/>'
            '<node class="android.view.ViewGroup" content-desc="立即购买" '
            'clickable="true" enabled="true" bounds="[35,2180][1045,2306]"/>'
        )
        action = InspectionAction(
            action_type="click",
            action_key="foreground-buy",
            locator_candidates=[
                {
                    "selector": "(//node[@content-desc='立即购买' and @bounds='[35,2180][1045,2306]'])[1]",
                    "by": "xpath",
                    "expected_class": "android.view.ViewGroup",
                    "target_description": "立即购买",
                    "bounds": [35, 2180, 1045, 2306],
                    "bounds_constrained": True,
                }
            ],
            target_meta={"screen_size": [1080, 2412]},
        )
        device = Mock()
        device.window_size.return_value = (1080, 2412)

        self.assertEqual(
            perform_action(device, action, current_xml=xml),
            "semantic-bounds",
        )
        device.click.assert_called_once_with(540, 2243)

    def test_bounds_constrained_input_focuses_and_enters_authorized_text(self):
        xml = _page(
            '<node class="android.widget.EditText" clickable="true" '
            'enabled="true" bounds="[311,135][885,184]"/>'
        )
        action = InspectionAction(
            action_type="input",
            action_key="fixed-search-input",
            locator_candidates=[
                {
                    "selector": "(//node[@class='android.widget.EditText' and @bounds='[311,135][885,184]'])[1]",
                    "by": "xpath",
                    "expected_class": "android.widget.EditText",
                    "bounds": [311, 135, 885, 184],
                    "bounds_constrained": True,
                }
            ],
            target_meta={"screen_size": [1080, 2412]},
        )
        device = Mock()
        device.window_size.return_value = (1080, 2412)

        self.assertEqual(
            perform_action(
                device,
                action,
                current_xml=xml,
                input_value="冰箱",
            ),
            "semantic-bounds",
        )
        device.click.assert_called_once_with(598, 159)
        device.clear_text.assert_called_once_with()
        device.send_keys.assert_called_once_with("冰箱", clear=True)

    def test_coordinate_click_requires_discovery_grant_and_scales_once(self):
        action = InspectionAction(
            action_type="click",
            action_key="coordinate-control",
            locator_candidates=[],
            target_meta={
                "bounds": [100, 200, 300, 600],
                "screen_size": [1000, 2000],
            },
            coordinate_only=True,
            replayable=False,
        )
        device = Mock()
        device.window_size.return_value = (500, 1000)

        with self.assertRaises(LocatorAmbiguous):
            perform_action(device, action, current_xml=_page(""))
        device.click.assert_not_called()

        used = perform_action(
            device,
            action,
            current_xml=_page(""),
            allow_coordinate_discovery=True,
        )

        self.assertEqual(used, "coordinate")
        device.click.assert_called_once_with(100, 200)

    def test_coordinate_click_rejects_offscreen_center_instead_of_clamping(self):
        action = InspectionAction(
            action_type="click",
            action_key="offscreen-coordinate",
            locator_candidates=[],
            target_meta={
                "bounds": [100, 2200, 300, 2600],
                "screen_size": [1000, 2000],
            },
            coordinate_only=True,
            replayable=False,
        )
        device = Mock()
        device.window_size.return_value = (500, 1000)

        with self.assertRaises(LocatorAmbiguous):
            perform_action(
                device,
                action,
                current_xml=_page(""),
                allow_coordinate_discovery=True,
            )
        device.click.assert_not_called()

    def test_dangerous_coordinate_click_is_blocked_even_during_discovery(self):
        action = InspectionAction(
            action_type="click",
            action_key="dangerous-coordinate",
            locator_candidates=[],
            target_meta={
                "bounds": [100, 200, 300, 600],
                "screen_size": [1000, 2000],
            },
            coordinate_only=True,
            replayable=False,
            risk_type="DESTRUCTIVE",
            blocked_reason="危险操作",
        )
        device = Mock()

        with self.assertRaises(PermissionError):
            perform_action(
                device,
                action,
                current_xml=_page(""),
                allow_coordinate_discovery=True,
            )

        device.click.assert_not_called()

    def test_runtime_not_found_errors_are_reported_as_locator_drift(self):
        from uiautomator2.exceptions import (
            UiObjectNotFoundError,
            XPathElementNotFoundError,
        )

        text_xml = _page(
            '<node class="android.widget.TextView" text="商品详情" '
            'clickable="true" enabled="true" bounds="[10,10][300,100]"/>'
        )
        text_action = enumerate_actions(
            build_page_model(
                text_xml,
                package_name="com.demo",
                activity=".Detail",
            ),
            screen_size=(1080, 2400),
        )[0]
        text_target = Mock()
        text_target.click.side_effect = UiObjectNotFoundError(
            {
                "code": -32002,
                "data": "Selector [text='商品详情']",
                "method": "wait",
            }
        )
        text_device = Mock(return_value=text_target)
        with self.assertRaises(LocatorDrift):
            perform_action(
                text_device,
                text_action,
                current_xml=text_xml,
            )
        text_target.click.assert_called_once_with(timeout=0.5)

        xpath_xml = _page(
            '<node class="android.view.ViewGroup" content-desc="首页" '
            'clickable="true" enabled="true" bounds="[10,10][300,100]"/>'
            '<node class="android.view.ViewGroup" content-desc="首页" '
            'clickable="true" enabled="true" bounds="[10,110][300,200]"/>'
        )
        xpath_action = enumerate_actions(
            build_page_model(
                xpath_xml,
                package_name="com.demo",
                activity=".Main",
            ),
            screen_size=(1080, 2400),
        )[0]
        xpath_target = Mock()
        xpath_target.click.side_effect = XPathElementNotFoundError(
            xpath_action.locator_candidates[0]["selector"]
        )
        xpath_device = Mock()
        xpath_device.xpath.return_value = xpath_target
        with self.assertRaises(LocatorDrift):
            perform_action(
                xpath_device,
                xpath_action,
                current_xml=xpath_xml,
            )
        xpath_target.click.assert_called_once_with(timeout=0.5)

    def test_zero_stability_timeout_captures_once_for_locator_rebinding(self):
        device = Mock()
        device.dump_hierarchy.return_value = _page(
            '<node class="android.widget.Button" text="刷新后动作" '
            'clickable="true" enabled="true" bounds="[10,10][300,100]"/>'
        )
        with patch(
            "backend.inspection.device.capture_quick",
            return_value=(
                "com.demo",
                ".Main",
                b"png",
                "fresh-sha",
                "0" * 16,
            ),
        ) as capture_quick:
            captured = wait_for_stable_page(
                device,
                expected_package="com.demo",
                abort_event=threading.Event(),
                max_wait_seconds=0.0,
            )

        capture_quick.assert_called_once_with(device)
        device.dump_hierarchy.assert_called_once_with(compressed=False)
        self.assertEqual(captured.screenshot_sha, "fresh-sha")
        self.assertEqual(captured.stable_by, "timeout")

    def test_ui_unresponsive_requires_independent_health_probe_failure(self):
        healthy = Mock()
        healthy.app_current.return_value = {
            "package": "com.demo",
            "activity": ".Main",
        }
        healthy.dump_hierarchy.return_value = _page("")
        self.assertTrue(
            _probe_ui_automation_responsive(
                healthy,
                abort_event=threading.Event(),
            )
        )

        unhealthy = Mock()
        unhealthy.app_current.side_effect = RuntimeError("rpc unavailable")
        self.assertFalse(
            _probe_ui_automation_responsive(
                unhealthy,
                abort_event=threading.Event(),
            )
        )
        self.assertEqual(unhealthy.app_current.call_count, 2)

    def test_locator_drift_is_warning_without_hard_fault(self):
        test_engine = create_engine(
            "sqlite://",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        SQLModel.metadata.create_all(test_engine)
        with Session(test_engine) as session:
            run = InspectionRun(
                name="locator drift",
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
            run_id = run.id
            branch_id = branch.id

        xml = _page(
            '<node class="android.widget.Button" content-desc="商品详情" '
            'clickable="true" enabled="true" bounds="[10,10][300,100]"/>'
        )
        model = build_page_model(
            xml,
            package_name="com.demo",
            activity=".Main",
        )
        action = enumerate_actions(model, screen_size=(1080, 2400))[0]
        capture = CapturedPage(
            package_name="com.demo",
            activity=".Main",
            xml=xml,
            screenshot_png=b"root",
            screenshot_sha="root-sha",
            perceptual_hash="root-phash",
            model=model,
            stable_by="exact",
        )
        work = StateWork(
            state_id=301,
            state_key=model.state_key,
            cluster_key=model.cluster_key,
            replay_key=model.replay_key,
            package_name="com.demo",
            activity=".Main",
            screenshot_sha="root-sha",
            depth=0,
            path=[],
            actions=[action],
        )
        device = Mock()
        device.window_size.return_value = (1080, 2400)

        with patch(
            "backend.inspection.engine.engine",
            test_engine,
        ), patch(
            "backend.inspection.engine._prepare_branch",
        ), patch(
            "backend.inspection.engine.wait_for_stable_page",
            return_value=capture,
        ), patch(
            "backend.inspection.engine._ensure_parent",
            return_value=capture,
        ), patch(
            "backend.inspection.engine._persist_state",
            return_value=PersistedState(work=work, is_new=True),
        ), patch(
            "backend.inspection.engine.perform_action",
            side_effect=LocatorDrift("locator disappeared"),
        ), patch(
            "backend.inspection.engine._persist_fault",
        ) as persist_fault, patch(
            "backend.inspection.engine._verify_stable_paths",
            return_value=1,
        ):
            outcome = _execute_branch(
                run_id=run_id,
                branch_run_id=branch_id,
                device=device,
                device_serial="android-1",
                package_name="com.demo",
                profile={
                    "budgets": {
                        "duration_seconds": 30,
                        "max_states": 10,
                        "max_actions": 10,
                        "max_depth": 3,
                    }
                },
                branch_config={},
                abort_event=threading.Event(),
                monitor=None,
            )

        self.assertEqual(outcome.status, "WARNING")
        self.assertFalse(outcome.hard_fault)
        persist_fault.assert_not_called()
        with Session(test_engine) as session:
            transition = session.exec(
                select(InspectionTransition).where(
                    InspectionTransition.run_id == run_id
                )
            ).one()
            self.assertEqual(transition.status, "LOCATOR_NOT_FOUND")
        test_engine.dispose()

    def test_xpath_ordinal_drift_is_not_uniquely_resolvable(self):
        xml = _page(
            '<node class="android.widget.Button" content-desc="查看" '
            'clickable="true" enabled="true" bounds="[10,10][200,100]"/>'
        )
        candidate = {
            "selector": "(//android.widget.Button[@content-desc='查看'])[2]",
            "by": "xpath",
            "expected_class": "android.widget.Button",
            "target_description": "查看",
            "ordinal": 2,
        }
        self.assertEqual(locator_match_count(xml, candidate), 0)

    def test_coordinate_scroll_is_discovery_only_and_scales_to_device(self):
        xml = _page(
            '<node class="androidx.recyclerview.widget.RecyclerView" '
            'scrollable="true" enabled="true" bounds="[0,200][1000,2000]">'
            '<node class="android.widget.TextView" content-desc="列表首项" '
            'enabled="true" bounds="[10,300][500,400]"/>'
            "</node>"
        )
        page = build_page_model(xml, package_name="com.demo", activity=".List")
        actions = enumerate_actions(page, screen_size=(1080, 2400))
        up = next(
            item
            for item in actions
            if item.action_type == "scroll"
            and item.target_meta.get("direction") == "up"
        )
        self.assertTrue(up.coordinate_only)
        self.assertFalse(up.replayable)
        self.assertEqual(up.locator_candidates, [])
        self.assertEqual(up.target_meta["content_desc"], "")

        device = Mock()
        device.window_size.return_value = (540, 1200)
        used = perform_action(device, up, current_xml=xml)
        self.assertEqual(used, "scroll:up:coordinate")
        device.swipe.assert_called_once_with(250, 775, 250, 325, 0.25)

    def test_scroll_limit_counts_only_current_consecutive_chain(self):
        action = InspectionAction(
            action_type="scroll",
            action_key="list-up",
            locator_candidates=[],
            target_meta={"direction": "up"},
            coordinate_only=True,
            replayable=False,
        )
        serialized = {
            "action_type": "scroll",
            "action_key": "list-up",
        }
        self.assertEqual(
            _consecutive_scroll_repetitions(
                [serialized, serialized, serialized],
                action,
            ),
            3,
        )
        self.assertEqual(
            _consecutive_scroll_repetitions(
                [
                    serialized,
                    {"action_type": "click", "action_key": "open"},
                ],
                action,
            ),
            0,
        )

    def test_unmapped_input_is_blocked_and_password_requires_explicit_rule(self):
        xml = _page(
            '<node class="android.widget.EditText" content-desc="Password" '
            'password="true" clickable="true" enabled="true" '
            'bounds="[10,10][400,100]"/>'
        )
        page = build_page_model(xml, package_name="com.demo", activity=".Login")
        unmapped = enumerate_actions(page, screen_size=(1080, 2400))[0]
        self.assertEqual(unmapped.risk_type, "UNMAPPED_INPUT")
        self.assertFalse(unmapped.replayable)

        mapped = enumerate_actions(
            page,
            screen_size=(1080, 2400),
            input_rules=[
                {
                    "id": "password-secret",
                    "content_desc_regex": "^Password$",
                    "value_source": "environment",
                    "variable_key": "LOGIN_PASSWORD",
                    "allow_sensitive": True,
                }
            ],
        )[0]
        self.assertIsNone(mapped.risk_type)
        self.assertEqual(mapped.input_rule_id, "password-secret")
        self.assertEqual(mapped.input_variable_key, "LOGIN_PASSWORD")

    def test_system_permission_surface_is_blocked_unless_explicitly_allowed(self):
        xml = _page(
            '<node package="com.android.permissioncontroller" '
            'class="android.widget.Button" content-desc="仅在使用时允许" '
            'clickable="true" enabled="true" bounds="[10,10][400,100]"/>'
        )
        page = build_page_model(
            xml,
            package_name="com.demo",
            activity=".Permission",
        )
        blocked = enumerate_actions(page, screen_size=(1080, 2400))[0]
        self.assertEqual(blocked.risk_type, "SYSTEM_OR_EXTERNAL")

        allowed = enumerate_actions(
            page,
            screen_size=(1080, 2400),
            safety_rules=[
                {
                    "id": "allow-camera",
                    "pattern": "仅在使用时允许",
                    "risk_type": "ALLOW",
                    "allow": True,
                }
            ],
        )[0]
        self.assertIsNone(allowed.risk_type)

    def test_same_length_path_prefers_more_description_locators(self):
        description_path = [
            {
                "action_type": "click",
                "locator_candidates": [
                    {"selector": "购物车", "by": "description"}
                ],
            },
            {
                "action_type": "click",
                "locator_candidates": [
                    {"selector": "结算", "by": "description"}
                ],
            },
        ]
        text_path = [
            {
                "action_type": "click",
                "locator_candidates": [{"selector": "购物车", "by": "text"}],
            },
            {
                "action_type": "click",
                "locator_candidates": [{"selector": "结算", "by": "text"}],
            },
        ]
        self.assertLess(_path_score(description_path), _path_score(text_path))

    def test_secret_values_are_preloaded_for_redaction_but_not_in_transition(self):
        test_engine = create_engine(
            "sqlite://",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        SQLModel.metadata.create_all(test_engine)
        with Session(test_engine) as session:
            environment = Environment(name="inspection secret env")
            session.add(environment)
            session.flush()
            session.add(
                GlobalVariable(
                    env_id=environment.id,
                    key="LOGIN_PASSWORD",
                    value="real-secret-value",
                    is_secret=True,
                )
            )
            session.add(
                GlobalVariable(
                    env_id=environment.id,
                    key="PUBLIC_VALUE",
                    value="public-value",
                    is_secret=False,
                )
            )
            session.commit()
            environment_id = environment.id

        with patch("backend.inspection.engine.engine", test_engine):
            secrets = _environment_secret_values(environment_id)
        self.assertEqual(secrets, ["real-secret-value"])
        self.assertEqual(
            _redact("failure: real-secret-value", secrets),
            "failure: ***",
        )

        action = InspectionAction(
            action_type="input",
            action_key="password",
            locator_candidates=[
                {"selector": "Password", "by": "description"}
            ],
            target_meta={"password": True},
            input_rule_id="password-rule",
            input_variable_key="LOGIN_PASSWORD",
        )
        payload = _transition_payload(
            from_state_id=1,
            sequence=1,
            action=action,
            status="PASS",
            input_length=len("real-secret-value"),
        )
        serialized = repr(payload)
        self.assertNotIn("real-secret-value", serialized)
        self.assertEqual(payload["input_variable_key"], "LOGIN_PASSWORD")
        self.assertEqual(payload["input_length"], len("real-secret-value"))
        test_engine.dispose()

    def test_product_family_ignores_instance_copy_and_coverage_variants(self):
        def product_page(title: str, primary_action: str, extra_action: str) -> object:
            return build_page_model(
                _page(
                    '<node class="android.widget.Button" content-desc="返回" '
                    'clickable="true" enabled="true" bounds="[10,40][120,140]"/>'
                    f'<node class="android.widget.TextView" text="{title}" '
                    'enabled="true" bounds="[60,300][1000,430]"/>'
                    '<node class="android.widget.Button" content-desc="购物车" '
                    'clickable="true" enabled="true" bounds="[40,2050][220,2180]"/>'
                    f'<node class="android.widget.Button" text="{primary_action}" '
                    'clickable="true" enabled="true" bounds="[650,2050][1040,2180]"/>'
                    f'<node class="android.widget.Button" text="{extra_action}" '
                    'clickable="true" enabled="true" bounds="[50,900][1000,1020]"/>'
                    '<node class="android.widget.ScrollView" scrollable="true" '
                    'enabled="true" bounds="[0,180][1080,2000]"/>'
                ),
                package_name="com.ehaier.zgq.shop.mall",
                activity="com.ehaier.mall.MainActivity",
            )

        available = product_page("海尔冰箱 336L", "立即购买", "一级变频 336L")
        coverage_variant = product_page("海尔洗衣机 10kg", "立即购买", "删除")

        self.assertEqual(available.role, "PRODUCT_DETAIL")
        self.assertEqual(coverage_variant.role, "PRODUCT_DETAIL")
        similarity = compare_exploration_families(available, coverage_variant)
        self.assertTrue(similarity.equivalent, similarity.to_dict())
        self.assertFalse(similarity.evidence["risk_match"])
        self.assertTrue(similarity.evidence["risk_is_coverage_variant"])
        self.assertEqual(
            exploration_family_signature(available)["family_key"],
            exploration_family_signature(coverage_variant)["family_key"],
        )
        available_actions = enumerate_actions(
            available,
            screen_size=(1080, 2400),
        )
        instance_action = next(
            item
            for item in available_actions
            if item.target_meta.get("text") == "一级变频 336L"
        )
        self.assertTrue(instance_action.action_role.startswith("INSTANCE:"))

        coverage_actions = enumerate_actions(
            available,
            screen_size=(1080, 2400),
            coverage_scheduler_v2=True,
        )
        self.assertFalse(
            any(item.action_role.startswith("INSTANCE:") for item in coverage_actions)
        )
        capture = CapturedPage(
            package_name=available.package_name,
            activity=available.activity,
            xml=available.xml,
            screenshot_png=b"png",
            screenshot_sha="sha",
            perceptual_hash="phash",
            model=available,
            stable_by="exact",
        )
        ordered = _state_actions(
            capture,
            screen_size=(1080, 2400),
            safety_rules=[],
            input_rules=[],
            max_scrolls=3,
            depth=2,
            coverage_scheduler=True,
        )
        self.assertEqual(ordered[0].action_role, "BUY_NOW")
        self.assertEqual(ordered[-1].action_role, "BACK")

    def test_service_product_detail_is_separate_from_goods_detail(self):
        def detail_page(*labels: str, option: str) -> object:
            copy = "".join(
                f'<node class="android.widget.TextView" text="{label}" '
                'enabled="true" bounds="[60,300][1020,430]"/>'
                for label in labels
            )
            return build_page_model(
                _page(
                    copy
                    + f'<node class="android.view.ViewGroup" content-desc="已选, {option}, 1件" '
                    'clickable="true" enabled="true" bounds="[35,1750][1045,1840]"/>'
                    '<node class="android.view.ViewGroup" content-desc="加入购物车" '
                    'clickable="true" enabled="true" bounds="[390,2050][707,2180]"/>'
                    '<node class="android.view.ViewGroup" content-desc="立即购买" '
                    'clickable="true" enabled="true" bounds="[731,2050][1048,2180]"/>'
                    '<node class="android.widget.ScrollView" scrollable="true" '
                    'enabled="true" bounds="[0,180][1080,2000]"/>'
                ),
                package_name="com.ehaier.zgq.shop.mall",
                activity="com.ehaier.mall.MainActivity",
            )

        service_variants = (
            detail_page(
                "家生活服务 热水器深度清洗",
                "Haier/海尔，热水器深度清洗",
                "本服务仅限清洗电热水器产品",
                option="热水器深度清洗",
            ),
            detail_page(
                "海尔服务",
                "空调上门服务",
                "服务流程",
                option="空调上门服务",
            ),
            detail_page(
                "海尔服务",
                "整机延保服务",
                "服务须知",
                option="整机延保服务",
            ),
        )
        goods = detail_page(
            "海尔洗衣机 10kg",
            "深度清洗程序，上门服务收费50元，延保权益以订单为准",
            "普通商品描述，不是独立服务商品",
            option="白色",
        )

        for service in service_variants:
            with self.subTest(service=service.semantic_key):
                self.assertEqual(service.role, "PRODUCT_DETAIL")
                self.assertEqual(service.page_subtype, "SERVICE_DETAIL")
        self.assertEqual(goods.role, "PRODUCT_DETAIL")
        self.assertEqual(goods.page_subtype, "PRODUCT_DETAIL")

        similarity = compare_exploration_families(service_variants[0], goods)
        self.assertFalse(similarity.equivalent)
        self.assertFalse(similarity.evidence["page_subtype_match"])
        self.assertNotEqual(
            exploration_family_signature(service_variants[0])["family_key"],
            exploration_family_signature(goods)["family_key"],
        )

    def test_product_family_preserves_distinct_transaction_capabilities(self):
        def product_page(*actions: str) -> object:
            action_nodes = "".join(
                f'<node class="android.view.ViewGroup" content-desc="{label}" '
                'clickable="true" enabled="true" bounds="[390,2050][1048,2180]"/>'
                for label in actions
            )
            return build_page_model(
                _page(
                    '<node class="android.widget.TextView" text="海尔商品详情" '
                    'enabled="true" bounds="[60,300][1020,430]"/>'
                    + action_nodes
                    + '<node class="android.widget.ScrollView" scrollable="true" '
                    'enabled="true" bounds="[0,180][1080,2000]"/>'
                ),
                package_name="com.ehaier.zgq.shop.mall",
                activity="com.ehaier.mall.MainActivity",
            )

        pages = {
            "buy_and_add": product_page("加入购物车", "立即购买"),
            "add_only": product_page("加入购物车"),
            "arrival": product_page("到货通知"),
        }
        signatures = {
            name: exploration_family_signature(page)
            for name, page in pages.items()
        }

        self.assertEqual(
            signatures["buy_and_add"]["transaction_capability_roles"],
            ["ADD_CART", "BUY_NOW"],
        )
        self.assertEqual(
            signatures["add_only"]["transaction_capability_roles"],
            ["ADD_CART"],
        )
        self.assertEqual(
            signatures["arrival"]["transaction_capability_roles"],
            ["ARRIVAL_NOTICE"],
        )
        self.assertEqual(
            len({signature["family_key"] for signature in signatures.values()}),
            3,
        )
        for left_name, right_name in (
            ("buy_and_add", "add_only"),
            ("buy_and_add", "arrival"),
            ("add_only", "arrival"),
        ):
            with self.subTest(left=left_name, right=right_name):
                self.assertFalse(
                    compare_exploration_families(
                        pages[left_name], pages[right_name]
                    ).equivalent
                )

    def test_product_family_preserves_option_select_capability(self):
        def product_page(*, with_option: bool) -> object:
            option = (
                '<node class="android.view.ViewGroup" '
                'content-desc="已选, 白色, 1件" clickable="true" '
                'enabled="true" bounds="[35,1750][1045,1840]"/>'
                if with_option
                else ""
            )
            return build_page_model(
                _page(
                    '<node class="android.widget.TextView" text="海尔商品详情" '
                    'enabled="true" bounds="[60,300][1020,430]"/>'
                    + option
                    + '<node class="android.view.ViewGroup" content-desc="加入购物车" '
                    'clickable="true" enabled="true" bounds="[390,2050][707,2180]"/>'
                    '<node class="android.widget.ScrollView" scrollable="true" '
                    'enabled="true" bounds="[0,180][1080,2000]"/>'
                ),
                package_name="com.ehaier.zgq.shop.mall",
                activity="com.ehaier.mall.MainActivity",
            )

        configurable = product_page(with_option=True)
        fixed = product_page(with_option=False)
        configurable_signature = exploration_family_signature(configurable)
        fixed_signature = exploration_family_signature(fixed)

        self.assertIn(
            "OPTION_SELECT",
            [
                action.action_role
                for action in enumerate_actions(
                    configurable,
                    screen_size=(1080, 2400),
                )
            ],
        )
        self.assertEqual(
            configurable_signature["transaction_capability_roles"],
            ["ADD_CART", "OPTION_SELECT"],
        )
        self.assertEqual(
            fixed_signature["transaction_capability_roles"],
            ["ADD_CART"],
        )
        self.assertNotEqual(
            configurable_signature["family_key"], fixed_signature["family_key"]
        )
        self.assertFalse(
            compare_exploration_families(configurable, fixed).equivalent
        )

    def test_product_family_preserves_core_action_parent_structure(self):
        def product_page(*, actions_in_collection: bool):
            action_nodes = (
                '<node class="android.view.ViewGroup" content-desc="购物车" '
                'clickable="true" enabled="true" bounds="[40,2050][300,2180]"/>'
                '<node class="android.view.ViewGroup" text="立即购买" '
                'clickable="true" enabled="true" bounds="[650,2050][1040,2180]"/>'
            )
            if actions_in_collection:
                body = (
                    '<node class="android.widget.LinearLayout" enabled="true" '
                    'bounds="[0,200][1080,400]"/>'
                    '<node class="androidx.recyclerview.widget.RecyclerView" '
                    'scrollable="true" enabled="true" bounds="[0,400][1080,2200]">'
                    + action_nodes
                    + '</node>'
                )
            else:
                body = (
                    '<node class="android.widget.LinearLayout" enabled="true" '
                    'bounds="[0,1900][1080,2200]">'
                    + action_nodes
                    + '</node>'
                    '<node class="androidx.recyclerview.widget.RecyclerView" '
                    'scrollable="true" enabled="true" bounds="[0,400][1080,1800]"/>'
                )
            return build_page_model(
                _page(body),
                package_name="com.ehaier.zgq.shop.mall",
                activity="com.ehaier.mall.MainActivity",
            )

        toolbar_shell = product_page(actions_in_collection=False)
        collection_shell = product_page(actions_in_collection=True)
        similarity = compare_exploration_families(toolbar_shell, collection_shell)

        self.assertEqual(toolbar_shell.role, "PRODUCT_DETAIL")
        self.assertEqual(collection_shell.role, "PRODUCT_DETAIL")
        self.assertFalse(similarity.equivalent)
        self.assertLess(similarity.evidence["structure_similarity"], 0.94)

    def test_list_card_locator_skips_decorative_badge(self):
        page = build_page_model(
            _page(
                '<node class="android.widget.TextView" text="分类" '
                'enabled="true" bounds="[400,20][680,110]"/>'
                '<node class="android.view.ViewGroup" clickable="true" '
                'enabled="true" bounds="[0,460][1080,1050]">'
                '<node class="android.widget.TextView" text="自营" '
                'enabled="true" bounds="[400,500][500,560]"/>'
                '<node class="android.widget.TextView" text="海尔冰箱 336L" '
                'enabled="true" bounds="[400,580][950,680]"/>'
                "</node>"
            ),
            package_name="com.ehaier.zgq.shop.mall",
            activity="com.ehaier.mall.MainActivity",
        )
        action = enumerate_actions(page, screen_size=(1080, 2400))[0]
        self.assertEqual(action.action_role, "ITEM_OPEN:collection")
        self.assertEqual(action.target_meta.get("text"), "海尔冰箱 336L")
        self.assertNotIn("自营", repr(action.locator_candidates))

    def test_role_inference_ignores_checkout_copy_and_detects_permission_dialog(self):
        product = build_page_model(
            _page(
                '<node class="android.widget.TextView" '
                'text="公告：如需服务可在提交订单时勾选，确认型号后再下单。'
                '这是一段商品说明而不是结算页标题，且不应参与页面角色判定。" '
                'enabled="true" bounds="[40,900][1040,1350]"/>'
                '<node class="android.widget.Button" text="到货通知" '
                'clickable="true" enabled="true" bounds="[600,2050][1040,2180]"/>'
            ),
            package_name="com.ehaier.zgq.shop.mall",
            activity="com.ehaier.mall.MainActivity",
        )
        permission = build_page_model(
            _page(
                '<node class="android.widget.TextView" text="海尔商城申请定位权限" '
                'enabled="true" bounds="[140,700][940,850]"/>'
                '<node class="android.widget.Button" text="暂不开启" '
                'clickable="true" enabled="true" bounds="[160,1350][500,1500]"/>'
                '<node class="android.widget.Button" text="去开启定位" '
                'clickable="true" enabled="true" bounds="[540,1350][900,1500]"/>'
            ),
            package_name="com.ehaier.zgq.shop.mall",
            activity="com.ehaier.mall.MainActivity",
        )
        self.assertEqual(product.role, "PRODUCT_DETAIL")
        self.assertEqual(permission.role, "DIALOG")
        self.assertEqual(permission.page_subtype, "MODAL_PANEL")
        permission_actions = enumerate_actions(
            permission,
            screen_size=(1080, 2400),
            coverage_scheduler_v2=True,
        )
        self.assertEqual(
            [action.action_role for action in permission_actions],
            ["DIALOG_CLOSE"],
        )

    def test_horizontal_message_tabs_are_one_sampled_action_group(self):
        labels = ("全部", "物流", "提醒, 8", "优惠, 21", "互动", "其他")
        tabs = "".join(
            '<node class="android.view.ViewGroup" '
            f'content-desc="{label}" clickable="true" enabled="true" '
            f'bounds="[{index * 180},269][{index * 180 + 138},482]"/>'
            for index, label in enumerate(labels)
        )
        page = build_page_model(
            _page(
                '<node class="android.widget.HorizontalScrollView" '
                'scrollable="true" enabled="true" '
                'bounds="[0,223][1080,517]">'
                '<node class="android.view.ViewGroup" enabled="true" '
                f'bounds="[0,223][1080,517]">{tabs}</node></node>'
            ),
            package_name="com.ehaier.zgq.shop.mall",
            activity="com.ehaier.mall.MainActivity",
        )
        actions = enumerate_actions(
            page,
            screen_size=(1080, 2412),
            coverage_scheduler_v2=True,
        )
        tab_actions = [
            action
            for action in actions
            if action.action_role == "CATEGORY_TAB:top"
        ]

        self.assertEqual(len(tab_actions), len(labels))
        self.assertEqual(
            {action.sample_policy for action in tab_actions},
            {"FAMILY_ONE"},
        )
        self.assertEqual(
            len({action.action_group_key for action in tab_actions}),
            1,
        )

    def test_repeated_checkout_words_in_body_copy_do_not_define_page_role(self):
        product = build_page_model(
            _page(
                '<node class="android.widget.TextView" text="商品详情" '
                'enabled="true" bounds="[40,40][1040,160]"/>'
                '<node class="android.widget.TextView" '
                'text="提交订单时可以选择配送服务" enabled="true" '
                'bounds="[40,900][1040,1080]"/>'
                '<node class="android.widget.TextView" '
                'text="确认订单前请再次核对商品型号" enabled="true" '
                'bounds="[40,1120][1040,1300]"/>'
                '<node class="android.widget.Button" text="立即购买" '
                'clickable="true" enabled="true" bounds="[600,2050][1040,2180]"/>'
            ),
            package_name="com.ehaier.zgq.shop.mall",
            activity="com.ehaier.mall.MainActivity",
        )

        self.assertEqual(product.role, "PRODUCT_DETAIL")

    def test_fixed_navigation_filters_occluded_scroll_children_and_active_home(self):
        xml = _page(
            '<node class="android.widget.ScrollView" scrollable="true" '
            'enabled="true" bounds="[0,0][1080,2200]">'
            '<node class="android.view.ViewGroup" clickable="true" enabled="true" '
            'bounds="[50,0][430,330]">'
            '<node class="android.widget.TextView" text="冰箱延保" enabled="true" '
            'bounds="[70,80][410,180]"/>'
            "</node>"
            '<node class="android.view.ViewGroup" clickable="true" enabled="true" '
            'bounds="[50,420][1030,720]">'
            '<node class="android.widget.TextView" text="可见活动" enabled="true" '
            'bounds="[80,470][500,560]"/>'
            "</node>"
            "</node>"
            '<node class="android.view.ViewGroup" enabled="true" '
            'bounds="[0,0][1080,360]">'
            '<node class="android.view.ViewGroup" content-desc="首页" '
            'clickable="true" enabled="true" bounds="[300,100][540,220]"/>'
            '<node class="android.view.ViewGroup" content-desc="附近门店" '
            'clickable="true" enabled="true" bounds="[540,100][780,220]"/>'
            '<node class="android.view.View" enabled="true" '
            'bounds="[340,208][500,216]"/>'
            "</node>"
            '<node class="android.widget.Button" text="签到" clickable="true" '
            'enabled="true" bounds="[850,110][1040,210]"/>'
        )
        page = build_page_model(
            xml,
            package_name="com.ehaier.zgq.shop.mall",
            activity="com.ehaier.mall.MainActivity",
        )

        actions = enumerate_actions(page, screen_size=(1080, 2400))
        labels = {
            action.target_meta.get("content_desc") or action.target_meta.get("text")
            for action in actions
        }
        self.assertNotIn("冰箱延保", labels)
        self.assertNotIn("首页", labels)
        self.assertIn("可见活动", labels)
        self.assertIn("附近门店", labels)
        self.assertIn("签到", labels)
        recovery_navigation = enumerate_actions(
            page,
            screen_size=(1080, 2400),
            include_current_navigation=True,
        )
        current_home = next(
            action
            for action in recovery_navigation
            if action.target_meta.get("content_desc") == "首页"
        )
        self.assertTrue(current_home.target_meta["navigation"]["member"]["active"])
        self.assertEqual(
            locator_match_count(
                xml,
                {
                    "by": "text",
                    "selector": "冰箱延保",
                    "expected_class": "android.widget.TextView",
                },
            ),
            0,
        )

    def test_overlapping_scrolls_collapse_and_horizontal_swipes_left_right(self):
        xml = _page(
            '<node class="androidx.recyclerview.widget.RecyclerView" '
            'scrollable="true" enabled="true" bounds="[0,250][1080,2200]">'
            '<node class="android.widget.ScrollView" scrollable="true" '
            'enabled="true" bounds="[0,250][1080,2150]">'
            '<node class="android.widget.HorizontalScrollView" scrollable="true" '
            'enabled="true" bounds="[100,600][980,900]"/>'
            "</node>"
            "</node>"
        )
        page = build_page_model(xml, package_name="com.demo", activity=".Main")
        scrolls = [
            action
            for action in enumerate_actions(page, screen_size=(1080, 2400))
            if action.action_type == "scroll"
        ]

        vertical = [
            action
            for action in scrolls
            if action.action_role and action.action_role.startswith("SCROLL:vertical:")
        ]
        horizontal = [
            action
            for action in scrolls
            if action.action_role and action.action_role.startswith("SCROLL:horizontal:")
        ]
        self.assertEqual({item.target_meta["direction"] for item in vertical}, {"up", "down"})
        self.assertEqual(
            {item.target_meta["class"] for item in vertical},
            {"android.widget.ScrollView"},
        )
        self.assertEqual(
            {item.target_meta["direction"] for item in horizontal},
            {"left", "right"},
        )

        left = next(item for item in horizontal if item.target_meta["direction"] == "left")
        right = next(item for item in horizontal if item.target_meta["direction"] == "right")
        device = Mock()
        device.window_size.return_value = (1080, 2400)
        self.assertEqual(perform_action(device, left, current_xml=xml), "scroll:left:coordinate")
        device.swipe.assert_called_once_with(760, 750, 320, 750, 0.25)
        device.reset_mock()
        device.window_size.return_value = (1080, 2400)
        self.assertEqual(perform_action(device, right, current_xml=xml), "scroll:right:coordinate")
        device.swipe.assert_called_once_with(320, 750, 760, 750, 0.25)

    def test_install_copy_does_not_block_product_cards_or_scrolls(self):
        xml = _page(
            '<node class="android.widget.TextView" text="分类" enabled="true" '
            'bounds="[400,20][680,110]"/>'
            '<node class="android.widget.ScrollView" scrollable="true" '
            'enabled="true" bounds="[0,200][1080,2200]">'
            '<node class="android.view.ViewGroup" clickable="true" enabled="true" '
            'bounds="[20,400][1060,900]">'
            '<node class="android.widget.TextView" '
            'text="净肤洗 | 小尺寸 随心安装 | 水质更健康" enabled="true" '
            'bounds="[60,500][1020,650]"/>'
            "</node>"
            "</node>"
            '<node class="android.widget.Button" text="家电安装" '
            'clickable="true" enabled="true" bounds="[50,2250][450,2350]"/>'
            '<node class="android.widget.Button" text="安装应用" '
            'clickable="true" enabled="true" bounds="[550,2250][1030,2350]"/>'
        )
        page = build_page_model(xml, package_name="com.demo", activity=".Category")
        actions = enumerate_actions(page, screen_size=(1080, 2400))
        product = next(
            item for item in actions if "随心安装" in str(item.target_meta.get("text"))
        )
        service = next(item for item in actions if item.target_meta.get("text") == "家电安装")
        install_app = next(item for item in actions if item.target_meta.get("text") == "安装应用")
        scrolls = [item for item in actions if item.action_type == "scroll"]

        self.assertIsNone(product.risk_type)
        self.assertIsNone(service.risk_type)
        self.assertTrue(scrolls)
        self.assertTrue(all(item.risk_type is None for item in scrolls))
        self.assertEqual(install_app.risk_type, "SYSTEM_OR_EXTERNAL")

    def test_family_action_keys_are_viewport_stable_but_keep_control_state(self):
        def list_page(*, label: str, top: int):
            return build_page_model(
                _page(
                    '<node class="android.widget.TextView" text="分类" enabled="true" '
                    'bounds="[400,20][680,110]"/>'
                    '<node class="android.view.ViewGroup" clickable="true" enabled="true" '
                    f'bounds="[0,{top}][1080,{top + 320}]">'
                    f'<node class="android.widget.TextView" text="{label}" enabled="true" '
                    f'bounds="[60,{top + 40}][1000,{top + 140}]"/>'
                    "</node>"
                ),
                package_name="com.demo",
                activity=".Category",
            )

        first = next(
            item
            for item in enumerate_actions(
                list_page(label="冰箱商品", top=500), screen_size=(1080, 2400)
            )
            if item.action_role == "ITEM_OPEN:collection"
        )
        second = next(
            item
            for item in enumerate_actions(
                list_page(label="洗衣机商品", top=1900), screen_size=(1080, 2400)
            )
            if item.action_role == "ITEM_OPEN:collection"
        )
        self.assertEqual(first.action_anchor_key, second.action_anchor_key)
        self.assertEqual(first.action_role_key, second.action_role_key)

        def command_page(top: int):
            return build_page_model(
                _page(
                    '<node class="android.widget.Button" text="查看详情" '
                    f'clickable="true" enabled="true" bounds="[100,{top}]'
                    f'[900,{top + 140}]"/>'
                ),
                package_name="com.demo",
                activity=".Main",
            )

        command_top = enumerate_actions(
            command_page(300), screen_size=(1080, 2400)
        )[0]
        command_bottom = enumerate_actions(
            command_page(2050), screen_size=(1080, 2400)
        )[0]
        self.assertNotEqual(command_top.action_anchor_key, command_bottom.action_anchor_key)
        self.assertEqual(command_top.action_role_key, command_bottom.action_role_key)

        def toggle_page(checked: bool):
            return build_page_model(
                _page(
                    '<node class="android.widget.Switch" text="通知" clickable="true" '
                    f'enabled="true" checked="{str(checked).lower()}" '
                    'bounds="[100,500][900,650]"/>'
                ),
                package_name="com.demo",
                activity=".Settings",
            )

        unchecked = enumerate_actions(
            toggle_page(False), screen_size=(1080, 2400)
        )[0]
        checked = enumerate_actions(
            toggle_page(True), screen_size=(1080, 2400)
        )[0]
        self.assertEqual(unchecked.action_anchor_key, checked.action_anchor_key)
        self.assertNotEqual(unchecked.action_role_key, checked.action_role_key)

    def test_product_card_uses_clean_title_and_clickable_ancestor_locator(self):
        xml = _page(
            '<node class="android.widget.TextView" text="分类" enabled="true" '
            'bounds="[400,20][680,110]"/>'
            '<node class="android.view.ViewGroup" content-desc="综合" clickable="true" '
            'enabled="true" bounds="[0,260][260,390]"/>'
            '<node class="android.view.ViewGroup" content-desc="销量" clickable="true" '
            'enabled="true" bounds="[260,260][520,390]"/>'
            '<node class="android.view.ViewGroup" content-desc="筛选" clickable="true" '
            'enabled="true" bounds="[800,260][1080,390]"/>'
            '<node class="android.view.ViewGroup" clickable="true" enabled="true" '
            'bounds="[0,500][1080,1000]">'
            '<node class="android.widget.TextView" text="\uFFFC海尔 法式四门336升冰箱" '
            'enabled="true" bounds="[410,550][1020,660]"/>'
            '<node class="android.widget.TextView" text="一级能耗" enabled="true" '
            'bounds="[410,700][600,760]"/>'
            '</node>'
        )
        page = build_page_model(xml, package_name="com.demo", activity=".Category")
        product = next(
            item
            for item in enumerate_actions(page, screen_size=(1080, 2400))
            if item.action_role == "ITEM_OPEN:collection"
        )
        self.assertEqual(page.page_subtype, "CATALOG_CATEGORY")
        self.assertEqual(product.target_meta["product_title"], "海尔 法式四门336升冰箱")
        self.assertEqual(product.target_meta["text"], "海尔 法式四门336升冰箱")
        self.assertEqual(product.sample_policy, "FAMILY_TWO_SAMPLES")
        self.assertTrue(product.action_group_key)
        sort_actions = [
            action
            for action in enumerate_actions(
                page,
                screen_size=(1080, 2400),
                coverage_scheduler_v2=True,
            )
            if str(action.action_role or "").startswith("SORT:")
        ]
        self.assertTrue(sort_actions)
        self.assertTrue(
            all(action.sample_policy == "PAGE_ONE" for action in sort_actions)
        )
        self.assertEqual(locator_match_count(xml, product.locator_candidates[0]), 1)
        self.assertIn("/ancestor::node", product.locator_candidates[0]["selector"])
        self.assertEqual(
            locator_unique_bounds(xml, product.locator_candidates[0]),
            (0, 500, 1080, 1000),
        )

        nested_xml = _page(
            '<node class="android.view.ViewGroup" clickable="true" enabled="true" '
            'bounds="[0,0][1080,2360]">'
            '<node class="android.view.ViewGroup" clickable="true" enabled="true" '
            'bounds="[0,500][1080,1000]">'
            '<node class="android.widget.TextView" text="\uFFFC海尔 法式四门336升冰箱" '
            'enabled="true" bounds="[410,550][1020,660]"/>'
            '</node></node>'
        )
        self.assertEqual(
            locator_unique_bounds(nested_xml, product.locator_candidates[0]),
            (0, 500, 1080, 1000),
        )
        device = Mock()
        self.assertEqual(perform_action(device, product, current_xml=nested_xml), "xpath")
        device.click.assert_called_once_with(540, 750)
        device.xpath.assert_not_called()

    def test_top_viewport_product_card_is_not_a_category_tab(self):
        xml = _page(
            '<node class="android.view.ViewGroup" content-desc="综合" '
            'clickable="true" enabled="true" bounds="[0,120][260,240]"/>'
            '<node class="android.view.ViewGroup" content-desc="销量" '
            'clickable="true" enabled="true" bounds="[260,120][520,240]"/>'
            '<node class="android.view.ViewGroup" content-desc="筛选" '
            'clickable="true" enabled="true" bounds="[800,120][1080,240]"/>'
            '<node class="android.view.ViewGroup" clickable="true" enabled="true" '
            'bounds="[0,250][1080,620]">'
            '<node class="android.widget.TextView" '
            'text="海尔云溪法式四门485升全空间保鲜冰箱" enabled="true" '
            'bounds="[410,300][1020,410]"/>'
            '<node class="android.widget.TextView" '
            'text="阻氧干湿分储 | EPP超净杀菌 | 三档变温" enabled="true" '
            'bounds="[410,430][1020,520]"/>'
            "</node>"
        )
        page = build_page_model(xml, package_name="com.demo", activity=".Category")
        product = next(
            action
            for action in enumerate_actions(
                page,
                screen_size=(1080, 2400),
                coverage_scheduler_v2=True,
            )
            if "海尔云溪" in str(action.target_meta.get("text"))
        )

        self.assertEqual(page.page_subtype, "CATALOG_CATEGORY")
        self.assertEqual(product.action_role, "ITEM_OPEN:collection")
        self.assertEqual(product.sample_policy, "FAMILY_TWO_SAMPLES")

    def test_coverage_special_lists_skip_tabs_and_horizontal_scrolls(self):
        xml = _page(
            '<node class="android.widget.HorizontalScrollView" scrollable="true" '
            'enabled="true" bounds="[0,120][1080,260]">'
            '<node class="android.view.ViewGroup" content-desc="滤芯专属活动" '
            'clickable="true" enabled="true" bounds="[20,130][360,250]"/>'
            "</node>"
            '<node class="android.widget.ScrollView" scrollable="true" '
            'enabled="true" bounds="[0,280][1080,2200]">'
            '<node class="android.view.ViewGroup" clickable="true" enabled="true" '
            'bounds="[0,320][1080,760]">'
            '<node class="android.widget.TextView" text="卡萨帝云鳟Q3滤芯" '
            'enabled="true" bounds="[400,380][1000,500]"/>'
            "</node>"
            '<node class="android.view.ViewGroup" clickable="true" enabled="true" '
            'bounds="[0,800][1080,1240]">'
            '<node class="android.widget.TextView" text="卡萨帝云澜800G滤芯" '
            'enabled="true" bounds="[400,860][1000,980]"/>'
            "</node></node>"
        )
        page = build_page_model(xml, package_name="com.demo", activity=".List")
        actions = enumerate_actions(
            page,
            screen_size=(1080, 2400),
            coverage_scheduler_v2=True,
        )

        self.assertEqual(page.page_subtype, "CONSUMABLE_LIST")
        self.assertTrue(
            any(action.action_role == "ITEM_OPEN:collection" for action in actions)
        )
        self.assertFalse(
            any(
                str(action.action_role or "").startswith("CATEGORY_TAB:")
                for action in actions
            )
        )
        self.assertFalse(
            any(
                str(action.action_role or "").startswith("SCROLL:horizontal:")
                for action in actions
            )
        )
        self.assertTrue(
            any(
                str(action.action_role or "").startswith("SCROLL:vertical:")
                for action in actions
            )
        )
        service_actions = enumerate_actions(
            replace(page, page_subtype="SERVICE_LIST"),
            screen_size=(1080, 2400),
            coverage_scheduler_v2=True,
        )
        consumable_groups = {
            str(action.action_group_key)
            for action in actions
            if action.action_role == "ITEM_OPEN:collection"
        }
        service_groups = {
            str(action.action_group_key)
            for action in service_actions
            if action.action_role == "ITEM_OPEN:collection"
        }
        self.assertEqual(consumable_groups, service_groups)

    def test_search_surface_samples_one_suggestion_group(self):
        xml = _page(
            '<node class="android.widget.TextView" text="搜索历史" enabled="true" '
            'bounds="[30,260][400,360]"/>'
            '<node class="android.widget.EditText" text="空调品类日" '
            'clickable="true" enabled="true" focusable="true" focused="true" '
            'bounds="[260,100][900,220]"/>'
            '<node class="android.view.ViewGroup" content-desc="搜索" '
            'clickable="true" enabled="true" bounds="[920,100][1070,220]"/>'
            '<node class="android.view.ViewGroup" content-desc="商品" '
            'clickable="true" enabled="true" bounds="[120,100][250,220]"/>'
            '<node class="android.widget.TextView" text="热门搜索" enabled="true" '
            'bounds="[30,560][400,660]"/>'
            '<node class="android.view.ViewGroup" content-desc="海尔10年管家" '
            'clickable="true" enabled="true" bounds="[30,700][310,790]"/>'
            '<node class="android.view.ViewGroup" content-desc="外骨骼机器人" '
            'clickable="true" enabled="true" bounds="[340,700][620,790]"/>'
            '<node class="android.widget.ScrollView" scrollable="true" '
            'enabled="true" bounds="[0,240][1080,2200]"/>'
        )
        page = build_page_model(xml, package_name="com.demo", activity=".Search")
        actions = enumerate_actions(
            page,
            screen_size=(1080, 2400),
            coverage_scheduler_v2=True,
        )

        self.assertEqual(page.page_subtype, "SEARCH")
        self.assertEqual(len(actions), 2)
        self.assertTrue(
            all(action.action_role == "SEARCH_SUGGESTION" for action in actions)
        )
        self.assertTrue(
            all(action.sample_policy == "PAGE_ONE" for action in actions)
        )
        self.assertEqual(len({action.action_group_key for action in actions}), 1)

    def test_haier_v2_search_keeps_fixed_input_and_submit_only(self):
        xml = _page(
            '<node class="android.widget.TextView" text="搜索历史" enabled="true" '
            'bounds="[30,260][400,360]"/>'
            '<node class="android.widget.EditText" text="" '
            'clickable="true" enabled="true" focusable="true" focused="true" '
            'bounds="[260,100][900,220]"/>'
            '<node class="android.view.ViewGroup" content-desc="搜索" '
            'clickable="true" enabled="true" bounds="[920,100][1070,220]"/>'
            '<node class="android.view.ViewGroup" content-desc="热门冰箱" '
            'clickable="true" enabled="true" bounds="[30,700][310,790]"/>'
        )
        page = build_page_model(
            xml,
            package_name="com.ehaier.zgq.shop.mall",
            activity=".Search",
        )
        actions = enumerate_actions(
            page,
            screen_size=(1080, 2400),
            coverage_scheduler_v2=True,
            input_rules=[
                {
                    "id": "haier_v2_search_keyword",
                    "class_regex": "EditText",
                    "page_subtype_regex": "^SEARCH$",
                    "value_source": "literal",
                    "value": "冰箱",
                }
            ],
        )

        self.assertEqual(
            [action.action_role for action in actions],
            ["INPUT", "SEARCH_SUBMIT"],
        )
        self.assertEqual(actions[0].input_rule_id, "haier_v2_search_keyword")

    def test_haier_populated_search_suggestions_remain_a_search_surface(self):
        xml = _page(
            '<node class="android.widget.EditText" text="冰箱" '
            'clickable="true" enabled="true" focusable="true" focused="true" '
            'bounds="[311,135][770,184]"/>'
            '<node class="android.view.ViewGroup" content-desc="搜索" '
            'clickable="true" enabled="true" bounds="[943,102][1045,217]"/>'
            '<node class="android.view.ViewGroup" content-desc="海尔 冰箱" '
            'clickable="true" enabled="true" bounds="[35,223][1045,350]"/>'
            '<node class="android.view.ViewGroup" content-desc="冰箱 风冷 双门" '
            'clickable="true" enabled="true" bounds="[35,352][1045,479]"/>'
        )
        page = build_page_model(
            xml,
            package_name="com.ehaier.zgq.shop.mall",
            activity="com.ehaier.mall.MainActivity",
        )
        actions = enumerate_actions(
            page,
            screen_size=(1080, 2412),
            coverage_scheduler_v2=True,
            input_rules=[
                {
                    "id": "haier_v2_search_keyword",
                    "class_regex": "EditText",
                    "page_subtype_regex": "^SEARCH$",
                    "value_source": "literal",
                    "value": "冰箱",
                }
            ],
        )

        self.assertEqual(page.page_subtype, "SEARCH")
        self.assertEqual(
            [action.action_role for action in actions],
            ["INPUT", "SEARCH_SUBMIT"],
        )

    def test_haier_v2_search_input_survives_rotating_hot_word_anchor(self):
        xml = _page(
            '<node class="android.view.ViewGroup" content-desc="Hi新品" '
            'clickable="true" enabled="true" bounds="[71,113][1009,205]">'
            '<node class="android.widget.EditText" text="" clickable="true" '
            'enabled="true" focusable="true" focused="true" '
            'bounds="[311,135][885,184]"/>'
            '</node>'
            '<node class="android.widget.TextView" text="搜索历史" enabled="true" '
            'bounds="[30,260][400,360]"/>'
            '<node class="android.view.ViewGroup" content-desc="搜索" '
            'clickable="true" enabled="true" bounds="[920,100][1070,220]"/>'
        )
        page = build_page_model(
            xml,
            package_name="com.ehaier.zgq.shop.mall",
            activity=".Search",
        )
        actions = enumerate_actions(
            page,
            screen_size=(1080, 2412),
            coverage_scheduler_v2=True,
            input_rules=[
                {
                    "id": "haier_v2_search_keyword",
                    "class_regex": "EditText",
                    "page_subtype_regex": "^SEARCH$",
                    "value_source": "literal",
                    "value": "冰箱",
                }
            ],
        )
        input_action = next(action for action in actions if action.action_role == "INPUT")
        bounds_candidate = input_action.locator_candidates[0]
        self.assertTrue(bounds_candidate["bounds_constrained"])
        self.assertNotIn("anchor", bounds_candidate)

        fresh_xml = xml.replace("Hi新品", "卡萨帝新品")
        fresh_page = build_page_model(
            fresh_xml,
            package_name="com.ehaier.zgq.shop.mall",
            activity=".Search",
        )
        self.assertEqual(page.semantic_key, fresh_page.semantic_key)
        self.assertEqual(locator_match_count(fresh_xml, bounds_candidate), 1)
        self.assertEqual(
            locator_unique_bounds(fresh_xml, bounds_candidate),
            (311, 135, 885, 184),
        )

    def test_haier_campaign_product_grid_is_a_product_list(self):
        cards = []
        products = (
            "512升五门冰箱",
            "法式四门510升冰箱",
            "变频滚筒洗烘一体机",
            "全自动变频滚筒洗衣机",
            "60升无镁棒电热水器",
            "净省电变频空调挂机",
        )
        for index, product in enumerate(products):
            row, column = divmod(index, 3)
            left = 35 + column * 345
            top = 620 + row * 550
            cards.append(
                '<node class="android.view.ViewGroup" '
                f'content-desc="{product}, 换新补贴, ¥{1598 + index * 200}.85" '
                'clickable="true" enabled="true" '
                f'bounds="[{left},{top}][{left + 321},{top + 530}]">'
                f'<node class="android.widget.TextView" text="{product}" '
                'enabled="true" '
                f'bounds="[{left + 20},{top + 300}][{left + 290},{top + 380}]"/>'
                '</node>'
            )
        page = build_page_model(
            _page(
                '<node class="android.view.ViewGroup" enabled="true" '
                'bounds="[0,0][1080,2364]"/>'
                + "".join(cards)
            ),
            package_name="com.ehaier.zgq.shop.mall",
            activity="com.ehaier.mall.MainActivity",
        )
        actions = enumerate_actions(
            page,
            screen_size=(1080, 2400),
            coverage_scheduler_v2=True,
        )

        self.assertEqual(page.page_subtype, "PRODUCT_LIST")
        self.assertEqual(page.role, "LIST")
        item_actions = [
            action
            for action in actions
            if action.action_role == "ITEM_OPEN:collection"
        ]
        self.assertTrue(item_actions)
        self.assertTrue(all(action.replayable for action in item_actions))
        first_card = next(
            action
            for action in item_actions
            if action.target_meta["bounds"] == [35, 620, 356, 1150]
        )
        first_locator = first_card.locator_candidates[0]
        self.assertTrue(first_locator["bounds_constrained"])
        self.assertEqual(first_locator["expected_class"], "android.view.ViewGroup")
        self.assertEqual(locator_match_count(page.xml, first_locator), 1)
        self.assertEqual(
            locator_unique_bounds(page.xml, first_locator),
            (35, 620, 356, 1150),
        )

    def test_haier_keyword_results_are_not_a_catalog_category(self):
        xml = _page(
            '<node class="android.view.ViewGroup" content-desc="海尔 冰箱" '
            'clickable="true" enabled="true" bounds="[162,114][895,206]"/>'
            '<node class="android.view.ViewGroup" content-desc="综合" '
            'clickable="true" enabled="true" bounds="[0,226][270,318]"/>'
            '<node class="android.view.ViewGroup" content-desc="销量" '
            'clickable="true" enabled="true" bounds="[270,226][540,318]"/>'
            '<node class="android.view.ViewGroup" content-desc="价格" '
            'clickable="true" enabled="true" bounds="[540,226][810,318]"/>'
            '<node class="android.view.ViewGroup" content-desc="筛选" '
            'clickable="true" enabled="true" bounds="[810,226][1080,318]"/>'
            '<node class="android.view.ViewGroup" clickable="true" enabled="true" '
            'bounds="[0,440][1080,960]">'
            '<node class="android.widget.TextView" '
            'text="海尔 三门203升 黑金净化风冷无霜冰箱" enabled="true" '
            'bounds="[410,480][1030,600]"/>'
            '<node class="android.widget.TextView" text="¥1125" enabled="true" '
            'bounds="[410,700][600,780]"/>'
            '<node class="android.widget.TextView" text="立即购买" enabled="true" '
            'bounds="[760,800][1020,900]"/>'
            '</node>'
            '<node class="android.view.ViewGroup" clickable="true" enabled="true" '
            'bounds="[0,980][1080,1500]">'
            '<node class="android.widget.TextView" '
            'text="海尔 对开门616升 变频风冷无霜冰箱" enabled="true" '
            'bounds="[410,1020][1030,1140]"/>'
            '<node class="android.widget.TextView" text="¥2499" enabled="true" '
            'bounds="[410,1240][600,1320]"/>'
            '<node class="android.widget.TextView" text="立即购买" enabled="true" '
            'bounds="[760,1340][1020,1440]"/>'
            '</node>'
        )
        page = build_page_model(
            xml,
            package_name="com.ehaier.zgq.shop.mall",
            activity="com.ehaier.mall.MainActivity",
        )
        actions = enumerate_actions(
            page,
            screen_size=(1080, 2412),
            coverage_scheduler_v2=True,
        )

        self.assertEqual(page.page_subtype, "PRODUCT_LIST")
        self.assertTrue(
            any(action.action_role == "ITEM_OPEN:collection" for action in actions)
        )

    def test_haier_recent_search_surface_keeps_fixed_search_input(self):
        xml = _page(
            '<node class="android.widget.EditText" clickable="true" '
            'enabled="true" bounds="[222,114][888,206]"/>'
            '<node class="android.view.ViewGroup" content-desc="搜索" '
            'clickable="true" enabled="true" bounds="[955,131][1045,189]"/>'
            '<node class="android.widget.TextView" text="最近搜索" '
            'enabled="true" bounds="[35,281][191,339]"/>'
            '<node class="android.view.ViewGroup" content-desc="小厨宝" '
            'clickable="true" enabled="true" bounds="[35,374][213,454]"/>'
        )
        page = build_page_model(
            xml,
            package_name="com.ehaier.zgq.shop.mall",
            activity="com.ehaier.mall.MainActivity",
        )
        actions = enumerate_actions(
            page,
            screen_size=(1080, 2400),
            coverage_scheduler_v2=True,
            input_rules=[
                {
                    "id": "haier_v2_search_keyword",
                    "class_regex": "EditText",
                    "page_subtype_regex": "^SEARCH$",
                    "value_source": "literal",
                    "value": "冰箱",
                }
            ],
        )

        self.assertEqual(page.page_subtype, "SEARCH")
        input_action = next(action for action in actions if action.action_role == "INPUT")
        self.assertEqual(input_action.input_rule_id, "haier_v2_search_keyword")
        self.assertIsNone(input_action.risk_type)

    def test_haier_category_hot_word_header_is_the_search_entry(self):
        xml = _page(
            '<node class="android.widget.TextView" text="分类" '
            'enabled="true" bounds="[400,240][680,320]"/>'
            '<node class="android.view.ViewGroup" content-desc="Hi新品" '
            'clickable="true" enabled="true" bounds="[71,113][1009,205]"/>'
            + "".join(
                '<node class="android.view.ViewGroup" content-desc="{}" '
                'clickable="true" enabled="true" bounds="[0,{}][253,{}]"/>'.format(
                    label,
                    384 + index * 144,
                    528 + index * 144,
                )
                for index, label in enumerate(
                    ("新品", "冰箱", "洗衣机", "空调", "热水器")
                )
            )
            + '<node class="android.view.ViewGroup" enabled="true" '
            'bounds="[253,384][1080,2100]"/>'
            + '<node class="android.view.ViewGroup" content-desc="海尔" '
            'clickable="true" enabled="true" bounds="[275,630][511,918]"/>'
        )
        page = build_page_model(
            xml,
            package_name="com.ehaier.zgq.shop.mall",
            activity="com.ehaier.mall.MainActivity",
        )
        actions = enumerate_actions(
            page,
            screen_size=(1080, 2412),
            coverage_scheduler_v2=True,
        )
        header = next(
            action
            for action in actions
            if action.target_meta["content_desc"] == "Hi新品"
        )

        self.assertEqual(page.page_subtype, "CATALOG_CATEGORY")
        self.assertEqual(header.action_role, "COMMAND:SEARCH")
        self.assertEqual(header.sample_policy, "PAGE_ONE")
        self.assertTrue(header.locator_candidates[0]["bounds_constrained"])

        changed = build_page_model(
            xml.replace("Hi新品", "智家焕新"),
            package_name="com.ehaier.zgq.shop.mall",
            activity="com.ehaier.mall.MainActivity",
        )
        self.assertEqual(page.semantic_key, changed.semantic_key)
        self.assertEqual(
            locator_match_count(changed.xml, header.locator_candidates[0]),
            1,
        )

    def test_haier_checkout_benefit_prompt_keeps_direct_submit_only(self):
        page = build_page_model(
            _page(
                '<node class="android.widget.TextView" text="权益选择提醒" '
                'enabled="true" bounds="[320,840][760,960]"/>'
                '<node class="android.widget.TextView" '
                'text="此订单存在可选择的权益" enabled="true" '
                'bounds="[250,980][830,1080]"/>'
                '<node class="android.view.ViewGroup" content-desc="直接提交" '
                'clickable="true" enabled="true" bounds="[180,1120][500,1260]"/>'
                '<node class="android.view.ViewGroup" content-desc="选择权益" '
                'clickable="true" enabled="true" bounds="[540,1120][900,1260]"/>'
            ),
            package_name="com.ehaier.zgq.shop.mall",
            activity="com.ehaier.mall.MainActivity",
        )
        actions = enumerate_actions(
            page,
            screen_size=(1080, 2412),
            coverage_scheduler_v2=True,
        )

        self.assertEqual(page.page_subtype, "CHECKOUT_CONFIRMATION")
        self.assertEqual(len(actions), 1)
        self.assertEqual(actions[0].action_role, "PLACE_ORDER")
        self.assertEqual(actions[0].target_meta["content_desc"], "直接提交")

    def test_haier_order_tabs_identify_the_order_center(self):
        page = build_page_model(
            _page(
                "".join(
                    '<node class="android.view.ViewGroup" content-desc="{}" '
                    'clickable="true" enabled="true" bounds="[{},220][{},340]"/>'.format(
                        label,
                        20 + index * 200,
                        190 + index * 200,
                    )
                    for index, label in enumerate(
                        ("全部", "待付款", "待发货", "待收货/验收", "待评价")
                    )
                )
                + '<node class="android.view.ViewGroup" '
                'content-desc="海尔自营, 等待付款, 去支付, 取消订单" '
                'clickable="true" enabled="true" bounds="[40,420][1040,940]"/>'
            ),
            package_name="com.ehaier.zgq.shop.mall",
            activity="com.ehaier.mall.MainActivity",
        )

        self.assertEqual((page.role, page.page_subtype), ("ORDER", "ORDER"))

    def test_haier_factory_extended_warranty_is_a_service_detail(self):
        page = build_page_model(
            _page(
                '<node class="android.widget.TextView" text="海尔原厂延保" '
                'enabled="true" bounds="[80,260][520,360]"/>'
                '<node class="android.widget.TextView" '
                'text="整机保修延长至10年" enabled="true" '
                'bounds="[80,400][700,500]"/>'
                '<node class="android.view.ViewGroup" content-desc="到货通知" '
                'clickable="true" enabled="true" bounds="[700,2200][1040,2340]"/>'
            ),
            package_name="com.ehaier.zgq.shop.mall",
            activity="com.ehaier.mall.MainActivity",
        )

        self.assertEqual(page.page_subtype, "SERVICE_DETAIL")

    def test_explicit_guest_login_boundary_is_auth_gate(self):
        xml = _page(
            '<node class="android.widget.TextView" text="请先登录" enabled="true" '
            'bounds="[380,280][700,380]"/>'
            '<node class="android.widget.EditText" content-desc="手机号" '
            'clickable="true" enabled="true" bounds="[100,500][980,620]"/>'
            '<node class="android.widget.EditText" content-desc="验证码" '
            'clickable="true" enabled="true" bounds="[100,660][980,780]"/>'
            '<node class="android.view.ViewGroup" content-desc="立即登录" '
            'clickable="true" enabled="true" bounds="[100,840][980,960]"/>'
        )
        page = build_page_model(
            xml,
            package_name="com.ehaier.zgq.shop.mall",
            activity=".Login",
        )

        self.assertEqual(page.page_subtype, "AUTH_GATE")

    def test_appointment_list_only_keeps_destructive_blocked_edges(self):
        xml = _page(
            '<node class="android.widget.TextView" text="我的预约" enabled="true" '
            'bounds="[400,100][680,220]"/>'
            '<node class="android.widget.ScrollView" scrollable="true" '
            'enabled="true" bounds="[0,240][1080,2200]">'
            '<node class="android.widget.TextView" text="预约时间" enabled="true" '
            'bounds="[60,500][260,580]"/>'
            '<node class="android.view.ViewGroup" content-desc="取消预约" '
            'clickable="true" enabled="true" bounds="[800,620][1030,720]"/>'
            '<node class="android.view.ViewGroup" content-desc="取消预约" '
            'clickable="true" enabled="true" bounds="[800,1120][1030,1220]"/>'
            "</node>"
        )
        page = build_page_model(
            xml,
            package_name="com.ehaier.zgq.shop.mall",
            activity="com.ehaier.mall.MainActivity",
        )
        actions = enumerate_actions(
            page,
            screen_size=(1080, 2400),
            coverage_scheduler_v2=True,
        )

        self.assertEqual(page.page_subtype, "APPOINTMENT_LIST")
        self.assertEqual(len(actions), 2)
        self.assertTrue(all(action.risk_type == "DESTRUCTIVE" for action in actions))
        self.assertFalse(any(action.action_type == "scroll" for action in actions))

    def test_phone_action_is_an_external_side_effect(self):
        xml = _page(
            '<node class="android.view.ViewGroup" content-desc="电话" '
            'clickable="true" enabled="true" bounds="[700,300][980,430]"/>'
        )
        page = build_page_model(xml, package_name="com.demo", activity=".Store")
        action = enumerate_actions(page, screen_size=(1080, 2400))[0]
        self.assertEqual(action.risk_type, "EXTERNAL_SIDE_EFFECT")

    def test_filter_panel_is_one_close_only_state_under_coverage_scheduler(self):
        xml = _page(
            '<node class="android.widget.TextView" text="全部筛选" enabled="true" '
            'bounds="[380,480][700,580]"/>'
            '<node class="android.view.ViewGroup" content-desc="商品" clickable="true" '
            'enabled="true" bounds="[0,600][200,760]"/>'
            '<node class="android.view.ViewGroup" content-desc="价格" clickable="true" '
            'enabled="true" bounds="[0,760][200,920]"/>'
            '<node class="android.view.ViewGroup" content-desc="尺寸" clickable="true" '
            'enabled="true" bounds="[0,920][200,1080]"/>'
            '<node class="android.view.ViewGroup" content-desc="能效等级" clickable="true" '
            'enabled="true" bounds="[0,1080][200,1240]"/>'
            '<node class="android.widget.Button" text="重置" clickable="true" '
            'enabled="true" bounds="[20,2100][520,2300]"/>'
            '<node class="android.widget.Button" text="确定" clickable="true" '
            'enabled="true" bounds="[560,2100][1060,2300]"/>'
        )
        page = build_page_model(
            xml,
            package_name="com.ehaier.zgq.shop.mall",
            activity="com.ehaier.mall.MainActivity",
        )
        self.assertEqual(page.role, "DIALOG")
        self.assertEqual(page.page_subtype, "FILTER_PANEL")

        actions = enumerate_actions(
            page,
            screen_size=(1080, 2400),
            coverage_scheduler_v2=True,
        )
        self.assertEqual(len(actions), 1)
        self.assertEqual(actions[0].action_type, "back")
        self.assertEqual(actions[0].action_role, "FILTER_CLOSE")
        self.assertEqual(actions[0].sample_policy, "PAGE_ONE")
        self.assertEqual(actions[0].target_meta["text"], "关闭筛选")

        device = Mock()
        self.assertEqual(perform_action(device, actions[0], current_xml=xml), "back")
        device.press.assert_called_once_with("back")

    def test_filter_panel_remains_close_only_when_one_footer_button_is_missing(self):
        xml = _page(
            '<node class="android.widget.TextView" text="全部筛选" enabled="true" '
            'bounds="[380,480][700,580]"/>'
            '<node class="android.view.ViewGroup" content-desc="商品" clickable="true" '
            'enabled="true" bounds="[0,600][200,760]"/>'
            '<node class="android.view.ViewGroup" content-desc="价格" clickable="true" '
            'enabled="true" bounds="[0,760][200,920]"/>'
            '<node class="android.view.ViewGroup" content-desc="尺寸" clickable="true" '
            'enabled="true" bounds="[0,920][200,1080]"/>'
            '<node class="android.widget.Button" text="确定" clickable="true" '
            'enabled="true" bounds="[560,2100][1060,2300]"/>'
        )
        page = build_page_model(
            xml,
            package_name="com.ehaier.zgq.shop.mall",
            activity="com.ehaier.mall.MainActivity",
        )
        actions = enumerate_actions(
            page,
            screen_size=(1080, 2400),
            coverage_scheduler_v2=True,
        )

        self.assertEqual(page.page_subtype, "FILTER_PANEL")
        self.assertEqual([action.action_role for action in actions], ["FILTER_CLOSE"])

    def test_purchase_options_panel_uses_one_primary_cta_and_skips_variants(self):
        xml = _page(
            '<node class="android.view.ViewGroup" enabled="true" '
            'bounds="[0,0][1080,2364]">'
            '<node class="android.view.ViewGroup" content-desc="立即购买" '
            'clickable="true" enabled="true" bounds="[730,2200][1040,2340]"/>'
            '<node class="android.view.ViewGroup" clickable="true" '
            'enabled="true" bounds="[0,0][1080,2364]"/>'
            '<node class="android.view.ViewGroup" enabled="true" '
            'bounds="[0,900][1080,2364]">'
            '<node class="android.widget.TextView" text="库存 99 件" '
            'enabled="true" bounds="[360,1050][700,1120]"/>'
            '<node class="android.widget.TextView" text="规格" '
            'enabled="true" bounds="[40,1300][180,1380]"/>'
            '<node class="android.view.ViewGroup" content-desc="标准款" '
            'clickable="true" selected="true" enabled="true" '
            'bounds="[40,1420][400,1530]"/>'
            '<node class="android.view.ViewGroup" content-desc="大尺寸" '
            'clickable="true" enabled="true" bounds="[440,1420][800,1530]"/>'
            '<node class="android.view.ViewGroup" content-desc="立即购买" '
            'clickable="true" enabled="true" bounds="[35,2180][1045,2306]"/>'
            '</node></node>'
        )
        page = build_page_model(
            xml,
            package_name="com.ehaier.zgq.shop.mall",
            activity="com.ehaier.mall.MainActivity",
        )

        self.assertEqual(page.role, "DIALOG")
        self.assertEqual(page.page_subtype, "PURCHASE_OPTIONS")
        actions = enumerate_actions(
            page,
            screen_size=(1080, 2400),
            coverage_scheduler_v2=True,
        )

        self.assertEqual(
            [action.action_role for action in actions],
            ["BUY_NOW", "DIALOG_CLOSE"],
        )
        self.assertEqual(actions[0].target_meta["bounds"], [35, 2180, 1045, 2306])
        self.assertIsNone(actions[0].risk_type)
        self.assertFalse(
            any(
                action.target_meta.get("content_desc") in {"标准款", "大尺寸"}
                for action in actions
            )
        )

    def test_modal_overlay_takes_precedence_over_store_detail_background(self):
        xml = _page(
            '<node class="android.view.ViewGroup" enabled="true" '
            'bounds="[0,0][1080,2364]">'
            '<node class="android.widget.TextView" text="切换门店" '
            'enabled="true" bounds="[40,160][260,240]"/>'
            '<node class="android.widget.TextView" text="距您 1.2km" '
            'enabled="true" bounds="[40,300][300,380]"/>'
            '<node class="android.view.ViewGroup" content-desc="预约" '
            'clickable="true" enabled="true" bounds="[40,500][300,620]"/>'
            '<node class="android.view.ViewGroup" content-desc="电话" '
            'clickable="true" enabled="true" bounds="[340,500][600,620]"/>'
            '<node class="android.view.ViewGroup" clickable="true" '
            'enabled="true" bounds="[0,0][1080,2364]"/>'
            '<node class="android.view.ViewGroup" enabled="true" '
            'bounds="[0,900][1080,2364]">'
            '<node class="android.widget.TextView" text="预约到店" '
            'enabled="true" bounds="[420,960][660,1040]"/>'
            '</node></node>'
        )
        page = build_page_model(
            xml,
            package_name="com.ehaier.zgq.shop.mall",
            activity="com.ehaier.mall.MainActivity",
        )

        self.assertEqual(page.role, "DIALOG")
        self.assertEqual(page.page_subtype, "MODAL_PANEL")
        actions = enumerate_actions(
            page,
            screen_size=(1080, 2400),
            coverage_scheduler_v2=True,
        )
        self.assertEqual([action.action_role for action in actions], ["DIALOG_CLOSE"])

    def test_modal_scrim_hides_background_checkout_actions(self):
        xml = _page(
            '<node class="android.view.ViewGroup" enabled="true" '
            'bounds="[0,0][1080,2364]">'
            '<node class="android.widget.TextView" text="提交订单" '
            'enabled="true" bounds="[420,100][660,220]"/>'
            '<node class="android.view.ViewGroup" content-desc="立即支付" '
            'clickable="true" enabled="true" '
            'bounds="[750,2200][1030,2340]"/>'
            '<node class="android.view.ViewGroup" clickable="true" '
            'enabled="true" bounds="[0,0][1080,2364]"/>'
            '<node class="android.view.ViewGroup" enabled="true" '
            'bounds="[0,895][1080,2364]">'
            '<node class="android.widget.TextView" text="配送至" '
            'enabled="true" bounds="[460,950][620,1030]"/>'
            '<node class="android.view.ViewGroup" content-desc="新建收货地址" '
            'clickable="true" enabled="true" '
            'bounds="[35,2210][1045,2330]"/>'
            '</node></node>'
        )
        page = build_page_model(
            xml,
            package_name="com.ehaier.zgq.shop.mall",
            activity="com.ehaier.mall.MainActivity",
        )
        self.assertEqual(page.role, "DIALOG")
        self.assertEqual(page.page_subtype, "MODAL_PANEL")
        actions = enumerate_actions(
            page,
            screen_size=(1080, 2400),
            coverage_scheduler_v2=True,
        )
        self.assertEqual(len(actions), 1)
        self.assertEqual(actions[0].action_type, "back")
        self.assertEqual(actions[0].action_role, "DIALOG_CLOSE")
        self.assertEqual(actions[0].target_meta["text"], "关闭弹窗")

    def test_cart_is_a_terminal_coverage_state(self):
        xml = _page(
            '<node class="android.widget.TextView" text="购物车" '
            'enabled="true" bounds="[430,100][650,220]"/>'
            '<node class="android.view.ViewGroup" content-desc="编辑" '
            'clickable="true" enabled="true" bounds="[900,110][1040,210]"/>'
            '<node class="android.view.ViewGroup" content-desc="海尔自营" '
            'clickable="true" enabled="true" bounds="[40,250][1040,900]"/>'
            '<node class="android.view.ViewGroup" content-desc="全选" '
            'clickable="true" enabled="true" bounds="[30,2200][250,2360]"/>'
            '<node class="android.view.ViewGroup" content-desc="结算 (0)" '
            'clickable="true" enabled="true" bounds="[760,2200][1040,2360]"/>'
        )
        page = build_page_model(
            xml,
            package_name="com.ehaier.zgq.shop.mall",
            activity="com.ehaier.mall.MainActivity",
        )
        self.assertEqual(page.role, "LIST")
        self.assertEqual(page.page_subtype, "CART")
        self.assertEqual(
            enumerate_actions(
                page,
                screen_size=(1080, 2400),
                coverage_scheduler_v2=True,
            ),
            [],
        )

    def test_coverage_frontier_prioritizes_new_family_over_shallower_repeat(self):
        def work(state_id, depth, priority):
            return StateWork(
                state_id=state_id,
                state_key=str(state_id),
                cluster_key=str(state_id),
                replay_key=str(state_id),
                package_name="com.demo",
                activity=".Main",
                screenshot_sha=str(state_id),
                depth=depth,
                path=[],
                actions=[],
                frontier_priority=priority,
            )

        queue = deque([work(1, 1, 800), work(2, 2, 100), work(3, 1, 500)])
        selected = _pop_most_local(queue, [], coverage_scheduler=True)
        self.assertEqual(selected.state_id, 2)

    def test_product_detail_store_module_does_not_replace_page_role(self):
        xml = _page(
            '<node class="android.widget.TextView" text="商品详情" '
            'enabled="true" bounds="[420,100][660,220]"/>'
            '<node class="android.widget.TextView" text="进店逛逛" '
            'enabled="true" bounds="[180,1200][420,1280]"/>'
            '<node class="android.widget.TextView" text="距离7.13km" '
            'enabled="true" bounds="[700,1200][950,1280]"/>'
            '<node class="android.view.ViewGroup" content-desc="立即购买" '
            'clickable="true" enabled="true" '
            'bounds="[650,1800][1040,1980]"/>'
        )
        page = build_page_model(
            xml,
            package_name="com.ehaier.zgq.shop.mall",
            activity="com.ehaier.mall.MainActivity",
        )
        self.assertEqual(page.role, "PRODUCT_DETAIL")
        self.assertEqual(page.page_subtype, "PRODUCT_DETAIL")
        actions = enumerate_actions(
            page,
            screen_size=(1080, 2400),
            coverage_scheduler_v2=True,
        )
        self.assertEqual([item.action_role for item in actions], ["BUY_NOW"])

    def test_haier_price_cta_and_option_row_use_verified_bounds(self):
        xml = _page(
            '<node class="android.widget.TextView" text="商品详情" '
            'enabled="true" bounds="[420,100][660,220]"/>'
            '<node class="android.view.ViewGroup" '
            'content-desc="已选, LEC5TP , ，1件 " clickable="true" '
            'enabled="true" bounds="[35,2123][1045,2177]"/>'
            '<node class="android.view.ViewGroup" content-desc="加入购物车" '
            'clickable="true" enabled="true" bounds="[390,2222][707,2348]"/>'
            '<node class="android.view.ViewGroup" content-desc="到手价, ¥288.15" '
            'clickable="true" enabled="true" bounds="[731,2222][1048,2348]">'
            '<node class="android.widget.TextView" text="到手价" '
            'enabled="true" bounds="[840,2242][939,2286]"/>'
            '<node class="android.widget.TextView" text="¥288.15" '
            'enabled="true" bounds="[810,2286][970,2338]"/>'
            "</node>"
        )
        page = build_page_model(
            xml,
            package_name="com.ehaier.zgq.shop.mall",
            activity="com.ehaier.mall.MainActivity",
        )
        actions = enumerate_actions(
            page,
            screen_size=(1080, 2412),
            coverage_scheduler_v2=True,
        )
        by_role = {action.action_role: action for action in actions}

        self.assertEqual(page.page_subtype, "PRODUCT_DETAIL")
        self.assertIn("BUY_NOW", by_role)
        self.assertIn("OPTION_SELECT", by_role)
        for role, expected_bounds in (
            ("OPTION_SELECT", (35, 2123, 1045, 2177)),
            ("BUY_NOW", (731, 2222, 1048, 2348)),
        ):
            candidate = by_role[role].locator_candidates[0]
            self.assertTrue(candidate["bounds_constrained"])
            self.assertEqual(locator_match_count(xml, candidate), 1)
            self.assertEqual(locator_unique_bounds(xml, candidate), expected_bounds)

    def test_store_list_groups_actions_and_blocks_phone(self):
        xml = _page(
            '<node class="android.widget.TextView" text="附近门店" enabled="true" '
            'bounds="[400,30][680,130]"/>'
            '<node class="android.view.ViewGroup" content-desc="海尔专卖店, 青岛市, 1.2km" '
            'clickable="true" enabled="true" bounds="[0,400][1080,1000]">'
            '<node class="android.widget.TextView" text="海尔专卖店" enabled="true" '
            'bounds="[50,450][700,540]"/>'
            '<node class="android.view.ViewGroup" content-desc="立即预约" clickable="true" '
            'enabled="true" bounds="[50,800][430,940]"/>'
            '<node class="android.view.ViewGroup" content-desc="门店电话" clickable="true" '
            'enabled="true" bounds="[600,800][1030,940]"/>'
            '</node>'
        )
        page = build_page_model(xml, package_name="com.demo", activity=".Main")
        actions = enumerate_actions(page, screen_size=(1080, 2400))
        by_role = {item.action_role: item for item in actions}
        self.assertEqual(page.role, "LIST")
        self.assertEqual(page.page_subtype, "STORE_LIST")
        self.assertIn("STORE_OPEN", by_role)
        self.assertIsNone(by_role["STORE_OPEN"].risk_type)
        self.assertIn("STORE_APPOINTMENT", by_role)
        self.assertEqual(by_role["STORE_CALL"].risk_type, "EXTERNAL_SIDE_EFFECT")

    def test_titleless_store_list_ignores_location_controls_and_uses_store_title(self):
        xml = _page(
            '<node class="android.view.ViewGroup" content-desc="收货地址" '
            'clickable="true" enabled="true" bounds="[35,900][320,1010]"/>'
            '<node class="android.view.ViewGroup" content-desc="当前定位" '
            'clickable="true" enabled="true" bounds="[350,900][640,1010]"/>'
            '<node class="android.view.ViewGroup" clickable="true" enabled="true" '
            'bounds="[35,1100][1045,1500]">'
            '<node class="android.widget.TextView" text="距离最近" enabled="true" '
            'bounds="[800,1120][1020,1190]"/>'
            '<node class="android.widget.TextView" text="海尔智慧厨房店" enabled="true" '
            'bounds="[60,1150][700,1240]"/>'
            '<node class="android.widget.TextView" text="进店逛逛" enabled="true" '
            'bounds="[760,1300][1020,1380]"/>'
            '<node class="android.widget.TextView" text="距离 1.2km" enabled="true" '
            'bounds="[760,1390][1020,1460]"/>'
            '</node>'
            '<node class="android.view.ViewGroup" clickable="true" enabled="true" '
            'bounds="[35,1530][1045,1930]">'
            '<node class="android.widget.TextView" text="海尔热水器店" enabled="true" '
            'bounds="[60,1580][700,1670]"/>'
            '<node class="android.widget.TextView" text="进店逛逛" enabled="true" '
            'bounds="[760,1730][1020,1810]"/>'
            '<node class="android.widget.TextView" text="距离 1.5km" enabled="true" '
            'bounds="[760,1820][1020,1890]"/>'
            '</node>'
        )
        page = build_page_model(
            xml,
            package_name="com.ehaier.zgq.shop.mall",
            activity="com.ehaier.mall.MainActivity",
        )
        actions = enumerate_actions(
            page,
            screen_size=(1080, 2400),
            coverage_scheduler_v2=True,
        )

        self.assertEqual(page.role, "LIST")
        self.assertEqual(page.page_subtype, "STORE_LIST")
        self.assertEqual({action.action_role for action in actions}, {"STORE_OPEN"})
        first_store = actions[0]
        self.assertEqual(first_store.target_meta["text"], "海尔智慧厨房店")
        self.assertEqual(first_store.sample_policy, "PAGE_ONE")
        self.assertTrue(first_store.locator_candidates[0]["requires_clickable_ancestor"])

    def test_store_detail_keeps_only_business_exits_and_blocks_mutations(self):
        xml = _page(
            '<node class="android.widget.TextView" text="门店直销" enabled="true" '
            'bounds="[400,30][680,130]"/>'
            '<node class="android.view.ViewGroup" content-desc="切换门店" '
            'clickable="true" enabled="true" bounds="[820,240][1040,340]"/>'
            '<node class="android.widget.TextView" text="距您 7.13km" enabled="true" '
            'bounds="[40,360][350,440]"/>'
            '<node class="android.view.ViewGroup" content-desc="电话" clickable="true" '
            'enabled="true" bounds="[800,360][1040,470]"/>'
            '<node class="android.view.ViewGroup" content-desc="我的预约" '
            'clickable="true" enabled="true" bounds="[700,760][1040,880]"/>'
            '<node class="android.view.ViewGroup" content-desc="提交预约" '
            'clickable="true" enabled="true" bounds="[60,1600][1020,1760]"/>'
            '<node class="android.view.ViewGroup" content-desc="选品" clickable="true" '
            'enabled="true" bounds="[540,500][1080,650]"/>'
            '<node class="android.widget.ScrollView" scrollable="true" enabled="true" '
            'bounds="[0,180][1080,2200]"/>'
        )
        page = build_page_model(
            xml,
            package_name="com.ehaier.zgq.shop.mall",
            activity="com.ehaier.mall.MainActivity",
        )
        actions = enumerate_actions(
            page,
            screen_size=(1080, 2400),
            coverage_scheduler_v2=True,
        )
        by_role = {action.action_role: action for action in actions}

        self.assertEqual(page.page_subtype, "STORE_DETAIL")
        self.assertEqual(
            set(by_role),
            {"STORE_CALL", "STORE_BOOKINGS", "STORE_APPOINTMENT", "STORE_PRODUCTS"},
        )
        self.assertEqual(by_role["STORE_CALL"].risk_type, "EXTERNAL_SIDE_EFFECT")
        self.assertEqual(
            by_role["STORE_APPOINTMENT"].risk_type,
            "EXTERNAL_SIDE_EFFECT",
        )
        self.assertTrue(all(action.sample_policy == "PAGE_ONE" for action in actions))

    def test_catalog_sidebar_categories_share_one_family_action_group(self):
        xml = _page(
            '<node class="android.view.ViewGroup" content-desc="综合" clickable="true" '
            'enabled="true" bounds="[250,200][500,330]"/>'
            '<node class="android.view.ViewGroup" content-desc="销量" clickable="true" '
            'enabled="true" bounds="[500,200][750,330]"/>'
            '<node class="android.view.ViewGroup" content-desc="筛选" clickable="true" '
            'enabled="true" bounds="[810,200][1080,330]"/>'
            + "".join(
                '<node class="android.view.ViewGroup" content-desc="{}" '
                'clickable="true" enabled="true" bounds="[0,{}][240,{}]"/>'.format(
                    label,
                    360 + index * 180,
                    500 + index * 180,
                )
                for index, label in enumerate(
                    ("冰箱冷柜", "洗衣机", "空调", "热水器", "厨房电器")
                )
            )
            + '<node class="android.view.ViewGroup" clickable="true" enabled="true" '
            'bounds="[260,380][1060,900]">'
            '<node class="android.widget.TextView" text="海尔法式四门冰箱" '
            'enabled="true" bounds="[430,450][1000,560]"/>'
            "</node>"
        )
        page = build_page_model(xml, package_name="com.demo", activity=".StoreProducts")
        actions = enumerate_actions(
            page,
            screen_size=(1080, 2400),
            coverage_scheduler_v2=True,
        )
        categories = [
            action
            for action in actions
            if str(action.action_role or "").startswith("CATEGORY_TAB:side")
        ]

        self.assertEqual(page.page_subtype, "CATALOG_CATEGORY")
        self.assertEqual(len(categories), 5)
        self.assertEqual({action.sample_policy for action in categories}, {"FAMILY_ONE"})
        self.assertEqual(len({action.action_group_key for action in categories}), 1)

    def test_profile_surface_identity_ignores_dynamic_account_counters(self):
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
                    '<node class="android.view.ViewGroup" content-desc="{}" '
                    'clickable="true" enabled="true" bounds="[40,{}][1040,{}]"/>'.format(
                        label,
                        220 + index * 180,
                        360 + index * 180,
                    )
                    for index, label in enumerate(labels)
                )
            )

        first = build_page_model(
            profile_page(coupon_count=0, order_count=21),
            package_name="com.demo",
            activity=".Main",
        )
        second = build_page_model(
            profile_page(coupon_count=2, order_count=22),
            package_name="com.demo",
            activity=".Main",
        )

        self.assertEqual(first.role, "PROFILE")
        self.assertEqual(first.page_subtype, "PROFILE")
        self.assertEqual(second.role, "PROFILE")
        self.assertEqual(second.page_subtype, "PROFILE")
        self.assertEqual(
            derive_instance_anchor(first),
            derive_instance_anchor(second),
        )
        self.assertTrue(compare_exploration_families(first, second).equivalent)

    def test_community_feed_samples_one_dynamic_post_card(self):
        def community_page(*, author: str, day: str) -> str:
            return _page(
                '<node class="android.widget.ScrollView" scrollable="true" '
                'enabled="true" bounds="[0,220][1080,2360]">'
                '<node class="android.widget.TextView" '
                'text="亲爱的许愿池社区的伙伴们" enabled="true" '
                'bounds="[40,260][1040,340]"/>'
                '<node class="android.view.ViewGroup" clickable="true" enabled="true" '
                'content-desc="06-25 09:46 · 山东, 商城智多星, 官方" '
                'bounds="[35,420][1045,1180]">'
                '<node class="android.widget.TextView" text="商城智多星" '
                'enabled="true" bounds="[160,440][500,500]"/></node>'
                '<node class="android.view.ViewGroup" clickable="true" enabled="true" '
                f'content-desc="{day} 17:40 · 山东, {author}" '
                'bounds="[35,1220][1045,2200]">'
                f'<node class="android.widget.TextView" text="{author}" '
                'enabled="true" bounds="[160,1240][600,1300]"/></node>'
                "</node>"
            )

        first = build_page_model(
            community_page(author="下雨要打伞雷欧", day="05-25"),
            package_name="com.demo",
            activity=".Main",
        )
        second = build_page_model(
            community_page(author="新的社区用户", day="07-22"),
            package_name="com.demo",
            activity=".Main",
        )
        actions = enumerate_actions(
            first,
            screen_size=(1080, 2400),
            coverage_scheduler_v2=True,
        )
        posts = [
            action for action in actions if action.action_role == "ITEM_OPEN:collection"
        ]

        self.assertEqual(first.role, "LIST")
        self.assertEqual(first.page_subtype, "COMMUNITY_FEED")
        self.assertEqual(len(posts), 2)
        self.assertEqual({action.sample_policy for action in posts}, {"PAGE_ONE"})
        self.assertEqual(len({action.action_group_key for action in posts}), 1)
        self.assertEqual(
            derive_instance_anchor(first),
            derive_instance_anchor(second),
        )
        self.assertTrue(compare_exploration_families(first, second).equivalent)

    def test_haier_community_article_is_a_distinct_coverage_endpoint(self):
        page = build_page_model(
            _page(
                '<node class="android.widget.TextView" text="海尔商城" '
                'enabled="true" bounds="[150,80][430,160]"/>'
                '<node class="android.widget.TextView" text="官方" '
                'enabled="true" bounds="[440,80][560,160]"/>'
                '<node class="android.widget.TextView" text="精选" '
                'enabled="true" bounds="[150,170][270,230]"/>'
                '<node class="android.widget.TextView" text="06-30 09:08 · 山东" '
                'enabled="true" bounds="[280,170][650,230]"/>'
                '<node class="android.widget.TextView" text="参与话题，赢好礼" '
                'enabled="true" bounds="[40,320][700,410]"/>'
                '<node class="android.widget.EditText" content-desc="来说点什么..." '
                'clickable="true" enabled="true" bounds="[40,2180][680,2300]"/>'
            ),
            package_name="com.ehaier.zgq.shop.mall",
            activity="com.ehaier.mall.MainActivity",
        )

        self.assertEqual(page.page_subtype, "COMMUNITY_DETAIL")

    def test_haier_smart_life_benefits_requires_combined_coupon_signals(self):
        xml = _page(
            '<node class="android.widget.TextView" text="Smart life" '
            'enabled="true" bounds="[80,260][420,360]"/>'
            '<node class="android.widget.TextView" text="满减券" '
            'enabled="true" bounds="[80,900][280,980]"/>'
            '<node class="android.widget.TextView" text="新会员专享" '
            'enabled="true" bounds="[80,1000][420,1080]"/>'
            '<node class="android.view.ViewGroup" content-desc="去使用" '
            'clickable="true" enabled="true" bounds="[700,900][980,1020]"/>'
        )
        haier = build_page_model(
            xml,
            package_name="com.ehaier.zgq.shop.mall",
            activity="com.ehaier.mall.MainActivity",
        )
        unrelated_app = build_page_model(
            xml,
            package_name="com.demo",
            activity=".MainActivity",
        )

        self.assertEqual(haier.page_subtype, "MEMBER_BENEFITS")
        self.assertNotEqual(unrelated_app.page_subtype, "MEMBER_BENEFITS")

    def test_haier_rights_hub_is_member_benefits(self):
        page = build_page_model(
            _page(
                '<node class="android.widget.TextView" text="我的权益" '
                'enabled="true" bounds="[400,80][680,180]"/>'
                '<node class="android.widget.TextView" text="796积分" '
                'enabled="true" bounds="[80,300][360,410]"/>'
                '<node class="android.view.ViewGroup" content-desc="我的优惠券, 3" '
                'clickable="true" enabled="true" bounds="[60,700][1020,840]"/>'
                '<node class="android.view.ViewGroup" content-desc="生态权益, 0" '
                'clickable="true" enabled="true" bounds="[60,1100][1020,1240]"/>'
            ),
            package_name="com.ehaier.zgq.shop.mall",
            activity="com.ehaier.mall.MainActivity",
        )

        self.assertEqual(page.page_subtype, "MEMBER_BENEFITS")

    def test_haier_favorites_and_history_are_explicit_list_subtypes(self):
        favorites = build_page_model(
            _page(
                '<node class="android.widget.TextView" text="商品收藏" '
                'enabled="true" bounds="[400,80][680,180]"/>'
                '<node class="android.view.ViewGroup" content-desc="管理" '
                'clickable="true" enabled="true" bounds="[900,80][1060,180]"/>'
                '<node class="android.view.ViewGroup" content-desc="全部(39)" '
                'clickable="true" enabled="true" bounds="[40,220][260,340]"/>'
                '<node class="android.view.ViewGroup" content-desc="Haier/海尔 冰箱" '
                'clickable="true" enabled="true" bounds="[40,400][1040,800]"/>'
            ),
            package_name="com.ehaier.zgq.shop.mall",
            activity="com.ehaier.mall.MainActivity",
        )
        history = build_page_model(
            _page(
                '<node class="android.widget.TextView" text="历史浏览" '
                'enabled="true" bounds="[400,80][680,180]"/>'
                '<node class="android.view.ViewGroup" content-desc="商品浏览" '
                'clickable="true" enabled="true" bounds="[80,220][320,340]"/>'
                '<node class="android.view.ViewGroup" content-desc="线下门店" '
                'clickable="true" enabled="true" bounds="[360,220][600,340]"/>'
                '<node class="android.widget.TextView" text="今天" '
                'enabled="true" bounds="[40,380][180,460]"/>'
            ),
            package_name="com.ehaier.zgq.shop.mall",
            activity="com.ehaier.mall.MainActivity",
        )

        self.assertEqual((favorites.role, favorites.page_subtype), ("LIST", "FAVORITES"))
        self.assertEqual(
            (history.role, history.page_subtype),
            ("LIST", "BROWSING_HISTORY"),
        )

    def test_settings_and_address_pages_do_not_become_checkout(self):
        settings = build_page_model(
            _page(
                '<node class="android.widget.TextView" text="设置" enabled="true" '
                'bounds="[420,80][660,160]"/>'
                '<node class="android.widget.TextView" text="账号与安全" enabled="true" '
                'bounds="[40,300][400,380]"/>'
                '<node class="android.widget.TextView" text="隐私设置" enabled="true" '
                'bounds="[40,420][400,500]"/>'
            ),
            package_name="com.demo",
            activity=".Main",
        )
        address = build_page_model(
            _page(
                '<node class="android.widget.TextView" text="收货地址" enabled="true" '
                'bounds="[420,80][660,160]"/>'
                '<node class="android.view.ViewGroup" content-desc="全部" '
                'clickable="true" enabled="true" bounds="[40,300][180,420]"/>'
                '<node class="android.view.ViewGroup" content-desc="修改" '
                'clickable="true" enabled="true" bounds="[800,600][1000,720]"/>'
                '<node class="android.view.ViewGroup" content-desc="修改" '
                'clickable="true" enabled="true" bounds="[800,800][1000,920]"/>'
            ),
            package_name="com.demo",
            activity=".Main",
        )
        self.assertEqual(settings.role, "SETTINGS")
        self.assertEqual(settings.page_subtype, "SETTINGS")
        self.assertEqual(address.role, "LIST")
        self.assertEqual(address.page_subtype, "ADDRESS_LIST")

        address_form = build_page_model(
            _page(
                '<node class="android.widget.TextView" text="新建收货地址" '
                'enabled="true" bounds="[360,80][720,160]"/>'
                '<node class="android.widget.TextView" text="收货人姓名" '
                'enabled="true" bounds="[40,300][300,380]"/>'
                '<node class="android.widget.TextView" text="所在地区" '
                'enabled="true" bounds="[40,500][300,580]"/>'
                '<node class="android.widget.TextView" text="详细地址" '
                'enabled="true" bounds="[40,700][300,780]"/>'
                '<node class="android.widget.Button" text="保存" clickable="true" '
                'enabled="true" bounds="[100,2000][980,2200]"/>'
            ),
            package_name="com.demo",
            activity=".Main",
        )
        form_actions = enumerate_actions(
            address_form,
            screen_size=(1080, 2400),
            coverage_scheduler_v2=True,
        )
        save = next(
            action for action in form_actions if action.action_role == "COMMAND:SAVE"
        )
        self.assertEqual(address_form.role, "SETTINGS")
        self.assertEqual(address_form.page_subtype, "ADDRESS_FORM")
        self.assertEqual(save.risk_type, "EXTERNAL_SIDE_EFFECT")
        self.assertFalse(save.replayable)

        invoice_form = build_page_model(
            _page(
                '<node class="android.widget.TextView" text="添加抬头" enabled="true" '
                'bounds="[420,80][660,160]"/>'
                '<node class="android.widget.TextView" text="发票类型" enabled="true" '
                'bounds="[40,300][300,380]"/>'
                '<node class="android.widget.TextView" text="发票抬头" enabled="true" '
                'bounds="[40,500][300,580]"/>'
                '<node class="android.widget.EditText" text="请输入个人/单位名称" '
                'editable="true" enabled="true" bounds="[300,500][1000,620]"/>'
                '<node class="android.widget.Button" text="确定添加" clickable="true" '
                'enabled="true" bounds="[100,2000][980,2200]"/>'
            ),
            package_name="com.demo",
            activity=".Main",
        )
        invoice_actions = enumerate_actions(
            invoice_form,
            screen_size=(1080, 2400),
            coverage_scheduler_v2=True,
        )
        confirm = next(
            action
            for action in invoice_actions
            if action.action_role == "COMMAND:CONFIRM"
        )
        self.assertEqual(invoice_form.page_subtype, "INVOICE_FORM")
        self.assertEqual(confirm.risk_type, "EXTERNAL_SIDE_EFFECT")

    def test_two_pane_category_hub_converges_without_sort_or_filter_controls(self):
        def category_hub(*, selected_index: int, entries: tuple[str, ...]) -> str:
            side_labels = ("热门分类", "新品", "冰箱", "洗衣机", "空调", "热水器")
            side_nodes = []
            for index, label in enumerate(side_labels):
                top = 240 + index * 144
                indicator = (
                    '<node class="android.view.ViewGroup" enabled="true" '
                    f'bounds="[0,{top + 9}][12,{top + 135}]"/>'
                    if index == selected_index
                    else ""
                )
                side_nodes.append(
                    '<node class="android.view.ViewGroup" content-desc="{}" '
                    'clickable="true" enabled="true" bounds="[0,{}][253,{}]">'
                    '{}<node class="android.widget.TextView" text="{}" '
                    'enabled="true" bounds="[0,{}][253,{}]"/></node>'.format(
                        label,
                        top,
                        top + 144,
                        indicator,
                        label,
                        top + 46,
                        top + 98,
                    )
                )
            entry_nodes = []
            for index, label in enumerate(entries):
                left = 275 + index * 268
                entry_nodes.append(
                    '<node class="android.view.ViewGroup" content-desc="{}" '
                    'clickable="true" enabled="true" bounds="[{},630][{},918]">'
                    '<node class="android.widget.ImageView" enabled="true" '
                    'bounds="[{},650][{},850]"/>'
                    '<node class="android.widget.TextView" text="{}" enabled="true" '
                    'bounds="[{},860][{},910]"/></node>'.format(
                        label,
                        left,
                        left + 236,
                        left + 20,
                        left + 216,
                        label,
                        left,
                        left + 236,
                    )
                )
            return _page(
                '<node class="android.widget.TextView" text="分类" enabled="true" '
                'bounds="[480,80][600,160]"/>'
                '<node class="androidx.recyclerview.widget.RecyclerView" '
                'scrollable="true" enabled="true" bounds="[0,0][1080,2364]">'
                '<node class="android.view.ViewGroup" enabled="true" '
                'bounds="[0,240][253,2217]">'
                + "".join(side_nodes)
                + '</node><node class="android.widget.ScrollView" scrollable="true" '
                'enabled="true" bounds="[275,240][1047,2217]">'
                + "".join(entry_nodes)
                + "</node></node>"
            )

        first = build_page_model(
            category_hub(selected_index=0, entries=("海尔", "卡萨帝")),
            package_name="com.demo",
            activity=".Main",
        )
        second = build_page_model(
            category_hub(selected_index=2, entries=("冷柜", "冰吧")),
            package_name="com.demo",
            activity=".Main",
        )
        actions = enumerate_actions(
            first,
            screen_size=(1080, 2400),
            coverage_scheduler_v2=True,
        )
        side_tabs = [
            action for action in actions if action.action_role == "CATEGORY_TAB:side"
        ]
        grid_entries = [
            action for action in actions if action.action_role == "ITEM_OPEN:collection"
        ]
        scrolls = [action for action in actions if action.action_type == "scroll"]

        self.assertEqual(first.page_subtype, "CATALOG_CATEGORY")
        self.assertEqual(second.page_subtype, "CATALOG_CATEGORY")
        self.assertEqual(len(side_tabs), 5)
        self.assertNotIn("热门分类", {action.target_meta["text"] for action in side_tabs})
        self.assertEqual({action.sample_policy for action in side_tabs}, {"FAMILY_ONE"})
        self.assertEqual(len({action.action_group_key for action in side_tabs}), 1)
        self.assertEqual(len(grid_entries), 2)
        self.assertEqual(
            {action.sample_policy for action in grid_entries},
            {"FAMILY_TWO_SAMPLES"},
        )
        self.assertEqual(len({action.action_group_key for action in grid_entries}), 1)
        self.assertEqual(len(scrolls), 1)
        self.assertEqual(scrolls[0].target_meta["bounds"], [275, 240, 1047, 2217])
        self.assertTrue(compare_exploration_families(first, second).equivalent)

    def test_home_visual_actions_deduplicate_and_revalidate_crop(self):
        image = Image.new("RGB", (100, 200), "white")
        for x in range(10, 90):
            for y in range(40, 120):
                image.putpixel((x, y), (0, 0, 0) if x < 50 else (255, 255, 255))
        source = io.BytesIO()
        image.save(source, format="PNG")
        screenshot = source.getvalue()
        xml = (
            '<hierarchy><node class="android.widget.FrameLayout" enabled="true" '
            'bounds="[0,0][100,200]">'
            '<node class="android.widget.TextView" text="首页" enabled="true" '
            'bounds="[35,5][65,20]"/>'
            '<node class="android.view.ViewGroup" clickable="true" enabled="true" '
            'bounds="[10,40][90,120]">'
            '<node class="android.view.ViewGroup" clickable="true" enabled="true" '
            'bounds="[12,42][88,118]">'
            '<node class="android.widget.ImageView" enabled="true" bounds="[12,42][88,118]"/>'
            '</node></node></node></hierarchy>'
        )
        page = build_page_model(xml, package_name="com.demo", activity=".Main")
        visual = [
            item
            for item in enumerate_actions(
                page,
                screen_size=(100, 200),
                screenshot_png=screenshot,
                enable_visual_home_actions=True,
            )
            if str(item.action_role).startswith("VISUAL_HOME:")
        ]
        self.assertEqual(len(visual), 1)
        self.assertTrue(visual[0].coordinate_only)
        self.assertTrue(visual[0].target_meta["coordinate_authorized"])
        self.assertTrue(visual_locator_matches(visual[0], page, screenshot))

        changed = Image.new("RGB", (100, 200), "white")
        for x in range(10, 90):
            for y in range(40, 120):
                changed.putpixel((x, y), (255, 255, 255) if x < 50 else (0, 0, 0))
        changed_bytes = io.BytesIO()
        changed.save(changed_bytes, format="PNG")
        self.assertFalse(visual_locator_matches(visual[0], page, changed_bytes.getvalue()))

    def test_sanitizer_removes_secret_xml_and_blurs_image(self):
        xml = _page(
            '<node class="android.widget.EditText" content-desc="密码" '
            'text="my-real-secret" password="true" enabled="true" '
            'bounds="[10,10][200,100]"/>'
        )
        image = Image.new("RGB", (300, 200), "red")
        source = io.BytesIO()
        image.save(source, format="PNG")
        result = InspectionArtifactSanitizer().sanitize(xml, source.getvalue())
        self.assertNotIn("my-real-secret", result.xml)
        self.assertNotIn('content-desc="密码"', result.xml)
        self.assertTrue(result.screenshot_png)
        self.assertEqual(result.sensitive_regions, [(10, 10, 200, 100)])


if __name__ == "__main__":
    unittest.main()
