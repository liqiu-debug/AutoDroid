"""T1 定时任务云端/客户端双触发协调测试。

契约（与 GET /api/client-tasks ∩ 桌面 should_schedule_locally 口径一致；
P4 终验必修缺陷 2 后统一谓词 = client_scheduled_serials 同源）：
- 「应由客户端调度」的任务（ui + scenario_id + 绑定设备全部命中认领/心跳
  客户端设备）云端注册**观测 job**（终验修正：不注册 job 时触发时刻桌面引擎
  缺席 = 静默漏跑无记录）：触发时只做心跳观测不执行——桌面心跳在线 → 静默
  交本地引擎；无客户端心跳 → warning 日志 + 落 SKIPPED 执行记录。物理任务照
  旧注册执行 job；
- 非 ui 任务（fastbot/inspection/api）桌面不接管（LOCAL_TASK_TYPES={'ui'}），
  即使绑定客户端设备也保留云端注册；
- 无绑定设备任务桌面 execute_task 不触发，保留云端注册；
- 混合绑定（客户端设备 + 物理设备）保留云端 job，但触发时只跑非客户端设备
  （防御设备事后被桌面认领 / 残留 job），客户端设备按心跳观测（缺席记
  SKIPPED），不双跑不漏跑。
"""
import json
import unittest
from datetime import datetime, timedelta
from unittest.mock import MagicMock, patch

from sqlmodel import Session, SQLModel, create_engine, select

from backend.api import tasks as tasks_api
from backend.api.tasks import (
    _run_scheduled_scenario,
    create_task,
    is_client_scheduled_task,
    toggle_task,
    update_task,
)
from backend.models import Device, ScheduledTask, TestExecution, TestScenario, User
from backend.schemas import ScheduledTaskCreate, ScheduledTaskUpdate, TaskStrategy


class FakeScheduler:
    """最小调度器替身：add/remove 维护 job 集合，与「scheduler 有/无 job」断言对应。"""

    def __init__(self):
        self.jobs = {}

    def add_task(self, task_id, strategy, config, job_func, **kwargs):
        self.jobs[task_id] = {"strategy": strategy, "config": config}
        return datetime(2026, 10, 27, 8, 0, 0)

    def remove_task(self, task_id):
        self.jobs.pop(task_id, None)

    def get_next_run_time(self, task_id):
        return datetime(2026, 10, 27, 8, 0, 0) if task_id in self.jobs else None

    def preview_next_run(self, strategy, config):
        return datetime(2026, 10, 27, 8, 0, 0)

    def has_job(self, task_id):
        return task_id in self.jobs


class DualTriggerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = create_engine(
            "sqlite://",
            connect_args={"check_same_thread": False},
        )
        SQLModel.metadata.create_all(self.engine)
        self.session = Session(self.engine)
        self.user = User(username="alice", hashed_password="x", role="user")
        self.session.add(self.user)
        self.session.commit()
        self.session.refresh(self.user)

        self.scenario = TestScenario(name="夜间回归", user_id=self.user.id)
        self.session.add(self.scenario)
        self.session.commit()
        self.session.refresh(self.scenario)

        self.scheduler = FakeScheduler()
        self._scheduler_patch = patch(
            "backend.api.tasks.get_scheduler", return_value=self.scheduler
        )
        self._scheduler_patch.start()

    def tearDown(self) -> None:
        self._scheduler_patch.stop()
        self.session.close()
        self.engine.dispose()

    def _add_device(self, serial: str, **fields) -> Device:
        device = Device(serial=serial, **fields)
        self.session.add(device)
        self.session.commit()
        self.session.refresh(device)
        return device

    def _create(self, name: str, device_serials, strategy_config=None, strategy="DAILY"):
        data = ScheduledTaskCreate(
            name=name,
            scenario_id=self.scenario.id,
            device_serials=list(device_serials),
            strategy=TaskStrategy(strategy),
            strategy_config=strategy_config
            or {"_task_type": "ui", "hour": 8, "minute": 30},
            enable_notification=False,
        )
        return create_task(data=data, session=self.session, current_user=self.user)

    def _task_row(self, task_id: int) -> ScheduledTask:
        return self.session.get(ScheduledTask, task_id)

    # -- 注册口径（终验修正：客户端任务注册观测 job，触发时心跳观测不执行） --

    def test_create_client_task_registers_observation_job(self):
        """客户端任务创建后云端有观测 job（触发时不执行；无心跳时记 SKIPPED）。"""
        self._add_device("d-client", client_instance_id="desktop-1", last_seen_at=datetime.now())
        read = self._create("客户端任务", ["d-client"])

        self.assertTrue(self.scheduler.has_job(read["id"]))
        self.assertIsNotNone(self._task_row(read["id"]).next_run_time)

    def test_create_physical_task_registers_cloud_job(self):
        """物理任务创建后服务端 scheduler 有 job。"""
        self._add_device("d-usb")  # 服务端物理设备：无 client_instance_id / last_seen_at
        read = self._create("物理任务", ["d-usb"])

        self.assertTrue(self.scheduler.has_job(read["id"]))
        self.assertIsNotNone(self._task_row(read["id"]).next_run_time)

    def test_heartbeat_only_device_counts_as_client(self):
        """认领/心跳口径：仅 last_seen_at 心跳也算客户端设备。"""
        self._add_device("d-heartbeat", last_seen_at=datetime.now())
        read = self._create("心跳任务", ["d-heartbeat"])
        self.assertTrue(self.scheduler.has_job(read["id"]))  # 观测 job（触发时不执行）
        self.assertTrue(is_client_scheduled_task(self.session, self._task_row(read["id"])))

    def test_unbound_device_task_stays_cloud_scheduled(self):
        """无绑定设备任务桌面 execute_task 不触发，保留云端注册。"""
        read = self._create("任意设备任务", [])
        self.assertTrue(self.scheduler.has_job(read["id"]))

    def test_mixed_binding_keeps_cloud_job(self):
        """混合绑定保留云端 job（物理设备仍需云端跑），触发时再过滤客户端设备。"""
        self._add_device("d-client", client_instance_id="desktop-1")
        self._add_device("d-usb")
        read = self._create("混合任务", ["d-client", "d-usb"])
        self.assertTrue(self.scheduler.has_job(read["id"]))

    def test_non_ui_task_on_client_device_stays_cloud_scheduled(self):
        """fastbot/inspection/api 桌面不接管（LOCAL_TASK_TYPES={'ui'}），云端照旧注册。"""
        self._add_device("d-client", client_instance_id="desktop-1")
        read = self._create(
            "智能探索任务",
            ["d-client"],
            strategy_config={"_task_type": "fastbot", "fb_package_name": "com.x"},
        )
        self.assertTrue(self.scheduler.has_job(read["id"]))

    def test_ui_task_without_scenario_stays_cloud_scheduled(self):
        self._add_device("d-client", client_instance_id="desktop-1")
        data = ScheduledTaskCreate(
            name="缺场景任务",
            scenario_id=None,
            device_serials=["d-client"],
            strategy=TaskStrategy("DAILY"),
            strategy_config={"_task_type": "fastbot"},
            enable_notification=False,
        )
        # fastbot 无 scenario_id：桌面不接管
        read = create_task(data=data, session=self.session, current_user=self.user)
        self.assertTrue(self.scheduler.has_job(read["id"]))

    # -- 更新 / 开关 --

    def test_update_to_client_device_keeps_observation_job(self):
        self._add_device("d-usb")
        self._add_device("d-client", client_instance_id="desktop-1")
        read = self._create("迁移任务", ["d-usb"])
        self.assertTrue(self.scheduler.has_job(read["id"]))

        update_task(
            task_id=read["id"],
            data=ScheduledTaskUpdate(device_serials=["d-client"]),
            session=self.session,
            current_user=self.user,
        )
        # 迁移到客户端设备后云端保留观测 job（触发时心跳观测不执行）
        self.assertTrue(self.scheduler.has_job(read["id"]))

    def test_update_to_physical_device_registers_cloud_job(self):
        self._add_device("d-client", client_instance_id="desktop-1")
        self._add_device("d-usb")
        read = self._create("迁移任务", ["d-client"])
        self.assertTrue(self.scheduler.has_job(read["id"]))

        update_task(
            task_id=read["id"],
            data=ScheduledTaskUpdate(device_serials=["d-usb"]),
            session=self.session,
            current_user=self.user,
        )
        self.assertTrue(self.scheduler.has_job(read["id"]))

    def test_toggle_client_task_keeps_observation_job(self):
        self._add_device("d-client", client_instance_id="desktop-1")
        read = self._create("客户端任务", ["d-client"])
        self.assertTrue(self.scheduler.has_job(read["id"]))

        toggled = toggle_task(task_id=read["id"], session=self.session, current_user=self.user)
        self.assertFalse(toggled["is_active"])
        self.assertFalse(self.scheduler.has_job(read["id"]), "暂停任务不保留任何 job")

        toggled = toggle_task(task_id=read["id"], session=self.session, current_user=self.user)
        self.assertTrue(toggled["is_active"])
        self.assertTrue(self.scheduler.has_job(read["id"]), "恢复后保留观测 job")

    def test_toggle_physical_task_registers_cloud_job(self):
        self._add_device("d-usb")
        read = self._create("物理任务", ["d-usb"])
        self.assertTrue(self.scheduler.has_job(read["id"]))

        toggle_task(task_id=read["id"], session=self.session, current_user=self.user)
        self.assertFalse(self.scheduler.has_job(read["id"]))

        toggle_task(task_id=read["id"], session=self.session, current_user=self.user)
        self.assertTrue(self.scheduler.has_job(read["id"]))

    # -- 恢复 --

    def test_restore_registers_observation_job_for_client_tasks(self):
        self._add_device("d-client", client_instance_id="desktop-1")
        self._add_device("d-usb")
        client_task = ScheduledTask(
            name="客户端任务",
            scenario_id=self.scenario.id,
            device_serial="d-client",
            strategy="DAILY",
            strategy_config=json.dumps({"_task_type": "ui", "hour": 8, "minute": 30}),
            is_active=True,
            user_id=self.user.id,
        )
        physical_task = ScheduledTask(
            name="物理任务",
            scenario_id=self.scenario.id,
            device_serial="d-usb",
            strategy="DAILY",
            strategy_config=json.dumps({"_task_type": "ui", "hour": 8, "minute": 30}),
            is_active=True,
            user_id=self.user.id,
        )
        self.session.add(client_task)
        self.session.add(physical_task)
        self.session.commit()
        self.session.refresh(client_task)
        self.session.refresh(physical_task)

        with patch("backend.main.engine", self.engine):
            from backend.main import _restore_scheduled_tasks

            _restore_scheduled_tasks()

        # 终验修正：客户端任务也注册（观测 job），物理任务照旧执行 job
        self.assertTrue(self.scheduler.has_job(client_task.id))
        self.assertTrue(self.scheduler.has_job(physical_task.id))

    # -- 触发时过滤（防御残留 job / 事后认领） --

    def test_trigger_skips_client_managed_serials(self):
        self._add_device("d-client", client_instance_id="desktop-1")
        self._add_device("d-usb")
        task = ScheduledTask(
            name="混合任务",
            scenario_id=self.scenario.id,
            device_serial="d-client,d-usb",
            strategy="DAILY",
            strategy_config=json.dumps({"_task_type": "ui", "env_id": 88}),
            is_active=True,
            enable_notification=False,
            user_id=self.user.id,
        )
        self.session.add(task)
        self.session.commit()
        self.session.refresh(task)

        def _fake_precheck(*, session, scenario_id, device_serial, env_id):
            _ = (session, scenario_id, env_id)
            return {"ok": True}

        with patch("backend.database.engine", self.engine), \
             patch("backend.api.scenarios.precheck_scenario_execution", side_effect=_fake_precheck), \
             patch("backend.api.scenarios._summarize_precheck_failure", return_value="blocked"), \
             patch("backend.api.scenarios.execute_scenario_batch_background", return_value="batch-1") as execute_mock:
            _run_scheduled_scenario(task.id)

        execute_mock.assert_called_once_with(
            self.scenario.id,
            "定时任务: 混合任务",
            88,
            ["d-usb"],
        )

    def test_trigger_cancels_when_all_serials_client_managed(self):
        self._add_device("d-client", client_instance_id="desktop-1")
        task = ScheduledTask(
            name="残留任务",
            scenario_id=self.scenario.id,
            device_serial="d-client",
            strategy="DAILY",
            strategy_config=json.dumps({"_task_type": "ui"}),
            is_active=True,
            enable_notification=False,
            user_id=self.user.id,
        )
        self.session.add(task)
        self.session.commit()
        self.session.refresh(task)

        with patch("backend.database.engine", self.engine), \
             patch("backend.api.scenarios.execute_scenario_batch_background", return_value="batch-2") as execute_mock:
            _run_scheduled_scenario(task.id)

        execute_mock.assert_not_called()

    # -- T1 可观测（终验必修缺陷 2）：客户端缺席触发时刻不得静默漏跑 --

    def _add_task(self, name: str, device_serial: str) -> ScheduledTask:
        task = ScheduledTask(
            name=name,
            scenario_id=self.scenario.id,
            device_serial=device_serial,
            strategy="DAILY",
            strategy_config=json.dumps({"_task_type": "ui", "hour": 8, "minute": 30}),
            is_active=True,
            enable_notification=False,
            user_id=self.user.id,
        )
        self.session.add(task)
        self.session.commit()
        self.session.refresh(task)
        return task

    def _executions(self):
        self.session.expire_all()
        return self.session.exec(select(TestExecution)).all()

    def test_trigger_client_task_absent_heartbeat_records_skipped(self):
        """客户端任务触发时刻无心跳（桌面引擎缺席）→ warning + SKIPPED 执行记录。"""
        self._add_device("d-client", client_instance_id="desktop-1")  # 无 last_seen_at：心跳缺席
        task = self._add_task("客户端任务", "d-client")

        with patch("backend.database.engine", self.engine), \
             patch("backend.api.scenarios.execute_scenario_batch_background") as execute_mock:
            _run_scheduled_scenario(task.id)

        execute_mock.assert_not_called()
        executions = self._executions()
        self.assertEqual(len(executions), 1, "桌面缺席必须落 SKIPPED 记录，不得静默漏跑")
        record = executions[0]
        self.assertEqual(record.status, "SKIPPED")
        self.assertEqual(record.scenario_id, self.scenario.id)
        self.assertEqual(record.device_serial, "d-client")
        self.assertTrue(record.scenario_name)

    def test_trigger_client_task_stale_heartbeat_records_skipped(self):
        """心跳过期（> CLIENT_HEARTBEAT_TIMEOUT_SECONDS）同视为桌面缺席。"""
        stale = datetime.now() - timedelta(minutes=30)
        self._add_device("d-client", client_instance_id="desktop-1", last_seen_at=stale)
        task = self._add_task("客户端任务", "d-client")

        with patch("backend.database.engine", self.engine), \
             patch("backend.api.scenarios.execute_scenario_batch_background") as execute_mock:
            _run_scheduled_scenario(task.id)

        execute_mock.assert_not_called()
        self.assertEqual([e.status for e in self._executions()], ["SKIPPED"])

    def test_trigger_client_task_fresh_heartbeat_records_nothing(self):
        """桌面心跳在线：静默交本地引擎执行，云端不重复执行也不落 SKIPPED。"""
        self._add_device("d-client", client_instance_id="desktop-1", last_seen_at=datetime.now())
        task = self._add_task("客户端任务", "d-client")

        with patch("backend.database.engine", self.engine), \
             patch("backend.api.scenarios.execute_scenario_batch_background") as execute_mock:
            _run_scheduled_scenario(task.id)

        execute_mock.assert_not_called()
        self.assertEqual(self._executions(), [], "心跳在线时客户端会跑，不得重复记 SKIPPED")

    def test_mixed_trigger_records_skipped_for_absent_client_serial(self):
        """混合绑定：物理设备照跑；客户端设备缺席 → SKIPPED 记录补可观测。"""
        self._add_device("d-client", client_instance_id="desktop-1")  # 心跳缺席
        self._add_device("d-usb")
        task = self._add_task("混合任务", "d-client,d-usb")

        def _fake_precheck(*, session, scenario_id, device_serial, env_id):
            _ = (session, scenario_id, env_id)
            return {"ok": True}

        with patch("backend.database.engine", self.engine), \
             patch("backend.api.scenarios.precheck_scenario_execution", side_effect=_fake_precheck), \
             patch("backend.api.scenarios._summarize_precheck_failure", return_value="blocked"), \
             patch("backend.api.scenarios.execute_scenario_batch_background", return_value="batch-3") as execute_mock:
            _run_scheduled_scenario(task.id)

        execute_mock.assert_called_once_with(self.scenario.id, "定时任务: 混合任务", None, ["d-usb"])
        executions = self._executions()
        self.assertEqual([e.status for e in executions], ["SKIPPED"])
        self.assertEqual(executions[0].device_serial, "d-client")

    def test_mixed_trigger_fresh_heartbeat_records_nothing(self):
        """混合绑定 + 桌面心跳在线：客户端设备交本地引擎，云端只跑物理设备。"""
        self._add_device("d-client", client_instance_id="desktop-1", last_seen_at=datetime.now())
        self._add_device("d-usb")
        task = self._add_task("混合任务", "d-client,d-usb")

        def _fake_precheck(*, session, scenario_id, device_serial, env_id):
            _ = (session, scenario_id, env_id)
            return {"ok": True}

        with patch("backend.database.engine", self.engine), \
             patch("backend.api.scenarios.precheck_scenario_execution", side_effect=_fake_precheck), \
             patch("backend.api.scenarios._summarize_precheck_failure", return_value="blocked"), \
             patch("backend.api.scenarios.execute_scenario_batch_background", return_value="batch-4") as execute_mock:
            _run_scheduled_scenario(task.id)

        execute_mock.assert_called_once_with(self.scenario.id, "定时任务: 混合任务", None, ["d-usb"])
        self.assertEqual(self._executions(), [])

    # -- 判定函数 --

    def test_is_client_scheduled_task_requires_all_serials_client(self):
        self._add_device("d-client", client_instance_id="desktop-1")
        self._add_device("d-usb")
        client_only = ScheduledTask(
            name="a",
            scenario_id=1,
            device_serial="d-client",
            strategy="DAILY",
            strategy_config="{}",
        )
        mixed = ScheduledTask(
            name="b",
            scenario_id=1,
            device_serial="d-client,d-usb",
            strategy="DAILY",
            strategy_config="{}",
        )
        self.assertTrue(is_client_scheduled_task(self.session, client_only))
        self.assertFalse(is_client_scheduled_task(self.session, mixed))

    def test_is_client_scheduled_task_ignores_unknown_serials(self):
        """未入库 serial 不是客户端设备 → 云端保留。"""
        self._add_device("d-client", client_instance_id="desktop-1")
        task = ScheduledTask(
            name="a",
            scenario_id=1,
            device_serial="d-client,d-missing",
            strategy="DAILY",
            strategy_config="{}",
        )
        self.assertFalse(is_client_scheduled_task(self.session, task))


