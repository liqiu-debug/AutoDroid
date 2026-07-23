"""Owner-safe device lease for long-running execution families."""
from __future__ import annotations

import logging
import threading
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Optional

from sqlalchemy import func, or_, update
from sqlmodel import Session, select

from backend.database import engine
from backend.execution_limiter import (
    ExecutionLease,
    ExecutionTicket,
    QueueAbortedError,
    QueueTimeoutError,
    get_execution_limiter,
)
from backend.models import Device

logger = logging.getLogger(__name__)


class DeviceLeaseUnavailable(RuntimeError):
    """The selected device could not be leased without a race."""


def legacy_fastbot_device_locked(serial: str) -> bool:
    """Keep Fastbot's process lock as a migration-time compatibility guard."""
    try:
        from backend.api.fastbot import _is_device_busy

        return bool(_is_device_busy(serial))
    except Exception:
        return False


@dataclass
class DeviceExecutionLease:
    serial: str
    task_id: str
    kind: str
    limiter_lease: ExecutionLease
    db_engine: Any = engine
    _released: bool = False

    @classmethod
    def acquire(
        cls,
        *,
        user_id: int,
        serial: str,
        task_id: str,
        kind: str,
        abort_event: Optional[threading.Event] = None,
        timeout: float = 1800.0,
        check_legacy_fastbot: bool = True,
        db_engine=None,
    ) -> "DeviceExecutionLease":
        """Acquire limiter capacity, then atomically transition IDLE -> BUSY."""
        limiter = get_execution_limiter()
        ticket: ExecutionTicket = limiter.enqueue(
            user_id=int(user_id or 0),
            device_serial=serial,
            task_id=task_id,
            kind=kind,
        )
        limiter_lease: Optional[ExecutionLease] = None
        try:
            limiter_lease = ticket.wait(timeout=timeout, abort_event=abort_event)
            if check_legacy_fastbot and legacy_fastbot_device_locked(serial):
                raise DeviceLeaseUnavailable(
                    f"设备 {serial} 已被 Fastbot 进程锁占用"
                )

            target_engine = db_engine or engine
            with Session(target_engine) as session:
                statement = (
                    update(Device)
                    .where(
                        Device.serial == serial,
                        func.lower(Device.platform) == "android",
                        func.upper(Device.status) == "IDLE",
                        or_(Device.lease_task_id.is_(None), Device.lease_task_id == ""),
                    )
                    .values(
                        status="BUSY",
                        lease_task_id=task_id,
                        lease_kind=kind,
                        lease_acquired_at=datetime.now(),
                        updated_at=datetime.now(),
                    )
                )
                result = session.exec(statement)
                if int(getattr(result, "rowcount", 0) or 0) != 1:
                    session.rollback()
                    device = session.exec(
                        select(Device).where(Device.serial == serial)
                    ).first()
                    if device is None:
                        raise DeviceLeaseUnavailable(f"设备不存在: {serial}")
                    if str(device.platform or "").lower() != "android":
                        raise DeviceLeaseUnavailable(
                            f"智能巡检仅支持 Android 设备: {serial}"
                        )
                    raise DeviceLeaseUnavailable(
                        f"设备 {serial} 非空闲或已被其他任务租用 "
                        f"(status={device.status}, owner={device.lease_task_id or '-'})"
                    )
                session.commit()

            logger.info(
                "device execution lease acquired: serial=%s kind=%s task=%s",
                serial,
                kind,
                task_id,
            )
            return cls(
                serial=serial,
                task_id=task_id,
                kind=kind,
                limiter_lease=limiter_lease,
                db_engine=target_engine,
            )
        except (QueueTimeoutError, QueueAbortedError) as exc:
            ticket.cancel()
            raise DeviceLeaseUnavailable(str(exc)) from exc
        except Exception:
            if limiter_lease is not None:
                limiter_lease.release()
            else:
                ticket.cancel()
            raise

    def release(self) -> bool:
        """Restore IDLE only if this exact owner still holds the DB lease."""
        if self._released:
            return False
        self._released = True
        restored = False
        try:
            with Session(self.db_engine) as session:
                result = session.exec(
                    update(Device)
                    .where(
                        Device.serial == self.serial,
                        Device.lease_task_id == self.task_id,
                    )
                    .values(
                        status="IDLE",
                        lease_task_id=None,
                        lease_kind=None,
                        lease_acquired_at=None,
                        updated_at=datetime.now(),
                    )
                )
                restored = int(getattr(result, "rowcount", 0) or 0) == 1
                session.commit()
        finally:
            self.limiter_lease.release()
        logger.info(
            "device execution lease released: serial=%s kind=%s task=%s restored=%s",
            self.serial,
            self.kind,
            self.task_id,
            restored,
        )
        return restored

    def __enter__(self) -> "DeviceExecutionLease":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.release()
