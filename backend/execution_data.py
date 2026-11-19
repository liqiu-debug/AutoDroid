"""Read-only execution inputs shared by desktop prechecks and runners.

Keep raw inputs here: device selection, variable rendering and prechecks belong to
the desktop engine. Reading this snapshot must never migrate legacy steps.
"""
from __future__ import annotations

from typing import Any, Dict, Iterable, Optional

from fastapi import HTTPException
from fastapi.encoders import jsonable_encoder
from sqlmodel import Session, select

from backend.cross_platform_execution import load_app_key_mapping
from backend.models import (
    Environment,
    GlobalVariable,
    ScenarioStep,
    SystemSetting,
    TestCase,
    TestCaseStep,
    TestScenario,
)


def _case_payload(case: TestCase) -> Dict[str, Any]:
    return jsonable_encoder(
        {
            "id": case.id,
            "name": case.name,
            "variables": case.variables or [],
            "steps": case.steps or [],
            "version": int(case.version or 1),
            "updated_at": case.updated_at,
        }
    )


def _case_inputs(session: Session, case_ids: Iterable[int]) -> Dict[str, Any]:
    ids = sorted(set(case_ids))
    if not ids:
        return {}

    # Import lazily: the cases router also exposes these shared input readers.
    from backend.api.cases import _row_to_standard_step_read

    cases = session.exec(select(TestCase).where(TestCase.id.in_(ids))).all()
    result = {
        str(case.id): {"case": _case_payload(case), "standard_steps": []}
        for case in cases
    }
    rows = session.exec(
        select(TestCaseStep)
        .where(TestCaseStep.case_id.in_(ids))
        .order_by(TestCaseStep.case_id, TestCaseStep.order, TestCaseStep.id)
    ).all()
    for row in rows:
        item = result.get(str(row.case_id))
        if item is not None:
            item["standard_steps"].append(jsonable_encoder(_row_to_standard_step_read(row)))
    return result


def _context_inputs(session: Session, env_id: Optional[int]) -> Dict[str, Any]:
    variables: Dict[str, str] = {}
    if env_id is not None:
        if session.get(Environment, env_id) is None:
            raise HTTPException(status_code=404, detail="环境不存在")
        rows = session.exec(
            select(GlobalVariable)
            .where(GlobalVariable.env_id == env_id)
            .order_by(GlobalVariable.id)
        ).all()
        variables = {row.key: "" if row.value is None else str(row.value) for row in rows}
    settings = session.exec(
        select(SystemSetting).where(
            SystemSetting.key.in_(("app_key_map", "app_key_mapping", "android_app_map", "ios_app_map"))
        )
    ).all()
    return {
        "env_variables": variables,
        "app_key_mapping": load_app_key_mapping(
            session, settings_map={row.key: row.value for row in settings}
        ),
    }


def case_execution_data(
    session: Session, case_id: int, env_id: Optional[int] = None
) -> Dict[str, Any]:
    with session.no_autoflush:
        cases = _case_inputs(session, [case_id])
        item = cases.get(str(case_id))
        if item is None:
            raise HTTPException(status_code=404, detail="用例不存在")
        return {"schema_version": 1, **item, **_context_inputs(session, env_id)}


def scenario_execution_data(
    session: Session, scenario_id: int, env_id: Optional[int] = None
) -> Dict[str, Any]:
    with session.no_autoflush:
        scenario = session.get(TestScenario, scenario_id)
        if scenario is None:
            raise HTTPException(status_code=404, detail="场景不存在")
        rows = session.exec(
            select(ScenarioStep)
            .where(ScenarioStep.scenario_id == scenario_id)
            .order_by(ScenarioStep.order, ScenarioStep.id)
        ).all()
        return {
            "schema_version": 1,
            "scenario": jsonable_encoder(
                {
                    "id": scenario.id,
                    "name": scenario.name,
                    "version": int(scenario.version or 1),
                    "updated_at": scenario.updated_at,
                }
            ),
            "scenario_steps": [
                {"id": row.id, "case_id": row.case_id, "order": row.order, "alias": row.alias}
                for row in rows
            ],
            "cases": _case_inputs(session, [row.case_id for row in rows]),
            **_context_inputs(session, env_id),
        }
