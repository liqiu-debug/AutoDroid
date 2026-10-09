import json
import unittest

from backend.api_testing.curl import parse_curl
from backend.api_testing.schemas import Assertion, Parameter, Step, Value, from_json, literal
from backend.api_testing.values import (
    ExecutionError,
    evaluate_assertions,
    field_tree,
    request_issues,
    resolve,
    sync_preview,
    validate_steps,
)


class ApiValueTests(unittest.TestCase):
    def test_types_and_special_paths(self):
        data = {"one": {"body": {"a.b": [{"/key": 0, "flag": False, "null": None}], "obj": {"x": 1}}}}
        for field, expected in [("/key", 0), ("flag", False), ("null", None)]:
            value = Value(kind="ref", step_id="one", path=["body", "a.b", 0, field])
            actual = resolve(value, {}, data)
            self.assertEqual(actual, expected)
            self.assertIs(type(actual), type(expected))
        self.assertEqual(resolve(Value(kind="ref", step_id="one", path=["body", "obj"]), {}, data), {"x": 1})
        for path in [["body", "missing"], ["body", "a.b", -1], ["body", "a.b", "0"]]:
            with self.assertRaises(ExecutionError):
                resolve(Value(kind="ref", step_id="one", path=path), {}, data)

    def test_templates_env_and_literal_json_are_not_code(self):
        value = Value(kind="template", parts=[literal("Bearer "), Value(kind="env", name="TOKEN")])
        self.assertEqual(resolve(value, {"TOKEN": "abc"}, {}), "Bearer abc")
        self.assertEqual(resolve(literal("{{ TOKEN }}"), {"TOKEN": "abc"}, {}), "abc")
        with self.assertRaises(ExecutionError):
            resolve(
                Value(kind="template", parts=[Value(kind="ref", step_id="a", path=["body"])]), {}, {"a": {"body": {}}}
            )
        with self.assertRaises(ExecutionError):
            resolve(Value(kind="env", name="ABSENT"), {}, {})
        raw = {"kind": "ref", "step_id": "this-is-plain-json", "nested": [False, 2, None]}
        self.assertEqual(resolve(from_json(raw), {}, {}), raw)

    def test_refs_only_predecessors_and_unique_ids(self):
        one, two = Step(id="a"), Step(id="b")
        two.snapshot.request.body_type = "json"
        two.snapshot.request.body = Value(kind="ref", step_id="a", path=["body", "x"])
        self.assertFalse(validate_steps([one, two]))
        self.assertTrue(validate_steps([two, one]))
        one.name = "rename-does-not-break-ref"
        self.assertFalse(validate_steps([one, two]))
        self.assertTrue(validate_steps([one, one]))

    def test_precheck_ignores_disabled_and_inactive_inputs(self):
        step = Step(id="one")
        step.snapshot.request.url = literal("https://example.com")
        missing = Value(kind="env", name="UNUSED")
        step.snapshot.request.auth.token = missing
        step.snapshot.request.body = missing
        from backend.api_testing.schemas import Parameter
        step.snapshot.request.headers = [Parameter(name="disabled", value=missing, enabled=False)]
        step.snapshot.assertions[0].expected = missing
        self.assertEqual(validate_steps([step], {}), [])
        step.snapshot.request.headers[0].enabled = True
        self.assertEqual(validate_steps([step], {})[0]["location"], ["request", "headers", 0, "value"])

    def test_configured_frame_headers_are_no_longer_blocked_by_precheck(self):
        step = Step(id="one")
        step.snapshot.request.url = literal("https://example.com")
        from backend.api_testing.schemas import Parameter

        step.snapshot.request.headers = [
            Parameter(name=name, value=literal(value))
            for name, value in (
                ("Host", "orders.internal.example"),
                ("Content-Length", "17"),
                ("Connection", "close"),
                ("Transfer-Encoding", "chunked"),
            )
        ]
        # The executor derives these only when the request does not set them, so
        # an explicit value is the user's decision and must not be an error.
        self.assertEqual(validate_steps([step], {}), [])
        self.assertEqual(request_issues(step.snapshot.request, {}), [])
        self.assertEqual(validate_steps([step], {}, validation_mode="run"), [])

    def test_assertions_are_type_strict_and_handle_missing(self):
        assertions = [
            Assertion(path=["code"], op="eq", expected=literal(True)),
            Assertion(path=["zero"], op="not_empty"),
            Assertion(path=["nil"], op="exists"),
            Assertion(path=["missing"], op="exists"),
            Assertion(path=["arr"], op="length", expected=literal(2)),
        ]
        result = evaluate_assertions(assertions, {"code": 1, "zero": 0, "nil": None, "arr": [1, 2]}, {}, {}, [])
        self.assertEqual([r["passed"] for r in result], [False, True, True, False, True])

    def test_execution_precheck_reports_known_configuration_without_blocking_drafts(self):
        step = Step(id="request", name="订单")
        step.snapshot.assertions = []
        self.assertEqual(validate_steps([step]), [])
        errors = validate_steps([step], {}, validation_mode="run")
        self.assertEqual({tuple(error["location"]) for error in errors}, {("request", "url"), ("assertions",)})
        self.assertTrue(all(error["step_name"] == "订单" and error["error_category"] == "configuration" for error in errors))
        for url in ("not-a-url", "ftp://example.com", "https://", "https://user:password@example.com"):
            step.snapshot.request.url = literal(url)
            errors = validate_steps([step], {}, validation_mode="debug")
            self.assertEqual(errors[0]["location"], ["request", "url"])
        step.snapshot.request.url = literal("https://example.com/orders/{id}")
        self.assertIn("路径参数", validate_steps([step], {}, validation_mode="debug")[0]["message"])
        step.snapshot.request.path_params = [Parameter(name="id", value=literal(""))]
        self.assertEqual(validate_steps([step], {}, validation_mode="debug")[0]["location"], ["request", "path_params", 0, "value"])

    def test_precheck_defers_upstream_values_and_checks_enabled_auth(self):
        first, second = Step(id="first"), Step(id="second")
        first.snapshot.request.url = literal("https://example.com/login")
        second.snapshot.request.url = Value(kind="ref", step_id="first", path=["body", "url"])
        second.snapshot.request.auth.kind = "bearer"
        second.snapshot.request.auth.token = Value(kind="ref", step_id="first", path=["body", "token"])
        self.assertEqual(validate_steps([first, second], {}, validation_mode="debug"), [])
        second.snapshot.request.auth.token = literal(" ")
        errors = validate_steps([first, second], {}, validation_mode="debug")
        self.assertEqual(errors[0]["location"], ["request", "auth", "token"])
        second.snapshot.request.auth.kind = "none"
        self.assertEqual(validate_steps([first, second], {}, validation_mode="debug"), [])
        second.snapshot.request.auth.kind = "basic"
        self.assertEqual({tuple(e["location"]) for e in validate_steps([first, second], {}, validation_mode="debug")},
                         {("request", "auth", "username"), ("request", "auth", "password")})
        second.snapshot.request.auth.kind = "api_key"
        second.snapshot.request.auth.key_name = " "
        self.assertTrue(any(e["location"] == ["request", "auth", "key_name"] for e in validate_steps([first, second], {}, validation_mode="debug")))

    def test_run_requires_requests_and_assertions_but_debug_does_not(self):
        wait = Step(id="wait", kind="wait")
        self.assertEqual(validate_steps([wait], {}, validation_mode="debug"), [])
        self.assertEqual(validate_steps([wait], {}, validation_mode="run")[0]["section"], "steps")
        request = Step(id="request")
        request.snapshot.request.url = literal("https://example.com")
        request.snapshot.assertions = []
        self.assertEqual(validate_steps([request, wait], {}, validation_mode="debug"), [])
        self.assertEqual(validate_steps([request, wait], {}, validation_mode="run")[0]["location"], ["assertions"])

    def test_sync_preserves_step_and_exposes_overlapping_changes(self):
        step = Step(id="stable", interface_id=2, interface_version=1)
        step.overrides = {"request": {"body": from_json({"custom": True}).model_dump()}}
        config = step.snapshot.model_copy(deep=True)
        config.request.body = from_json({"new": 4})
        result = sync_preview(step, config, 2)
        self.assertEqual(result["step"]["id"], "stable")
        self.assertEqual(result["step"]["overrides"], step.overrides)
        self.assertEqual(result["conflicts"], [["request", "body"]])


