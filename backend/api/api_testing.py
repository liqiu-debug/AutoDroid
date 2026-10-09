from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import HTMLResponse
from sqlalchemy import update
from sqlmodel import Session, select, func, delete

from backend.api.deps import get_current_active_user, ensure_owner_or_admin
from backend.database import get_session
from backend.models import (
    ApiDefinition,
    ApiFolder,
    ApiRun,
    ApiScenario,
    ApiScenarioStep,
    ApiStepResult,
    Environment,
    ScheduledTask,
    User,
)
from backend.api_testing import service
from backend.api_testing.ai_routes import router as ai_router
from backend.api_testing.curl import parse_curl
from backend.api_testing.reporting import render_report, report_warnings, summary
from backend.api_testing.schemas import (
    CurlInput,
    DebugCreate,
    DebugExecute,
    DefinitionConfig,
    DefinitionWrite,
    FolderWrite,
    PrecheckRequest,
    RunRequest,
    ScenarioWrite,
    SpecApplyRequest,
    SpecInput,
    Step,
    SyncApplyRequest,
)
from backend.api_testing.security import MASK
from backend.api_testing.specs import parse_spec
from backend.api_testing.values import apply_sync, field_tree, sync_preview, validate_steps, at_path

router = APIRouter(dependencies=[Depends(get_current_active_user)])
router.include_router(ai_router)


def require(session, model, key):
    value = session.get(model, key)
    if not value:
        raise HTTPException(404, "记录不存在")
    return value


def check_folder(session, folder_id, kind):
    if folder_id is not None and require(session, ApiFolder, folder_id).kind != kind:
        raise HTTPException(422, "目录类型不匹配")


def asset_read(session, row, users=None):
    data = row.model_dump()
    def name(user_id):
        user = users.get(user_id) if users is not None else session.get(User, user_id) if user_id else None
        return (user.full_name or user.username) if user else None
    data["creator_name"] = name(row.user_id)
    data["updater_name"] = name(row.updater_id)
    return data


def definition_read(session, row, users=None):
    data = asset_read(session, row, users)
    data["sample_fields"] = (row.sample or {}).get("fields", [])
    data["sample"] = (row.sample or {}).get("response")
    return data


def scenario_read(session, row):
    data = asset_read(session, row)
    data["steps"] = [s.model_dump() for s in service.scenario_steps(session, row.id)]
    return data


def sample_data(sample, config, types=()):
    if sample is None:
        return None
    fields = field_tree(sample)[0]["children"]
    type_map = {tuple(item.path): item.type for item in types}

    def restore_types(nodes):
        for node in nodes:
            # Keep type metadata when re-saving legacy masked samples.
            if at_path(sample, node["path"], None) == MASK and tuple(node["path"]) in type_map:
                node["type"] = type_map[tuple(node["path"])]
            restore_types(node.get("children", []))

    restore_types(fields)
    return {"response": sample, "fields": fields}


def definitions_values(payload):
    errors = validate_steps([Step(id="interface", snapshot=payload.config)])
    if errors:
        raise HTTPException(422, "公共接口不能引用场景步骤，请在场景内配置引用")
    return {
        "name": payload.name,
        "description": payload.description,
        "folder_id": payload.folder_id,
        "config": payload.config.model_dump(),
        "sample": sample_data(payload.sample, payload.config, payload.sample_types),
        "updated_at": datetime.now(),
    }


def optimistic_update(session, model, key, version, values):
    if version is None:
        raise HTTPException(409, "缺少版本号，请刷新后重试")
    result = session.execute(
        update(model).where(model.id == key, model.version == version).values(**values, version=version + 1)
    )
    if result.rowcount != 1:
        session.rollback()
        raise HTTPException(409, "记录已被其他人修改，请刷新后合并更改")


