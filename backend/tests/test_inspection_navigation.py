import json
import unittest
from typing import Iterable, Optional

from backend.inspection.semantics import (
    build_page_model,
    confirm_peer_navigation,
    discover_navigation_groups,
    enumerate_actions,
)


SCREEN_SIZE = (1080, 2400)
PACKAGE = "com.demo"


def _page(body: str, *, title: str = "页面") -> str:
    return (
        '<hierarchy rotation="0">'
        f'<node package="{PACKAGE}" class="android.widget.FrameLayout" '
        'enabled="true" bounds="[0,0][1080,2400]">'
        f'<node package="{PACKAGE}" class="android.widget.TextView" '
        f'text="{title}" enabled="true" bounds="[20,300][800,400]"/>'
        f"{body}"
        "</node>"
        "</hierarchy>"
    )


def _bottom_navigation(
    labels: Iterable[str],
    *,
    selected_index: Optional[int] = None,
    checked_index: Optional[int] = None,
    dynamic_cart_count: Optional[int] = None,
    cart_badge_style: str = "comma",
) -> str:
    labels = list(labels)
    item_width = 1080 // len(labels)
    items = []
    for index, label in enumerate(labels):
        x1 = index * item_width
        x2 = 1080 if index == len(labels) - 1 else (index + 1) * item_width
        selected = "true" if index == selected_index else "false"
        checked = "true" if index == checked_index else "false"
        if label == "购物车" and dynamic_cart_count is not None:
            description = (
                f"{label}({dynamic_cart_count})"
                if cart_badge_style == "parentheses"
                else f"{label}, {dynamic_cart_count}"
            )
        else:
            description = label
        child = (
            f'<node package="{PACKAGE}" class="android.widget.TextView" '
            f'text="{label}" enabled="true" bounds="[{x1},2260][{x2},2340]"/>'
            if label == "购物车" and dynamic_cart_count is not None
            else ""
        )
        items.append(
            f'<node package="{PACKAGE}" class="android.view.ViewGroup" '
            f'content-desc="{description}" clickable="true" enabled="true" '
            f'selected="{selected}" checked="{checked}" '
            f'bounds="[{x1},2160][{x2},2380]">'
            f"{child}</node>"
        )
    return (
        f'<node package="{PACKAGE}" class="android.view.ViewGroup" '
        'enabled="true" bounds="[0,2140][1080,2400]">'
        f'{"".join(items)}</node>'
    )


def _top_tabs(
    *,
    selected_index: Optional[int] = None,
    indicator_index: Optional[int] = None,
) -> str:
    labels = ("首页", "附近门店")
    items = []
    for index, label in enumerate(labels):
        x1 = 180 + index * 360
        x2 = x1 + 360
        selected = "true" if index == selected_index else "false"
        items.append(
            f'<node package="{PACKAGE}" class="android.view.ViewGroup" '
            f'content-desc="{label}" clickable="true" enabled="true" '
            f'selected="{selected}" bounds="[{x1},120][{x2},240]"/>'
        )
        if index == indicator_index:
            items.append(
                f'<node package="{PACKAGE}" class="android.view.ViewGroup" '
                f'enabled="true" bounds="[{x1 + 120},225][{x1 + 240},233]"/>'
            )
    return (
        f'<node package="{PACKAGE}" class="android.widget.LinearLayout" '
        'enabled="true" bounds="[160,100][920,260]">'
        f'{"".join(items)}</node>'
    )


def _navigation_action(
    page,
    member_index: int,
    *,
    include_current_navigation: bool = False,
):
    return next(
        action
        for action in enumerate_actions(
            page,
            screen_size=SCREEN_SIZE,
            include_current_navigation=include_current_navigation,
        )
        if action.target_meta.get("navigation", {}).get("member_index")
        == member_index
    )


