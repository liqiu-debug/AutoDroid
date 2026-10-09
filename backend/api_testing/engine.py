import asyncio
import base64
import json
import time
from urllib.parse import quote, urlencode

import httpx

from .security import PRIVATE_HTTP_LOGS
from .values import ExecutionError, assertion_outcome, evaluate_assertions, field_tree, request_issues, resolve, scalar_text

try:
    # httpcore re-raises h11's own protocol error, and httpx only remaps the
    # httpcore classes, so this one escapes the usual httpx hierarchy. h11 ships
    # with httpx via httpcore.
    from h11 import LocalProtocolError as H11ProtocolError
except ImportError:  # pragma: no cover - only if httpx changes transport stack
    H11ProtocolError = None
# It sits outside the httpx exception hierarchy, so it has to be listed
# explicitly or it surfaces as an internal runner error instead of a
# configuration error the user can act on.
_PROTOCOL_ERRORS = (H11ProtocolError,) if H11ProtocolError else ()

MAX_RESPONSE_BYTES = 5 * 1024 * 1024
RETRY_DELAY_SECONDS = 1


class RunCancelled(Exception):
    pass


async def cancellable(awaitable, cancel, timeout=None):
    async def watch():
        while not cancel.is_set():
            await asyncio.sleep(0.05)
        raise RunCancelled()

    operation = asyncio.ensure_future(awaitable)
    watcher = asyncio.create_task(watch())
    try:
        done, _ = await asyncio.wait({operation, watcher}, timeout=timeout, return_when=asyncio.FIRST_COMPLETED)
        if not done:
            raise ExecutionError("请求超过总超时时间", category="connection")
        if cancel.is_set():
            raise RunCancelled()
        return await operation
    finally:
        operation.cancel()
        watcher.cancel()
        await asyncio.gather(operation, watcher, return_exceptions=True)


def is_chunked(headers):
    return any(
        name.lower() == "transfer-encoding" and value.strip().lower() == "chunked" for name, value in headers
    )


async def chunked_body(data):
    """A single-chunk async stream; httpx frames streams as chunked."""
    yield data


def make_request(spec, env, outputs, refs):
    issues = request_issues(spec, env, outputs)
    if issues:
        raise ExecutionError(issues[0]["message"], location=issues[0]["location"])

    def text(value):
        return scalar_text(resolve(value, env, outputs, refs))

    url = text(spec.url)
    for param in spec.path_params:
        if param.enabled:
            url = url.replace("{" + param.name + "}", quote(text(param.value), safe=""))
    if "{" in url or "}" in url:
        raise ExecutionError("URL 存在未解析的路径参数")
    try:
        target = httpx.URL(url)
    except httpx.InvalidURL:
        raise ExecutionError("请求 URL 无效") from None
    if target.scheme not in {"http", "https"} or not target.host or target.userinfo:
        raise ExecutionError("仅支持不含用户凭证的 HTTP/HTTPS URL")
    # Configured headers win: Host, Content-Length and Connection are derived by
    # httpx only when the request does not already set them, so an explicit value
    # reproduces the request exactly as written.
    headers = [(p.name, text(p.value)) for p in spec.headers if p.enabled]
    params = [(p.name, text(p.value)) for p in spec.query if p.enabled]
    auth = spec.auth
    if auth.kind == "bearer":
        token = text(auth.token)
        headers = [(k, v) for k, v in headers if k.lower() != "authorization"] + [("Authorization", "Bearer " + token)]
    elif auth.kind == "basic":
        user, password = text(auth.username), text(auth.password)
        credential = base64.b64encode(f"{user}:{password}".encode()).decode()
        headers = [(k, v) for k, v in headers if k.lower() != "authorization"] + [
            ("Authorization", "Basic " + credential)
        ]
    elif auth.kind == "api_key":
        if not auth.key_name:
            raise ExecutionError("API Key 名称不能为空")
        token = text(auth.token)
        if auth.location == "header":
            headers = [(k, v) for k, v in headers if k.lower() != auth.key_name.lower()] + [(auth.key_name, token)]
        else:
            params = [(k, v) for k, v in params if k != auth.key_name] + [(auth.key_name, token)]
    target = target.copy_merge_params(params)
    body, kwargs = None, {}
    if spec.body_type == "json":
        body = resolve(spec.body, env, outputs, refs)
        # Explicit encoding also supports JSON null (httpx json=None means no body).
        kwargs["content"] = json.dumps(body, ensure_ascii=False, allow_nan=False).encode("utf-8")
        if not any(k.lower() == "content-type" for k, _ in headers):
            headers.append(("Content-Type", "application/json"))
    elif spec.body_type == "text":
        body = text(spec.body)
        kwargs["content"] = body.encode("utf-8")
    elif spec.body_type == "form":
        pairs = [(p.name, text(p.value)) for p in spec.form if p.enabled]
        body = [{"name": k, "value": v} for k, v in pairs]
        kwargs["content"] = urlencode(pairs).encode("utf-8")
        if not any(k.lower() == "content-type" for k, _ in headers):
            headers.append(("Content-Type", "application/x-www-form-urlencoded"))
    if is_chunked(headers) and isinstance(kwargs.get("content"), (bytes, bytearray)):
        # Feeding the body as a stream is how httpx emits chunked framing and
        # drops Content-Length; sending the header over a body with a length
        # would leave both on the wire and corrupt the request.
        kwargs["content"] = chunked_body(bytes(kwargs["content"]))
    display = {"method": spec.method, "url": str(target), "headers": dict(headers), "body": body}
    return target, headers, kwargs, display