@router.post("/imports/curl")
def import_curl(payload: CurlInput):
    try:
        spec = parse_curl(payload.command)
    except (ValueError, TypeError) as exc:
        raise HTTPException(422, str(exc)) from None
    return {
        "config": DefinitionConfig(request=spec).model_dump(),
        "warning": "仅解析，尚未发送请求。请将鉴权值替换为敏感环境变量。",
    }


@router.post("/imports/spec")
def import_spec(payload: SpecInput):
    """Parse an OpenAPI/Swagger/Postman document. Never sends a request."""
    try:
        return parse_spec(payload.content)
    except (ValueError, TypeError) as exc:
        raise HTTPException(422, str(exc)) from None


@router.post("/imports/apply")
def apply_spec(
    payload: SpecApplyRequest,
    session: Session = Depends(get_session),
    user: User = Depends(get_current_active_user),
):
    """Create the selected entries in one transaction: all of them, or none."""
    check_folder(session, payload.folder_id, "interface")
    created = []
    for item in payload.items:
        row = ApiDefinition(
            **{**definitions_values(item), "folder_id": payload.folder_id},
            user_id=user.id,
            updater_id=user.id,
        )
        session.add(row)
        created.append(row)
    session.commit()
    for row in created:
        session.refresh(row)
    return {"created": [definition_read(session, row) for row in created]}


@router.get("/folders")
def folders(session: Session = Depends(get_session)):
    return session.exec(select(ApiFolder).order_by(ApiFolder.id)).all()


@router.post("/folders")
def create_folder(
    payload: FolderWrite, session: Session = Depends(get_session), user: User = Depends(get_current_active_user)
):
    check_folder(session, payload.parent_id, payload.kind)
    row = ApiFolder(**payload.model_dump(), user_id=user.id)
    session.add(row)
    session.commit()
    session.refresh(row)
    return row


@router.put("/folders/{folder_id}")
def update_folder(folder_id: int, payload: FolderWrite, session: Session = Depends(get_session)):
    row = require(session, ApiFolder, folder_id)
    if row.kind != payload.kind:
        raise HTTPException(422, "不能改变目录类型")
    check_folder(session, payload.parent_id, payload.kind)
    parent = payload.parent_id
    while parent:
        if parent == folder_id:
            raise HTTPException(422, "不能将目录移动到自身或子目录")
        parent = require(session, ApiFolder, parent).parent_id
    row.name, row.parent_id = payload.name, payload.parent_id
    session.add(row)
    session.commit()
    session.refresh(row)
    return row


@router.delete("/folders/{folder_id}")
def delete_folder(
    folder_id: int, session: Session = Depends(get_session), user: User = Depends(get_current_active_user)
):
    row = require(session, ApiFolder, folder_id)
    ensure_owner_or_admin(row.user_id, user)
    if (
        session.exec(select(ApiFolder).where(ApiFolder.parent_id == folder_id)).first()
        or session.exec(select(ApiDefinition).where(ApiDefinition.folder_id == folder_id)).first()
        or session.exec(select(ApiScenario).where(ApiScenario.folder_id == folder_id)).first()
    ):
        raise HTTPException(409, "目录非空，请先移动或删除其中内容")
    session.delete(row)
    session.commit()
    return {"ok": True}


def list_rows(session, model, keyword, folder_id, skip, limit):
    query = select(model)
    if keyword:
        query = query.where(model.name.contains(keyword))
    if folder_id is not None:
        query = query.where(model.folder_id == folder_id) if folder_id else query.where(model.folder_id.is_(None))
    total = session.exec(select(func.count()).select_from(query.subquery())).one()
    return total, session.exec(query.order_by(model.updated_at.desc()).offset(skip).limit(limit)).all()


def asset_users(session, rows):
    ids = {uid for row in rows for uid in (row.user_id, row.updater_id) if uid is not None}
    return {user.id: user for user in session.exec(select(User).where(User.id.in_(ids))).all()} if ids else {}


