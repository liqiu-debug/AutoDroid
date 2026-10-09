"""Read-only interface document import: OpenAPI 3, Swagger 2.0, Postman v2.1.

Mirrors curl.py's contract: nothing is executed, no file is read, no network
call is made. Parsing only produces drafts, and only the entries the user
selects are ever turned into interfaces. Credentials found in a document are
never silently trusted: values are kept (so a request still works) but always
reported as a warning, and structured auth schemes import as empty values.
"""

import json
import re
from urllib.parse import parse_qsl, urlsplit

from .schemas import DefinitionConfig, Parameter, RequestSpec, from_json, literal
from .values import ExecutionError

MAX_SPEC_CHARS = 2 * 1024 * 1024
MAX_CANDIDATES = 300
MAX_SCHEMA_DEPTH = 12
METHODS = ("get", "post", "put", "patch", "delete", "head", "options")
JSON_MEDIA = "application/json"
FORM_MEDIA = ("application/x-www-form-urlencoded", "multipart/form-data")
CREDENTIAL_HEADERS = {"authorization", "cookie", "x-api-key", "api-key", "x-auth-token", "token"}


def parse_spec(content):
    """Return {"format", "candidates", "warnings"} without sending anything."""
    document = _load(content)
    if "openapi" in document:
        return _dedupe(_openapi(document))
    if "swagger" in document:
        return _dedupe(_swagger(document))
    if isinstance(document.get("item"), list):
        return _dedupe(_postman(document))
    raise ExecutionError("无法识别文档格式，请提供 OpenAPI 3、Swagger 2.0 或 Postman v2.1 文档")


def _load(content):
    if not isinstance(content, str) or not content.strip():
        raise ExecutionError("请粘贴接口文档内容")
    if len(content) > MAX_SPEC_CHARS:
        raise ExecutionError("接口文档超过 2 MiB 上限，请拆分后再导入")
    text = content.strip()
    if text.startswith("{"):
        try:
            document = json.loads(text)
        except ValueError:
            raise ExecutionError("JSON 格式无效，请检查粘贴内容是否完整") from None
    else:
        try:
            import yaml
        except ImportError:
            raise ExecutionError("当前部署未安装 PyYAML，无法解析 YAML；请改用 JSON 或安装 pyyaml") from None
        try:
            # safe_load refuses arbitrary Python tags, so a document can never
            # construct objects.
            document = yaml.safe_load(text)
        except yaml.YAMLError:
            raise ExecutionError("YAML 格式无效，请检查粘贴内容是否完整") from None
    if not isinstance(document, dict):
        raise ExecutionError("接口文档必须是一个对象")
    return document


def _dedupe(result):
    seen = {}
    for candidate in result["candidates"]:
        base = candidate["key"]
        seen[base] = seen.get(base, 0) + 1
        if seen[base] > 1:
            candidate["key"] = f"{base} #{seen[base]}"
    return result


def _result(format_name, candidates, warnings):
    if not candidates:
        raise ExecutionError("文档中没有可导入的接口")
    return {"format": format_name, "candidates": candidates, "warnings": warnings}


def _candidate(key, name, summary, spec, sample, warnings):
    return {
        "key": key,
        "name": (str(name or "").strip() or f"{spec.method} {key}")[:200],
        "method": spec.method,
        "path": key,
        "summary": str(summary or "")[:4000],
        "config": DefinitionConfig(request=spec).model_dump(),
        "sample": sample,
        "warnings": warnings,
    }


def _resolve(node, root, depth=0):
    """Follow local ``$ref`` pointers; anything unreachable resolves to {}."""
    while isinstance(node, dict) and "$ref" in node and depth <= MAX_SCHEMA_DEPTH:
        pointer = node["$ref"]
        if not isinstance(pointer, str) or not pointer.startswith("#/"):
            return {}
        target = root
        for part in pointer[2:].split("/"):
            part = part.replace("~1", "/").replace("~0", "~")
            if isinstance(target, dict) and part in target:
                target = target[part]
            else:
                return {}
        node, depth = target, depth + 1
    return node if isinstance(node, dict) else {}


