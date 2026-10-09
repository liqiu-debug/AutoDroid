"""Isolated browser QA harness; no mobile devices, real HTTP targets or webhooks.

Requires AUTODROID_DB_PATH to point inside a temporary directory and binds only
to localhost when launched as documented in docs/API_TESTING.md.
"""

import os
import tempfile
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path

import httpx
from fastapi import FastAPI, HTTPException
from backend.core.errors import install_error_handlers
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from sqlmodel import SQLModel, Session, select

if not os.environ.get("AUTODROID_API_PREVIEW") == "1":
    raise RuntimeError("Preview harness requires explicit AUTODROID_API_PREVIEW=1")
if not os.environ.get("AUTODROID_DB_PATH"):
    raise RuntimeError("An isolated AUTODROID_DB_PATH is required")
_preview_db = Path(os.environ["AUTODROID_DB_PATH"]).resolve()
_temporary_roots = {Path(tempfile.gettempdir()).resolve(), Path("/tmp").resolve()}
if not any(root in _preview_db.parents for root in _temporary_roots):
    raise RuntimeError("Preview database must be inside a temporary directory")

from backend.api import api_testing, auth, environments, tasks, reports, settings
from backend.api_testing import ai_service, service
from backend.api_testing.engine import make_client
from backend.api_testing.schemas import Step, Value, Assertion, Parameter, literal, from_json
from backend.core.security import get_password_hash
from backend.database import engine, PROJECT_ROOT
from backend.models import User, ApiDefinition, ApiScenario, ApiScenarioStep, ApiRun, ApiStepResult, Environment, SystemSetting
from backend.notification_service import NotificationService


_flaky_attempts = []


def simulated_api(request):
    if request.url.host != "api.demo.test":
        return httpx.Response(403, json={"error": "Preview only permits api.demo.test"})
    # Fails the first time only, so the retry path can be exercised end to end
    # without ever leaving this process.
    if request.url.path == "/flaky":
        if not _flaky_attempts:
            _flaky_attempts.append(1)
            raise httpx.ConnectError("隔离预览：模拟连接失败", request=request)
        return httpx.Response(200, json={"code": 0, "data": {"attempt": "retried"}})
    if request.url.path == "/login":
        return httpx.Response(
            200,
            json={"code": 0, "data": {"token": "preview-token", "user_id": 7}},
            headers={"Set-Cookie": "session=preview; Path=/"},
        )
    if request.headers.get("authorization") != "Bearer preview-token":
        return httpx.Response(401, json={"code": 401, "message": "请先登录并引用 token"})
    if request.url.path == "/orders" and request.method == "POST":
        return httpx.Response(201, json={"code": 0, "data": {"id": 42, "amount": 99.5, "paid": False}})
    if request.url.path == "/orders/42":
        return httpx.Response(
            200, json={"code": 0, "data": {"id": 42, "status": "created", "items": [{"sku": "A-01", "count": 2}]}}
        )
    return httpx.Response(404, json={"code": 404})


service.make_client = lambda cookies=None: make_client(cookies, httpx.MockTransport(simulated_api))
# This harness never sends a real notification.
NotificationService.send_test_message = lambda _webhook: {"success": True, "message": "隔离预览：模拟发送成功"}
service.send_notification = lambda db_engine, run_id: _mark_preview_notification(db_engine, run_id)


async def simulated_model(_config, task, context, _schema):
    """Unconditionally installed; preview credentials never reach any provider."""
    if task == "assertions":
        fields = context.get("fields", [])
        candidate = next((field for field in fields if field.get("path", [None])[0] == "body" and field.get("type") in {"string", "number", "boolean"} and not any(ai_service._DYNAMIC.search(str(part)) for part in field["path"])), None)
        candidate = candidate or next((field for field in fields if field.get("path") == ["status_code"]), None)
        suggestions = []
        if candidate:
            for op, expected, reason in (("type", candidate["type"], "确认返回字段类型符合约定"), ("not_empty", None, "确认关键字段有返回值")):
                suggestions.append({"assertion": {"path": candidate["path"], "op": op, "expected": expected}, "reason": reason})
        dynamic = next((field for field in fields if any(ai_service._DYNAMIC.search(str(part)) for part in field.get("path", [])) and field.get("type") in {"string", "number"}), None)
        if dynamic:
            suggestions.append({"assertion": {"path": dynamic["path"], "op": "eq", "expected": 42 if dynamic["type"] == "number" else "preview-id"}, "reason": "隔离验收：此动态 ID 固定值建议应被服务端过滤"})
        return {"suggestions": suggestions}, 0
    ids = [fact["id"] for fact in context.get("facts", [])]
    if not ids:
        return {"possible_causes": [], "next_steps": []}, 0
    return {"possible_causes": [{"text": "可能是当前接口结果与配置的校验条件不一致，需结合响应与接口约定核对。", "evidence_ids": ids[:2]}], "next_steps": [{"text": "定位报告中的失败字段，核对测试环境和接口约定后，再决定调整请求或校验规则。", "evidence_ids": ids[:2]}]}, 0


