"""执行排队（P2.2）核心语义测试：FIFO 顺序、排队位置、取消、超时、设备排队。"""
import threading
import unittest
from unittest.mock import patch

from fastapi import BackgroundTasks
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine

from backend import scenario_execution
from backend.api import cases as cases_api
from backend.api.runs import active_runs
from backend.execution_limiter import (
    ExecutionLimiter,
    QueueAbortedError,
    QueueTimeoutError,
    get_execution_limiter,
    reset_execution_limiter,
)
from backend.models import TestCase, TestExecution, TestScenario, User
from backend.run_control import registry


class ExecutionQueueFifoTests(unittest.TestCase):
    def test_fifo_order_with_single_slot(self):
        """3 个排队任务竞争 1 个槽位，获得槽位顺序 == 提交顺序。"""
        limiter = ExecutionLimiter(max_concurrent_per_user=5, max_global=1)
        holder = limiter.acquire_lease(user_id=99, task_id="holder", timeout=0)

        tickets = [
            limiter.enqueue(user_id=1, task_id=f"task-{i}") for i in range(3)
        ]
        for i, ticket in enumerate(tickets):
            self.assertIsNone(ticket.lease)
            self.assertEqual(ticket.initial_queue_position, i + 1)

        grant_order = []
        granted_events = [threading.Event() for _ in tickets]
        release_events = [threading.Event() for _ in tickets]

        def worker(index):
            lease = tickets[index].wait(timeout=10)
            grant_order.append(index)
            granted_events[index].set()
            release_events[index].wait(10)
            lease.release()

        threads = [
            threading.Thread(target=worker, args=(i,), daemon=True)
            for i in range(3)
        ]
        for thread in threads:
            thread.start()

        holder.release()
        for i in range(3):
            self.assertTrue(granted_events[i].wait(5), f"task-{i} 未按序获得槽位")
            self.assertEqual(grant_order, list(range(i + 1)))
            release_events[i].set()
        for thread in threads:
            thread.join(5)

        stats = limiter.get_stats()
        self.assertEqual(stats["active_tasks"], 0)
        self.assertEqual(stats["queue_length"], 0)

    def test_waiter_for_free_device_not_blocked_by_busy_device_head(self):
        """设备锁独立：队首在等被占设备时，等空闲设备的后续任务可先执行。"""
        limiter = ExecutionLimiter(max_concurrent_per_user=5, max_global=10)
        holder = limiter.acquire_lease(
            user_id=1, device_serial="device-a", task_id="holder", timeout=0
        )

        queued_a = limiter.enqueue(user_id=2, device_serial="device-a", task_id="want-a")
        self.assertIsNone(queued_a.lease)

        direct_b = limiter.enqueue(user_id=3, device_serial="device-b", task_id="want-b")
        self.assertIsNotNone(direct_b.lease, "空闲设备任务不应被队首的设备等待阻塞")

        direct_b.lease.release()
        holder.release()
        lease_a = queued_a.wait(timeout=5)
        self.assertEqual(limiter.get_device_owner("device-a"), 2)
        lease_a.release()


class ExecutionQueuePositionTests(unittest.TestCase):
    def test_queue_position_and_stats(self):
        limiter = ExecutionLimiter(max_concurrent_per_user=5, max_global=1)
        holder = limiter.acquire_lease(user_id=9, task_id="holder", timeout=0)

        first = limiter.enqueue(
            user_id=1, task_id="first", kind="case", target_id=11
        )
        second = limiter.enqueue(
            user_id=2, task_id="second", kind="scenario", target_id=22
        )

        self.assertEqual(limiter.get_queue_position("first"), 1)
        self.assertEqual(limiter.get_queue_position("second"), 2)
        self.assertIsNone(limiter.get_queue_position("missing"))
        self.assertEqual(first.queue_position(), 1)
        self.assertEqual(second.queue_position(), 2)

        stats = limiter.get_stats()
        self.assertEqual(stats["queue_length"], 2)
        self.assertEqual(
            [item["task_id"] for item in stats["queued_tasks"]], ["first", "second"]
        )
        self.assertEqual(stats["queued_tasks"][0]["position"], 1)
        self.assertEqual(stats["queued_tasks"][0]["kind"], "case")
        self.assertEqual(stats["queued_tasks"][0]["target_id"], 11)
        self.assertEqual(stats["queued_tasks"][1]["kind"], "scenario")
        self.assertGreaterEqual(stats["queued_tasks"][0]["waited_seconds"], 0.0)

        # 队首取消后，后续任务位置前移
        first.cancel()
        self.assertEqual(limiter.get_queue_position("second"), 1)
        self.assertEqual(limiter.get_stats()["queue_length"], 1)

        second.cancel()
        holder.release()


