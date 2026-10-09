import json
import unittest

from backend.api_testing.specs import MAX_CANDIDATES, parse_spec
from backend.api_testing.values import ExecutionError

OPENAPI = {
    "openapi": "3.0.3",
    "info": {"title": "订单服务", "version": "1.0"},
    "servers": [{"url": "https://api.demo.test/v1"}],
    "components": {"securitySchemes": {"bearer": {"type": "http", "scheme": "bearer"}}},
    "paths": {
        "/orders": {
            "get": {"summary": "查询订单列表", "parameters": [{"name": "page", "in": "query", "schema": {"type": "integer"}}]},
            "post": {
                "summary": "创建订单",
                "requestBody": {
                    "content": {
                        "application/json": {
                            "schema": {
                                "type": "object",
                                "properties": {
                                    "sku": {"type": "string", "example": "A-01"},
                                    "count": {"type": "integer"},
                                    "tags": {"type": "array", "items": {"type": "string"}},
                                },
                            }
                        }
                    }
                },
                "responses": {"201": {"content": {"application/json": {"example": {"code": 0, "data": {"id": 42}}}}}},
            },
        },
        "/orders/{id}": {
            "get": {
                "summary": "查询订单",
                "parameters": [{"name": "id", "in": "path", "required": True, "schema": {"type": "integer"}}],
            }
        },
    },
}