@router.get("/interfaces")
def list_interfaces(
    keyword: str = "",
    folder_id: Optional[int] = None,
    skip: int = Query(0, ge=0),
    limit: int = Query(100, ge=1, le=1000),
    session: Session = Depends(get_session),
):
    total, rows = list_rows(session, ApiDefinition, keyword, folder_id, skip, limit)
    users = asset_users(session, rows)
    return {"total": total, "items": [definition_read(session, row, users) for row in rows]}


@router.post("/interfaces")
def create_interface(
    payload: DefinitionWrite, session: Session = Depends(get_session), user: User = Depends(get_current_active_user)
):
    check_folder(session, payload.folder_id, "interface")
    row = ApiDefinition(**definitions_values(payload), user_id=user.id, updater_id=user.id)
    session.add(row)
    session.commit()
    session.refresh(row)
    return definition_read(session, row)


@router.get("/interfaces/{interface_id}")
def get_interface(interface_id: int, session: Session = Depends(get_session)):
    return definition_read(session, require(session, ApiDefinition, interface_id))


@router.put("/interfaces/{interface_id}")
def update_interface(interface_id: int, payload: DefinitionWrite, session: Session = Depends(get_session),
                     user: User = Depends(get_current_active_user)):
    require(session, ApiDefinition, interface_id)
    check_folder(session, payload.folder_id, "interface")
    optimistic_update(session, ApiDefinition, interface_id, payload.version,
                      {**definitions_values(payload), "updater_id": user.id})
    session.commit()
    session.expire_all()
    return definition_read(session, require(session, ApiDefinition, interface_id))


@router.delete("/interfaces/{interface_id}")
def delete_interface(
    interface_id: int, session: Session = Depends(get_session), user: User = Depends(get_current_active_user)
):
    row = require(session, ApiDefinition, interface_id)
    ensure_owner_or_admin(row.user_id, user)
    ids = session.exec(select(ApiScenarioStep.scenario_id).where(ApiScenarioStep.interface_id == interface_id)).all()
    if ids:
        names = session.exec(select(ApiScenario.name).where(ApiScenario.id.in_(ids))).all()
        raise HTTPException(409, {"message": "接口被场景引用，不能删除", "scenarios": names})
    session.delete(row)
    session.commit()
    return {"ok": True}


@router.post("/interfaces/{interface_id}/sync-preview")
def preview_sync(interface_id: int, step: Step, session: Session = Depends(get_session)):
    row = require(session, ApiDefinition, interface_id)
    if step.interface_id != interface_id:
        raise HTTPException(422, "接口与步骤不匹配")
    return sync_preview(step, DefinitionConfig.model_validate(row.config), row.version)


def step_uses_interface(definition, interface_id, interface_version):
    """A step is stale when its frozen template version trails the interface."""
    if definition.get("interface_id") != interface_id:
        return False
    return definition.get("interface_version") != interface_version


@router.get("/interfaces/{interface_id}/usages")
def interface_usages(interface_id: int, session: Session = Depends(get_session)):
    """Where this interface is used, and which steps are behind the current version."""
    row = require(session, ApiDefinition, interface_id)
    config = DefinitionConfig.model_validate(row.config)
    rows = session.exec(select(ApiScenarioStep).where(ApiScenarioStep.interface_id == interface_id)).all()
    scenarios = {
        item.id: item
        for item in session.exec(
            select(ApiScenario).where(ApiScenario.id.in_({r.scenario_id for r in rows}))
        ).all()
    } if rows else {}
    grouped, total, stale = {}, 0, 0
    for step_row in sorted(rows, key=lambda r: (r.scenario_id, r.position)):
        step = Step.model_validate(step_row.definition)
        behind = step_uses_interface(step_row.definition, interface_id, row.version)
        total += 1
        stale += behind
        _, conflicts = (None, sync_preview(step, config, row.version)["conflicts"]) if behind else (None, [])
        grouped.setdefault(step_row.scenario_id, []).append(
            {
                "step_id": step_row.step_id,
                "name": step.name,
                "position": step_row.position,
                "interface_version": step.interface_version,
                "stale": behind,
                "conflicts": conflicts,
            }
        )
    items = [
        {
            "scenario_id": scenario_id,
            "scenario_name": scenarios[scenario_id].name if scenario_id in scenarios else "已删除的场景",
            "folder_id": scenarios[scenario_id].folder_id if scenario_id in scenarios else None,
            "version": scenarios[scenario_id].version if scenario_id in scenarios else None,
            "steps": steps,
        }
        for scenario_id, steps in grouped.items()
    ]
    return {
        "interface_id": interface_id,
        "version": row.version,
        "total_scenarios": len(items),
        "total_steps": total,
        "stale_steps": stale,
        "items": items,
    }