class ExecutionQueueCancelTests(unittest.TestCase):
    def test_abort_event_exits_queue_immediately(self):
        limiter = ExecutionLimiter(max_concurrent_per_user=5, max_global=1)
        holder = limiter.acquire_lease(user_id=9, task_id="holder", timeout=0)

        ticket = limiter.enqueue(user_id=1, task_id="queued")
        abort_event = threading.Event()
        result = {}
        done = threading.Event()

        def worker():
            try:
                ticket.wait(timeout=30, abort_event=abort_event, poll_interval=0.05)
                result["outcome"] = "granted"
            except QueueAbortedError:
                result["outcome"] = "aborted"
            except QueueTimeoutError:
                result["outcome"] = "timeout"
            finally:
                done.set()

        thread = threading.Thread(target=worker, daemon=True)
        thread.start()
        abort_event.set()
        self.assertTrue(done.wait(5))
        self.assertEqual(result["outcome"], "aborted")
        self.assertEqual(limiter.get_stats()["queue_length"], 0)

        # 取消的等待者不占槽位：释放后新的请求可直接获得
        holder.release()
        lease = limiter.acquire_lease(user_id=2, task_id="next", timeout=0)
        lease.release()

    def test_cancel_ticket_promotes_later_waiters(self):
        limiter = ExecutionLimiter(max_concurrent_per_user=5, max_global=1)
        holder = limiter.acquire_lease(user_id=9, task_id="holder", timeout=0)

        first = limiter.enqueue(user_id=1, task_id="first")
        second = limiter.enqueue(user_id=2, task_id="second")
        first.cancel()

        holder.release()
        lease = second.wait(timeout=5)
        self.assertIsNotNone(lease)
        lease.release()

    def test_cancel_granted_but_unclaimed_ticket_releases_slot(self):
        limiter = ExecutionLimiter(max_concurrent_per_user=5, max_global=1)
        ticket = limiter.enqueue(user_id=1, task_id="instant")
        self.assertIsNotNone(ticket.lease)

        ticket.cancel()
        stats = limiter.get_stats()
        self.assertEqual(stats["active_tasks"], 0)
        # cancel 幂等
        ticket.cancel()
        self.assertEqual(limiter.get_stats()["active_tasks"], 0)


class ExecutionQueueTimeoutTests(unittest.TestCase):
    def test_wait_timeout_raises_and_leaves_queue(self):
        limiter = ExecutionLimiter(max_concurrent_per_user=5, max_global=1)
        holder = limiter.acquire_lease(user_id=9, task_id="holder", timeout=0)

        ticket = limiter.enqueue(user_id=1, task_id="queued")
        with self.assertRaises(QueueTimeoutError) as context:
            ticket.wait(timeout=0.05)
        self.assertIn("排队超时", str(context.exception))
        self.assertEqual(limiter.get_stats()["queue_length"], 0)

        holder.release()

    def test_timeout_of_head_does_not_block_later_waiters(self):
        limiter = ExecutionLimiter(max_concurrent_per_user=5, max_global=1)
        holder = limiter.acquire_lease(user_id=9, task_id="holder", timeout=0)

        head = limiter.enqueue(user_id=1, task_id="head")
        tail = limiter.enqueue(user_id=2, task_id="tail")
        with self.assertRaises(QueueTimeoutError):
            head.wait(timeout=0.05)

        holder.release()
        lease = tail.wait(timeout=5)
        lease.release()