def _example_from_schema(schema, root, depth=0):
    """Build a representative value so an imported body is editable, not empty."""
    if depth > MAX_SCHEMA_DEPTH:
        return None
    schema = _resolve(schema, root, depth)
    for key in ("example", "default"):
        if key in schema:
            return schema[key]
    examples = schema.get("examples")
    if isinstance(examples, list) and examples and isinstance(examples[0], dict):
        if "value" in examples[0]:
            return examples[0]["value"]
    if isinstance(schema.get("enum"), list) and schema["enum"]:
        return schema["enum"][0]
    kind = schema.get("type")
    if isinstance(kind, list):
        kind = next((item for item in kind if item != "null"), None)
    if kind == "object" or "properties" in schema:
        return {
            str(name): _example_from_schema(child, root, depth + 1)
            for name, child in (schema.get("properties") or {}).items()
        }
    if kind == "array":
        item = _example_from_schema(schema.get("items"), root, depth + 1)
        return [] if item is None else [item]
    if kind in {"integer", "number"}:
        return 0
    if kind == "boolean":
        return False
    if kind == "null":
        return None
    return "" if kind == "string" else None


def _join_url(base, path):
    if not base:
        return path or ""
    if not path:
        return base
    return base.rstrip("/") + "/" + path.lstrip("/")


def _path_parameters(path, declared):
    """Keep ``{placeholder}`` in the URL and declare each one for substitution."""
    names = []
    for name in re.findall(r"\{([^{}]+)\}", path or ""):
        if name not in names:
            names.append(name)
    return [Parameter(name=name, value=literal(declared.get(name, ""))) for name in names]


def _parameter_example(parameter, root):
    parameter = _resolve(parameter, root)
    if "example" in parameter:
        return parameter["example"]
    # Swagger 2 keeps the type and default inline on the parameter itself.
    if "default" in parameter:
        return parameter["default"]
    schema = _resolve(parameter.get("schema"), root)
    if "example" in schema:
        return schema["example"]
    if "default" in schema:
        return schema["default"]
    return ""


def _apply_parameters(spec, parameters, root, declared, path):
    query, headers = [], []
    for parameter in parameters:
        parameter = _resolve(parameter, root)
        name, location = str(parameter.get("name") or ""), parameter.get("in")
        if not name:
            continue
        value = _parameter_example(parameter, root)
        if location == "path":
            declared[name] = value
        elif location == "query":
            query.append(Parameter(name=name, value=literal(value)))
        elif location == "header":
            headers.append(Parameter(name=name, value=literal(value)))
    spec.path_params = _path_parameters(path, declared)
    spec.query, spec.headers = query, headers


def _credential_warning(headers):
    if any(row.name.lower() in CREDENTIAL_HEADERS and str(row.value.value or "").strip() for row in headers):
        return ["请求头中的凭证已按原文导入，建议替换为敏感环境变量。"]
    return []


def _openapi(document):
    warnings = []
    base = ""
    servers = document.get("servers")
    if isinstance(servers, list) and servers and isinstance(servers[0], dict):
        base = str(servers[0].get("url") or "").strip()
    warnings.append(
        f"请求地址使用文档中的 {base}；建议改为 {{{{BASE_URL}}}} 环境变量以便切换环境。" if base
        else "文档未声明服务地址，导入后需要补全请求地址。"
    )
    paths = document.get("paths")
    if not isinstance(paths, dict):
        raise ExecutionError("文档缺少 paths 定义")
    candidates = []
    for path, operations in paths.items():
        if not isinstance(operations, dict):
            continue
        shared = operations.get("parameters")
        for method in METHODS:
            operation = operations.get(method)
            if not isinstance(operation, dict):
                continue
            candidates.append(_openapi_candidate(document, method, path, operation, shared, base))
            if len(candidates) >= MAX_CANDIDATES:
                warnings.append(f"文档接口超过 {MAX_CANDIDATES} 个，只导入前 {MAX_CANDIDATES} 个。")
                return _result("openapi", candidates, warnings)
    return _result("openapi", candidates, warnings)


