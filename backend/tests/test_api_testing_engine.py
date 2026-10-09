import asyncio
import json
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch

import httpx

from backend.api_testing.engine import execute_step, make_client
from backend.api_testing.schemas import Assertion, Parameter, Step, Value, from_json, literal


async def _stop_server(server, thread):
    await asyncio.to_thread(server.shutdown)
    server.server_close()
    thread.join(timeout=2)


class ApiEngineTests(unittest.IsolatedAsyncioTestCase):
    async def test_real_socket_transport_keeps_host_and_reads_json(self):
        observed = []

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                observed.append(self.headers.get("Host"))
                payload = b'{"ok":true,"id":42}'
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def log_message(self, *_args):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            step = self.step("/")
            step.snapshot.request.url = literal(f"http://127.0.0.1:{server.server_port}/")
            async with make_client() as client:
                result, response = await execute_step(step, client, {}, {}, threading.Event())
            self.assertEqual(result["status"], "PASS", result)
            self.assertEqual(response["body"]["id"], 42)
            self.assertEqual(observed, [f"127.0.0.1:{server.server_port}"])
        finally:
            await asyncio.to_thread(server.shutdown)
            server.server_close()
            thread.join(timeout=2)

    def step(self, path, method="GET"):
        step = Step(id=path, name=path)
        step.snapshot.request.url = literal("https://api.example.com" + path)
        step.snapshot.request.method = method
        return step

    async def test_login_create_query_cookie_reference_journey(self):
        received = []

        def handler(request):
            received.append(request)
            if request.url.path == "/login":
                return httpx.Response(200, json={"token": "fresh-token"}, headers={"Set-Cookie": "session=abc; Path=/"})
            self.assertEqual(request.headers["authorization"], "Bearer fresh-token")
            self.assertIn("session=abc", request.headers["cookie"])
            if request.url.path == "/orders":
                self.assertEqual(json.loads(request.content), {"quantity": 2, "enabled": False})
                return httpx.Response(201, json={"id": 42})
            self.assertEqual(request.url.path, "/orders/42")
            return httpx.Response(200, json={"id": 42, "paid": False})

        login, create, query = self.step("/login", "POST"), self.step("/orders", "POST"), self.step("/orders/{id}")
        for step in (create, query):
            step.snapshot.request.auth.kind = "bearer"
            step.snapshot.request.auth.token = Value(kind="ref", step_id=login.id, path=["body", "token"])
        create.snapshot.request.body_type = "json"
        create.snapshot.request.body = from_json({"quantity": 2, "enabled": False})
        query.snapshot.request.path_params = [
            Parameter(name="id", value=Value(kind="ref", step_id=create.id, path=["body", "id"]))
        ]
        query.snapshot.assertions.append(
            Assertion(path=["body", "id"], op="eq", expected=Value(kind="ref", step_id=create.id, path=["body", "id"]))
        )
        outputs = {}
        async with make_client(transport=httpx.MockTransport(handler)) as client:
            for step in (login, create, query):
                result, response = await execute_step(step, client, {}, outputs, threading.Event())
                self.assertEqual(result["status"], "PASS", result)
                if step.id == login.id:
                    self.assertEqual(result["detail"]["response"]["body"]["token"], "fresh-token")
                    self.assertEqual(result["detail"]["response"]["cookies"], {"session": "abc"})
                    self.assertEqual(result["detail"]["response"]["headers"]["set-cookie"], "session=abc; Path=/")
                else:
                    self.assertEqual(result["detail"]["request"]["headers"]["authorization"], "Bearer fresh-token")
                    self.assertIn("session=abc", result["detail"]["request"]["headers"]["cookie"])
                outputs[step.id] = response
        self.assertEqual(len(received), 3)
        self.assertIs(type(outputs[create.id]["body"]["id"]), int)

    async def test_non_json_empty_invalid_json_and_negative_status(self):
        for response in [
            httpx.Response(204),
            httpx.Response(200, text="plain"),
            httpx.Response(200, text="{bad", headers={"content-type": "application/json"}),
        ]:
            async with make_client(transport=httpx.MockTransport(lambda _: response)) as client:
                result, output = await execute_step(self.step("/"), client, {}, {}, threading.Event())
                self.assertEqual(result["status"], "PASS")
                self.assertNotIn("body", output)
        step = self.step("/")
        step.snapshot.assertions = [Assertion(path=["status_code"], op="eq", expected=literal(400))]
        async with make_client(transport=httpx.MockTransport(lambda _: httpx.Response(400, json={}))) as client:
            result, _ = await execute_step(step, client, {}, {}, threading.Event())
            self.assertEqual(result["status"], "PASS")

    async def test_missing_reference_never_sends_request(self):
        calls = []
        step = self.step("/")
        step.snapshot.request.body_type = "json"
        step.snapshot.request.body = Value(kind="ref", step_id="missing", path=["body"])
        async with make_client(
            transport=httpx.MockTransport(lambda r: (calls.append(r), httpx.Response(200))[1])
        ) as client:
            result, _ = await execute_step(step, client, {}, {}, threading.Event())
        self.assertEqual(result["status"], "ERROR")
        self.assertFalse(calls)

    async def test_timeout_size_and_abort(self):
        async def slow(_):
            await asyncio.sleep(2)
            return httpx.Response(200)

        step = self.step("/")
        step.snapshot.request.timeout_seconds = 0.03
        async with make_client(transport=httpx.MockTransport(slow)) as client:
            result, _ = await execute_step(step, client, {}, {}, threading.Event())
            self.assertEqual(result["status"], "ERROR")
        with patch("backend.api_testing.engine.MAX_RESPONSE_BYTES", 8):
            async with make_client(
                transport=httpx.MockTransport(lambda _: httpx.Response(200, text="x" * 9))
            ) as client:
                result, output = await execute_step(self.step("/"), client, {}, {}, threading.Event())
                self.assertEqual(result["status"], "ERROR")
                self.assertIsNone(output)
        cancel = threading.Event()
        async with make_client(transport=httpx.MockTransport(slow)) as client:
            asyncio.get_running_loop().call_later(0.03, cancel.set)
            result, _ = await execute_step(self.step("/"), client, {}, {}, cancel)
            self.assertEqual(result["status"], "ABORTED")

    async def test_retry_only_repeats_requests_that_never_reached_the_server(self):
        calls = []

        def flaky(request):
            calls.append(request.url.path)
            if len(calls) == 1:
                raise httpx.ConnectError("refused", request=request)
            return httpx.Response(200, json={"ok": True})

        step = self.step("/orders", "POST")
        step.snapshot.request.body_type = "json"
        step.snapshot.request.body = from_json({"sku": "A-01"})
        step.retry_count = 1
        async with make_client(transport=httpx.MockTransport(flaky)) as client:
            result, response = await execute_step(step, client, {}, {}, threading.Event())
        self.assertEqual(result["status"], "PASS", result)
        self.assertEqual(calls, ["/orders", "/orders"])
        self.assertEqual(result["detail"]["attempts"], 2)
        self.assertEqual(len(result["detail"]["retry_history"]), 1)
        self.assertEqual(result["detail"]["retry_history"][0]["attempt"], 1)
        self.assertNotIn("retryable", result["detail"])
        self.assertEqual(response["body"], {"ok": True})

    async def test_read_timeout_and_received_responses_are_never_repeated(self):
        # A read timeout can arrive after the server processed a write, so it
        # must not be repeated even though its category is also "connection".
        timeouts = []

        def slow(request):
            timeouts.append(request.url.path)
            raise httpx.ReadTimeout("read timed out", request=request)

        step = self.step("/orders", "POST")
        step.retry_count = 3
        async with make_client(transport=httpx.MockTransport(slow)) as client:
            result, _ = await execute_step(step, client, {}, {}, threading.Event())
        self.assertEqual(result["status"], "ERROR")
        self.assertEqual(timeouts, ["/orders"])
        self.assertEqual(result["detail"]["attempts"], 1)
        self.assertEqual(result["detail"]["error_category"], "connection")
        self.assertNotIn("retry_history", result["detail"])

        # Assertions failing on a real response are a business result, not a
        # transport blip; repeating them would duplicate the write.
        served = []

        def failing(request):
            served.append(request.url.path)
            return httpx.Response(500, json={"code": 500})

        step = self.step("/orders", "POST")
        step.retry_count = 3
        async with make_client(transport=httpx.MockTransport(failing)) as client:
            result, response = await execute_step(step, client, {}, {}, threading.Event())
        self.assertEqual(result["status"], "FAIL")
        self.assertEqual(served, ["/orders"])
        self.assertEqual(result["detail"]["attempts"], 1)
        self.assertIsNotNone(response)

    async def test_zero_retries_keep_current_behaviour_and_backoff_is_cancellable(self):
        refused = []

        def refuse(request):
            refused.append(request.url.path)
            raise httpx.ConnectError("refused", request=request)

        async with make_client(transport=httpx.MockTransport(refuse)) as client:
            result, _ = await execute_step(self.step("/orders"), client, {}, {}, threading.Event())
        self.assertEqual(result["status"], "ERROR")
        self.assertEqual(refused, ["/orders"])
        self.assertEqual(result["detail"]["attempts"], 1)

        cancel = threading.Event()
        attempts = []

        def refuse_and_cancel(request):
            attempts.append(request.url.path)
            cancel.set()
            raise httpx.ConnectError("refused", request=request)

        step = self.step("/orders")
        step.retry_count = 3
        async with make_client(transport=httpx.MockTransport(refuse_and_cancel)) as client:
            result, _ = await execute_step(step, client, {}, {}, cancel)
        self.assertEqual(result["status"], "ABORTED")
        self.assertEqual(len(attempts), 1)
        self.assertEqual(result["detail"]["error_category"], "cancelled")
        self.assertEqual(result["detail"]["attempts"], 1)

    async def start_server(self, handler_class):
        server = ThreadingHTTPServer(("127.0.0.1", 0), handler_class)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addAsyncCleanup(_stop_server, server, thread)
        return server

    async def test_configured_host_frame_headers_are_sent_as_written(self):
        seen = {}
        body = b'{"ok": true}'

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def do_POST(self):
                seen.update({k.lower(): v for k, v in self.headers.items()})
                seen["body"] = self.rfile.read(int(self.headers.get("Content-Length") or 0)).decode()
                payload = b'{"ok":true}'
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def log_message(self, *_args):
                pass

        server = await self.start_server(Handler)
        step = self.step("/", "POST")
        step.snapshot.request.url = literal(f"http://127.0.0.1:{server.server_port}/")
        step.snapshot.request.body_type = "json"
        step.snapshot.request.body = from_json({"ok": True})
        step.snapshot.request.headers = [
            Parameter(name="Host", value=literal("orders.internal.example")),
            Parameter(name="Content-Length", value=literal(str(len(body)))),
            Parameter(name="Connection", value=literal("close")),
        ]
        async with make_client() as client:
            result, _ = await execute_step(step, client, {}, {}, threading.Event())
        self.assertEqual(result["status"], "PASS", result)
        self.assertEqual(json.loads(seen["body"]), {"ok": True})
        self.assertEqual(seen["host"], "orders.internal.example")
        self.assertEqual(seen["connection"], "close")
        self.assertEqual(seen["content-length"], str(len(body)))

    async def test_chunked_body_is_framed_by_the_transport(self):
        seen = {}

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def do_POST(self):
                seen.update({k.lower(): v for k, v in self.headers.items()})
                chunks = []
                while True:
                    size = int(self.rfile.readline().strip(), 16)
                    if size == 0:
                        self.rfile.readline()
                        break
                    chunks.append(self.rfile.read(size))
                    self.rfile.read(2)
                seen["body"] = b"".join(chunks).decode()
                payload = b'{"ok":true}'
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def log_message(self, *_args):
                pass

        server = await self.start_server(Handler)
        step = self.step("/", "POST")
        step.snapshot.request.url = literal(f"http://127.0.0.1:{server.server_port}/")
        step.snapshot.request.body_type = "json"
        step.snapshot.request.body = from_json({"ok": True})
        step.snapshot.request.headers = [Parameter(name="Transfer-Encoding", value=literal("chunked"))]
        async with make_client() as client:
            result, _ = await execute_step(step, client, {}, {}, threading.Event())
        self.assertEqual(result["status"], "PASS", result)
        self.assertEqual(seen["transfer-encoding"], "chunked")
        # Both framing headers on one message is a protocol violation, so the
        # transport must drop Content-Length or servers misread the body.
        self.assertNotIn("content-length", seen)
        self.assertEqual(json.loads(seen["body"]), {"ok": True})

    async def test_headers_the_transport_refuses_report_a_configuration_error(self):
        attempts = []

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def do_POST(self):
                attempts.append(self.path)
                self.send_response(200)
                self.send_header("Content-Length", "0")
                self.end_headers()

            def log_message(self, *_args):
                pass

        server = await self.start_server(Handler)
        step = self.step("/", "POST")
        step.snapshot.request.url = literal(f"http://127.0.0.1:{server.server_port}/")
        step.snapshot.request.body_type = "json"
        step.snapshot.request.body = from_json({"ok": True})
        step.snapshot.request.headers = [Parameter(name="Transfer-Encoding", value=literal("identity"))]
        async with make_client() as client:
            result, _ = await execute_step(step, client, {}, {}, threading.Event())
        self.assertEqual(result["status"], "ERROR")
        self.assertEqual(result["detail"]["error_category"], "configuration")
        self.assertIn("Transfer-Encoding", result["detail"]["error"])
        self.assertEqual(attempts, [])

        step.snapshot.request.headers = [Parameter(name="Content-Length", value=literal("99"))]
        async with make_client() as client:
            result, _ = await execute_step(step, client, {}, {}, threading.Event())
        self.assertEqual(result["status"], "ERROR")
        self.assertEqual(result["detail"]["error_category"], "configuration")
        self.assertIn("Content-Length", result["detail"]["error"])

    async def test_form_repeated_keys_and_no_redirect(self):
        def handler(request):
            self.assertEqual(request.content.decode(), "x=1&x=2")
            return httpx.Response(302, headers={"Location": "http://127.0.0.1/"})

        step = self.step("/", "POST")
        step.snapshot.request.body_type = "form"
        step.snapshot.request.form = [Parameter(name="x", value=literal("1")), Parameter(name="x", value=literal("2"))]
        async with make_client(transport=httpx.MockTransport(handler)) as client:
            result, _ = await execute_step(step, client, {}, {}, threading.Event())
            self.assertEqual(result["status"], "FAIL")

    async def test_zero_assertions_are_unchecked_for_success_and_error_responses(self):
        step = self.step("/")
        step.snapshot.assertions = []
        for code in (200, 500):
            async with make_client(transport=httpx.MockTransport(lambda _: httpx.Response(code, json={"id": 42}))) as client:
                result, response = await execute_step(step, client, {}, {}, threading.Event())
            self.assertEqual(result["status"], "UNCHECKED")
            self.assertEqual(result["detail"]["error_category"], "unchecked")
            self.assertEqual(response["body"], {"id": 42})
        wait = Step(kind="wait", seconds=0)
        wait.snapshot.assertions = []
        async with make_client(transport=httpx.MockTransport(lambda _: self.fail("wait sent HTTP"))) as client:
            result, _ = await execute_step(wait, client, {}, {}, threading.Event())
        self.assertEqual(result["status"], "PASS")

    async def test_errors_have_categories_and_runtime_auth_is_validated_before_http(self):
        step = self.step("/")
        step.snapshot.request.auth.kind = "bearer"
        step.snapshot.request.auth.token = Value(kind="ref", step_id="login", path=["body", "token"])
        async with make_client(transport=httpx.MockTransport(lambda _: self.fail("invalid token sent HTTP"))) as client:
            result, _ = await execute_step(step, client, {}, {"login": {"body": {"token": ""}}}, threading.Event())
        self.assertEqual(result["detail"]["error_category"], "configuration")
        self.assertEqual(result["detail"]["error_location"], ["request", "auth", "token"])

        step.snapshot.request.auth.kind = "none"
        def disconnected(_):
            raise httpx.ConnectError("private URL must not leak")
        async with make_client(transport=httpx.MockTransport(disconnected)) as client:
            result, _ = await execute_step(step, client, {}, {}, threading.Event())
        self.assertEqual(result["detail"]["error_category"], "connection")
        self.assertNotIn("private URL", result["detail"]["error"])
        async with make_client(transport=httpx.MockTransport(lambda _: httpx.Response(500, json={"id": 42}))) as client:
            result, _ = await execute_step(step, client, {}, {}, threading.Event())
        self.assertEqual(result["detail"]["error_category"], "http")
        step.snapshot.assertions = [Assertion(path=["body", "id"], op="eq", expected=literal(7))]
        async with make_client(transport=httpx.MockTransport(lambda _: httpx.Response(200, json={"id": 42}))) as client:
            result, _ = await execute_step(step, client, {}, {}, threading.Event())
        self.assertEqual(result["detail"]["error_category"], "assertion")


if __name__ == "__main__":
    unittest.main()