class RestoreUsesSharedHelperTests(unittest.TestCase):
    """_restore_scheduled_tasks 统一走 tasks._sync_cloud_schedule（同一判定口径）。"""

    def test_restore_calls_shared_sync_helper(self):
        engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
        SQLModel.metadata.create_all(engine)
        session = Session(engine)
        try:
            user = User(username="alice", hashed_password="x", role="user")
            session.add(user)
            session.commit()
            session.refresh(user)
            scenario = TestScenario(name="s", user_id=user.id)
            session.add(scenario)
            session.commit()
            session.refresh(scenario)
            task = ScheduledTask(
                name="t",
                scenario_id=scenario.id,
                device_serial="d-usb",
                strategy="DAILY",
                strategy_config=json.dumps({"_task_type": "ui"}),
                is_active=True,
                user_id=user.id,
            )
            session.add(task)
            session.commit()
            session.refresh(task)

            scheduler = MagicMock()
            scheduler.preview_next_run.return_value = None
            scheduler.add_task.return_value = datetime(2026, 10, 27, 8, 0, 0)
            with patch("backend.main.engine", engine), \
                 patch("backend.api.tasks.get_scheduler", return_value=scheduler):
                from backend.main import _restore_scheduled_tasks

                _restore_scheduled_tasks()

            scheduler.add_task.assert_called_once()
            self.assertEqual(scheduler.add_task.call_args.kwargs.get("task_id"), task.id)
        finally:
            session.close()
            engine.dispose()


if __name__ == "__main__":
    unittest.main()