class CurlTests(unittest.TestCase):
    def test_browser_json_and_query(self):
        request = parse_curl(
            "curl 'https://example.com/api?q=a&q=b' -H 'Content-Type: application/json' -H 'Authorization: Bearer abc' --data-raw '{\"n\":2,\"ok\":false}'"
        )
        self.assertEqual(request.method, "POST")
        self.assertEqual(len(request.query), 2)
        self.assertEqual(resolve(request.body, {}, {}), {"n": 2, "ok": False})

    def test_form_basic_and_text(self):
        request = parse_curl("curl https://example.com --data-urlencode 'name=A B' --user 'u:p'")
        self.assertEqual(request.form[0].value.value, "A B")
        self.assertEqual(request.auth.password.value, "p")
        request = parse_curl("curl https://example.com -H 'Content-Type: text/plain' --data-raw 'hello'")
        self.assertEqual(request.body.value, "hello")

    def test_rejects_shell_files_and_unsupported_flags(self):
        for command in [
            "curl https://example.com -d @secret",
            "curl https://example.com -H @headers",
            "curl https://example.com -b cookies.txt",
            "curl https://example.com --data '$(id)'",
            "curl https://example.com `id`",
            "curl https://example.com --location",
            "curl https://example.com; id",
            "curl https://example.com https://other.com",
            "curl https://example.com -F x=@file",
        ]:
            with self.subTest(command=command), self.assertRaises(ExecutionError):
                parse_curl(command)


class ResponseFieldTests(unittest.TestCase):
    def test_saved_field_schema_does_not_invent_null_examples(self):
        from backend.api_testing.schemas import ResponseSchema
        data = ResponseSchema(fields=field_tree({"data":{"empty":None,"flag":False},"count":0})[0]["children"]).model_dump()
        self.assertNotIn("example", data["fields"][0])
        self.assertIsNone(data["fields"][0]["children"][0]["example"])
        self.assertIs(data["fields"][0]["children"][1]["example"], False)
        self.assertEqual(data["fields"][1]["example"], 0)

    def test_plaintext_examples_preserve_values_and_types(self):
        secret = 'a"b\n密钥'
        response = {
            "body": {"token": secret, "private_id": 12345, "safe": False},
            "text": json.dumps({"token": secret}),
        }
        fields = field_tree(response)[0]["children"][0]["children"]
        self.assertEqual(next(f for f in fields if f["path"][-1] == "token")["example"], secret)
        self.assertEqual(next(f for f in fields if f["path"][-1] == "private_id")["example"], 12345)
        self.assertEqual(next(f for f in fields if f["path"][-1] == "private_id")["type"], "number")
        self.assertIs(next(f for f in fields if f["path"][-1] == "safe")["example"], False)


if __name__ == "__main__":
    unittest.main()