async def _execute_once(step, client, env, outputs, cancel):
    started = time.monotonic()
    log_token = PRIVATE_HTTP_LOGS.set(True)
    detail = {"references": [], "assertions": [], "error": None, "error_category": None}
    status, response = "PASS", None
    try:
        if cancel.is_set():
            raise RunCancelled()
        if step.kind == "wait":
            await cancellable(asyncio.sleep(step.seconds), cancel)
        else:
            config = step.effective()
            target, headers, kwargs, request = make_request(
                config.request, env, outputs, detail["references"]
            )
            detail["request"] = request

            async def send():
                async with client.stream(
                    config.request.method, target, headers=headers, timeout=config.request.timeout_seconds, **kwargs
                ) as res:
                    detail["request"]["headers"] = dict(res.request.headers)
                    data = bytearray()
                    async for chunk in res.aiter_bytes():
                        data.extend(chunk)
                        if len(data) > MAX_RESPONSE_BYTES:
                            raise ExecutionError("响应体超过 5 MiB 上限，未解析截断数据", category="http")
                    text = bytes(data).decode(res.encoding or "utf-8", errors="replace")
                    parsed, json_valid = None, False
                    if text:
                        try:
                            parsed = json.loads(text)
                            json_valid = True
                        except (ValueError, RecursionError):
                            pass
                    body = {
                        "status_code": res.status_code,
                        "headers": dict(res.headers),
                        "cookies": {cookie.name: cookie.value for cookie in res.cookies.jar},
                        "text": text,
                        "elapsed_ms": round((time.monotonic() - started) * 1000, 2),
                    }
                    # Missing body differs from an actual JSON null response.
                    if json_valid:
                        body["body"] = parsed
                    detail["json_valid"] = json_valid
                    return body

            response = await cancellable(send(), cancel, config.request.timeout_seconds)
            detail["response"] = response
            detail["fields"] = field_tree(response)[0]["children"]
            detail["request_references"] = list(detail["references"])
            detail["assertions"] = evaluate_assertions(config.assertions, response, env, outputs, detail["references"])
            status, detail["error"], detail["error_category"] = assertion_outcome(detail["assertions"])
    except RunCancelled:
        status, detail["error"] = "ABORTED", "用户已中止；已发送的请求可能已经生效"
        detail["error_category"] = "cancelled"
    except (ExecutionError, httpx.HTTPError, ValueError, TypeError, *_PROTOCOL_ERRORS) as exc:
        status = "ERROR"
        # Never expose raw client exceptions, which may contain resolved URLs.
        if isinstance(exc, ExecutionError):
            detail["error"], detail["error_category"] = str(exc), exc.category
            if exc.location:
                detail["error_location"] = exc.location
        elif isinstance(exc, httpx.TimeoutException):
            detail["error"], detail["error_category"] = "请求超时，请检查目标服务和超时设置", "connection"
            # Only a connect timeout proves the request never arrived; read and
            # write timeouts may have been processed by the server already.
            detail["retryable"] = isinstance(exc, httpx.ConnectTimeout)
        elif isinstance(exc, (httpx.ConnectError, httpx.NetworkError, httpx.ProxyError)):
            detail["error"], detail["error_category"] = "无法连接目标服务，请检查地址、网络和证书", "connection"
            # NetworkError also covers read/write errors, which must not repeat.
            detail["retryable"] = isinstance(exc, (httpx.ConnectError, httpx.ProxyError))
        elif isinstance(exc, httpx.LocalProtocolError) or (
            H11ProtocolError is not None and isinstance(exc, H11ProtocolError)
        ):
            # The transport refused the configured headers themselves.
            detail["error"], detail["error_category"] = (
                "请求头无法按配置发送：请检查 Content-Length 是否与请求体一致；Transfer-Encoding 仅支持 chunked",
                "configuration",
            )
        elif isinstance(exc, httpx.HTTPError):
            detail["error"], detail["error_category"] = "HTTP 通信异常，请检查服务响应和连接状态", "http"
        else:
            detail["error"], detail["error_category"] = "请求配置无效，请检查参数类型和内容", "configuration"
    finally:
        PRIVATE_HTTP_LOGS.reset(log_token)
    return {
        "step_id": step.id,
        "name": step.name,
        "status": status,
        "duration_ms": round((time.monotonic() - started) * 1000, 2),
        "detail": detail,
    }, response


async def execute_step(step, client, env, outputs, cancel):
    """Run one step, repeating only failures that never reached the server.

    Interface steps can create business data, and this module never rolls back,
    so an already-received response (or a timeout after the request was sent) is
    never repeated even when its assertions failed.
    """
    attempts = 1 + (step.retry_count or 0)
    history = []
    for attempt in range(1, attempts + 1):
        result, response = await _execute_once(step, client, env, outputs, cancel)
        detail = result["detail"]
        # Internal marker: never persisted, never shown to the user.
        retryable = detail.pop("retryable", False)
        detail["attempts"] = attempt
        if history:
            # Keep the failures that led here, so a retried pass is never read
            # as a stable one.
            detail["retry_history"] = list(history)
        if not retryable or attempt == attempts:
            return result, response
        history.append(
            {"attempt": attempt, "error": detail.get("error"), "error_category": detail.get("error_category")}
        )
        try:
            await cancellable(asyncio.sleep(RETRY_DELAY_SECONDS), cancel)
        except RunCancelled:
            result["status"] = "ABORTED"
            detail["error"], detail["error_category"] = "用户已中止", "cancelled"
            detail["retry_history"] = list(history)
            return result, response
    return result, response


def make_client(cookies=None, transport=None):
    return httpx.AsyncClient(
        transport=transport or httpx.AsyncHTTPTransport(verify=True, retries=0, trust_env=False), cookies=cookies, follow_redirects=False, trust_env=False
    )
