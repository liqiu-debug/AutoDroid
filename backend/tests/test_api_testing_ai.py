import copy
import json
import unittest
from unittest.mock import AsyncMock, patch

import httpx
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from sqlmodel import SQLModel, Session, create_engine
from sqlalchemy.pool import StaticPool

from backend.api.deps import get_current_active_user
from backend.api_testing import ai_service, service
from backend.api_testing.ai_routes import router
from backend.api_testing.ai_schemas import ExplainFailureRequest, FeedbackRequest, SuggestAssertionsRequest
from backend.api_testing.schemas import Assertion, DebugExecute, Parameter, Step, Value, literal
from backend.database import get_session
from backend.models import ApiRun, ApiStepResult, Environment, GlobalVariable, SystemSetting, User


def suggestion(path, op="exists", expected=None):
    return {"assertion": {"path": path, "op": op, "expected": expected}, "reason": "建议检查这个字段"}


class ApiTestingAiTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
        SQLModel.metadata.create_all(self.engine)
        self.session = Session(self.engine)
        self.user = User(username="ai-reviewer", hashed_password="unused")
        self.session.add(self.user)
        self.session.commit()
        self.session.refresh(self.user)
        self.user_id = self.user.id
        self.step = Step(id="first")
        self.step.snapshot.request.url = literal("https://business.example/orders")
        self.item = service.create_debug(self.user_id, "editor", None)
        self.prepare_debug([self.step])
        self.payload = SuggestAssertionsRequest(steps=[self.step], debug_session_id=self.item.id, editor_id="editor", step_id=self.step.id, draft_token="draft-1")
        ai_service._calls.clear()
        ai_service._active_users.clear()

    def tearDown(self):
        service.debug_sessions.clear()
        ai_service._calls.clear()
        ai_service._active_users.clear()
        self.session.close()
        self.engine.dispose()

    def prepare_debug(self, steps, env=None):
        service._sync_debug(self.item, steps, env or {})
        for step in steps:
            self.item.responses[step.id] = {"status_code": 200, "body": {"id": "dynamic-123", "count": 2, "active": True, "name": "some value"}, "headers": {"Authorization": "secret-header"}, "cookies": {"session": "secret-cookie"}}
            self.item.results[step.id] = {"status": "PASS", "detail": {"response": self.item.responses[step.id]}, "name": step.name}
            self.item.assertion_fingerprints[step.id] = json.dumps([a.model_dump() for a in step.effective().assertions], sort_keys=True)
        self.item.status = "PASS"

    def configure(self, **overrides):
        values = {"api_testing_ai_enabled": "true", "ai_api_key": "provider-secret", "ai_api_base": "https://model.example/v1", "ai_model": "configured-model", **overrides}
        for key, value in values.items():
            self.session.add(SystemSetting(key=key, value=value))
        self.session.commit()

    def model(self, items):
        return patch.object(ai_service, "call_model", AsyncMock(return_value=({"suggestions": items}, 123)))

    async def test_default_disabled_and_missing_model_never_call_model(self):
        self.assertEqual(ai_service.settings(self.session)[0]["available"], False)
        with self.model([]) as model:
            with self.assertRaises(HTTPException) as raised:
                await ai_service.suggest_assertions(self.payload, self.session, self.user_id)
            self.assertEqual(raised.exception.status_code, 503)
            model.assert_not_called()
        self.configure(ai_model="")
        self.assertFalse(ai_service.settings(self.session)[0]["available"])

    async def test_suggestions_are_read_only_and_feedback_is_user_bound(self):
        self.configure()
        original = copy.deepcopy(self.item.results)
        with self.model([suggestion(["body", "count"], "type", "number")]) as model:
            result = await ai_service.suggest_assertions(self.payload, self.session, self.user_id)
        self.assertEqual(result["draft_token"], "draft-1")
        self.assertEqual(result["suggestions"][0]["assertion"]["expected"]["value"], "number")
        self.assertEqual(original, self.item.results)
        self.assertEqual(result["usage"]["total_tokens"], 123)
        self.assertEqual(model.call_args.args[2]["fields"][0]["value"], 200)
        feedback = FeedbackRequest(call_id=result["call_id"], action="accepted", selected_count=1)
        self.assertEqual(ai_service.feedback(feedback, self.user_id), {"ok": True})
        with self.assertRaises(HTTPException) as raised:
            ai_service.feedback(feedback, self.user_id + 1)
        self.assertEqual(raised.exception.status_code, 404)
        ai_service._calls[result["call_id"]]["created"] -= 1801
        with self.assertRaises(HTTPException):
            ai_service.feedback(feedback, self.user_id)

    async def test_invalid_dynamic_typed_and_unknown_suggestions_are_filtered(self):
        self.configure()
        items = [suggestion(["body", "missing"]), suggestion(["body", "id"], "eq", "dynamic-123"), suggestion(["body", "count"], "eq", "2"), suggestion(["body", "count"], "type", "string"), suggestion(["body", "active"], "gt", 0), suggestion(["body", "count"], "exists"), {"code": "print('bad')"}]
        with self.model(items):
            result = await ai_service.suggest_assertions(self.payload, self.session, self.user_id)
        self.assertEqual(len(result["suggestions"]), 1)
        self.assertEqual(len(result["warnings"]), 6)

    async def test_wrong_user_stale_request_and_environment_cannot_generate(self):
        self.configure()
        with self.model([]) as model:
            with self.assertRaises(HTTPException) as raised:
                await ai_service.suggest_assertions(self.payload, self.session, self.user_id + 1)
            self.assertEqual(raised.exception.status_code, 404)
            self.payload.steps[0].snapshot.request.url = literal("https://changed.example")
            with self.assertRaises(HTTPException) as raised:
                await ai_service.suggest_assertions(self.payload, self.session, self.user_id)
            self.assertEqual(raised.exception.status_code, 409)
            model.assert_not_called()

    async def test_edited_predecessor_assertions_require_revalidation(self):
        self.configure()
        second = Step(id="second", snapshot=self.step.snapshot.model_copy(deep=True))
        self.prepare_debug([self.step, second])
        self.payload.steps = [self.step, second]
        self.payload.step_id = "second"
        self.payload.steps[0].snapshot.assertions.append(Assertion(path=["body", "count"], op="eq", expected=literal(3)))
        before = copy.deepcopy(self.item.results)
        with self.model([]) as model:
            with self.assertRaises(HTTPException) as raised:
                await ai_service.suggest_assertions(self.payload, self.session, self.user_id)
            self.assertEqual(raised.exception.status_code, 409)
            model.assert_not_called()
        self.assertEqual(before, self.item.results)

    def test_short_secrets_do_not_hide_unrelated_field_names(self):
        sanitizer = ai_service.Sanitizer(["actual-provider-secret"])
        # A registered value of "id" used to make text("user_id") differ from
        # "user_id", so a perfectly ordinary field never reached the model.
        sanitizer.add("id")
        response = {"status_code": 200, "body": {"user_id": 7, "token": "value-of-token"}}
        fields, _ = ai_service.response_fields(response, sanitizer)
        paths = [item["path"] for item in fields]
        self.assertIn(["body", "user_id"], paths)
        # Sensitive names and real credentials are still withheld.
        self.assertNotIn(["body", "token"], paths)
        self.assertFalse(sanitizer.allowed_path(["body", "actual-provider-secret"]))
        # Prose redaction keeps using every registered secret, however short.
        self.assertEqual(sanitizer.text("id actual-provider-secret"), "[已隐藏] [已隐藏]")

    def test_suggestions_collapse_per_field_rank_by_value_and_cap(self):
        sanitizer = ai_service.Sanitizer()
        values = {("status_code",): 200, ("body", "code"): 0, ("body", "count"): 2, ("body", "active"): True,
                  ("body", "name_x"): "n", ("body", "deep", "leaf"): "x", ("body", "list"): [1]}
        for key in list(values):
            values[key[:-1]] = values.get(key[:-1], {})  # parents exist for allowed_path purposes
        raw = {"suggestions": [
            suggestion(["body", "count"], "exists"), suggestion(["body", "count"], "not_empty"), suggestion(["body", "count"], "type", "number"),
            suggestion(["body", "active"], "not_empty"), suggestion(["body", "active"], "type", "boolean"),
            suggestion(["body", "name_x"], "exists"), suggestion(["body", "name_x"], "not_empty"), suggestion(["body", "name_x"], "type", "string"),
            suggestion(["body", "deep", "leaf"], "not_empty"), suggestion(["body", "list"], "not_empty"),
            suggestion(["body", "code"], "type", "number"), suggestion(["status_code"], "is_2xx"),
        ]}
        kept, warnings = ai_service.validate_suggestions(raw, values, sanitizer, referenced={("body", "list")}, limit=5)
        ops = [(tuple(s["assertion"]["path"]), s["assertion"]["op"]) for s in kept]
        # One check per field: type for number/boolean, not_empty for strings.
        self.assertEqual(len(set(path for path, _ in ops)), len(ops))
        self.assertIn((("body", "count"), "type"), ops)
        self.assertIn((("body", "active"), "type"), ops)
        # Referenced field first, then the business result code, then status.
        self.assertEqual(ops[0][0], ("body", "list"))
        self.assertEqual(ops[1][0], ("body", "code"))
        self.assertEqual(ops[2][0], ("status_code",))
        self.assertEqual(len(kept), 5)
        self.assertTrue(any("按价值保留前 5 条" in w for w in warnings))
        # The deep leaf loses to shallow fields and is what got cut.
        self.assertNotIn(("body", "deep", "leaf"), [path for path, _ in ops])

    def test_suggestions_already_in_the_step_are_not_offered_again(self):
        sanitizer = ai_service.Sanitizer()
        values = {("body",): {}, ("body", "count"): 2, ("body", "active"): True}
        existing = [Assertion(path=["body", "count"], op="type", expected=literal("number"))]
        # A sibling structural check on an already-covered field is also redundant.
        raw = {"suggestions": [suggestion(["body", "count"], "type", "number"), suggestion(["body", "count"], "not_empty"), suggestion(["body", "active"], "type", "boolean")]}
        kept, warnings = ai_service.validate_suggestions(raw, values, sanitizer, existing=existing)
        self.assertEqual([s["assertion"]["path"] for s in kept], [["body", "active"]])
        self.assertIn("2 条建议已存在于当前校验，未重复列出。", warnings)
        kept, warnings = ai_service.validate_suggestions({"suggestions": [suggestion(["body", "count"], "type", "number")]}, values, sanitizer, existing=existing)
        self.assertEqual(kept, [])
        self.assertIn("没有新的建议，可继续手动配置校验。", warnings)

    async def test_downstream_references_and_goal_shape_the_request(self):
        self.configure()
        second = Step(id="second", snapshot=self.step.snapshot.model_copy(deep=True))
        second.snapshot.request.auth.kind = "bearer"
        second.snapshot.request.auth.token = Value(kind="ref", step_id="first", path=["body", "count"])
        self.prepare_debug([self.step, second])
        self.payload.steps = [self.step, second]
        many = [suggestion(["body", "active"], "type", "boolean"), suggestion(["body", "count"], "type", "number")]
        with self.model(many) as model:
            result = await ai_service.suggest_assertions(self.payload, self.session, self.user_id)
        context = model.call_args.args[2]
        self.assertEqual(context["referenced_paths"], [["body", "count"]])
        # Paths mixing list indexes and keys must not break the request (tuple
        # ordering would compare int with str).
        second.snapshot.request.headers = [Parameter(name="X-Item", value=Value(kind="ref", step_id="first", path=["body", "items", 0])),
                                           Parameter(name="X-Total", value=Value(kind="ref", step_id="first", path=["body", "items", "total"]))]
        self.prepare_debug([self.step, second])
        with self.model(many) as model:
            await ai_service.suggest_assertions(self.payload, self.session, self.user_id)
        self.assertIn(["body", "items", 0], model.call_args.args[2]["referenced_paths"])
        self.assertEqual(context["limit"], ai_service.MAX_SUGGESTIONS)
        self.assertEqual(result["suggestions"][0]["assertion"]["path"], ["body", "count"])
        self.payload.goal = "订单数量应为 2"
        with self.model(many) as model:
            await ai_service.suggest_assertions(self.payload, self.session, self.user_id)
        self.assertEqual(model.call_args.args[2]["limit"], ai_service.MAX_SUGGESTIONS_WITH_GOAL)

    async def test_batch_flow_must_generate_for_every_step_before_applying(self):
        """Locks the ordering the batch UI relies on: generate all, apply, recheck in order."""
        self.configure()
        second = Step(id="second", snapshot=self.step.snapshot.model_copy(deep=True))
        third = Step(id="third", snapshot=self.step.snapshot.model_copy(deep=True))
        steps = [self.step, second, third]
        self.prepare_debug(steps)

        def request(step_id, token):
            return SuggestAssertionsRequest(steps=steps, debug_session_id=self.item.id, editor_id="editor", step_id=step_id, draft_token=token)

        # One through-run is enough for every step.
        with self.model([suggestion(["body", "count"], "type", "number")]):
            for step in steps:
                result = await ai_service.suggest_assertions(request(step.id, f"draft-{step.id}"), self.session, self.user_id)
                self.assertEqual(len(result["suggestions"]), 1)

        # Applying to an earlier step before later steps were generated would be
        # refused: the predecessor's assertions changed and are unverified.
        steps[0].snapshot.assertions.append(Assertion(path=["body", "count"], op="type", expected=literal("number")))
        with self.model([]) as model:
            with self.assertRaises(HTTPException) as raised:
                await ai_service.suggest_assertions(request("second", "late"), self.session, self.user_id)
            self.assertEqual(raised.exception.status_code, 409)
            model.assert_not_called()

        # Rechecking in order repairs the chain without sending any request.
        for step in steps:
            result = service.recheck_debug(self.item, DebugExecute(editor_id="editor", env_id=None, steps=steps, step_id=step.id), {})
            self.assertEqual(result["status"], "PASS", step.id)
        with self.model([suggestion(["body", "active"], "type", "boolean")]):
            result = await ai_service.suggest_assertions(request("third", "again"), self.session, self.user_id)
        self.assertEqual(len(result["suggestions"]), 1)

    async def test_sanitizes_secrets_auth_cookies_and_personal_free_text_before_model(self):
        self.configure()
        env = Environment(name="test")
        self.session.add(env)
        self.session.commit()
        self.session.refresh(env)
        self.session.add(GlobalVariable(env_id=env.id, key="API_SECRET", value="environment-secret", is_secret=True))
        self.session.commit()
        self.payload.env_id = self.item.env_id = env.id
        self.step.snapshot.request.headers = [Parameter(name="Authorization", value=literal("header-literal-secret"))]
        # Revalidate through the ordinary contract before fingerprinting.
        self.step = Step.model_validate(self.step.model_dump())
        self.payload.steps = [self.step]
        self.prepare_debug([self.step], {"API_SECRET": "environment-secret"})
        self.item.responses[self.step.id]["body"].update({"token": "body-token-secret", "email": "someone@example.com", "message": "Call 13800138000"})
        secrets = ["environment-secret", "provider-secret", "header-literal-secret", "body-token-secret", "secret-header", "secret-cookie", "someone@example.com", "13800138000"]
        self.payload.goal = " ".join(secrets)
        with self.model([suggestion(["body", "count"])]) as model:
            await ai_service.suggest_assertions(self.payload, self.session, self.user_id)
        serialized = json.dumps(model.call_args.args[2], ensure_ascii=False)
        for secret in secrets:
            self.assertNotIn(secret, serialized)
        self.assertNotIn('"headers"', serialized)
        self.assertNotIn('"cookies"', serialized)

    async def test_failure_facts_are_server_derived_and_unknown_evidence_is_filtered(self):
        self.configure()
        run = ApiRun(id="failed", scenario_name="test", status="FAIL", snapshot={})
        step = ApiStepResult(run_id=run.id, step_id="first", position=0, name="test", status="FAIL", detail={"response": {"status_code": 401, "body": {"token": "dont-send-this"}}, "assertions": [{"path": ["body", "count"], "passed": False, "actual": "dont-send-this", "expected": 2}]})
        self.session.add(run)
        self.session.add(step)
        self.session.commit()
        generated = {"possible_causes": [{"text": "可能鉴权未生效", "evidence_ids": ["F2"]}, {"text": "invented", "evidence_ids": ["F999"]}], "next_steps": [{"text": "检查测试环境中的凭据配置", "evidence_ids": ["F2"]}]}
        with patch.object(ai_service, "call_model", AsyncMock(return_value=(generated, 5))) as model:
            result = await ai_service.explain_failure(ExplainFailureRequest(run_id="failed"), self.session, self.user_id)
        self.assertEqual(result["facts"][1]["message"], "HTTP 状态码为 401")
        self.assertEqual(len(result["possible_causes"]), 1)
        self.assertEqual(len(result["warnings"]), 1)
        self.assertNotIn("dont-send-this", json.dumps(model.call_args.args[2]))
        self.session.refresh(run)
        self.assertEqual(run.status, "FAIL")

    async def test_precheck_failure_uses_structured_context_without_raw_messages(self):
        self.configure()
        run = ApiRun(id="precheck", scenario_name="test", status="ERROR", snapshot={"precheck_errors": [{"section": "auth", "step_id": "first", "message": "环境变量 missing-secret 不存在"}]})
        self.session.add(run)
        self.session.commit()
        facts, context, _ = ai_service.failure_context(self.session, run.id, ai_service.settings(self.session)[1])
        self.assertIn("鉴权", facts[0]["message"])
        self.assertIn("环境变量", facts[0]["message"])
        self.assertNotIn("missing-secret", json.dumps(context, ensure_ascii=False))

    async def test_model_error_does_not_fabricate_success_and_releases_slot(self):
        self.configure()
        with patch.object(ai_service, "call_model", AsyncMock(side_effect=HTTPException(504, "timeout"))):
            with self.assertRaises(HTTPException) as raised:
                await ai_service.suggest_assertions(self.payload, self.session, self.user_id)
        self.assertEqual(raised.exception.status_code, 504)
        self.assertFalse(ai_service._active_users)
        self.assertFalse(ai_service._calls)

    async def test_current_unchecked_response_can_receive_suggestions_but_not_predecessor(self):
        self.configure()
        self.item.results[self.step.id]["status"] = "UNCHECKED"
        with self.model([suggestion(["body", "count"])]):
            result = await ai_service.suggest_assertions(self.payload, self.session, self.user_id)
        self.assertEqual(len(result["suggestions"]), 1)
        second = Step(id="second", snapshot=self.step.snapshot.model_copy(deep=True))
        self.prepare_debug([self.step, second])
        self.item.results[self.step.id]["status"] = "UNCHECKED"
        self.payload.steps = [self.step, second]
        self.payload.step_id = second.id
        with self.model([]) as model:
            with self.assertRaises(HTTPException) as raised:
                await ai_service.suggest_assertions(self.payload, self.session, self.user_id)
            self.assertEqual(raised.exception.status_code, 409)
            model.assert_not_called()

    async def test_provider_schema_fallback_only_for_explicit_unsupported(self):
        config = {"base": "https://model.example/v1", "key": "key", "model": "model"}
        for message, expected_calls in (("response_format json_schema not supported", 2), ("schema properties invalid", 1)):
            requests = []
            def handler(request):
                requests.append(json.loads(request.content))
                if len(requests) == 1:
                    return httpx.Response(400, json={"error": {"message": message, "param": "response_format"}})
                return httpx.Response(200, json={"choices": [{"message": {"content": '{"suggestions":[]}'}}], "usage": {"total_tokens": 3}})
            client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
            with patch.object(ai_service.httpx, "AsyncClient", return_value=client) as factory:
                if expected_calls == 2:
                    result, usage = await ai_service.call_model(config, "assertions", {}, ai_service.suggestion_schema())
                    self.assertEqual(result, {"suggestions": []})
                    self.assertEqual(usage, 3)
                    self.assertNotIn("response_format", requests[1])
                else:
                    with self.assertRaises(HTTPException) as raised:
                        await ai_service.call_model(config, "assertions", {}, ai_service.suggestion_schema())
                    self.assertEqual(raised.exception.status_code, 502)
                self.assertFalse(factory.call_args.kwargs["trust_env"])
            self.assertEqual(len(requests), expected_calls)

    async def test_provider_timeout_is_504_and_errors_do_not_expose_raw_text(self):
        config = {"base": "https://model.example/v1", "key": "provider-secret", "model": "model"}
        for timeout in (True, False):
            def handler(request):
                if timeout:
                    raise httpx.ReadTimeout("raw-secret-in-error", request=request)
                return httpx.Response(500, text="raw-secret-in-error")
            client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
            with patch.object(ai_service.httpx, "AsyncClient", return_value=client):
                with self.assertRaises(HTTPException) as raised:
                    await ai_service.call_model(config, "assertions", {}, ai_service.suggestion_schema())
            self.assertEqual(raised.exception.status_code, 504 if timeout else 502)
            self.assertNotIn("raw-secret", raised.exception.detail)

    async def test_malformed_or_refused_model_result_is_not_a_success(self):
        config = {"base": "https://model.example/v1", "key": "provider-secret", "model": "model"}
        for message in ({"content": "```json bad```"}, {"content": "{}", "refusal": "unable"}):
            client = httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(200, json={"choices": [{"message": message}]})))
            with patch.object(ai_service.httpx, "AsyncClient", return_value=client):
                with self.assertRaises(HTTPException) as raised:
                    await ai_service.call_model(config, "assertions", {}, ai_service.suggestion_schema())
            self.assertEqual(raised.exception.status_code, 502)

    async def test_routes_require_auth_and_status_never_returns_credentials(self):
        self.configure()
        app = FastAPI()
        app.include_router(router, prefix="/api/api-testing")
        app.dependency_overrides[get_session] = lambda: self.session
        with TestClient(app) as client:
            self.assertEqual(client.get("/api/api-testing/ai/status").status_code, 401)
            app.dependency_overrides[get_current_active_user] = lambda: self.user
            response = client.get("/api/api-testing/ai/status")
            self.assertEqual(response.status_code, 200)
            self.assertTrue(response.json()["available"])
            self.assertNotIn("provider-secret", response.text)


if __name__ == "__main__":
    unittest.main()