def _openapi_candidate(document, method, path, operation, shared, base):
    spec = RequestSpec(method=method.upper())
    spec.url = literal(_join_url(base, path))
    declared = {}
    parameters = [*(shared if isinstance(shared, list) else []), *(operation.get("parameters") or [])]
    _apply_parameters(spec, parameters, document, declared, path)
    warnings = list(_credential_warning(spec.headers))
    if spec.path_params:
        names = "、".join(row.name for row in spec.path_params)
        warnings.append(f"路径参数 {names} 需要在场景中填写后才能调试。")
    warnings.extend(_openapi_body(document, operation, spec))
    if operation.get("security") or (document.get("components") or {}).get("securitySchemes"):
        warnings.append("文档声明了鉴权方式，请在「鉴权」中配置凭证或敏感环境变量。")
    summary = operation.get("summary") or operation.get("description") or operation.get("operationId") or ""
    name = operation.get("summary") or operation.get("operationId") or f"{spec.method} {path}"
    return _candidate(f"{spec.method} {path}", name, summary, spec, _openapi_sample(document, operation), warnings)


def _openapi_body(document, operation, spec):
    body = _resolve(operation.get("requestBody"), document)
    content = body.get("content")
    if not isinstance(content, dict):
        return []
    chosen = None
    for media in (JSON_MEDIA, *FORM_MEDIA, "text/plain"):
        for key, value in content.items():
            if str(key).split(";")[0].strip().lower() == media:
                chosen = (media, value)
                break
        if chosen:
            break
    if not chosen:
        return ["请求体使用了暂不支持的内容类型，未导入请求体。"]
    media, media_object = chosen
    media_object = _resolve(media_object, document)
    if media == JSON_MEDIA:
        example = _example_from_schema(media_object.get("schema"), document)
        spec.body_type, spec.body = "json", from_json(example if example is not None else {})
        return []
    if media in FORM_MEDIA:
        properties = _resolve(media_object.get("schema"), document).get("properties") or {}
        spec.body_type = "form"
        spec.form = [
            Parameter(name=str(name), value=literal(_example_from_schema(child, document)))
            for name, child in properties.items()
        ]
        return ["文件上传参数未导入，请在请求体中手工补充。"] if media == "multipart/form-data" else []
    spec.body_type, spec.body = "text", literal("")
    return []


def _openapi_sample(document, operation):
    responses = operation.get("responses")
    if not isinstance(responses, dict):
        return None
    for status, response in responses.items():
        if not str(status).startswith("2"):
            continue
        content = _resolve(response, document).get("content")
        if not isinstance(content, dict):
            continue
        media = next((value for key, value in content.items() if str(key).split(";")[0].strip().lower() == JSON_MEDIA), None)
        media = _resolve(media, document)
        example = media.get("example")
        if example is None:
            example = _example_from_schema(media.get("schema"), document)
        if example is not None:
            return {"body": example}
    return None


def _swagger(document):
    warnings = []
    schemes = document.get("schemes") if isinstance(document.get("schemes"), list) else []
    host = str(document.get("host") or "").strip()
    base = f"{schemes[0] if schemes else 'https'}://{host}{document.get('basePath') or ''}" if host else ""
    warnings.append(
        f"请求地址使用文档中的 {base}；建议改为 {{{{BASE_URL}}}} 环境变量以便切换环境。" if base
        else "文档未声明 host，导入后需要补全请求地址。"
    )
    paths = document.get("paths")
    if not isinstance(paths, dict):
        raise ExecutionError("文档缺少 paths 定义")
    candidates = []
    for path, operations in paths.items():
        if not isinstance(operations, dict):
            continue
        shared = operations.get("parameters")
        for method in METHODS:
            operation = operations.get(method)
            if not isinstance(operation, dict):
                continue
            candidates.append(_swagger_candidate(document, method, path, operation, shared, base))
            if len(candidates) >= MAX_CANDIDATES:
                warnings.append(f"文档接口超过 {MAX_CANDIDATES} 个，只导入前 {MAX_CANDIDATES} 个。")
                return _result("swagger", candidates, warnings)
    return _result("swagger", candidates, warnings)


