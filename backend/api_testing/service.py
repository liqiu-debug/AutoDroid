import asyncio
import hashlib
import json
import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict
from uuid import uuid4

import httpx
from fastapi import HTTPException
from sqlmodel import Session, select

from backend.database import engine as default_engine
from backend.models import (
    ApiRun,
    ApiScenario,
    ApiScenarioStep,
    ApiStepResult,
    Environment,
    GlobalVariable,
    ScheduledTask,
    User,
)
from .engine import execute_step, make_client
from .schemas import Step
from .values import ExecutionError, assertion_outcome, validate_steps, evaluate_assertions

logger = logging.getLogger(__name__)
ACTIVE = {"QUEUED", "RUNNING"}
pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix="api-testing")
controls: Dict[str, threading.Event] = {}
control_lock = threading.RLock()
scheduled_active = set()


def load_env(session, env_id):
    if env_id is None:
        return {}, [], "未选择环境"
    env = session.get(Environment, env_id)
    if not env:
        raise HTTPException(400, "运行环境不存在")
    variables = session.exec(select(GlobalVariable).where(GlobalVariable.env_id == env_id)).all()
    return {v.key: v.value for v in variables}, [v.value for v in variables if v.is_secret], env.name


def scenario_steps(session, scenario_id):
    rows = session.exec(
        select(ApiScenarioStep).where(ApiScenarioStep.scenario_id == scenario_id).order_by(ApiScenarioStep.position)
    ).all()
    return [Step.model_validate(r.definition) for r in rows]


def start_run(session, scenario, user, env_id, *, notify=False, task_id=None, db_engine=None):
    db_engine = db_engine or default_engine
    steps = scenario_steps(session, scenario.id)
    env, _, env_name = load_env(session, env_id)
    errors = validate_steps(steps, env, validation_mode="run")
    if errors:
        raise HTTPException(422, errors)
    # The description is frozen with the run so a reader can tell what the
    # scenario was for without opening the (possibly since-edited) scenario.
    snapshot = {"version": scenario.version, "description": scenario.description, "steps": [s.model_dump() for s in steps]}
    run_id, cancel = str(uuid4()), threading.Event()
    run = ApiRun(
        id=run_id,
        scenario_id=scenario.id,
        scenario_name=scenario.name,
        env_id=env_id,
        env_name=env_name,
        executor_id=user.id,
        executor_name=user.full_name or user.username,
        task_id=task_id,
        snapshot=snapshot,
        notification_status="PENDING" if notify else "DISABLED",
    )
    session.add(run)
    for i, step in enumerate(steps):
        session.add(ApiStepResult(run_id=run_id, step_id=step.id, position=i, name=step.name))
    session.commit()
    session.refresh(run)
    with control_lock:
        controls[run_id] = cancel
    try:
        pool.submit(_run_worker, db_engine, run_id, steps, env, cancel, notify, task_id)
    except Exception:
        with control_lock:
            controls.pop(run_id, None)
            scheduled_active.discard(task_id)
        run.status, run.error, run.finished_at = "ERROR", "无法提交执行任务", datetime.now()
        session.add(run)
        session.commit()
        raise
    return run


def _run_worker(db_engine, run_id, steps, env, cancel, notify, task_id):
    started = time.monotonic()
    status, failure = "PASS", None
    try:
        with Session(db_engine) as session:
            run = session.get(ApiRun, run_id)
            run.status, run.started_at = "RUNNING", datetime.now()
            session.add(run)
            session.commit()

        async def execute():
            outputs = {}
            async with make_client() as client:
                for step in steps:
                    with Session(db_engine) as session:
                        row = session.exec(
                            select(ApiStepResult).where(
                                ApiStepResult.run_id == run_id, ApiStepResult.step_id == step.id
                            )
                        ).one()
                        row.status = "RUNNING"
                        session.add(row)
                        session.commit()
                    result, response = await execute_step(step, client, env, outputs, cancel)
                    with Session(db_engine) as session:
                        row = session.exec(
                            select(ApiStepResult).where(
                                ApiStepResult.run_id == run_id, ApiStepResult.step_id == step.id
                            )
                        ).one()
                        row.status, row.duration_ms, row.detail = (
                            result["status"],
                            result["duration_ms"],
                            result["detail"],
                        )
                        session.add(row)
                        session.commit()
                    if result["status"] != "PASS":
                        return result["status"], result["detail"].get("error")
                    if response is not None:
                        outputs[step.id] = response
            return ("ABORTED", "用户已中止") if cancel.is_set() else ("PASS", None)

        status, failure = asyncio.run(execute())
    except Exception:
        # Never put raw HTTP payloads or credentials into application logs.
        logger.error("API run %s failed with an internal runner error", run_id)
        status, failure = "ERROR", "执行器内部异常"
    finally:
        try:
            with Session(db_engine) as session:
                run = session.get(ApiRun, run_id)
                run.status, run.error, run.finished_at = status, failure, datetime.now()
                run.duration_ms = (time.monotonic() - started) * 1000
                session.add(run)
                rows = session.exec(select(ApiStepResult).where(ApiStepResult.run_id == run_id)).all()
                for row in rows:
                    if row.status in {"PENDING", "RUNNING"}:
                        row.status = "SKIP"
                    session.add(row)
                session.commit()
            if notify:
                send_notification(db_engine, run_id)
        finally:
            with control_lock:
                controls.pop(run_id, None)
                scheduled_active.discard(task_id)