def apply_sync_item(db_engine, interface_id, config, interface_version, item):
    """One scenario step per transaction: a refusal must not affect the others."""
    identity = {"scenario_id": item.scenario_id, "step_id": item.step_id, "ok": False, "changed": False}

    def refuse(message, **extra):
        return {**identity, "error": message, **extra}

    try:
        with Session(db_engine) as session:
            scenario = session.get(ApiScenario, item.scenario_id)
            if not scenario or scenario.version != item.version:
                return refuse("场景已被其他人修改，请刷新后重试")
            if session.exec(
                select(ApiRun).where(ApiRun.scenario_id == item.scenario_id, ApiRun.status.in_(service.ACTIVE))
            ).first():
                return refuse("场景正在运行，请先结束执行")
            step_row = session.exec(
                select(ApiScenarioStep).where(
                    ApiScenarioStep.scenario_id == item.scenario_id, ApiScenarioStep.step_id == item.step_id
                )
            ).first()
            if not step_row or step_row.interface_id != interface_id:
                return refuse("步骤不存在或未引用该接口")
            step = Step.model_validate(step_row.definition)
            if not step_uses_interface(step_row.definition, interface_id, interface_version):
                return {**identity, "ok": True, "version": scenario.version}
            updated, conflicts = apply_sync(step, config, interface_version, item.choices)
            if conflicts:
                return refuse("存在需要逐项确认的冲突", conflicts=conflicts)
            optimistic_update(session, ApiScenario, scenario.id, item.version, {"updated_at": datetime.now()})
            step_row.definition = updated.model_dump()
            session.add(step_row)
            session.commit()
            return {**identity, "ok": True, "changed": True, "version": item.version + 1}
    except HTTPException as exc:
        return refuse(str(exc.detail))


@router.post("/interfaces/{interface_id}/sync-apply")
def apply_sync_batch(
    interface_id: int,
    payload: SyncApplyRequest,
    session: Session = Depends(get_session),
    user: User = Depends(get_current_active_user),
):
    """Update steps to the current interface version, one decision per conflict."""
    row = require(session, ApiDefinition, interface_id)
    config = DefinitionConfig.model_validate(row.config)
    db_engine = session.get_bind()
    results = [apply_sync_item(db_engine, interface_id, config, row.version, item) for item in payload.items]
    return {
        "applied": sum(1 for item in results if item["changed"]),
        "failed": sum(1 for item in results if not item["ok"]),
        "items": results,
    }


def validate_scenario(session, payload):
    check_folder(session, payload.folder_id, "scenario")
    if payload.env_id is not None:
        require(session, Environment, payload.env_id)
    errors = validate_steps(payload.steps)
    if errors:
        raise HTTPException(422, errors)
    for step in payload.steps:
        if step.kind == "request" and step.interface_id is not None:
            require(session, ApiDefinition, step.interface_id)


def save_steps(session, scenario_id, steps):
    session.exec(delete(ApiScenarioStep).where(ApiScenarioStep.scenario_id == scenario_id))
    for index, step in enumerate(steps):
        session.add(
            ApiScenarioStep(
                scenario_id=scenario_id,
                step_id=step.id,
                position=index,
                interface_id=step.interface_id if step.kind == "request" else None,
                definition=step.model_dump(),
            )
        )


