import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from backend.inspection.action_map import (
    build_action_map,
    finalize_action_map,
    read_action_map,
    update_action_map,
    write_action_map,
)
from backend.inspection.semantics import InspectionAction


def _action(
    key: str,
    *,
    label: str,
    action_type: str = "click",
    locator: str = "description",
    bounds=(10, 20, 110, 220),
    coordinate_only: bool = False,
    risk_type: str | None = None,
    blocked_reason: str | None = None,
    password: bool = False,
    action_role: str | None = None,
    action_role_key: str | None = None,
) -> InspectionAction:
    return InspectionAction(
        action_type=action_type,
        action_key=key,
        locator_candidates=([{"by": locator, "selector": label}] if locator else []),
        target_meta={
            "content_desc": label,
            "class": "android.widget.EditText" if action_type == "input" else "android.widget.Button",
            "bounds": bounds,
            "password": password,
        },
        coordinate_only=coordinate_only,
        replayable=not coordinate_only and risk_type is None,
        risk_type=risk_type,
        blocked_reason=blocked_reason,
        action_role=action_role,
        action_role_key=action_role_key,
    )


class InspectionActionMapTests(unittest.TestCase):
    def test_build_map_sanitizes_labels_and_never_serializes_selectors_or_secrets(self):
        secret = "p@ssword-value"
        actions = [
            _action("safe", label=f"欢迎 {secret}"),
            _action(
                "password",
                label=secret,
                action_type="input",
                password=True,
            ),
            _action(
                "custom-sensitive",
                label="会员手机号 13800138000",
                action_type="input",
            ),
        ]

        payload = build_action_map(
            run_id=7,
            branch_key="authenticated",
            state_id=11,
            activity=".MainActivity",
            screen_size=(1080, 2400),
            actions=actions,
            sanitizer_rules=[
                {
                    "content_desc_regex": "会员手机号",
                    "class_regex": "EditText",
                }
            ],
            secret_values=[secret],
        )

        self.assertEqual(payload["actions"][0]["label"], "欢迎 ***")
        self.assertEqual(payload["actions"][1]["label"], "敏感输入框")
        self.assertEqual(payload["actions"][2]["label"], "敏感输入框")
        serialized = json.dumps(payload, ensure_ascii=False)
        self.assertNotIn(secret, serialized)
        self.assertNotIn('"selector"', serialized)
        self.assertNotIn('"locator_candidates"', serialized)
        self.assertNotIn('"input_value"', serialized)

    def test_build_map_persists_action_role_without_locator_details(self):
        action = _action(
            "open-item",
            label="商品标题",
            action_role="ITEM_OPEN:collection",
            action_role_key="item-role",
        )
        payload = build_action_map(
            run_id=1,
            branch_key="guest",
            state_id=2,
            activity=".List",
            screen_size=(1080, 2400),
            actions=[action],
        )
        self.assertEqual(payload["schema_version"], 3)
        self.assertEqual(payload["actions"][0]["action_role_key"], "item-role")
        self.assertEqual(
            payload["actions"][0]["action_role"],
            "ITEM_OPEN:collection",
        )

    def test_numbering_includes_safe_coordinate_discovery_click(self):
        actions = [
            _action("safe-click", label="下一页"),
            _action("safe-input", label="搜索", action_type="input"),
            _action(
                "blocked",
                label="删除",
                risk_type="DESTRUCTIVE",
                blocked_reason="危险操作",
            ),
            _action(
                "coordinate",
                label="动态控件",
                locator="",
                coordinate_only=True,
            ),
            _action(
                "scroll",
                label="列表",
                action_type="scroll",
                locator="",
                coordinate_only=True,
            ),
        ]

        payload = build_action_map(
            run_id=1,
            branch_key="guest",
            state_id=2,
            activity=".Home",
            screen_size=(1080, 2400),
            actions=actions,
        )
        by_key = {item["action_key"]: item for item in payload["actions"]}

        self.assertEqual(by_key["safe-click"]["display_order"], 1)
        self.assertEqual(by_key["safe-input"]["display_order"], 2)
        self.assertIsNone(by_key["blocked"]["display_order"])
        self.assertEqual(by_key["coordinate"]["display_order"], 3)
        self.assertIsNone(by_key["scroll"]["display_order"])
        self.assertEqual(by_key["blocked"]["status"], "BLOCKED")
        self.assertFalse(by_key["blocked"]["invoked"])
        self.assertEqual(by_key["blocked"]["execution_disposition"], "SKIPPED")
        self.assertEqual(by_key["blocked"]["phase_at_finalize"], "discovery")
        self.assertIsNotNone(by_key["blocked"]["finalized_at"])
        self.assertEqual(by_key["coordinate"]["status"], "PENDING")
        self.assertFalse(by_key["coordinate"]["invoked"])
        self.assertEqual(by_key["scroll"]["status"], "PENDING")

    def test_finalize_can_classify_cancelled_pending_actions(self):
        action = _action("pending", label="稍后")
        payload = build_action_map(
            run_id=1,
            branch_key="guest",
            state_id=2,
            activity=".Home",
            screen_size=(1080, 2400),
            actions=[action],
        )
        finalize_action_map(
            payload,
            pending_status="CANCELLED",
            reason="用户取消",
            phase="explore",
        )
        entry = payload["actions"][0]
        self.assertEqual(entry["status"], "CANCELLED")
        self.assertEqual(entry["execution_disposition"], "NOT_REACHED")
        self.assertEqual(entry["phase_at_finalize"], "explore")
        self.assertIsNotNone(entry["finalized_at"])

    def test_finalize_classifies_filtered_pending_actions_as_skipped(self):
        action = _action("filtered", label="无业务语义容器")
        payload = build_action_map(
            run_id=1,
            branch_key="guest",
            state_id=2,
            activity=".Home",
            screen_size=(1080, 2400),
            actions=[action],
        )

        finalize_action_map(
            payload,
            pending_status="FILTERED_NON_ACTIONABLE",
            reason="动作未进入可执行探索前沿",
            phase="explore",
        )

        entry = payload["actions"][0]
        self.assertEqual(entry["status"], "FILTERED_NON_ACTIONABLE")
        self.assertEqual(entry["execution_disposition"], "SKIPPED")
        self.assertEqual(entry["phase_at_finalize"], "explore")
        self.assertIsNotNone(entry["finalized_at"])

    def test_updates_are_persisted_and_finalize_marks_only_unfinished_not_reached(self):
        pending = _action("pending", label="稍后")
        active = _action("active", label="当前")
        invoked = _action("invoked", label="调用已返回")
        passed = _action("passed", label="完成")
        blocked = _action(
            "blocked",
            label="支付",
            risk_type="PAYMENT",
            blocked_reason="危险操作",
        )
        payload = build_action_map(
            run_id=1,
            branch_key="guest",
            state_id=2,
            activity=".Home",
            screen_size=(1080, 2400),
            actions=[pending, active, invoked, passed, blocked],
        )
        update_action_map(
            payload,
            active,
            status="ACTIVE",
            sequence=4,
            increment_attempt=True,
        )
        update_action_map(
            payload,
            invoked,
            status="INVOKED",
            sequence=5,
            invoked=True,
            increment_attempt=True,
        )
        update_action_map(
            payload,
            passed,
            status="PASS",
            sequence=6,
            invoked=True,
            increment_attempt=True,
        )
        finalize_action_map(payload)
        by_key = {item["action_key"]: item for item in payload["actions"]}

        self.assertEqual(by_key["pending"]["status"], "NOT_REACHED")
        self.assertEqual(by_key["active"]["status"], "NOT_REACHED")
        self.assertEqual(by_key["active"]["attempt_count"], 1)
        self.assertEqual(by_key["active"]["global_sequence"], 4)
        self.assertEqual(by_key["invoked"]["status"], "ACTION_ERROR")
        self.assertTrue(by_key["invoked"]["invoked"])
        self.assertTrue(by_key["invoked"]["invocation_unknown"])
        self.assertEqual(
            by_key["invoked"]["reason"],
            "设备调用已返回，但未能确认最终结果",
        )
        self.assertEqual(by_key["passed"]["status"], "PASS")
        self.assertTrue(by_key["passed"]["invoked"])
        self.assertEqual(by_key["passed"]["attempt_count"], 1)
        self.assertEqual(by_key["passed"]["execution_disposition"], "EXECUTED")
        self.assertEqual(by_key["passed"]["phase_at_finalize"], "explore")
        self.assertIsNotNone(by_key["passed"]["finalized_at"])
        self.assertEqual(by_key["blocked"]["status"], "BLOCKED")
        self.assertFalse(by_key["blocked"]["invoked"])
        self.assertEqual(by_key["blocked"]["execution_disposition"], "SKIPPED")
        self.assertEqual(by_key["blocked"]["phase_at_finalize"], "discovery")

    def test_terminal_updates_never_retain_pending_disposition_or_finalize_fields(self):
        statuses = {
            "passed": ("PASS", "EXECUTED"),
            "same-page": ("SELF_LOOP", "EXECUTED"),
            "skipped": ("SKIPPED", "SKIPPED"),
            "missing": ("LOCATOR_NOT_FOUND", "FAILED"),
        }
        actions = [_action(key, label=key) for key in statuses]
        payload = build_action_map(
            run_id=1,
            branch_key="guest",
            state_id=2,
            activity=".Home",
            screen_size=(1080, 2400),
            actions=actions,
        )

        for action in actions:
            status, _ = statuses[action.action_key]
            update_action_map(payload, action, status=status, phase="explore")

        for entry in payload["actions"]:
            _, disposition = statuses[entry["action_key"]]
            self.assertEqual(entry["execution_disposition"], disposition)
            self.assertEqual(entry["phase_at_finalize"], "explore")
            self.assertIsNotNone(entry["finalized_at"])

    def test_runtime_reason_and_error_are_redacted_before_public_persistence(self):
        secret = "actual-secret-value"
        action = _action("input", label="搜索", action_type="input")
        payload = build_action_map(
            run_id=1,
            branch_key="guest",
            state_id=2,
            activity=".Home",
            screen_size=(1080, 2400),
            actions=[action],
        )

        updated = update_action_map(
            payload,
            action,
            status="ACTION_ERROR",
            reason=(f"输入失败 value={secret}; password=visible-password token=visible-token"),
            error=(f"selector=//android.widget.EditText[@text='{secret}']"),
            secret_values=[secret],
        )

        serialized = json.dumps(updated, ensure_ascii=False)
        self.assertNotIn(secret, serialized)
        self.assertNotIn("visible-password", serialized)
        self.assertNotIn("visible-token", serialized)
        self.assertNotIn("//android.widget.EditText", serialized)
        self.assertNotIn('"selector"', serialized)
        self.assertEqual(updated["error"], "动作执行异常")

        selector_reason = update_action_map(
            payload,
            action,
            status="LOCATOR_DRIFT",
            reason="XPath=//android.widget.Button[@text='私密定位']",
        )
        self.assertEqual(selector_reason["reason"], "定位详情已隐藏")

    def test_atomic_replace_preserves_previous_file_when_commit_fails(self):
        with TemporaryDirectory() as temp_dir:
            target = Path(temp_dir) / "state" / "actions.json"
            write_action_map(target, {"revision": 1, "actions": []})
            self.assertEqual(read_action_map(target)["revision"], 1)

            with patch.object(
                Path,
                "replace",
                side_effect=OSError("simulated replace failure"),
            ):
                with self.assertRaises(OSError):
                    write_action_map(
                        target,
                        {"revision": 2, "actions": [{"status": "PASS"}]},
                    )

            self.assertEqual(read_action_map(target)["revision"], 1)
            self.assertEqual(
                list(target.parent.glob(f".{target.name}.*.tmp")),
                [],
            )

            write_action_map(
                target,
                {"revision": 2, "actions": [{"status": "PASS"}]},
            )
            self.assertEqual(read_action_map(target)["revision"], 2)


if __name__ == "__main__":
    unittest.main()
