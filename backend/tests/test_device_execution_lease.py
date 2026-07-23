import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from sqlmodel import Session, SQLModel, create_engine, select

from backend.device_execution_lease import (
    DeviceExecutionLease,
    DeviceLeaseUnavailable,
)
from backend.execution_limiter import get_execution_limiter, reset_execution_limiter
from backend.models import Device


class DeviceExecutionLeaseTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        database_path = Path(self.temp_dir.name) / "lease.db"
        self.engine = create_engine(
            f"sqlite:///{database_path}",
            connect_args={"check_same_thread": False},
        )
        SQLModel.metadata.create_all(self.engine)
        with Session(self.engine) as session:
            session.add(
                Device(
                    serial="android-lease-1",
                    platform="android",
                    status="IDLE",
                )
            )
            session.commit()
        reset_execution_limiter()

    def tearDown(self) -> None:
        reset_execution_limiter()
        self.engine.dispose()
        self.temp_dir.cleanup()

    def _device(self) -> Device:
        with Session(self.engine) as session:
            return session.exec(
                select(Device).where(Device.serial == "android-lease-1")
            ).one()

    def test_acquire_and_release_atomically_updates_owner_fields(self):
        lease = DeviceExecutionLease.acquire(
            user_id=1,
            serial="android-lease-1",
            task_id="inspection:7",
            kind="inspection",
            timeout=0.1,
            check_legacy_fastbot=False,
            db_engine=self.engine,
        )
        busy = self._device()
        self.assertEqual(busy.status, "BUSY")
        self.assertEqual(busy.lease_task_id, "inspection:7")
        self.assertEqual(busy.lease_kind, "inspection")
        self.assertIsNotNone(busy.lease_acquired_at)

        self.assertTrue(lease.release())
        idle = self._device()
        self.assertEqual(idle.status, "IDLE")
        self.assertIsNone(idle.lease_task_id)
        self.assertIsNone(idle.lease_kind)
        self.assertIsNone(idle.lease_acquired_at)
        self.assertEqual(get_execution_limiter().get_stats()["active_tasks"], 0)

    def test_failed_atomic_claim_releases_limiter_capacity(self):
        with Session(self.engine) as session:
            device = session.exec(
                select(Device).where(Device.serial == "android-lease-1")
            ).one()
            device.status = "BUSY"
            device.lease_task_id = "scenario:existing"
            session.add(device)
            session.commit()

        with self.assertRaises(DeviceLeaseUnavailable):
            DeviceExecutionLease.acquire(
                user_id=2,
                serial="android-lease-1",
                task_id="inspection:8",
                kind="inspection",
                timeout=0.1,
                check_legacy_fastbot=False,
                db_engine=self.engine,
            )

        current = self._device()
        self.assertEqual(current.status, "BUSY")
        self.assertEqual(current.lease_task_id, "scenario:existing")
        self.assertEqual(get_execution_limiter().get_stats()["active_tasks"], 0)

    def test_legacy_fastbot_guard_releases_limiter_capacity(self):
        with patch(
            "backend.device_execution_lease.legacy_fastbot_device_locked",
            return_value=True,
        ), self.assertRaises(DeviceLeaseUnavailable):
            DeviceExecutionLease.acquire(
                user_id=5,
                serial="android-lease-1",
                task_id="inspection:legacy-race",
                kind="inspection",
                timeout=0.1,
                db_engine=self.engine,
            )
        self.assertEqual(self._device().status, "IDLE")
        self.assertEqual(get_execution_limiter().get_stats()["active_tasks"], 0)

    def test_release_never_clears_a_different_owner(self):
        lease = DeviceExecutionLease.acquire(
            user_id=3,
            serial="android-lease-1",
            task_id="inspection:9",
            kind="inspection",
            timeout=0.1,
            check_legacy_fastbot=False,
            db_engine=self.engine,
        )
        with Session(self.engine) as session:
            device = session.exec(
                select(Device).where(Device.serial == "android-lease-1")
            ).one()
            device.lease_task_id = "fastbot:replacement"
            device.lease_kind = "fastbot"
            session.add(device)
            session.commit()

        self.assertFalse(lease.release())
        current = self._device()
        self.assertEqual(current.status, "BUSY")
        self.assertEqual(current.lease_task_id, "fastbot:replacement")
        self.assertEqual(current.lease_kind, "fastbot")
        self.assertEqual(get_execution_limiter().get_stats()["active_tasks"], 0)

    def test_disconnect_status_is_restored_when_owner_releases(self):
        lease = DeviceExecutionLease.acquire(
            user_id=4,
            serial="android-lease-1",
            task_id="inspection:disconnect",
            kind="inspection",
            timeout=0.1,
            check_legacy_fastbot=False,
            db_engine=self.engine,
        )
        with Session(self.engine) as session:
            device = session.exec(
                select(Device).where(Device.serial == "android-lease-1")
            ).one()
            device.status = "OFFLINE"
            session.add(device)
            session.commit()

        self.assertTrue(lease.release())
        current = self._device()
        self.assertEqual(current.status, "IDLE")
        self.assertIsNone(current.lease_task_id)


if __name__ == "__main__":
    unittest.main()