def stale_step_counts(session, scenario_ids):
    """Per scenario, how many steps trail their interface's current version.

    A junior tester should not have to open every scenario to discover that an
    interface changed; the list surfaces it in one batched query.
    """
    if not scenario_ids:
        return {}
    rows = session.exec(
        select(ApiScenarioStep.scenario_id, ApiScenarioStep.definition).where(
            ApiScenarioStep.scenario_id.in_(scenario_ids), ApiScenarioStep.interface_id.isnot(None)
        )
    ).all()
    versions = (
        dict(
            session.exec(
                select(ApiDefinition.id, ApiDefinition.version).where(
                    ApiDefinition.id.in_({definition.get("interface_id") for _, definition in rows})
                )
            ).all()
        )
        if rows
        else {}
    )
    counts = {}
    for scenario_id, definition in rows:
        interface_id = definition.get("interface_id")
        if interface_id in versions and definition.get("interface_version") != versions[interface_id]:
            counts[scenario_id] = counts.get(scenario_id, 0) + 1
    return counts


@router.get("/scenarios")
def list_scenarios(
    keyword: str = "",
    folder_id: Optional[int] = None,
    skip: int = Query(0, ge=0),
    limit: int = Query(100, ge=1, le=1000),
    session: Session = Depends(get_session),
):
    total, rows = list_rows(session, ApiScenario, keyword, folder_id, skip, limit)
    users = asset_users(session, rows)
    ids = [row.id for row in rows]
    counts = dict(session.exec(select(ApiScenarioStep.scenario_id, func.count()).where(ApiScenarioStep.scenario_id.in_(ids)).group_by(ApiScenarioStep.scenario_id)).all())
    stale = stale_step_counts(session, ids)
    latest_ids = select(ApiRun.scenario_id, func.max(ApiRun.created_at).label("latest")).where(ApiRun.scenario_id.in_(ids)).group_by(ApiRun.scenario_id).subquery()
    latest = {}
    for run in session.exec(select(ApiRun).join(latest_ids, (ApiRun.scenario_id == latest_ids.c.scenario_id) & (ApiRun.created_at == latest_ids.c.latest))).all():
        latest[run.scenario_id] = {key: getattr(run, key) for key in ("id", "status", "created_at", "env_name")}
    return {"total": total, "items": [{**asset_read(session, row, users), "step_count": counts.get(row.id, 0), "stale_steps": stale.get(row.id, 0), "last_run": latest.get(row.id)} for row in rows]}


@router.post("/scenarios")
def create_scenario(
    payload: ScenarioWrite, session: Session = Depends(get_session), user: User = Depends(get_current_active_user)
):
    validate_scenario(session, payload)
    row = ApiScenario(
        name=payload.name,
        description=payload.description,
        folder_id=payload.folder_id,
        env_id=payload.env_id,
        user_id=user.id,
        updater_id=user.id,
    )
    session.add(row)
    session.flush()
    save_steps(session, row.id, payload.steps)
    session.commit()
    session.refresh(row)
    return scenario_read(session, row)


@router.get("/scenarios/{scenario_id}")
def get_scenario(scenario_id: int, session: Session = Depends(get_session)):
    return scenario_read(session, require(session, ApiScenario, scenario_id))


@router.put("/scenarios/{scenario_id}")
def update_scenario(scenario_id: int, payload: ScenarioWrite, session: Session = Depends(get_session),
                    user: User = Depends(get_current_active_user)):
    require(session, ApiScenario, scenario_id)
    validate_scenario(session, payload)
    optimistic_update(
        session,
        ApiScenario,
        scenario_id,
        payload.version,
        {
            "name": payload.name,
            "description": payload.description,
            "folder_id": payload.folder_id,
            "env_id": payload.env_id,
            "updated_at": datetime.now(),
            "updater_id": user.id,
        },
    )
    save_steps(session, scenario_id, payload.steps)
    session.commit()
    session.expire_all()
    return scenario_read(session, require(session, ApiScenario, scenario_id))


