from datetime import datetime
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field
from sqlmodel import Session, select

from backend.database import get_session
from backend.models import TestCase, TestExecution, TestScenario
from backend.run_control import ABORTED_STATUS, RUNNING_STATUSES, registry

router = APIRouter()


class CancelRunRequest(BaseModel):
    kind: str
    target_id: int
    batch_id: Optional[str] = None
    run_ids: List[str] = Field(default_factory=list)
    execution_ids: List[int] = Field(default_factory=list)
    device_serials: List[str] = Field(default_factory=list)


@router.post("/cancel")
def cancel_runs(payload: CancelRunRequest, session: Session = Depends(get_session)):
    """Cancel active case/scenario runs for the requested target or batch."""
    kind = str(payload.kind or "").strip().lower()
    records = registry.cancel(
        kind=kind,
        target_id=payload.target_id,
        batch_id=payload.batch_id,
        run_ids=payload.run_ids,
        execution_ids=payload.execution_ids,
        device_serials=payload.device_serials,
    )

    db_updates = 0
    if kind == "case":
        db_updates += _mark_case_aborted(session, payload.target_id)
    elif kind == "scenario":
        db_updates += _mark_scenario_executions_aborted(
            session=session,
            scenario_id=payload.target_id,
            batch_id=payload.batch_id,
            execution_ids=payload.execution_ids,
            device_serials=payload.device_serials,
        )

    session.commit()

    return {
        "status": ABORTED_STATUS,
        "cancelled_count": len(records),
        "db_updates": db_updates,
        "runs": [record.to_dict() for record in records],
    }


@router.get("/active")
def active_runs(
    kind: str = Query(...),
    target_id: int = Query(...),
    session: Session = Depends(get_session),
):
    """Return active in-process runs, plus scenario DB runs still marked running."""
    normalized_kind = str(kind or "").strip().lower()
    runs = [record.to_dict() for record in registry.active(kind=normalized_kind, target_id=target_id)]

    if normalized_kind == "scenario":
        existing_execution_ids = {
            int(item["execution_id"])
            for item in runs
            if item.get("execution_id") is not None
        }
        for execution in _active_scenario_executions(session, target_id):
            if execution.id in existing_execution_ids:
                continue
            runs.append(
                {
                    "run_id": None,
                    "kind": "scenario",
                    "target_id": target_id,
                    "batch_id": execution.batch_id,
                    "device_serial": execution.device_serial,
                    "execution_id": execution.id,
                    "status": execution.status,
                    "started_at": execution.start_time.isoformat() if execution.start_time else None,
                    "cancel_requested_at": None,
                    "metadata": {"source": "db"},
                }
            )

    return {"items": runs}


def _mark_case_aborted(session: Session, case_id: int) -> int:
    case = session.get(TestCase, case_id)
    if not case:
        return 0
    case.last_run_status = ABORTED_STATUS
    case.last_run_time = datetime.now()
    session.add(case)
    return 1


def _mark_scenario_executions_aborted(
    *,
    session: Session,
    scenario_id: int,
    batch_id: Optional[str],
    execution_ids: List[int],
    device_serials: List[str],
) -> int:
    query = select(TestExecution).where(
        TestExecution.scenario_id == scenario_id,
        TestExecution.status.in_(RUNNING_STATUSES),
    )
    if batch_id:
        query = query.where(TestExecution.batch_id == batch_id)
    if execution_ids:
        query = query.where(TestExecution.id.in_(execution_ids))
    if device_serials:
        query = query.where(TestExecution.device_serial.in_(device_serials))

    now = datetime.now()
    executions = session.exec(query).all()
    for execution in executions:
        execution.status = ABORTED_STATUS
        execution.end_time = now
        if execution.start_time:
            execution.duration = max((now - execution.start_time).total_seconds(), 0.0)
        session.add(execution)

    if executions:
        scenario = session.get(TestScenario, scenario_id)
        if scenario:
            scenario.last_run_status = ABORTED_STATUS
            scenario.last_run_time = now
            scenario.last_run_duration = int(
                max((now - min(item.start_time or now for item in executions)).total_seconds(), 0.0)
            )
            scenario.last_execution_id = executions[-1].id
            scenario.last_failed_step = "用户终止"
            session.add(scenario)

    return len(executions)


def _active_scenario_executions(session: Session, scenario_id: int) -> List[TestExecution]:
    return session.exec(
        select(TestExecution)
        .where(
            TestExecution.scenario_id == scenario_id,
            TestExecution.status.in_(RUNNING_STATUSES),
        )
        .order_by(TestExecution.start_time.desc())
    ).all()
