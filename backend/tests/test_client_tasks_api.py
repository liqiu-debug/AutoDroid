"""桌面客户端定时任务拉取（GET /api/client-tasks）测试。

契约（T1 统一谓词，P4 终验必修缺陷 2）：
- 鉴权 get_current_user（无 token 401）；
- 统一谓词（与云端 is_client_scheduled_task / 触发过滤同源
  client_scheduled_serials）：仅下发「ui + scenario_id + 绑定中存在认领/心跳
  客户端设备」的任务——「本人创建 + 未认领 serial」不下发（云端执行），否则
  两端都调度（真双跑窗口）；无绑定设备任务不下发（桌面 execute_task 本就
  不触发）；fastbot/inspection/api 不下发（桌面 LOCAL_TASK_TYPES={'ui'}）；
- 可见性过滤：创建者=本人 或 device_serials 命中本人设备（owner_id /
  app_owner_id == 本人，与 filter_devices_for_user 归属口径一致）；
- 仅返回 is_active 任务（暂停任务不进本地调度，对齐 _restore_scheduled_tasks）；
- 条目形状含 ScheduledTask 全字段（含 user_id / device_serial 原始 CSV /
  strategy_config 解析后字典）；envelope 为 {total, items}。

admin 语义按 tasks.py 现状对齐：任务侧无 admin 全量可见语义（列表不做归属
放大、创建仅打 user_id 印记），故 admin 与普通用户同规则，只见本人创建或
绑定本人设备的任务。
"""
import unittest
from datetime import datetime

from fastapi import FastAPI
from fastapi.testclient import TestClient
from jose import jwt
from sqlalchemy.pool import StaticPool
from sqlmodel import SQLModel, Session, create_engine

from backend.api import deps, tasks as tasks_api
from backend.api.tasks import (
    client_scheduled_serials,
    is_client_scheduled_task,
    list_client_tasks,
)
from backend.core.security import ALGORITHM, SECRET_KEY
from backend.database import get_session
from backend.models import Device, ScheduledTask, User


def _make_task(session: Session, name: str, *, user_id=None, device_serial=None, **fields) -> ScheduledTask:
    payload = dict(
        name=name,
        strategy="DAILY",
        strategy_config='{"_task_type": "ui", "hour": 8, "minute": 30}',
        is_active=True,
        enable_notification=True,
        device_serial=device_serial,
        user_id=user_id,
        scenario_id=1,  # 统一谓词要求 ui 任务带 scenario_id（桌面 execute_task 必需）
    )
    payload.update(fields)
    task = ScheduledTask(**payload)
    session.add(task)
    session.commit()
    session.refresh(task)
    return task


class ClientTasksVisibilityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = create_engine(
            "sqlite://",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        SQLModel.metadata.create_all(self.engine)
        self.session = Session(self.engine)
        self.admin = self._add_user("admin", role="admin")
        self.alice = self._add_user("alice")
        self.bob = self._add_user("bob")

    def tearDown(self) -> None:
        self.session.close()
        self.engine.dispose()

    def _add_user(self, username: str, role: str = "user") -> User:
        user = User(username=username, hashed_password="x", role=role)
        self.session.add(user)
        self.session.commit()
        self.session.refresh(user)
        return user

    def _add_device(self, serial: str, **fields) -> Device:
        device = Device(serial=serial, **fields)
        self.session.add(device)
        self.session.commit()
        self.session.refresh(device)
        return device

    def _add_client_device(self, serial: str, **fields) -> Device:
        """认领/心跳客户端设备（统一谓词 client_scheduled_serials 口径）。"""
        fields.setdefault("client_instance_id", f"desktop-{serial}")
        return self._add_device(serial, **fields)

    def _items(self, user: User):
        result = list_client_tasks(session=self.session, current_user=user)
        return result.total, [item.model_dump() if hasattr(item, "model_dump") else dict(item) for item in result.items]

    def test_creator_sees_own_task_with_full_fields(self):
        self._add_client_device("d-alice", owner_id=self.alice.id)
        now = datetime.now()
        task = _make_task(
            self.session,
            "每日回归",
            user_id=self.alice.id,
            device_serial="d-alice",
            scenario_id=7,
            strategy="WEEKLY",
            strategy_config='{"_task_type": "ui", "days": [0, 2], "hour": 21, "minute": 5}',
            enable_notification=False,
            next_run_time=now,
            updated_at=now,
        )

        total, items = self._items(self.alice)
        self.assertEqual(total, 1)
        self.assertEqual(len(items), 1)
        item = items[0]
        # ScheduledTask 全字段
        self.assertEqual(item["id"], task.id)
        self.assertEqual(item["name"], "每日回归")
        self.assertEqual(item["scenario_id"], 7)
        self.assertEqual(item["device_serial"], "d-alice")
        self.assertEqual(item["device_serials"], ["d-alice"])
        self.assertEqual(item["strategy"], "WEEKLY")
        self.assertEqual(item["strategy_config"], {"_task_type": "ui", "days": [0, 2], "hour": 21, "minute": 5})
        self.assertTrue(item["is_active"])
        self.assertFalse(item["enable_notification"])
        self.assertEqual(item["user_id"], self.alice.id)
        self.assertEqual(item["created_at"], task.created_at)
        self.assertEqual(item["updated_at"], task.updated_at)
        self.assertEqual(item["next_run_time"], task.next_run_time)

    def test_hides_tasks_owned_by_others(self):
        self._add_client_device("d-bob", owner_id=self.bob.id)
        _make_task(self.session, "bob 的任务", user_id=self.bob.id, device_serial="d-bob")

        total, items = self._items(self.alice)
        self.assertEqual(total, 0)
        self.assertEqual(items, [])

    def test_includes_task_bound_to_my_device_created_by_others(self):
        self._add_client_device("d-alice", owner_id=self.alice.id)
        task = _make_task(self.session, "共享设备任务", user_id=self.bob.id, device_serial="d-alice")

        total, items = self._items(self.alice)
        self.assertEqual(total, 1)
        self.assertEqual(items[0]["id"], task.id)
        self.assertEqual(items[0]["user_id"], self.bob.id)

    def test_matches_any_serial_of_multi_device_task(self):
        """混合绑定（客户端设备 + 未认领设备）：命中本人客户端设备即下发。"""
        self._add_client_device("d-alice", owner_id=self.alice.id)
        self._add_device("d-stranger", owner_id=self.bob.id)
        task = _make_task(
            self.session,
            "多设备任务",
            user_id=self.bob.id,
            device_serial="d-stranger, d-alice",
        )

        total, items = self._items(self.alice)
        self.assertEqual(total, 1)
        self.assertEqual(items[0]["id"], task.id)
        self.assertEqual(items[0]["device_serials"], ["d-stranger", "d-alice"])

    def test_unclaimed_device_serial_is_not_mine(self):
        self._add_device("d-orphan")  # owner_id=NULL 未认领
        _make_task(self.session, "未认领设备任务", user_id=self.bob.id, device_serial="d-orphan")

        total, _ = self._items(self.alice)
        self.assertEqual(total, 0)

    def test_app_owner_device_counts_as_mine(self):
        self._add_client_device("d-app", app_owner_id=self.alice.id)
        task = _make_task(self.session, "App 注册设备任务", user_id=self.bob.id, device_serial="d-app")

        total, items = self._items(self.alice)
        self.assertEqual(total, 1)
        self.assertEqual(items[0]["id"], task.id)

    def test_inactive_tasks_are_not_schedulable(self):
        self._add_client_device("d-alice", owner_id=self.alice.id)
        _make_task(self.session, "已暂停", user_id=self.alice.id, device_serial="d-alice", is_active=False)
        active = _make_task(self.session, "启用中", user_id=self.alice.id, device_serial="d-alice")

        total, items = self._items(self.alice)
        self.assertEqual(total, 1)
        self.assertEqual(items[0]["id"], active.id)

    def test_admin_follows_tasks_py_ownership_rule(self):
        """tasks.py 现状无 admin 全量任务语义：admin 与普通用户同过滤规则。"""
        self._add_client_device("d-bob", owner_id=self.bob.id)
        self._add_client_device("d-admin", owner_id=self.admin.id)
        own = _make_task(self.session, "admin 的任务", user_id=self.admin.id, device_serial="d-admin")
        _make_task(self.session, "bob 的任务", user_id=self.bob.id, device_serial="d-bob")

        total, items = self._items(self.admin)
        self.assertEqual(total, 1)
        self.assertEqual(items[0]["id"], own.id)

    # -- T1 统一谓词（P4 终验必修缺陷 2）：与云端 is_client_scheduled_task 同源 --

    def test_own_task_with_unclaimed_serial_not_delivered(self):
        """「本人创建 + 未认领 serial」不下发：否则云端与桌面两端都调度（真双跑窗口）。"""
        self._add_device("d-orphan")  # 未认领：不满足 client_scheduled_serials
        task = _make_task(self.session, "未认领任务", user_id=self.alice.id, device_serial="d-orphan")

        self.assertFalse(is_client_scheduled_task(self.session, task))
        self.assertEqual(client_scheduled_serials(self.session, task), [])
        total, _ = self._items(self.alice)
        self.assertEqual(total, 0, "未认领 serial 任务不得下发客户端（云端执行）")

    def test_fully_client_managed_task_matches_cloud_predicate(self):
        """全部 serial 认领：云端让位（is_client_scheduled_task）+ 客户端下发，口径一致。"""
        self._add_client_device("d-alice", owner_id=self.alice.id)
        task = _make_task(self.session, "客户端任务", user_id=self.alice.id, device_serial="d-alice", scenario_id=3)

        self.assertTrue(is_client_scheduled_task(self.session, task))
        self.assertEqual(client_scheduled_serials(self.session, task), ["d-alice"])
        total, items = self._items(self.alice)
        self.assertEqual(total, 1)
        self.assertEqual(items[0]["id"], task.id)

    def test_mixed_binding_delivered_with_client_serial(self):
        """混合绑定（认领 + 未认领/物理）：命中客户端 serial 即下发，桌面跑认领设备。"""
        self._add_client_device("d-alice", owner_id=self.alice.id)
        self._add_device("d-usb")  # 物理设备：云端跑
        task = _make_task(self.session, "混合任务", user_id=self.alice.id, device_serial="d-alice,d-usb")

        self.assertFalse(is_client_scheduled_task(self.session, task))
        self.assertEqual(client_scheduled_serials(self.session, task), ["d-alice"])
        total, items = self._items(self.alice)
        self.assertEqual(total, 1)
        self.assertEqual(items[0]["id"], task.id)

    def test_task_without_devices_not_delivered(self):
        """无绑定设备任务桌面 execute_task 不触发，不下发（云端保留执行）。"""
        task = _make_task(self.session, "无设备任务", user_id=self.alice.id, device_serial=None)

        self.assertFalse(is_client_scheduled_task(self.session, task))
        total, items = self._items(self.alice)
        self.assertEqual(total, 0, "无绑定设备任务不进客户端调度")
        self.assertEqual(items, [])

    def test_non_ui_task_not_delivered(self):
        """fastbot/inspection/api 桌面不接管（LOCAL_TASK_TYPES={'ui'}），不下发。"""
        self._add_client_device("d-alice", owner_id=self.alice.id)
        task = _make_task(
            self.session,
            "智能探索任务",
            user_id=self.alice.id,
            device_serial="d-alice",
            scenario_id=1,
            strategy_config='{"_task_type": "fastbot", "fb_package_name": "com.x"}',
        )

        self.assertFalse(is_client_scheduled_task(self.session, task))
        self.assertEqual(client_scheduled_serials(self.session, task), [])
        total, _ = self._items(self.alice)
        self.assertEqual(total, 0)

    def test_ui_task_without_scenario_not_delivered(self):
        self._add_client_device("d-alice", owner_id=self.alice.id)
        task = _make_task(
            self.session,
            "缺场景任务",
            user_id=self.alice.id,
            device_serial="d-alice",
            scenario_id=None,
        )

        self.assertFalse(is_client_scheduled_task(self.session, task))
        total, _ = self._items(self.alice)
        self.assertEqual(total, 0)

    def test_delivery_implies_client_scheduled_serials(self):
        """一致性不变量：下发集合 ⊆ 有客户端接管 serial 的任务；全接管任务必在下发集合。"""
        self._add_client_device("d-alice", owner_id=self.alice.id)
        self._add_device("d-usb")
        self._add_device("d-orphan")
        delivered = _make_task(self.session, "客户端任务", user_id=self.alice.id, device_serial="d-alice")
        mixed = _make_task(self.session, "混合任务", user_id=self.alice.id, device_serial="d-alice,d-usb")
        _make_task(self.session, "未认领任务", user_id=self.alice.id, device_serial="d-orphan")
        _make_task(self.session, "无设备任务", user_id=self.alice.id, device_serial=None)

        total, items = self._items(self.alice)
        delivered_ids = {item["id"] for item in items}
        self.assertEqual(delivered_ids, {delivered.id, mixed.id})

        for task in (delivered, mixed):
            self.assertTrue(client_scheduled_serials(self.session, task))
        self.assertTrue(is_client_scheduled_task(self.session, delivered))
        self.assertFalse(is_client_scheduled_task(self.session, mixed))


class ClientTasksAuthTests(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = create_engine(
            "sqlite://",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        SQLModel.metadata.create_all(self.engine)
        self.session = Session(self.engine)
        self.alice = User(username="alice", hashed_password="x", role="user")
        self.bob = User(username="bob", hashed_password="x", role="user")
        self.session.add(self.alice)
        self.session.add(self.bob)
        self.session.commit()
        self.session.refresh(self.alice)
        self.session.refresh(self.bob)
        for serial in ("d-alice", "d-bob"):
            self.session.add(
                Device(serial=serial, client_instance_id=f"desktop-{serial}")
            )
        self.session.commit()

        app = FastAPI()
        app.include_router(tasks_api.client_router, prefix="/api/client-tasks")
        app.dependency_overrides[get_session] = lambda: self.session
        self.client = TestClient(app)
        self.token = jwt.encode({"sub": "alice"}, SECRET_KEY, algorithm=ALGORITHM)

    def tearDown(self) -> None:
        self.session.close()
        self.engine.dispose()

    def test_requires_token(self):
        resp = self.client.get("/api/client-tasks")
        self.assertEqual(resp.status_code, 401)

        resp = self.client.get("/api/client-tasks/")
        self.assertEqual(resp.status_code, 401)

    def test_jwt_token_unlocks_and_scopes_to_user(self):
        _make_task(self.session, "alice 任务", user_id=self.alice.id, device_serial="d-alice")
        _make_task(self.session, "bob 任务", user_id=self.bob.id, device_serial="d-bob")

        resp = self.client.get(
            "/api/client-tasks",
            headers={"Authorization": f"Bearer {self.token}"},
        )
        self.assertEqual(resp.status_code, 200, resp.text)
        body = resp.json()
        self.assertEqual(body["total"], 1)
        self.assertEqual([item["name"] for item in body["items"]], ["alice 任务"])

    def test_trailing_slash_alias_matches(self):
        _make_task(self.session, "alice 任务", user_id=self.alice.id, device_serial="d-alice")
        headers = {"Authorization": f"Bearer {self.token}"}
        bare = self.client.get("/api/client-tasks", headers=headers)
        slashed = self.client.get("/api/client-tasks/", headers=headers)
        self.assertEqual(bare.status_code, 200, bare.text)
        self.assertEqual(slashed.status_code, 200, slashed.text)
        self.assertEqual(bare.json(), slashed.json())


if __name__ == "__main__":
    unittest.main()