class ExecutionQueueDeviceTests(unittest.TestCase):
    def test_device_busy_enqueues_instead_of_failing(self):
        limiter = ExecutionLimiter(max_concurrent_per_user=5, max_global=10)
        holder = limiter.acquire_lease(
            user_id=1, device_serial="device-1", task_id="holder", timeout=0
        )

        ticket = limiter.enqueue(
            user_id=2, device_serial="device-1", task_id="queued-device"
        )
        self.assertIsNone(ticket.lease)
        self.assertEqual(ticket.initial_queue_position, 1)
        self.assertTrue(limiter.is_device_busy("device-1"))

        granted = threading.Event()
        holder_released = threading.Event()
        result = {}

        def worker():
            lease = ticket.wait(timeout=5)
            # 必须在 holder 释放之后才能获得设备
            result["after_release"] = holder_released.is_set()
            result["owner"] = limiter.get_device_owner("device-1")
            granted.set()
            lease.release()

        thread = threading.Thread(target=worker, daemon=True)
        thread.start()

        holder_released.set()
        holder.release()
        self.assertTrue(granted.wait(5))
        self.assertTrue(result["after_release"])
        self.assertEqual(result["owner"], 2)
        thread.join(5)


class ExecutionQueueFastFailRegressionTests(unittest.TestCase):
    """原快速失败路径（acquire_lease timeout=0）语义回归。"""

    def test_fast_fail_global_limit_message(self):
        limiter = ExecutionLimiter(max_concurrent_per_user=5, max_global=1)
        lease = limiter.acquire_lease(user_id=1, task_id="run-1", timeout=0)
        try:
            with self.assertRaises(RuntimeError) as context:
                limiter.acquire_lease(user_id=2, task_id="run-2", timeout=0)
            self.assertIn("系统并发已达上限", str(context.exception))
        finally:
            lease.release()
        # 快速失败不残留排队项
        self.assertEqual(limiter.get_stats()["queue_length"], 0)

    def test_fast_fail_user_limit_message(self):
        limiter = ExecutionLimiter(max_concurrent_per_user=1, max_global=10)
        lease = limiter.acquire_lease(user_id=1, task_id="run-1", timeout=0)
        try:
            with self.assertRaises(RuntimeError) as context:
                limiter.acquire_lease(user_id=1, task_id="run-2", timeout=0)
            self.assertIn("您的并发任务已达上限", str(context.exception))
        finally:
            lease.release()

    def test_fast_fail_device_busy_message(self):
        limiter = ExecutionLimiter(max_concurrent_per_user=5, max_global=10)
        lease = limiter.acquire_lease(
            user_id=1, device_serial="device-1", task_id="run-1", timeout=0
        )
        try:
            with self.assertRaises(RuntimeError) as context:
                limiter.acquire_lease(
                    user_id=2, device_serial="device-1", task_id="run-2", timeout=0
                )
            self.assertIn("device-1", str(context.exception))
            self.assertIn("正在被其他任务使用", str(context.exception))
        finally:
            lease.release()
        self.assertEqual(limiter.get_stats()["queue_length"], 0)

    def test_fast_fail_does_not_jump_queue(self):
        """快速失败请求不得越过已排队的等待者抢占槽位。"""
        limiter = ExecutionLimiter(max_concurrent_per_user=5, max_global=1)
        holder = limiter.acquire_lease(user_id=9, task_id="holder", timeout=0)
        queued = limiter.enqueue(user_id=1, task_id="queued")

        with self.assertRaises(RuntimeError):
            limiter.acquire_lease(user_id=2, task_id="fast", timeout=0)

        holder.release()
        # 释放的槽位应归先排队者
        lease = queued.wait(timeout=5)
        lease.release()