def _swagger_candidate(document, method, path, operation, shared, base):
    spec = RequestSpec(method=method.upper())
    spec.url = literal(_join_url(base, path))
    declared, form, body_schema = {}, [], None
    parameters = [*(shared if isinstance(shared, list) else []), *(operation.get("parameters") or [])]
    _apply_parameters(spec, parameters, document, declared, path)
    for parameter in parameters:
        parameter = _resolve(parameter, document)
        location = parameter.get("in")
        if location == "body":
            body_schema = parameter.get("schema")
        elif location == "formData":
            name = str(parameter.get("name") or "")
            if name:
                form.append(Parameter(name=name, value=literal(_parameter_example(parameter, document))))
    warnings = list(_credential_warning(spec.headers))
    if spec.path_params:
        names = "、".join(row.name for row in spec.path_params)
        warnings.append(f"路径参数 {names} 需要在场景中填写后才能调试。")
    if body_schema is not None:
        example = _example_from_schema(body_schema, document)
        spec.body_type, spec.body = "json", from_json(example if example is not None else {})
    elif form:
        spec.body_type, spec.form = "form", form
    if operation.get("security") or document.get("securityDefinitions"):
        warnings.append("文档声明了鉴权方式，请在「鉴权」中配置凭证或敏感环境变量。")
    summary = operation.get("summary") or operation.get("description") or operation.get("operationId") or ""
    name = operation.get("summary") or operation.get("operationId") or f"{spec.method} {path}"
    return _candidate(f"{spec.method} {path}", name, summary, spec, _swagger_sample(document, operation), warnings)


def _swagger_sample(document, operation):
    responses = operation.get("responses")
    if not isinstance(responses, dict):
        return None
    for status, response in responses.items():
        if not str(status).startswith("2"):
            continue
        response = _resolve(response, document)
        example = response.get("examples")
        if isinstance(example, dict):
            value = next((item for key, item in example.items() if str(key).split(";")[0].strip().lower() == JSON_MEDIA), None)
            if isinstance(value, dict):
                return {"body": value}
        schema_example = _example_from_schema(response.get("schema"), document)
        if schema_example is not None:
            return {"body": schema_example}
    return None


def _postman(document):
    warnings = []
    items = []
    _flatten_postman(document.get("item"), items, 0)
    if not items:
        raise ExecutionError("Postman 集合中没有可导入的请求")
    if isinstance(document.get("auth"), dict):
        warnings.append("集合中声明的凭证未导入，请使用敏感环境变量配置鉴权。")
    candidates = []
    for item in items:
        if len(candidates) >= MAX_CANDIDATES:
            warnings.append(f"集合请求超过 {MAX_CANDIDATES} 个，只导入前 {MAX_CANDIDATES} 个。")
            break
        candidate, extra = _postman_candidate(document, item)
        if candidate:
            candidates.append(candidate)
        warnings.extend(extra)
    return _result("postman", candidates, warnings)


def _flatten_postman(items, out, depth):
    if depth > 10 or not isinstance(items, list):
        return
    for item in items:
        if not isinstance(item, dict):
            continue
        if isinstance(item.get("item"), list):
            _flatten_postman(item["item"], out, depth + 1)
        elif isinstance(item.get("request"), (dict, str)):
            out.append(item)