# Always replace the network path, including when an operator enables AI in UI.
ai_service.call_model = simulated_model


def _mark_preview_notification(db_engine, run_id):
    from backend.models import ApiRun

    with Session(db_engine) as session:
        run = session.get(ApiRun, run_id)
        run.notification_status = "SKIPPED"
        run.notification_error = "隔离预览不发送外部飞书消息"
        session.add(run)
        session.commit()


def seed():
    if Path(str(engine.url.database)).resolve() != _preview_db:
        raise RuntimeError("Preview engine does not point to its isolated database")
    SQLModel.metadata.create_all(engine)
    with Session(engine) as session:
        existing = session.exec(select(User)).first()
        if existing:
            if existing.username != "preview":
                raise RuntimeError("Refusing to seed a database that is not a preview database")
            _seed_ai(session, existing)
            return
        user = User(
            username="preview", full_name="接口验收", role="admin", hashed_password=get_password_hash("preview-only")
        )
        env = Environment(name="隔离演示环境")
        session.add(user)
        session.add(env)
        session.commit()
        session.refresh(user)
        session.refresh(env)
        definitions, frozen = [], []
        samples = [
            {"code": 0, "data": {"token": "preview-token", "user_id": 7}},
            {"code": 0, "data": {"id": 42, "amount": 99.5, "paid": False}},
            {"code": 0, "data": {"id": 42, "status": "created"}},
            {"code": 0, "data": {"attempt": "retried"}},
        ]
        # 查询订单 is deliberately one version ahead of the snapshots the steps
        # freeze below, so the preview can exercise stale detection, the list
        # badge, batch sync and the per-field conflict decision.
        versions = [1, 1, 2, 1]
        for index, (name, path, method) in enumerate(
            [
                ("用户登录", "/login", "POST"),
                ("创建订单", "/orders", "POST"),
                ("查询订单", "/orders/{id}", "GET"),
                ("不稳定接口", "/flaky", "GET"),
            ]
        ):
            step = Step()
            step.snapshot.request.url = literal("https://api.demo.test" + path)
            step.snapshot.request.method = method
            frozen.append(step.snapshot.model_dump())
            if versions[index] > 1:
                step.snapshot.request.query = [Parameter(name="detail", value=literal("1"))]
            row = ApiDefinition(
                name=name,
                description="隔离验收示例，不发送真实网络请求",
                version=versions[index],
                config=step.snapshot.model_dump(),
                sample=api_testing.sample_data({"body": samples[index]}, step.snapshot),
                user_id=user.id,
            )
            session.add(row)
            session.flush()
            definitions.append(row)
        scenario = ApiScenario(
            name="下单回归 · 示例",
            description="登录 → 创建订单 → 查询订单；返回值点选引用",
            user_id=user.id,
            env_id=env.id,
        )
        session.add(scenario)
        session.flush()

        for index, row in enumerate(definitions[:3]):
            step = Step(
                id=f"step-{index + 1}", name=row.name, interface_id=row.id, interface_version=1, snapshot=frozen[index]
            )
            config = step.snapshot.model_copy(deep=True)
            if index:
                config.request.auth.kind = "bearer"
                config.request.auth.token = Value(kind="ref", step_id="step-1", path=["body", "data", "token"])
                step.overrides["request"] = {"auth": config.request.auth.model_dump()}
            if index == 1:
                step.overrides["request"].update(
                    {"body_type": "json", "body": from_json({"sku": "A-01", "count": 2}).model_dump()}
                )
            if index == 2:
                step.overrides["request"]["path_params"] = [
                    Parameter(
                        name="id", value=Value(kind="ref", step_id="step-2", path=["body", "data", "id"])
                    ).model_dump()
                ]
                # Overriding a field the interface has since changed is what makes
                # this step need a per-field decision instead of a clean sync.
                step.overrides["request"]["query"] = [Parameter(name="verbose", value=literal("0")).model_dump()]
                step.overrides["assertions"] = [
                    *config.model_dump()["assertions"],
                    Assertion(
                        path=["body", "data", "id"],
                        op="eq",
                        expected=Value(kind="ref", step_id="step-2", path=["body", "data", "id"]),
                    ).model_dump(),
                ]
            session.add(
                ApiScenarioStep(
                    scenario_id=scenario.id,
                    position=index,
                    step_id=step.id,
                    interface_id=row.id,
                    definition=step.model_dump(),
                )
            )

        # A second scenario keeps the maintenance paths demoable: one step can be
        # synced in bulk, one waits on a decision, and one retries a connect failure.
        lightweight = ApiScenario(
            name="订单查询 · 轻量示例",
            description="单独调试查询订单与不稳定接口；用于验证影响面、批量同步和失败重试",
            user_id=user.id,
            env_id=env.id,
        )
        session.add(lightweight)
        session.flush()
        for position, (row, overrides, retry) in enumerate(
            [
                (definitions[2], {"path_params": [Parameter(name="id", value=literal("42")).model_dump()]}, 0),
                (definitions[3], {}, 1),
            ]
        ):
            step = Step(
                id=f"lite-{position + 1}",
                name=row.name,
                interface_id=row.id,
                interface_version=1,
                snapshot=frozen[2 + position],
                retry_count=retry,
            )
            step.overrides = {"request": overrides} if overrides else {}
            session.add(
                ApiScenarioStep(
                    scenario_id=lightweight.id,
                    position=position,
                    step_id=step.id,
                    interface_id=row.id,
                    definition=step.model_dump(),
                )
            )
        session.commit()
        _seed_ai(session, user)