@router.delete("/scenarios/{scenario_id}")
def delete_scenario(
    scenario_id: int, session: Session = Depends(get_session), user: User = Depends(get_current_active_user)
):
    import json

    row = require(session, ApiScenario, scenario_id)
    ensure_owner_or_admin(row.user_id, user)
    tasks = []
    for task in session.exec(select(ScheduledTask)).all():
        try:
            config = json.loads(task.strategy_config or "{}")
        except ValueError:
            continue
        if config.get("_task_type") == "api" and config.get("api_scenario_id") == scenario_id:
            tasks.append(task.name)
    if tasks:
        raise HTTPException(409, {"message": "场景被定时任务引用，不能删除", "tasks": tasks})
    if session.exec(select(ApiRun).where(ApiRun.scenario_id == scenario_id, ApiRun.status.in_(service.ACTIVE))).first():
        raise HTTPException(409, "场景正在运行，请先结束执行")
    session.exec(delete(ApiScenarioStep).where(ApiScenarioStep.scenario_id == scenario_id))
    session.delete(row)
    session.commit()
    return {"ok": True}


@router.post("/precheck")
def precheck(payload: PrecheckRequest, session: Session = Depends(get_session)):
    env, _, _ = service.load_env(session, payload.env_id)
    return {"errors": validate_steps(payload.steps, env, validation_mode=payload.validation_mode)}


@router.post("/scenarios/{scenario_id}/runs", status_code=202)
def run_scenario(
    scenario_id: int,
    payload: RunRequest,
    session: Session = Depends(get_session),
    user: User = Depends(get_current_active_user),
):
    row = require(session, ApiScenario, scenario_id)
    if row.version != payload.version:
        raise HTTPException(409, "场景版本已改变，请刷新后运行")
    env_id = payload.env_id if "env_id" in payload.model_fields_set else row.env_id
    return service.start_run(session, row, user, env_id, notify=payload.notify, db_engine=session.get_bind())


@router.post("/debug-sessions")
def create_debug(
    payload: DebugCreate, session: Session = Depends(get_session), user: User = Depends(get_current_active_user)
):
    service.load_env(session, payload.env_id)
    item = service.create_debug(user.id, payload.editor_id, payload.env_id)
    return {"id": item.id, "status": item.status}


@router.get("/debug-sessions/{debug_id}")
def get_debug(debug_id: str, user: User = Depends(get_current_active_user)):
    item = service.get_debug(debug_id, user.id)
    with item.lock:
        return {"id": item.id, "status": item.status, "results": item.results, "error": item.error}


@router.post("/debug-sessions/{debug_id}/execute", status_code=202)
def execute_debug(
    debug_id: str,
    payload: DebugExecute,
    session: Session = Depends(get_session),
    user: User = Depends(get_current_active_user),
):
    item = service.get_debug(debug_id, user.id)
    env, _, _ = service.load_env(session, payload.env_id)
    service.start_debug(item, payload, env)
    return {"id": item.id, "status": "RUNNING"}


@router.post("/debug-sessions/{debug_id}/assertions")
def recheck_debug(debug_id: str, payload: DebugExecute, session: Session = Depends(get_session), user: User = Depends(get_current_active_user)):
    item = service.get_debug(debug_id, user.id)
    env, _, _ = service.load_env(session, payload.env_id)
    result = service.recheck_debug(item, payload, env)
    return {"result": result}


@router.get("/notification-status")
def notification_status(session: Session = Depends(get_session)):
    from backend.models import SystemSetting
    setting = session.exec(select(SystemSetting).where(SystemSetting.key == "api_testing_webhook")).first()
    return {"configured": bool(setting and setting.value and setting.value.strip())}