class CaseRunQueueApiTests(unittest.TestCase):
    """用例执行链路的排队行为（api/cases.py）。"""

    def setUp(self) -> None:
        reset_execution_limiter()
        registry.clear()
        self.engine = create_engine(
            "sqlite://",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        SQLModel.metadata.create_all(self.engine)
        self.session = Session(self.engine)
        case = TestCase(name="case-queue", steps=[], variables=[])
        self.session.add(case)
        self.session.commit()
        self.session.refresh(case)
        self.case_id = case.id
        self.user = User(id=1, username="runner", hashed_password="x")

    def tearDown(self) -> None:
        self.session.close()
        registry.clear()
        reset_execution_limiter()

    def _session_factory(self):
        return Session(self.engine)

    def test_run_case_queues_when_device_busy(self):
        limiter = get_execution_limiter()
        holder = limiter.acquire_lease(
            user_id=42, device_serial="device-1", task_id="holder", timeout=0
        )
        try:
            resp = cases_api.run_test_case(
                case_id=self.case_id,
                background_tasks=BackgroundTasks(),
                env_id=None,
                device_serial="device-1",
                session=self.session,
                current_user=self.user,
            )
        finally:
            pass

        self.assertTrue(resp["queued"])
        self.assertEqual(resp["queue_position"], 1)

        # 执行记录/registry 标记 QUEUED，且可通过 /runs/active 查询排队位置
        records = registry.active(kind="case", target_id=self.case_id)
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0].status, "QUEUED")

        case = self.session.get(TestCase, self.case_id)
        self.session.refresh(case)
        self.assertEqual(case.last_run_status, "QUEUED")

        payload = active_runs(kind="case", target_id=self.case_id, session=self.session)
        items = payload["items"]
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["status"], "QUEUED")
        self.assertEqual(items[0]["queue_position"], 1)

        # 清理：取消排队项并释放占用
        registry.cancel(kind="case", target_id=self.case_id)
        holder.release()

    def test_run_case_starts_immediately_when_slot_available(self):
        resp = cases_api.run_test_case(
            case_id=self.case_id,
            background_tasks=BackgroundTasks(),
            env_id=None,
            device_serial="device-1",
            session=self.session,
            current_user=self.user,
        )
        self.assertFalse(resp["queued"])
        self.assertIsNone(resp["queue_position"])
        records = registry.active(kind="case", target_id=self.case_id)
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0].status, "RUNNING")

    def test_queued_case_aborts_when_cancelled(self):
        limiter = get_execution_limiter()
        holder = limiter.acquire_lease(
            user_id=42, device_serial="device-1", task_id="holder", timeout=0
        )
        ticket = limiter.enqueue(
            user_id=1,
            device_serial="device-1",
            task_id="queued-run",
            kind="case",
            target_id=self.case_id,
        )
        self.assertIsNone(ticket.lease)
        run_record = cases_api._make_case_run_record(
            case_id=self.case_id,
            batch_id="batch-1",
            device_serial="device-1",
            run_id="queued-run",
            queued=True,
        )

        thread = threading.Thread(
            target=cases_api._run_case_background_cross_platform,
            args=(
                self.case_id,
                self._session_factory,
                None,
                "device-1",
                run_record.run_id,
                run_record.abort_event,
                1,
                ticket,
            ),
            daemon=True,
        )
        thread.start()

        cancelled = registry.cancel(kind="case", target_id=self.case_id)
        self.assertEqual(len(cancelled), 1)
        thread.join(5)
        self.assertFalse(thread.is_alive())

        with Session(self.engine) as session:
            case = session.get(TestCase, self.case_id)
            self.assertEqual(case.last_run_status, "ABORTED")
        # 排队项已退出队列，不占槽位
        self.assertEqual(limiter.get_stats()["queue_length"], 0)
        self.assertEqual(registry.active(kind="case", target_id=self.case_id), [])
        holder.release()

    def test_queued_case_marks_error_on_queue_timeout(self):
        limiter = get_execution_limiter()
        limiter.queue_timeout = 0.05
        holder = limiter.acquire_lease(
            user_id=42, device_serial="device-1", task_id="holder", timeout=0
        )
        ticket = limiter.enqueue(
            user_id=1,
            device_serial="device-1",
            task_id="timeout-run",
            kind="case",
            target_id=self.case_id,
        )
        run_record = cases_api._make_case_run_record(
            case_id=self.case_id,
            batch_id="batch-1",
            device_serial="device-1",
            run_id="timeout-run",
            queued=True,
        )

        thread = threading.Thread(
            target=cases_api._run_case_background_cross_platform,
            args=(
                self.case_id,
                self._session_factory,
                None,
                "device-1",
                run_record.run_id,
                run_record.abort_event,
                1,
                ticket,
            ),
            daemon=True,
        )
        thread.start()
        thread.join(5)
        self.assertFalse(thread.is_alive())

        with Session(self.engine) as session:
            case = session.get(TestCase, self.case_id)
            self.assertEqual(case.last_run_status, "ERROR")
        self.assertEqual(limiter.get_stats()["queue_length"], 0)
        holder.release()