def _seed_ai(session, user):
    enabled = os.environ.get("AUTODROID_API_PREVIEW_AI") == "1"
    values = {"api_testing_ai_enabled": "true" if enabled else "false"}
    if enabled:
        values.update({"ai_api_key": "preview-fake-key-never-sent", "ai_api_base": "https://model.preview.invalid/v1", "ai_model": "preview-deterministic-mock"})
    for key, value in values.items():
        row = session.exec(select(SystemSetting).where(SystemSetting.key == key)).first()
        if row is None:
            row = SystemSetting(key=key)
        row.value = value
        session.add(row)
    if enabled and not session.get(ApiRun, "preview-ai-failure"):
        scenario = session.exec(select(ApiScenario)).first()
        run = ApiRun(id="preview-ai-failure", scenario_id=scenario.id if scenario else None, scenario_name="失败分析 · 隔离示例", executor_id=user.id, executor_name=user.full_name or user.username, status="FAIL", error="断言未全部通过", duration_ms=12, started_at=datetime.now(), finished_at=datetime.now())
        session.add(run)
        session.flush()
        session.add(ApiStepResult(run_id=run.id, step_id="step-3", position=0, name="查询订单", status="FAIL", duration_ms=12, detail={"error": "断言未全部通过", "error_category": "assertion", "response": {"status_code": 200, "body": {"code": 0, "data": {"id": 42, "status": "created"}}}, "assertions": [{"id": "preview-status", "path": ["body", "data", "status"], "op": "eq", "actual": "created", "expected": "paid", "passed": False, "missing": False, "message": "值不相等"}]}))
    session.commit()


@asynccontextmanager
async def lifespan(app):
    seed()
    yield
    from backend.scheduler_service import scheduler_service

    if scheduler_service:
        scheduler_service.shutdown()


app = FastAPI(lifespan=lifespan)
install_error_handlers(app)
app.include_router(auth.router, prefix="/api/auth")
app.include_router(api_testing.router, prefix="/api/api-testing")
app.include_router(environments.router, prefix="/api/environments")
app.include_router(tasks.router, prefix="/api/tasks")
app.include_router(reports.router, prefix="/api/reports")
app.include_router(settings.router, prefix="/api/settings")


@app.get("/api/settings/feature-flags")
def flags():
    return {}


@app.get("/api/devices/")
@app.get("/api/fastbot/devices")
@app.get("/api/inspections/profiles")
def empty_list():
    return []


@app.get("/api/fastbot/tasks")
@app.get("/api/compatibility/runs")
def empty_page():
    return {"items": [], "total": 0}


dist = PROJECT_ROOT / "frontend" / "dist"
app.mount("/assets", StaticFiles(directory=dist / "assets"), name="assets")


@app.get("/{path:path}")
def spa(path: str):
    if path.startswith("api/"):
        raise HTTPException(404, "Not available in isolated preview")
    return FileResponse(dist / "index.html")
