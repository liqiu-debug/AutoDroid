import threading
import unittest
from collections import deque
from unittest.mock import Mock, patch

from backend.inspection.device import CapturedPage
from backend.inspection.engine import (
    StateWork,
    _ensure_parent,
    _is_primary_entry_surface,
    _longest_replayed_prefix,
    _page_logical_key,
    _paths_equivalent,
    _pop_most_local,
    _primary_entry_continuation_priority,
    _restore_parent_after_transition,
    _serialize_action,
    _coverage_representative_priority,
)
from backend.inspection.semantics import InspectionAction, build_page_model


def _capture(label: str, *, activity: str = ".Main") -> CapturedPage:
    xml = (
        '<hierarchy rotation="0">'
        '<node package="com.demo" class="android.widget.FrameLayout" '
        'enabled="true" bounds="[0,0][1080,2400]">'
        f'<node package="com.demo" class="android.widget.TextView" text="{label}" '
        'enabled="true" bounds="[20,100][800,220]"/>'
        "</node></hierarchy>"
    )
    model = build_page_model(xml, package_name="com.demo", activity=activity)
    return CapturedPage(
        package_name="com.demo",
        activity=activity,
        xml=xml,
        screenshot_png=b"unused",
        screenshot_sha=f"sha-{label}",
        perceptual_hash="0" * 16,
        model=model,
        stable_by="exact",
    )


def _action(key: str, label: str) -> InspectionAction:
    return InspectionAction(
        action_type="click",
        action_key=key,
        locator_candidates=[{"by": "description", "selector": label}],
        target_meta={"content_desc": label},
    )


def _work(capture: CapturedPage, *, state_id=10, depth=0, path=None) -> StateWork:
    return StateWork(
        state_id=state_id,
        state_key=capture.model.state_key,
        cluster_key=capture.model.cluster_key,
        replay_key=capture.model.replay_key,
        package_name=capture.package_name,
        activity=capture.activity,
        screenshot_sha=capture.screenshot_sha,
        depth=depth,
        path=list(path or []),
        actions=[],
    )


def _step(action, source_capture, target_capture):
    return _serialize_action(
        action,
        expected_source_semantic_key=_page_logical_key(source_capture.model),
        expected_target_semantic_key=_page_logical_key(target_capture.model),
    )


class LongestReplayedPrefixTests(unittest.TestCase):
    def test_ready_capture_requires_the_same_replay_path(self):
        first = [{"action_key": "open-category"}]
        same = [{"action_key": "open-category", "target_meta": {"x": 1}}]
        different = [{"action_key": "open-profile"}]

        self.assertTrue(_paths_equivalent(first, same))
        self.assertFalse(_paths_equivalent(first, different))
        self.assertFalse(_paths_equivalent(first, []))

    def test_matches_step_source_and_final_target(self):
        home = _capture("首页")
        detail = _capture("详情", activity=".Detail")
        path = [_step(_action("open", "查看详情"), home, detail)]
        self.assertEqual(_longest_replayed_prefix(home, path), 0)
        self.assertEqual(_longest_replayed_prefix(detail, path), 1)

    def test_unknown_page_returns_negative(self):
        home = _capture("首页")
        detail = _capture("详情", activity=".Detail")
        other = _capture("未知", activity=".Other")
        path = [_step(_action("open", "查看详情"), home, detail)]
        self.assertEqual(_longest_replayed_prefix(other, path), -1)
        self.assertEqual(_longest_replayed_prefix(None, path), -1)
        self.assertEqual(_longest_replayed_prefix(home, []), -1)