def send_notification(db_engine, run_id):
    from backend.notification_service import NotificationService

    with Session(db_engine) as session:
        run = session.get(ApiRun, run_id)
        rows = session.exec(
            select(ApiStepResult).where(ApiStepResult.run_id == run_id).order_by(ApiStepResult.position)
        ).all()
        payload = {
            "run_id": run.id,
            "name": run.scenario_name,
            "status": run.status,
            "env_name": run.env_name,
            "duration_ms": run.duration_ms,
            "total": len(rows),
            "passed": sum(r.status == "PASS" for r in rows),
            "failed": sum(r.status in {"FAIL", "ERROR"} for r in rows),
            "skipped": sum(r.status == "SKIP" for r in rows),
        }
    try:
        result = NotificationService.send_api_report_card(**payload)
    except Exception:
        result = {"status": "FAILED", "error": "飞书通知发送失败"}
    with Session(db_engine) as session:
        run = session.get(ApiRun, run_id)
        run.notification_status, run.notification_error = result["status"], result.get("error")
        session.add(run)
        session.commit()


def run_scheduled(task_id, db_engine=None):
    db_engine = db_engine or default_engine
    with control_lock:
        if task_id in scheduled_active:
            logger.info("接口定时任务 #%s 上次运行未结束，跳过本次触发", task_id)
            return None
        scheduled_active.add(task_id)
    try:
        with Session(db_engine) as session:
            task = session.get(ScheduledTask, task_id)
            config = json.loads(task.strategy_config or "{}") if task else {}
            scenario = (
                session.get(ApiScenario, config.get("api_scenario_id")) if config.get("api_scenario_id") else None
            )
            user = session.get(User, task.user_id) if task else None
            if not task or not scenario or not user or not user.is_active:
                raise ExecutionError("定时任务目标或执行用户不可用")
            run = start_run(
                session,
                scenario,
                user,
                config.get("env_id", scenario.env_id),
                notify=task.enable_notification,
                task_id=task_id,
                db_engine=db_engine,
            )
            return run.id
    except Exception as exc:
        with control_lock:
            scheduled_active.discard(task_id)
        logger.error("接口定时任务 #%s 启动失败，请检查目标、环境和变量", task_id)
        if isinstance(exc, HTTPException):
            precheck_errors = exc.detail if isinstance(exc.detail, list) else [{"message": str(exc.detail)}]
        elif isinstance(exc, ExecutionError):
            precheck_errors = [{"message": str(exc)}]
        else:
            precheck_errors = [{"message": "无法初始化执行任务，请检查服务状态"}]
        precheck_errors = [{"error_category": "configuration", **error} for error in precheck_errors]
        # A scheduled precheck failure is still a reportable execution outcome.
        # Do not silently lose failures just because no HTTP request was sent.
        try:
            with Session(db_engine) as session:
                task = session.get(ScheduledTask, task_id)
                if task:
                    run = ApiRun(
                        id=str(uuid4()),
                        scenario_name=f"{task.name}（启动失败）",
                        task_id=task_id,
                        executor_id=task.user_id,
                        status="ERROR",
                        finished_at=datetime.now(),
                        error="定时任务启动失败：" + "；".join(error["message"] for error in precheck_errors),
                        snapshot={"precheck_errors": precheck_errors},
                        notification_status="PENDING" if task.enable_notification else "DISABLED",
                    )
                    session.add(run)
                    session.commit()
                    session.refresh(run)
                    failed_id, notify = run.id, task.enable_notification
                else:
                    return None
            if notify:
                send_notification(db_engine, failed_id)
            return failed_id
        except Exception:
            logger.error("无法记录接口定时任务 #%s 的启动失败", task_id)
        return None