class InspectionNavigationTests(unittest.TestCase):
    def test_react_native_five_item_bottom_bar_is_serializable(self):
        labels = ["首页", "分类", "许愿池", "购物车", "我的"]
        page = build_page_model(
            _page(_bottom_navigation(labels, selected_index=0), title="精选"),
            package_name=PACKAGE,
            activity=".MainActivity",
        )

        groups = discover_navigation_groups(page, screen_size=SCREEN_SIZE)

        self.assertEqual(len(groups), 1)
        self.assertEqual(groups[0].region, "bottom")
        self.assertEqual(list(groups[0].labels), labels)
        self.assertGreaterEqual(groups[0].coverage, 0.70)
        self.assertEqual(groups[0].active_member_count, 1)
        json.dumps(groups[0].to_dict(), ensure_ascii=False)

        actions = enumerate_actions(page, screen_size=SCREEN_SIZE)
        navigation_actions = [
            action for action in actions if "navigation" in action.target_meta
        ]
        self.assertEqual(len(navigation_actions), 4)
        self.assertNotIn(
            0,
            {
                action.target_meta["navigation"]["member_index"]
                for action in navigation_actions
            },
        )
        recovery_navigation_actions = [
            action
            for action in enumerate_actions(
                page,
                screen_size=SCREEN_SIZE,
                include_current_navigation=True,
            )
            if "navigation" in action.target_meta
        ]
        self.assertEqual(len(recovery_navigation_actions), 5)
        self.assertTrue(
            next(
                action
                for action in recovery_navigation_actions
                if action.target_meta["navigation"]["member_index"] == 0
            ).target_meta["navigation"]["member"]["active"]
        )
        metadata = next(
            action
            for action in navigation_actions
            if action.target_meta["navigation"]["member_index"] == 2
        ).target_meta["navigation"]
        self.assertEqual(metadata["member_index"], 2)
        self.assertEqual(metadata["member_count"], 5)
        self.assertEqual(len(metadata["members"]), 5)
        self.assertEqual(metadata["group_key"], groups[0].group_key)
        serialized = json.dumps(metadata, ensure_ascii=False)
        for label in labels:
            self.assertNotIn(label, serialized)
        self.assertNotIn(PACKAGE, serialized)
        self.assertNotIn(".MainActivity", serialized)
        self.assertNotIn("member_label", metadata)

    def test_top_double_tab_is_confirmed_without_selected_state(self):
        source = build_page_model(
            _page(_top_tabs(), title="首页内容"),
            package_name=PACKAGE,
            activity=".MainActivity",
        )
        target = build_page_model(
            _page(_top_tabs(), title="附近门店内容"),
            package_name=PACKAGE,
            activity=".MainActivity",
        )
        source_groups = discover_navigation_groups(source, screen_size=SCREEN_SIZE)
        self.assertEqual(len(source_groups), 1)
        self.assertEqual(source_groups[0].region, "top")
        self.assertEqual(source_groups[0].active_member_count, 0)
        action = next(
            item
            for item in enumerate_actions(source, screen_size=SCREEN_SIZE)
            if item.target_meta.get("navigation", {}).get("member_index") == 1
        )

        confirmation = confirm_peer_navigation(
            action.target_meta,
            target,
            screen_size=SCREEN_SIZE,
        )

        self.assertTrue(confirmation.matched)
        self.assertGreaterEqual(confirmation.confidence, 0.85)
        self.assertEqual(confirmation.group_key, source_groups[0].group_key)
        self.assertEqual(confirmation.evidence["label_overlap"], 1.0)
        self.assertFalse(confirmation.evidence["active_signal_bonus"])
        json.dumps(confirmation.to_dict(), ensure_ascii=False)

    def test_dynamic_cart_description_falls_back_to_stable_child_text(self):
        labels = ["首页", "许愿池", "购物车", "我的"]
        page = build_page_model(
            _page(
                _bottom_navigation(labels, dynamic_cart_count=20),
                title="首页内容",
            ),
            package_name=PACKAGE,
            activity=".MainActivity",
        )

        group = discover_navigation_groups(page, screen_size=SCREEN_SIZE)[0]

        self.assertEqual(group.labels[2], "购物车")
        cart_action = next(
            action
            for action in enumerate_actions(page, screen_size=SCREEN_SIZE)
            if action.target_meta.get("navigation", {}).get("member_index") == 2
        )
        metadata = cart_action.target_meta["navigation"]
        self.assertNotIn("购物车", json.dumps(metadata, ensure_ascii=False))
        self.assertEqual(metadata["members"][2]["member_key"], metadata["member_key"])

    def test_sibling_indicator_marks_exactly_one_active_top_tab(self):
        page = build_page_model(
            _page(_top_tabs(indicator_index=1), title="附近门店内容"),
            package_name=PACKAGE,
            activity=".MainActivity",
        )

        group = discover_navigation_groups(page, screen_size=SCREEN_SIZE)[0]

        self.assertEqual(group.active_member_count, 1)
        self.assertEqual(
            [member.label for member in group.members if member.active],
            ["附近门店"],
        )

    def test_product_operation_bar_is_not_navigation(self):
        operation_bar = (
            f'<node package="{PACKAGE}" class="android.widget.LinearLayout" '
            'enabled="true" bounds="[0,2140][1080,2400]">'
            f'<node package="{PACKAGE}" class="android.widget.Button" '
            'text="购物车" clickable="true" enabled="true" '
            'bounds="[0,2160][360,2380]"/>'
            f'<node package="{PACKAGE}" class="android.widget.Button" '
            'text="加入购物车" clickable="true" enabled="true" '
            'bounds="[360,2160][720,2380]"/>'
            f'<node package="{PACKAGE}" class="android.widget.Button" '
            'text="立即购买" clickable="true" enabled="true" '
            'bounds="[720,2160][1080,2380]"/>'
            "</node>"
        )
        page = build_page_model(
            _page(operation_bar, title="商品详情"),
            package_name=PACKAGE,
            activity=".ProductActivity",
        )

        self.assertEqual(
            discover_navigation_groups(page, screen_size=SCREEN_SIZE),
            [],
        )
        self.assertTrue(
            all(
                "navigation" not in action.target_meta
                for action in enumerate_actions(page, screen_size=SCREEN_SIZE)
            )
        )

    def test_dialog_buttons_are_not_navigation(self):
        dialog = (
            f'<node package="{PACKAGE}" class="android.app.Dialog" '
            'enabled="true" bounds="[40,1700][1040,2390]">'
            f'<node package="{PACKAGE}" class="android.widget.LinearLayout" '
            'enabled="true" bounds="[54,2100][1026,2360]">'
            f'<node package="{PACKAGE}" class="android.widget.Button" text="取消" '
            'clickable="true" enabled="true" bounds="[54,2120][378,2340]"/>'
            f'<node package="{PACKAGE}" class="android.widget.Button" text="稍后" '
            'clickable="true" enabled="true" bounds="[378,2120][702,2340]"/>'
            f'<node package="{PACKAGE}" class="android.widget.Button" text="确定" '
            'clickable="true" enabled="true" bounds="[702,2120][1026,2340]"/>'
            "</node></node>"
        )
        page = build_page_model(
            _page(dialog, title="弹窗背景"),
            package_name=PACKAGE,
            activity=".MainActivity",
        )

        self.assertEqual(
            discover_navigation_groups(page, screen_size=SCREEN_SIZE),
            [],
        )

    def test_filter_chips_and_sort_bar_are_not_navigation(self):
        chips = "".join(
            f'<node package="{PACKAGE}" class="android.view.ViewGroup" '
            f'content-desc="{label}" clickable="true" enabled="true" '
            f'bounds="[{index * 216},220][{(index + 1) * 216},460]"/>'
            for index, label in enumerate(("多门", "T型", "三门", "对开门"))
        )
        sort_items = "".join(
            f'<node package="{PACKAGE}" class="android.view.ViewGroup" '
            f'content-desc="{label}" clickable="true" enabled="true" '
            f'bounds="[{index * 270},520][{(index + 1) * 270},620]"/>'
            for index, label in enumerate(("综合", "销量", "价格", "筛选"))
        )
        body = (
            f'<node package="{PACKAGE}" class="android.widget.HorizontalScrollView" '
            'scrollable="true" enabled="true" bounds="[0,200][1080,480]">'
            f'<node package="{PACKAGE}" class="android.view.ViewGroup" enabled="true" '
            f'bounds="[0,220][864,460]">{chips}</node></node>'
            f'<node package="{PACKAGE}" class="android.view.ViewGroup" enabled="true" '
            f'bounds="[0,500][1080,640]">{sort_items}</node>'
        )
        page = build_page_model(
            _page(body, title="商品列表"),
            package_name=PACKAGE,
            activity=".CategoryActivity",
        )

        self.assertEqual(
            discover_navigation_groups(page, screen_size=SCREEN_SIZE),
            [],
        )

    def test_missing_target_group_does_not_confirm_peer_navigation(self):
        labels = ["首页", "分类", "许愿池", "购物车", "我的"]
        source = build_page_model(
            _page(_bottom_navigation(labels), title="首页内容"),
            package_name=PACKAGE,
            activity=".MainActivity",
        )
        target = build_page_model(
            _page("", title="独立详情页"),
            package_name=PACKAGE,
            activity=".DetailActivity",
        )
        action = next(
            item
            for item in enumerate_actions(source, screen_size=SCREEN_SIZE)
            if item.target_meta.get("navigation", {}).get("member_index") == 4
        )

        confirmation = confirm_peer_navigation(
            action.target_meta["navigation"],
            target,
            screen_size=SCREEN_SIZE,
        )

        self.assertFalse(confirmation.matched)
        self.assertIsNone(confirmation.target_group)
        self.assertTrue(confirmation.evidence["same_package"])
        self.assertTrue(confirmation.evidence["page_changed"])

    def test_selected_only_bottom_action_bar_change_is_not_a_peer_page(self):
        labels = ["收藏", "客服", "分享"]
        source = build_page_model(
            _page(_bottom_navigation(labels, selected_index=0), title="商品详情"),
            package_name=PACKAGE,
            activity=".ProductActivity",
        )
        target = build_page_model(
            _page(_bottom_navigation(labels, selected_index=1), title="商品详情"),
            package_name=PACKAGE,
            activity=".ProductActivity",
        )

        confirmation = confirm_peer_navigation(
            _navigation_action(source, 1).target_meta,
            target,
            screen_size=SCREEN_SIZE,
        )

        self.assertFalse(confirmation.matched)
        self.assertFalse(confirmation.evidence["page_changed"])

    def test_indicator_only_change_is_not_a_peer_page(self):
        source = build_page_model(
            _page(_top_tabs(indicator_index=0), title="相同内容"),
            package_name=PACKAGE,
            activity=".MainActivity",
        )
        target = build_page_model(
            _page(_top_tabs(indicator_index=1), title="相同内容"),
            package_name=PACKAGE,
            activity=".MainActivity",
        )

        confirmation = confirm_peer_navigation(
            _navigation_action(
                source,
                1,
                include_current_navigation=True,
            ).target_meta,
            target,
            screen_size=SCREEN_SIZE,
        )

        self.assertFalse(confirmation.matched)
        self.assertFalse(confirmation.evidence["page_changed"])

    def test_checked_only_change_is_not_a_peer_page(self):
        labels = ["首页", "分类", "许愿池", "购物车", "我的"]
        source = build_page_model(
            _page(_bottom_navigation(labels, checked_index=0), title="相同内容"),
            package_name=PACKAGE,
            activity=".MainActivity",
        )
        target = build_page_model(
            _page(_bottom_navigation(labels, checked_index=1), title="相同内容"),
            package_name=PACKAGE,
            activity=".MainActivity",
        )

        confirmation = confirm_peer_navigation(
            _navigation_action(source, 1).target_meta,
            target,
            screen_size=SCREEN_SIZE,
        )

        self.assertFalse(confirmation.matched)
        self.assertFalse(confirmation.evidence["page_changed"])

    def test_badge_only_change_is_not_a_peer_page(self):
        labels = ["首页", "许愿池", "购物车", "我的"]
        source = build_page_model(
            _page(
                _bottom_navigation(labels, dynamic_cart_count=20),
                title="相同内容",
            ),
            package_name=PACKAGE,
            activity=".MainActivity",
        )
        target = build_page_model(
            _page(
                _bottom_navigation(labels, dynamic_cart_count=21),
                title="相同内容",
            ),
            package_name=PACKAGE,
            activity=".MainActivity",
        )

        confirmation = confirm_peer_navigation(
            _navigation_action(source, 2).target_meta,
            target,
            screen_size=SCREEN_SIZE,
        )

        self.assertFalse(confirmation.matched)
        self.assertFalse(confirmation.evidence["page_changed"])

    def test_parenthesized_cart_badge_uses_stable_child_and_safe_metadata(self):
        labels = ["首页", "许愿池", "购物车", "我的"]
        page = build_page_model(
            _page(
                _bottom_navigation(
                    labels,
                    dynamic_cart_count=20,
                    cart_badge_style="parentheses",
                ),
                title="首页内容",
            ),
            package_name=PACKAGE,
            activity=".MainActivity",
        )

        group = discover_navigation_groups(page, screen_size=SCREEN_SIZE)[0]
        metadata = _navigation_action(page, 2).target_meta["navigation"]
        serialized = json.dumps(metadata, ensure_ascii=False)

        self.assertEqual(group.labels[2], "购物车")
        self.assertNotIn("购物车", serialized)
        self.assertNotIn("20", serialized)
        self.assertNotIn("label", serialized)
        self.assertNotIn("group_signature", metadata)
        self.assertEqual(metadata["member_count"], 4)

    def test_top_toolbar_without_active_signal_is_not_a_tab_group(self):
        items = "".join(
            f'<node package="{PACKAGE}" class="android.view.ViewGroup" '
            f'content-desc="{label}" clickable="true" enabled="true" '
            f'bounds="[{index * 360},100][{(index + 1) * 360},240]"/>'
            for index, label in enumerate(("返回", "搜索", "更多"))
        )
        toolbar = (
            f'<node package="{PACKAGE}" class="android.widget.LinearLayout" '
            f'enabled="true" bounds="[0,80][1080,260]">{items}</node>'
        )
        page = build_page_model(
            _page(toolbar, title="工具页"),
            package_name=PACKAGE,
            activity=".ToolActivity",
        )

        self.assertEqual(
            discover_navigation_groups(page, screen_size=SCREEN_SIZE),
            [],
        )

    def test_target_unique_active_must_be_the_clicked_member(self):
        labels = ["首页", "分类", "许愿池", "购物车", "我的"]
        source = build_page_model(
            _page(_bottom_navigation(labels, selected_index=0), title="首页内容"),
            package_name=PACKAGE,
            activity=".MainActivity",
        )
        target = build_page_model(
            _page(_bottom_navigation(labels, selected_index=0), title="分类内容"),
            package_name=PACKAGE,
            activity=".MainActivity",
        )

        confirmation = confirm_peer_navigation(
            _navigation_action(source, 1).target_meta,
            target,
            screen_size=SCREEN_SIZE,
        )

        self.assertFalse(confirmation.matched)
        self.assertTrue(confirmation.evidence["page_changed"])
        self.assertFalse(confirmation.evidence["active_state_valid"])
        self.assertFalse(confirmation.evidence["clicked_unique_active"])

    def test_clicking_the_already_active_tab_is_not_a_peer_switch(self):
        labels = ["首页", "分类", "许愿池", "购物车", "我的"]
        source = build_page_model(
            _page(_bottom_navigation(labels, selected_index=1), title="分类内容 A"),
            package_name=PACKAGE,
            activity=".MainActivity",
        )
        target = build_page_model(
            _page(_bottom_navigation(labels, selected_index=1), title="分类内容 B"),
            package_name=PACKAGE,
            activity=".MainActivity",
        )

        confirmation = confirm_peer_navigation(
            _navigation_action(
                source,
                1,
                include_current_navigation=True,
            ).target_meta,
            target,
            screen_size=SCREEN_SIZE,
        )

        self.assertFalse(confirmation.matched)
        self.assertTrue(confirmation.evidence["page_changed"])
        self.assertFalse(confirmation.evidence["active_state_valid"])

    def test_source_without_active_accepts_target_unique_clicked_member(self):
        labels = ["首页", "分类", "许愿池", "购物车", "我的"]
        source = build_page_model(
            _page(_bottom_navigation(labels), title="首页内容"),
            package_name=PACKAGE,
            activity=".MainActivity",
        )
        target = build_page_model(
            _page(_bottom_navigation(labels, selected_index=1), title="分类内容"),
            package_name=PACKAGE,
            activity=".MainActivity",
        )

        confirmation = confirm_peer_navigation(
            _navigation_action(source, 1).target_meta,
            target,
            screen_size=SCREEN_SIZE,
        )

        self.assertTrue(confirmation.matched)
        self.assertTrue(confirmation.evidence["clicked_unique_active"])

    def test_bottom_navigation_without_active_confirms_on_content_change(self):
        labels = ["首页", "分类", "许愿池", "购物车", "我的"]
        source = build_page_model(
            _page(_bottom_navigation(labels), title="首页内容"),
            package_name=PACKAGE,
            activity=".MainActivity",
        )
        target = build_page_model(
            _page(_bottom_navigation(labels), title="分类内容"),
            package_name=PACKAGE,
            activity=".MainActivity",
        )

        confirmation = confirm_peer_navigation(
            _navigation_action(source, 1).target_meta,
            target,
            screen_size=SCREEN_SIZE,
        )

        self.assertTrue(confirmation.matched)
        self.assertEqual(confirmation.evidence["source_active_member_count"], 0)
        self.assertEqual(confirmation.evidence["target_active_member_count"], 0)
        serialized = json.dumps(confirmation.to_dict(), ensure_ascii=False)
        for label in labels:
            self.assertNotIn(label, serialized)
        confirmation_payload = confirmation.to_dict()
        self.assertNotIn("target_group", confirmation_payload)
        self.assertIn("target_group_evidence", confirmation_payload)

    def test_navigation_and_page_content_can_share_one_react_native_parent(self):
        labels = ["首页", "分类", "许愿池", "购物车", "我的"]
        item_width = 1080 // len(labels)
        navigation = "".join(
            f'<node package="{PACKAGE}" class="android.view.ViewGroup" '
            f'content-desc="{label}" clickable="true" enabled="true" '
            f'bounds="[{index * item_width},2160]'
            f'[{(index + 1) * item_width},2380]"/>'
            for index, label in enumerate(labels)
        )
        source = build_page_model(
            _page(navigation, title="首页正文"),
            package_name=PACKAGE,
            activity=".MainActivity",
        )
        target = build_page_model(
            _page(navigation, title="分类正文"),
            package_name=PACKAGE,
            activity=".MainActivity",
        )

        confirmation = confirm_peer_navigation(
            _navigation_action(source, 1).target_meta,
            target,
            screen_size=SCREEN_SIZE,
        )

        self.assertTrue(confirmation.matched)
        self.assertTrue(confirmation.evidence["page_changed"])

    def test_modal_target_is_not_confirmed_even_with_navigation_behind_it(self):
        labels = ["首页", "分类", "许愿池", "购物车", "我的"]
        source = build_page_model(
            _page(_bottom_navigation(labels), title="首页内容"),
            package_name=PACKAGE,
            activity=".MainActivity",
        )
        modal = (
            f'<node package="{PACKAGE}" class="android.app.Dialog" '
            'enabled="true" bounds="[100,600][980,1800]">'
            f'<node package="{PACKAGE}" class="android.widget.TextView" '
            'text="活动提示" enabled="true" bounds="[200,800][800,900]"/>'
            "</node>"
        )
        target = build_page_model(
            _page(_bottom_navigation(labels) + modal, title="分类内容"),
            package_name=PACKAGE,
            activity=".MainActivity",
        )

        confirmation = confirm_peer_navigation(
            _navigation_action(source, 1).target_meta,
            target,
            screen_size=SCREEN_SIZE,
        )

        self.assertFalse(confirmation.matched)
        self.assertTrue(confirmation.evidence["target_modal"])

    def test_accessible_opaque_container_tabs_are_not_navigation(self):
        tabs = _top_tabs(selected_index=0)
        for class_name in (
            "android.webkit.WebView",
            "android.view.SurfaceView",
            "android.graphics.Canvas",
        ):
            with self.subTest(class_name=class_name):
                opaque = (
                    f'<node package="{PACKAGE}" class="{class_name}" '
                    f'enabled="true" bounds="[0,0][1080,2000]">{tabs}</node>'
                )
                page = build_page_model(
                    _page(opaque, title="容器页"),
                    package_name=PACKAGE,
                    activity=".OpaqueActivity",
                )
                self.assertEqual(
                    discover_navigation_groups(page, screen_size=SCREEN_SIZE),
                    [],
                )

    def test_bottom_horizontal_cards_near_scrollable_are_not_navigation(self):
        cards = "".join(
            f'<node package="{PACKAGE}" class="android.view.ViewGroup" '
            f'content-desc="{label}" clickable="true" enabled="true" '
            f'bounds="[{index * 360},2140][{(index + 1) * 360},2380]"/>'
            for index, label in enumerate(("门店卡片", "商品卡片", "活动卡片"))
        )
        carousel = (
            f'<node package="{PACKAGE}" class="android.widget.HorizontalScrollView" '
            'scrollable="true" enabled="true" bounds="[0,2100][1080,2400]">'
            f'<node package="{PACKAGE}" class="android.view.ViewGroup" '
            f'enabled="true" bounds="[0,2120][1080,2400]">{cards}</node></node>'
        )
        page = build_page_model(
            _page(carousel, title="推荐页"),
            package_name=PACKAGE,
            activity=".RecommendationActivity",
        )

        self.assertEqual(
            discover_navigation_groups(page, screen_size=SCREEN_SIZE),
            [],
        )

    def test_single_item_wrappers_share_the_navigation_container(self):
        labels = ["首页", "分类", "许愿池", "购物车", "我的"]
        item_width = 1080 // len(labels)
        wrapped_items = []
        for index, label in enumerate(labels):
            x1 = index * item_width
            x2 = 1080 if index == len(labels) - 1 else (index + 1) * item_width
            wrapped_items.append(
                f'<node package="{PACKAGE}" class="android.widget.FrameLayout" '
                f'enabled="true" bounds="[{x1},2140][{x2},2400]">'
                f'<node package="{PACKAGE}" class="android.widget.FrameLayout" '
                f'enabled="true" bounds="[{x1},2140][{x2},2400]">'
                f'<node package="{PACKAGE}" class="android.view.ViewGroup" '
                f'content-desc="{label}" clickable="true" enabled="true" '
                f'bounds="[{x1},2160][{x2},2380]"/></node></node>'
            )
        navigation = (
            f'<node package="{PACKAGE}" class="android.widget.LinearLayout" '
            'enabled="true" bounds="[0,2120][1080,2400]">'
            f'{"".join(wrapped_items)}</node>'
        )
        page = build_page_model(
            _page(navigation, title="首页内容"),
            package_name=PACKAGE,
            activity=".MainActivity",
        )

        groups = discover_navigation_groups(page, screen_size=SCREEN_SIZE)

        self.assertEqual(len(groups), 1)
        self.assertEqual(list(groups[0].labels), labels)


if __name__ == "__main__":
    unittest.main()