class EnsureParentIncrementalTests(unittest.TestCase):
    def _ensure(self, parent, device=None):
        return _ensure_parent(
            device=device or Mock(),
            parent=parent,
            branch_config={"entry_case_id": 7},
            device_serial="android-1",
            package_name="com.demo",
            abort_event=threading.Event(),
            input_rules=[],
            dynamic_patterns=[],
            stable_wait_seconds=2.0,
            secret_values=[],
        )

    def test_child_dequeue_replays_single_suffix_without_entry_case(self):
        home = _capture("首页")
        detail = _capture("详情", activity=".Detail")
        open_action = _action("open", "查看详情")
        child = _work(
            detail,
            state_id=11,
            depth=1,
            path=[_step(open_action, home, detail)],
        )

        with patch(
            "backend.inspection.engine.exact_parent_matches",
            return_value=False,
        ), patch(
            "backend.inspection.engine.wait_for_stable_page",
            side_effect=[home, detail],
        ), patch(
            "backend.inspection.engine.perform_action",
            return_value="description",
        ) as perform, patch(
            "backend.inspection.engine._try_run_case",
        ) as run_case:
            restored = self._ensure(child)

        self.assertIs(restored, detail)
        perform.assert_called_once()
        self.assertEqual(perform.call_args.args[1].action_key, "open")
        run_case.assert_not_called()

    def test_unknown_position_falls_back_to_entry_case_full_replay(self):
        home = _capture("首页")
        detail = _capture("详情", activity=".Detail")
        unknown = _capture("未知", activity=".Other")
        open_action = _action("open", "查看详情")
        child = _work(
            detail,
            state_id=11,
            depth=1,
            path=[_step(open_action, home, detail)],
        )

        with patch(
            "backend.inspection.engine.exact_parent_matches",
            return_value=False,
        ), patch(
            "backend.inspection.engine.wait_for_stable_page",
            side_effect=[unknown, home, detail],
        ), patch(
            "backend.inspection.engine.perform_action",
            return_value="description",
        ), patch(
            "backend.inspection.engine.ready_assertion_exists",
            return_value=True,
        ), patch(
            "backend.inspection.engine._try_run_case",
        ) as run_case:
            restored = self._ensure(child)

        self.assertIs(restored, detail)
        run_case.assert_called_once()


class RestoreParentBackRetryTests(unittest.TestCase):
    def test_unstable_first_capture_after_back_retries_before_full_replay(self):
        home = _capture("首页")
        detail = _capture("详情", activity=".Detail")
        unstable = _capture("未知", activity=".Other")
        parent = _work(home)
        device = Mock()

        with patch(
            "backend.inspection.engine.wait_for_stable_page",
            side_effect=[unstable, home],
        ), patch(
            "backend.inspection.engine._replay_path",
        ) as replay:
            restored = _restore_parent_after_transition(
                device=device,
                parent=parent,
                target_capture=detail,
                relation_type="CHILD",
                navigation_group_key=None,
                navigation_entries=[],
                branch_config={},
                device_serial="android-1",
                package_name="com.demo",
                abort_event=threading.Event(),
                input_rules=[],
                dynamic_patterns=[],
                stable_wait_seconds=3.0,
                secret_values=[],
            )

        self.assertIs(restored, home)
        device.press.assert_called_once_with("back")
        replay.assert_not_called()