@router.get("/scenarios/{scenario_id}/schedules")
def scenario_schedules(scenario_id: int, session: Session = Depends(get_session)):
    import json
    from backend.api.tasks import _task_to_read
    scenario = require(session, ApiScenario, scenario_id)
    items = []
    for task in session.exec(select(ScheduledTask)).all():
        try:
            config = json.loads(task.strategy_config or "{}")
        except ValueError:
            continue
        if config.get("_task_type") == "api" and config.get("api_scenario_id") == scenario_id:
            items.append(_task_to_read(task, scenario.name))
    return {"items": items}


@router.delete("/debug-sessions/{debug_id}")
def close_debug(debug_id: str, user: User = Depends(get_current_active_user)):
    item = service.get_debug(debug_id, user.id)
    item.cancel.set()
    with service.debug_lock:
        service.debug_sessions.pop(debug_id, None)
    return {"ok": True}


@router.get("/runs")
def list_runs(
    keyword: str = "",
    status: Optional[str] = None,
    scenario_id: Optional[int] = None,
    skip: int = Query(0, ge=0),
    limit: int = Query(20, ge=1, le=100),
    session: Session = Depends(get_session),
):
    query = select(ApiRun)
    if keyword:
        query = query.where(ApiRun.scenario_name.contains(keyword))
    if status:
        query = query.where(ApiRun.status == status)
    if scenario_id is not None:
        query = query.where(ApiRun.scenario_id == scenario_id)
    total = session.exec(select(func.count()).select_from(query.subquery())).one()
    rows = session.exec(query.order_by(ApiRun.created_at.desc()).offset(skip).limit(limit)).all()
    return {"total": total, "items": [{k: v for k, v in r.model_dump().items() if k != "snapshot"} for r in rows]}


@router.get("/runs/{run_id}")
def get_run(run_id: str, session: Session = Depends(get_session)):
    row = require(session, ApiRun, run_id)
    steps = session.exec(
        select(ApiStepResult).where(ApiStepResult.run_id == run_id).order_by(ApiStepResult.position)
    ).all()
    precheck_errors = (row.snapshot or {}).get("precheck_errors", [])
    error_category = "configuration" if precheck_errors else next(
        (step.detail.get("error_category") for step in steps if step.detail.get("error_category")), None
    )
    return {**row.model_dump(), "steps": steps, "summary": summary(steps), "warnings": report_warnings(row, steps),
            "error_category": error_category, "precheck_errors": precheck_errors}


@router.post("/runs/{run_id}/cancel")
def cancel_run(run_id: str, session: Session = Depends(get_session), user: User = Depends(get_current_active_user)):
    row = require(session, ApiRun, run_id)
    ensure_owner_or_admin(row.executor_id, user, "仅执行人或管理员可以中止")
    with service.control_lock:
        event = service.controls.get(run_id)
        if event:
            event.set()
    return {"ok": True, "message": "停止发送后续请求；已发送的请求可能已生效"}


@router.get("/runs/{run_id}/download", response_class=HTMLResponse)
def download_run(run_id: str, session: Session = Depends(get_session)):
    row = require(session, ApiRun, run_id)
    steps = session.exec(
        select(ApiStepResult).where(ApiStepResult.run_id == run_id).order_by(ApiStepResult.position)
    ).all()
    return HTMLResponse(
        render_report(row, steps),
        headers={
            "Content-Disposition": f'attachment; filename="api-report-{row.id}.html"',
            "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'",
        },
    )


@router.delete("/runs/{run_id}")
def delete_run(run_id: str, session: Session = Depends(get_session), user: User = Depends(get_current_active_user)):
    row = require(session, ApiRun, run_id)
    ensure_owner_or_admin(row.executor_id, user)
    if row.status in service.ACTIVE or row.notification_status == "PENDING":
        raise HTTPException(409, "执行或通知尚未完成，暂不能删除")
    session.exec(delete(ApiStepResult).where(ApiStepResult.run_id == run_id))
    session.delete(row)
    session.commit()
    return {"ok": True}
