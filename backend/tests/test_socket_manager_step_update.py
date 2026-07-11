import unittest
from unittest.mock import AsyncMock, patch

from backend.socket_manager import ConnectionManager


class SocketManagerStepUpdateTests(unittest.IsolatedAsyncioTestCase):
    async def test_step_update_payload_carries_error_code_and_suggestion(self):
        manager = ConnectionManager()

        with patch.object(manager, "send_message", new=AsyncMock()) as send_mock:
            await manager.broadcast_step_update(
                1,
                2,
                "failed",
                "✗ 步骤 3 失败: 元素未找到",
                duration=1.234,
                error="元素未找到",
                error_code="E2001_ELEMENT_NOT_FOUND",
                suggestion="请检查定位器是否正确",
            )

        payload = send_mock.await_args.args[1]
        self.assertEqual(payload["type"], "step_update")
        self.assertEqual(payload["status"], "failed")
        self.assertEqual(payload["error"], "元素未找到")
        self.assertEqual(payload["error_code"], "E2001_ELEMENT_NOT_FOUND")
        self.assertEqual(payload["suggestion"], "请检查定位器是否正确")

    async def test_step_update_payload_omits_empty_error_fields(self):
        manager = ConnectionManager()

        with patch.object(manager, "send_message", new=AsyncMock()) as send_mock:
            await manager.broadcast_step_update(1, 0, "success", "✓ 步骤 1 成功", duration=0.5)

        payload = send_mock.await_args.args[1]
        self.assertNotIn("error", payload)
        self.assertNotIn("error_code", payload)
        self.assertNotIn("suggestion", payload)


if __name__ == "__main__":
    unittest.main()