class PopMostLocalTests(unittest.TestCase):
    def test_unknown_representative_is_deprioritized_but_commerce_is_urgent(self):
        unknown = _work(_capture("未知"), state_id=31)
        unknown.page_subtype = "UNKNOWN"
        commerce = _work(_capture("结算", activity=".Checkout"), state_id=32)
        commerce.page_subtype = "CHECKOUT"

        self.assertEqual(_coverage_representative_priority(unknown), 550)
        self.assertEqual(_coverage_representative_priority(commerce), 100)

    def test_prefers_state_sharing_longest_prefix_within_layer(self):
        home = _capture("首页")
        cart = _capture("购物车", activity=".Cart")
        detail = _capture("详情", activity=".Detail")
        to_cart = _step(_action("cart", "购物车"), home, cart)
        to_detail = _step(_action("open", "查看详情"), home, detail)
        cart_work = _work(cart, state_id=21, depth=1, path=[to_cart])
        detail_work = _work(detail, state_id=22, depth=1, path=[to_detail])
        queue = deque([cart_work, detail_work])

        popped = _pop_most_local(queue, [to_detail])

        self.assertIs(popped, detail_work)
        self.assertEqual(list(queue), [cart_work])

    def test_without_position_keeps_fifo_order(self):
        home = _capture("首页")
        cart = _capture("购物车", activity=".Cart")
        first = _work(home, state_id=21, depth=1)
        second = _work(cart, state_id=22, depth=1)
        queue = deque([first, second])

        self.assertIs(_pop_most_local(queue, None), first)

    def test_never_crosses_depth_layers(self):
        home = _capture("首页")
        cart = _capture("购物车", activity=".Cart")
        detail = _capture("详情", activity=".Detail")
        to_detail = _step(_action("open", "查看详情"), cart, detail)
        shallow = _work(home, state_id=21, depth=1)
        deeper = _work(detail, state_id=22, depth=2, path=[to_detail])
        queue = deque([shallow, deeper])

        self.assertIs(_pop_most_local(queue, [to_detail]), shallow)

    def test_primary_entry_surface_preempts_local_deep_chain(self):
        home = _capture("首页")
        category = _capture("分类", activity=".Category")
        detail = _capture("商品详情", activity=".Detail")
        category_action = InspectionAction(
            action_type="click",
            action_key="open-category",
            locator_candidates=[{"by": "description", "selector": "分类"}],
            target_meta={
                "content_desc": "分类",
                "navigation": {"group_region": "bottom"},
            },
            sample_policy="RUN_NAV_ONCE",
        )
        detail_action = _action("open-detail", "商品详情")
        category_step = _serialize_action(category_action)
        detail_step = _step(detail_action, home, detail)
        category_work = _work(
            category,
            state_id=21,
            depth=0,
            path=[category_step],
        )
        category_work.frontier_priority = 40
        deep_work = _work(
            detail,
            state_id=22,
            depth=3,
            path=[detail_step],
        )
        deep_work.frontier_priority = 100
        queue = deque([deep_work, category_work])

        popped = _pop_most_local(
            queue,
            [detail_step],
            coverage_scheduler=True,
        )

        self.assertTrue(_is_primary_entry_surface(category_work))
        self.assertFalse(_is_primary_entry_surface(deep_work))
        self.assertIs(popped, category_work)
        self.assertEqual(list(queue), [deep_work])

    def test_primary_entry_continuations_rotate_fifo_before_path_locality(self):
        category = _capture("分类", activity=".Category")
        wish = _capture("许愿池", activity=".Wish")

        def entry_step(key: str, label: str):
            return _serialize_action(
                InspectionAction(
                    action_type="click",
                    action_key=key,
                    locator_candidates=[
                        {"by": "description", "selector": label}
                    ],
                    target_meta={
                        "content_desc": label,
                        "navigation": {"group_region": "bottom"},
                    },
                    sample_policy="RUN_NAV_ONCE",
                )
            )

        category_step = entry_step("open-category", "分类")
        wish_step = entry_step("open-wish", "许愿池")
        category_work = _work(
            category,
            state_id=41,
            depth=0,
            path=[category_step],
        )
        wish_work = _work(
            wish,
            state_id=42,
            depth=0,
            path=[wish_step],
        )
        category_work.frontier_priority = 350
        wish_work.frontier_priority = 350
        queue = deque([category_work, wish_work])

        popped = _pop_most_local(
            queue,
            [wish_step],
            coverage_scheduler=True,
        )

        self.assertIs(popped, category_work)
        self.assertEqual(list(queue), [wish_work])

    def test_new_business_page_preempts_primary_continuation(self):
        continuation = _work(_capture("首页"), state_id=51)
        continuation.frontier_priority = 350
        business = _work(_capture("商品列表"), state_id=52)
        business.frontier_priority = 200
        queue = deque([continuation, business])

        popped = _pop_most_local(queue, [], coverage_scheduler=True)

        self.assertIs(popped, business)
        self.assertEqual(list(queue), [continuation])

    def test_profile_continuation_preempts_ordinary_business_page(self):
        profile = _work(_capture("我的"), state_id=61)
        profile.page_subtype = "PROFILE"
        profile.frontier_priority = _primary_entry_continuation_priority(profile)
        business = _work(_capture("门店列表"), state_id=62)
        business.frontier_priority = 200
        queue = deque([business, profile])

        popped = _pop_most_local(queue, [], coverage_scheduler=True)

        self.assertEqual(profile.frontier_priority, 150)
        self.assertIs(popped, profile)
        self.assertEqual(list(queue), [business])


if __name__ == "__main__":
    unittest.main()