class SpecParserTests(unittest.TestCase):
    def candidate(self, result, key):
        return next(item for item in result["candidates"] if item["key"] == key)

    def test_openapi_json_builds_editable_drafts(self):
        result = parse_spec(json.dumps(OPENAPI))
        self.assertEqual(result["format"], "openapi")
        self.assertEqual(len(result["candidates"]), 3)
        self.assertIn("{{BASE_URL}}", result["warnings"][0])

        created = self.candidate(result, "POST /orders")
        self.assertEqual(created["name"], "创建订单")
        self.assertEqual(created["config"]["request"]["url"]["value"], "https://api.demo.test/v1/orders")
        body = created["config"]["request"]["body"]
        self.assertEqual(created["config"]["request"]["body_type"], "json")
        self.assertEqual(body["kind"], "object")
        self.assertEqual(body["fields"]["sku"]["value"], "A-01")
        self.assertEqual(body["fields"]["count"]["value"], 0)
        self.assertEqual(body["fields"]["tags"]["kind"], "array")
        # A saved response example gives the field tree without a first debug run.
        self.assertEqual(created["sample"], {"body": {"code": 0, "data": {"id": 42}}})
        self.assertEqual(created["config"]["assertions"][0]["op"], "is_2xx")
        self.assertTrue(any("鉴权" in warning for warning in created["warnings"]))

        listed = self.candidate(result, "GET /orders")
        self.assertEqual(listed["config"]["request"]["query"][0]["name"], "page")

        with_path = self.candidate(result, "GET /orders/{id}")
        self.assertEqual(with_path["config"]["request"]["url"]["value"], "https://api.demo.test/v1/orders/{id}")
        self.assertEqual([row["name"] for row in with_path["config"]["request"]["path_params"]], ["id"])
        self.assertTrue(any("路径参数 id" in warning for warning in with_path["warnings"]))

    def test_openapi_yaml_and_unsupported_content_type(self):
        document = {
            "openapi": "3.0.0",
            "servers": [{"url": "https://api.demo.test"}],
            "paths": {
                "/upload": {
                    "post": {
                        "requestBody": {"content": {"application/octet-stream": {"schema": {"type": "string"}}}},
                        "responses": {},
                    }
                }
            },
        }
        yaml_text = "\n".join(
            [
                "openapi: 3.0.0",
                "servers:",
                "  - url: https://api.demo.test",
                "paths:",
                "  /upload:",
                "    post:",
                "      requestBody:",
                "        content:",
                "          application/octet-stream:",
                "            schema: {type: string}",
                "      responses: {}",
            ]
        )
        result = parse_spec(yaml_text)
        self.assertEqual(result["format"], "openapi")
        self.assertEqual(result["candidates"][0]["config"]["request"]["body_type"], "none")
        self.assertIn("未导入请求体", result["candidates"][0]["warnings"][0])
        # The same document as JSON must build the same request. Assertion ids
        # are generated per parse, so compare the request configuration only.
        as_json = parse_spec(json.dumps(document))
        self.assertEqual(as_json["candidates"][0]["config"]["request"], result["candidates"][0]["config"]["request"])

    def test_openapi_refs_are_resolved_and_cycles_are_bounded(self):
        document = {
            "openapi": "3.0.0",
            "servers": [{"url": "https://api.demo.test"}],
            "components": {
                "schemas": {
                    "Order": {
                        "type": "object",
                        "properties": {"id": {"type": "integer"}, "child": {"$ref": "#/components/schemas/Order"}},
                    }
                }
            },
            "paths": {
                "/orders": {
                    "post": {
                        "requestBody": {"content": {"application/json": {"schema": {"$ref": "#/components/schemas/Order"}}}},
                        "responses": {},
                    }
                }
            },
        }
        candidate = parse_spec(json.dumps(document))["candidates"][0]
        fields = candidate["config"]["request"]["body"]["fields"]
        self.assertEqual(fields["id"]["value"], 0)
        # A self-referencing schema terminates instead of recursing forever.
        self.assertIsInstance(fields["child"], dict)

    def test_swagger_2_host_base_path_body_and_form(self):
        document = {
            "swagger": "2.0",
            "host": "api.demo.test",
            "basePath": "/v2",
            "schemes": ["http"],
            "securityDefinitions": {"key": {"type": "apiKey", "name": "X-API-Key", "in": "header"}},
            "paths": {
                "/orders": {
                    "post": {
                        "summary": "创建订单",
                        "consumes": ["application/json"],
                        "parameters": [{"in": "body", "name": "body", "schema": {"type": "object", "properties": {"sku": {"type": "string"}}}}],
                        "responses": {"200": {"schema": {"type": "object", "properties": {"code": {"type": "integer", "example": 0}}}}},
                    }
                },
                "/login": {
                    "post": {
                        "summary": "登录",
                        "parameters": [
                            {"in": "formData", "name": "username", "type": "string", "default": "tester"},
                            {"in": "formData", "name": "password", "type": "string"},
                        ],
                        "responses": {},
                    }
                },
            },
        }
        result = parse_spec(json.dumps(document))
        self.assertEqual(result["format"], "swagger")
        created = self.candidate(result, "POST /orders")
        self.assertEqual(created["config"]["request"]["url"]["value"], "http://api.demo.test/v2/orders")
        self.assertEqual(created["config"]["request"]["body"]["fields"]["sku"]["value"], "")
        self.assertEqual(created["sample"], {"body": {"code": 0}})
        login = self.candidate(result, "POST /login")
        self.assertEqual(login["config"]["request"]["body_type"], "form")
        self.assertEqual([(row["name"], row["value"]["value"]) for row in login["config"]["request"]["form"]],
                         [("username", "tester"), ("password", "")])

    def test_postman_folders_bodies_auth_and_examples(self):
        collection = {
            "info": {"name": "订单集合", "schema": "https://schema.getpostman.com/json/collection/v2.1.0/collection.json"},
            "item": [
                {
                    "name": "订单",
                    "item": [
                        {
                            "name": "创建订单",
                            "request": {
                                "method": "POST",
                                "header": [{"key": "Content-Type", "value": "application/json"}, {"key": "X-Trace", "value": "1", "disabled": True}],
                                "url": {"raw": "https://api.demo.test/orders?dry=1"},
                                "body": {"mode": "raw", "raw": '{"sku": "A-01", "count": 2}'},
                                "auth": {"type": "bearer", "bearer": [{"key": "token", "value": "secret-token"}]},
                            },
                            "response": [{"body": '{"code": 0, "data": {"id": 42}}'}],
                        },
                        {
                            "name": "登录",
                            "request": {
                                "method": "POST",
                                "header": [],
                                "url": "https://api.demo.test/login",
                                "body": {"mode": "urlencoded", "urlencoded": [{"key": "username", "value": "tester"}]},
                            },
                        },
                    ],
                },
                {"name": "缺少地址", "request": {"method": "GET", "url": "/relative"}},
            ],
        }
        result = parse_spec(json.dumps(collection))
        self.assertEqual(result["format"], "postman")
        self.assertEqual(len(result["candidates"]), 2)

        created = self.candidate(result, "POST /orders")
        request = created["config"]["request"]
        self.assertEqual(request["url"]["value"], "https://api.demo.test/orders")
        self.assertEqual(request["query"][0]["name"], "dry")
        self.assertEqual([row["enabled"] for row in request["headers"]], [True, False])
        self.assertEqual(request["body"]["fields"]["count"]["value"], 2)
        # Declared credentials are never imported as values.
        self.assertEqual(request["auth"]["kind"], "bearer")
        self.assertEqual(request["auth"]["token"]["value"], "")
        self.assertEqual(created["sample"], {"body": {"code": 0, "data": {"id": 42}}})
        self.assertTrue(any("凭证" in warning for warning in created["warnings"]))

        login = self.candidate(result, "POST /login")
        self.assertEqual(login["config"]["request"]["body_type"], "form")
        self.assertEqual(login["config"]["request"]["form"][0]["value"]["value"], "tester")
        self.assertTrue(any("缺少 HTTP/HTTPS 地址" in warning for warning in result["warnings"]))

    def test_rejects_unsafe_unsupported_and_oversized_input(self):
        for content in ["", "   ", "not: [a", "!!python/object/apply:os.system ['echo hi']", json.dumps([1, 2])]:
            with self.assertRaises(ExecutionError):
                parse_spec(content)
        with self.assertRaises(ExecutionError) as error:
            parse_spec(json.dumps({"openapi": "3.0.0", "paths": {}}))
        self.assertIn("没有可导入的接口", str(error.exception))
        with self.assertRaises(ExecutionError) as error:
            parse_spec(json.dumps({"info": {}, "item": []}))
        self.assertIn("没有可导入的请求", str(error.exception))
        with self.assertRaises(ExecutionError) as error:
            parse_spec("x" * (2 * 1024 * 1024 + 1))
        self.assertIn("2 MiB", str(error.exception))

    def test_candidate_count_is_capped_with_a_visible_warning(self):
        document = {
            "openapi": "3.0.0",
            "servers": [{"url": "https://api.demo.test"}],
            "paths": {f"/item/{index}": {"get": {"responses": {}}} for index in range(MAX_CANDIDATES + 20)},
        }
        result = parse_spec(json.dumps(document))
        self.assertEqual(len(result["candidates"]), MAX_CANDIDATES)
        self.assertTrue(any(f"只导入前 {MAX_CANDIDATES} 个" in warning for warning in result["warnings"]))

    def test_duplicate_keys_stay_unique_for_selection(self):
        collection = {
            "info": {"name": "重复", "schema": "v2.1.0"},
            "item": [
                {"name": "一", "request": {"method": "GET", "url": "https://api.demo.test/a"}},
                {"name": "二", "request": {"method": "GET", "url": "https://api.demo.test/a"}},
            ],
        }
        result = parse_spec(json.dumps(collection))
        keys = [item["key"] for item in result["candidates"]]
        self.assertEqual(len(keys), len(set(keys)))