def recover_runs(db_engine=None):
    with Session(db_engine or default_engine) as session:
        runs = session.exec(select(ApiRun).where(ApiRun.status.in_(ACTIVE))).all()
        for run in runs:
            run.status, run.error, run.finished_at = "ERROR", "服务重启，未完成运行已终止；不会自动续跑", datetime.now()
            if run.notification_status == "PENDING":
                run.notification_status, run.notification_error = "FAILED", "服务重启中断通知"
            session.add(run)
            for row in session.exec(select(ApiStepResult).where(ApiStepResult.run_id == run.id)).all():
                if row.status in {"PENDING", "RUNNING"}:
                    row.status = "SKIP"
                    session.add(row)
        for run in session.exec(select(ApiRun).where(ApiRun.notification_status == "PENDING")).all():
            run.notification_status, run.notification_error = "FAILED", "服务重启中断通知；为避免重复发送，不自动重发"
            session.add(run)
        session.commit()


@dataclass
class DebugSession:
    id: str
    user_id: int
    editor_id: str
    env_id: Any
    touched: float = field(default_factory=time.monotonic)
    outputs: dict = field(default_factory=dict)
    responses: dict = field(default_factory=dict)
    assertion_fingerprints: dict = field(default_factory=dict)
    fingerprints: dict = field(default_factory=dict)
    cookies: Any = field(default_factory=httpx.Cookies)
    cookies_after: dict = field(default_factory=dict)
    results: dict = field(default_factory=dict)
    status: str = "IDLE"
    error: str = ""
    cancel: Any = field(default_factory=threading.Event)
    lock: Any = field(default_factory=threading.RLock)


debug_sessions = {}
debug_lock = threading.RLock()


def reap_debug_sessions():
    with debug_lock:
        for key, item in list(debug_sessions.items()):
            if time.monotonic() - item.touched > 1800 and item.status != "RUNNING":
                debug_sessions.pop(key)


def register_debug_reaper():
    from backend.scheduler_service import get_scheduler

    get_scheduler().scheduler.add_job(
        reap_debug_sessions, "interval", seconds=60, id="api_testing_debug_cleanup", replace_existing=True
    )


def shutdown():
    with control_lock:
        for event in controls.values():
            event.set()
    with debug_lock:
        for item in debug_sessions.values():
            item.cancel.set()
        debug_sessions.clear()


def create_debug(user_id, editor_id, env_id):
    reap_debug_sessions()
    with debug_lock:
        if sum(d.user_id == user_id for d in debug_sessions.values()) >= 20:
            raise HTTPException(429, "调试会话过多，请关闭不再使用的编辑器")
        debug = DebugSession(str(uuid4()), user_id, editor_id, env_id)
        debug_sessions[debug.id] = debug
        return debug


def get_debug(debug_id, user_id):
    with debug_lock:
        item = debug_sessions.get(debug_id)
        if not item or item.user_id != user_id:
            raise HTTPException(404, "调试会话不存在")
        if time.monotonic() - item.touched > 1800 and item.status != "RUNNING":
            debug_sessions.pop(debug_id)
            raise HTTPException(410, "调试会话已过期，请重新调试")
        item.touched = time.monotonic()
        return item


def _debug_target(item, payload, env):
    if item.editor_id != payload.editor_id or item.env_id != payload.env_id:
        raise HTTPException(409, "编辑器或环境已改变，请创建新调试会话")
    steps = payload.steps
    index = next((i for i, s in enumerate(steps) if s.id == payload.step_id), None)
    if index is None:
        raise HTTPException(422, "调试步骤不存在")
    errors = validate_steps(steps[:index + 1], env, validation_mode="debug")
    if errors:
        raise HTTPException(422, errors)
    if item.status == "RUNNING":
        raise HTTPException(409, "当前调试尚未结束")
    return steps, index