def _postman_candidate(document, item):
    request = item.get("request")
    if isinstance(request, str):
        request = {"method": "GET", "url": request}
    method = str(request.get("method") or "GET").upper()
    if method.lower() not in METHODS:
        return None, [f"已跳过不支持的请求方法 {method}。"]
    raw = request.get("url")
    url_object = raw if isinstance(raw, dict) else {}
    raw = raw if isinstance(raw, str) else str(url_object.get("raw") or "")
    parsed = urlsplit(raw)
    if parsed.scheme not in {"http", "https"}:
        return None, [f"已跳过缺少 HTTP/HTTPS 地址的请求「{item.get('name') or raw}」。"]
    spec = RequestSpec(method=method)
    spec.url = literal(f"{parsed.scheme}://{parsed.netloc}{parsed.path}")
    spec.path_params = _path_parameters(parsed.path, {})
    spec.query = [Parameter(name=key, value=literal(value)) for key, value in parse_qsl(parsed.query, keep_blank_values=True)]
    disabled = {str(entry.get("key")) for entry in url_object.get("query") or [] if isinstance(entry, dict) and entry.get("disabled")}
    for row in spec.query:
        row.enabled = row.name not in disabled
    spec.headers = [
        Parameter(name=str(entry.get("key")), value=literal(entry.get("value")), enabled=not entry.get("disabled"))
        for entry in request.get("header") or []
        if isinstance(entry, dict) and entry.get("key")
    ]
    warnings = list(_credential_warning(spec.headers))
    if spec.path_params:
        names = "、".join(row.name for row in spec.path_params)
        warnings.append(f"路径参数 {names} 需要在场景中填写后才能调试。")
    request_auth = request.get("auth")
    auth = request_auth or document.get("auth")
    warnings.extend(_postman_auth(spec, auth))
    if isinstance(request_auth, dict):
        # Collection-level auth is reported once; per-request auth is worth
        # repeating because it is easy to miss on a single interface.
        warnings.append("该请求声明的凭证未导入，请使用敏感环境变量配置鉴权。")
    warnings.extend(_postman_body(spec, request.get("body")))
    summary = (request.get("description") or "") if isinstance(request.get("description"), str) else ""
    return (
        _candidate(f"{method} {parsed.path}", item.get("name") or f"{method} {parsed.path}", summary, spec,
                   _postman_sample(item), warnings),
        [],
    )


def _postman_auth(spec, auth):
    if not isinstance(auth, dict):
        return []
    kind = str(auth.get("type") or "").lower()
    fields = {str(entry.get("key")): entry.get("value") for entry in auth.get(kind) or [] if isinstance(entry, dict)}
    if kind == "bearer":
        spec.auth.kind, spec.auth.token = "bearer", literal("")
        return []
    if kind == "basic":
        spec.auth.kind, spec.auth.username, spec.auth.password = "basic", literal(""), literal("")
        return []
    if kind == "apikey":
        spec.auth.kind, spec.auth.token = "api_key", literal("")
        spec.auth.key_name = str(fields.get("key") or spec.auth.key_name)
        spec.auth.location = "query" if str(fields.get("in") or "").lower() == "query" else "header"
        return []
    if kind in {"noauth", ""}:
        return []
    return [f"暂不支持集合声明的鉴权方式 {kind}，请手工配置。"]


def _postman_body(spec, body):
    if not isinstance(body, dict):
        return []
    mode = str(body.get("mode") or "")
    if mode == "raw":
        text = str(body.get("raw") or "")
        content_type = next((row.value.value for row in spec.headers if row.name.lower() == "content-type"), "")
        if "json" in str(content_type).lower():
            try:
                spec.body_type, spec.body = "json", from_json(json.loads(text) if text.strip() else {})
            except ValueError:
                spec.body_type, spec.body = "text", literal(text)
                return ["请求体声明为 JSON 但不是有效 JSON，已按文本导入。"]
            return []
        spec.body_type, spec.body = "text", literal(text)
        return []
    if mode in {"urlencoded", "formdata"}:
        rows, warnings = [], []
        for entry in body.get(mode) or []:
            if not isinstance(entry, dict) or not entry.get("key"):
                continue
            if str(entry.get("type") or "") == "file":
                warnings.append("文件上传参数未导入，请在请求体中手工补充。")
                continue
            rows.append(Parameter(name=str(entry["key"]), value=literal(entry.get("value")), enabled=not entry.get("disabled")))
        spec.body_type, spec.form = "form", rows
        return warnings
    if mode in {"file", "graphql"}:
        return [f"暂不支持 {mode} 类型的请求体，未导入请求体。"]
    return []


def _postman_sample(item):
    for response in item.get("response") or []:
        if not isinstance(response, dict):
            continue
        text = response.get("body")
        if not isinstance(text, str) or not text.strip():
            continue
        try:
            return {"body": json.loads(text)}
        except ValueError:
            continue
    return None