class ScenarioQueueChainTests(unittest.TestCase):
    """场景执行链路的排队行为（scenario_execution.py）。"""

    def setUp(self) -> None:
        reset_execution_limiter()
        registry.clear()
        self.engine = create_engine(
            "sqlite://",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        SQLModel.metadata.create_all(self.engine)
        with Session(self.engine) as session:
            scenario = TestScenario(name="scenario-queue")
            session.add(scenario)
            session.commit()
            session.refresh(scenario)
            execution = TestExecution(
                scenario_id=scenario.id,
                scenario_name=scenario.name,
                status="PENDING",
                executor_name="tester",
                device_serial="device-1",
                batch_id="batch-1",
            )
            session.add(execution)
            session.commit()
            session.refresh(execution)
            self.scenario_id = scenario.id
            self.execution_id = execution.id

    def tearDown(self) -> None:
        registry.clear()
        reset_execution_limiter()

    def test_queued_scenario_aborts_when_cancelled(self):
        limiter = get_execution_limiter()
        holder = limiter.acquire_lease(
            user_id=42, device_serial="device-1", task_id="holder", timeout=0
        )
        ticket = limiter.enqueue(
            user_id=1,
            device_serial="device-1",
            task_id=scenario_execution.scenario_queue_task_id(self.execution_id),
            kind="scenario",
            target_id=self.scenario_id,
        )
        self.assertIsNone(ticket.lease)

        registered = threading.Event()
        original_register = registry.register

        def register_and_signal(**kwargs):
            record = original_register(**kwargs)
            registered.set()
            return record

        done = threading.Event()

        def worker():
            with patch.object(scenario_execution, "engine", self.engine):
                scenario_execution._run_single_device_sync_queued(
                    execution_id=self.execution_id,
                    scenario_id=self.scenario_id,
                    device_serial="device-1",
                    env_id=None,
                    ticket=ticket,
                )
            done.set()

        with patch.object(registry, "register", side_effect=register_and_signal):
            thread = threading.Thread(target=worker, daemon=True)
            thread.start()
            self.assertTrue(registered.wait(5), "排队 run 记录未注册")
            cancelled = registry.cancel(kind="scenario", target_id=self.scenario_id)
            self.assertEqual(len(cancelled), 1)
            self.assertEqual(cancelled[0].execution_id, self.execution_id)
            self.assertTrue(done.wait(5))
            thread.join(5)

        with Session(self.engine) as session:
            execution = session.get(TestExecution, self.execution_id)
            self.assertEqual(execution.status, "ABORTED")
            self.assertIsNotNone(execution.end_time)
        self.assertEqual(limiter.get_stats()["queue_length"], 0)
        holder.release()

    def test_queued_scenario_marks_error_on_queue_timeout(self):
        limiter = get_execution_limiter()
        limiter.queue_timeout = 0.05
        holder = limiter.acquire_lease(
            user_id=42, device_serial="device-1", task_id="holder", timeout=0
        )
        ticket = limiter.enqueue(
            user_id=1,
            device_serial="device-1",
            task_id=scenario_execution.scenario_queue_task_id(self.execution_id),
            kind="scenario",
            target_id=self.scenario_id,
        )

        with patch.object(scenario_execution, "engine", self.engine):
            scenario_execution._run_single_device_sync_queued(
                execution_id=self.execution_id,
                scenario_id=self.scenario_id,
                device_serial="device-1",
                env_id=None,
                ticket=ticket,
            )

        with Session(self.engine) as session:
            execution = session.get(TestExecution, self.execution_id)
            self.assertEqual(execution.status, "ERROR")
        self.assertEqual(limiter.get_stats()["queue_length"], 0)
        self.assertEqual(registry.active(kind="scenario", target_id=self.scenario_id), [])
        holder.release()


if __name__ == "__main__":
    unittest.main()