def _sync_debug(item, steps, env):
    fingerprints, chain = {}, json.dumps(env, sort_keys=True)
    for step in steps:
        request = step.effective().request.model_dump() if step.kind == "request" else step.seconds
        chain = hashlib.sha256((chain + json.dumps([step.id, step.kind, request], sort_keys=True)).encode()).hexdigest()
        fingerprints[step.id] = chain
    stale = {key for key in item.fingerprints if fingerprints.get(key) != item.fingerprints[key]}
    for key in stale:
        for mapping in (item.outputs, item.responses, item.results, item.cookies_after, item.assertion_fingerprints):
            mapping.pop(key, None)
    item.fingerprints = fingerprints
    for step in steps:
        result = item.results.get(step.id)
        if result:
            result["name"] = step.name
            current = json.dumps([a.model_dump() for a in step.effective().assertions], sort_keys=True)
            if step.kind == "request" and step.id in item.responses and item.assertion_fingerprints.get(step.id) != current:
                result["assertions_pending"] = True
                item.outputs.pop(step.id, None)
            elif result.get("assertions_pending"):
                result["assertions_pending"] = False
                if result["status"] == "PASS":
                    item.outputs[step.id] = item.responses[step.id]


def _check_predecessors(item, steps, index):
    for previous in steps[:index]:
        result = item.results.get(previous.id)
        if result and (result.get("assertions_pending") or result["status"] != "PASS"):
            raise HTTPException(409, f"请先校验或重新调试前序步骤「{previous.name}」")


def recheck_debug(item, payload, env):
    """Evaluate edited assertions against the valid cached response, without HTTP."""
    with item.lock:
        steps, index = _debug_target(item, payload, env)
        _sync_debug(item, steps, env)
        _check_predecessors(item, steps, index)
        step = steps[index]
        if step.kind != "request" or step.id not in item.responses:
            raise HTTPException(409, "没有与当前请求和环境匹配的真实响应，请先调试本步")
        result = item.results[step.id]
        detail = result["detail"]
        detail["references"] = list(detail.get("request_references", []))
        checks = evaluate_assertions(step.effective().assertions, item.responses[step.id], env, item.outputs, detail["references"])
        detail["assertions"] = checks
        result["status"], detail["error"], detail["error_category"] = assertion_outcome(checks)
        result["assertions_pending"] = False
        item.assertion_fingerprints[step.id] = json.dumps([a.model_dump() for a in step.effective().assertions], sort_keys=True)
        if result["status"] == "PASS":
            item.outputs[step.id] = item.responses[step.id]
        else:
            item.outputs.pop(step.id, None)
        item.status = result["status"]
        return result


def start_debug(item, payload, env):
    with item.lock:
        steps, index = _debug_target(item, payload, env)
        _sync_debug(item, steps, env)
        if payload.mode == "single":
            _check_predecessors(item, steps, index)
        if payload.mode == "through":
            targets = steps
        else:
            targets = steps[index:]
        for step in targets:
            for mapping in (item.outputs, item.responses, item.results, item.cookies_after, item.assertion_fingerprints):
                mapping.pop(step.id, None)
        # Restore the cookie snapshot at the last unchanged predecessor. This
        # lets users configure step B after debugging A without rerunning A,
        # while rerunning A cannot retain cookies produced by later steps.
        item.cookies = httpx.Cookies()
        if payload.mode == "single":
            for previous in steps[:index]:
                if previous.id in item.cookies_after:
                    item.cookies = httpx.Cookies(item.cookies_after[previous.id])
        item.cancel = threading.Event()
        item.status, item.error = "RUNNING", ""
        chosen = steps[: index + 1] if payload.mode == "through" else [steps[index]]
        pool.submit(_debug_worker, item, chosen, env)


def _debug_worker(item, steps, env):
    async def execute():
        async with make_client(item.cookies) as client:
            for step in steps:
                result, response = await execute_step(step, client, env, item.outputs, item.cancel)
                with item.lock:
                    result["captured_at"] = datetime.now().isoformat()
                    result["env_id"] = item.env_id
                    item.results[step.id] = result
                    item.cookies = httpx.Cookies(client.cookies)
                    if response is not None and result["status"] in {"PASS", "FAIL", "UNCHECKED"}:
                        item.responses[step.id] = response
                        item.assertion_fingerprints[step.id] = json.dumps([a.model_dump() for a in step.effective().assertions], sort_keys=True)
                        item.cookies_after[step.id] = httpx.Cookies(client.cookies)
                    if response is not None and result["status"] == "PASS":
                        item.outputs[step.id] = response
                        item.cookies_after[step.id] = httpx.Cookies(client.cookies)
                if result["status"] != "PASS":
                    return result["status"]
        return "PASS"

    try:
        status = asyncio.run(execute())
    except Exception:
        status, item.error = "ERROR", "调试执行器内部异常"
    with item.lock:
        item.status, item.touched = status, time.monotonic()
