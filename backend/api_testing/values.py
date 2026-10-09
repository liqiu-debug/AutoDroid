import json
import re
from urllib.parse import quote

import httpx

from .schemas import Step, Value


class ExecutionError(ValueError):
    def __init__(self, message, category="configuration", location=None):
        super().__init__(message)
        self.category = category
        self.location = location


MISSING = object()
ENV_PATTERN = re.compile(r"{{\s*([A-Z0-9_]+)\s*}}")


def at_path(data, path, default=MISSING):
    for key in path:
        if isinstance(data, dict) and isinstance(key, str) and key in data:
            data = data[key]
        elif isinstance(data, list) and type(key) is int and 0 <= key < len(data):
            data = data[key]
        else:
            if default is MISSING:
                raise ExecutionError(f"响应字段不存在或下标越界: {json.dumps(path, ensure_ascii=False)}")
            return default
    return data


def scalar_text(value):
    if isinstance(value, (dict, list)):
        raise ExecutionError("文本位置不能引用对象或数组，请选择标量字段")
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def resolve(value: Value, env, outputs, refs=None):
    if value.kind == "literal":
        if isinstance(value.value, str):

            def replace(match):
                key = match.group(1)
                if key not in env:
                    raise ExecutionError(f"环境变量不存在: {key}")
                return str(env[key])

            return ENV_PATTERN.sub(replace, value.value)
        return value.value
    if value.kind == "env":
        if value.name not in env:
            raise ExecutionError(f"环境变量不存在: {value.name}")
        return env[value.name]
    if value.kind == "ref":
        if value.step_id not in outputs:
            raise ExecutionError("缺少上游真实结果，请先调试上游步骤")
        result = at_path(outputs[value.step_id], value.path)
        if refs is not None:
            refs.append({"step_id": value.step_id, "path": value.path, "value": result})
        return result
    if value.kind == "template":
        return "".join(scalar_text(resolve(p, env, outputs, refs)) for p in value.parts)
    if value.kind == "object":
        return {k: resolve(v, env, outputs, refs) for k, v in value.fields.items()}
    return [resolve(v, env, outputs, refs) for v in value.items]


def expression_locations(value, path=()):
    if isinstance(value, Value):
        yield value, list(path)
        for key, child in value.fields.items():
            yield from expression_locations(child, (*path, "fields", key))
        for group in ("items", "parts"):
            for i, child in enumerate(getattr(value, group)):
                yield from expression_locations(child, (*path, group, i))
    elif hasattr(type(value), "model_fields"):
        for key in type(value).model_fields:
            yield from expression_locations(getattr(value, key), (*path, key))
    elif isinstance(value, dict):
        for key, child in value.items():
            yield from expression_locations(child, (*path, key))
    elif isinstance(value, list):
        for i, child in enumerate(value):
            yield from expression_locations(child, (*path, i))


def expressions(value):
    for expression, _ in expression_locations(value):
        yield expression


def input_locations(config):
    """Only validate inputs that the request/assertion evaluator will consume."""
    request = config.request
    yield from expression_locations(request.url, ("request", "url"))
    groups = ["path_params", "query", "headers"] + (["form"] if request.body_type == "form" else [])
    for group in groups:
        for index, parameter in enumerate(getattr(request, group)):
            if parameter.enabled:
                yield from expression_locations(parameter.value, ("request", group, index, "value"))
    if request.body_type in {"json", "text"}:
        yield from expression_locations(request.body, ("request", "body"))
    auth_fields = {"bearer": ["token"], "api_key": ["token"], "basic": ["username", "password"]}
    for key in auth_fields.get(request.auth.kind, []):
        yield from expression_locations(getattr(request.auth, key), ("request", "auth", key))
    for index, assertion in enumerate(config.assertions):
        if assertion.op not in {"exists", "not_empty", "is_2xx"}:
            yield from expression_locations(assertion.expected, ("assertions", index, "expected"))


def request_issues(request, env, outputs=None):
    """Validate known inputs without sending requests or guessing upstream values."""
    errors = []

    def add(location, message):
        errors.append({"location": ["request", *location], "message": message})

    def text(value, location, required=False):
        if outputs is None and any(item.kind == "ref" for item in expressions(value)):
            return MISSING
        try:
            resolved = resolve(value, env, outputs or {})
            result = scalar_text(resolved)
            if required and (resolved is None or not result.strip()):
                add(location, "必填值不能为空")
                return MISSING
            return result
        except ExecutionError as exc:
            add(location, str(exc))
            return MISSING

    url = text(request.url, ["url"], required=True)
    for index, parameter in enumerate(request.path_params):
        if not parameter.enabled:
            continue
        marker = "{" + parameter.name + "}"
        used = url is not MISSING and marker in url
        value = text(parameter.value, ["path_params", index, "value"], required=used)
        if used:
            # An upstream field may fill this parameter at execution time.
            replacement = "pending-value" if value is MISSING else quote(value, safe="")
            url = url.replace(marker, replacement)
    if url is not MISSING:
        if "{" in url or "}" in url:
            add(["url"], "URL 存在未配置的路径参数，请补充对应路径参数")
        try:
            target = httpx.URL(url)
            if target.scheme not in {"http", "https"} or not target.host or target.userinfo:
                add(["url"], "请输入不含用户凭证的完整 HTTP/HTTPS URL")
        except (httpx.InvalidURL, ValueError):
            add(["url"], "请求 URL 格式无效")

    for group in ("query", "headers", "form"):
        if group == "form" and request.body_type != "form":
            continue
        for index, parameter in enumerate(getattr(request, group)):
            if parameter.enabled:
                text(parameter.value, [group, index, "value"])
    if request.body_type == "text":
        text(request.body, ["body"])
    auth = request.auth
    for key in {"bearer": ["token"], "api_key": ["token"], "basic": ["username", "password"]}.get(auth.kind, []):
        text(getattr(auth, key), ["auth", key], required=True)
    if auth.kind == "api_key" and not auth.key_name.strip():
        add(["auth", "key_name"], "API Key 名称不能为空")
    return errors


def validation_error(step, location, message, **extra):
    section = "assertions" if location[0] == "assertions" else {
        "headers": "headers", "auth": "auth", "body": "body", "form": "body",
        "body_type": "body", "timeout_seconds": "settings",
    }.get(location[1] if len(location) > 1 else "", "params")
    return {"step_id": step.id, "step_name": step.name, "location": location, "section": section,
            "error_category": "configuration", "message": message, **extra}


def validate_steps(steps, env=None, *, validation_mode="save"):
    seen, errors = set(), []
    ids = set()
    for step in steps:
        if step.id in ids:
            errors.append(validation_error(step, ["id"], "步骤 ID 重复"))
        ids.add(step.id)
        if step.kind == "request":
            config = step.effective()
            for val, location in input_locations(config):
                if val.kind == "ref" and val.step_id not in seen:
                    errors.append(
                        validation_error(step, location, "只能引用已存在的前序接口步骤", source=val.step_id, path=val.path)
                    )
                names = (
                    [val.name]
                    if val.kind == "env"
                    else ENV_PATTERN.findall(val.value)
                    if val.kind == "literal" and isinstance(val.value, str)
                    else []
                )
                if env is not None:
                    for name in names:
                        if name not in env:
                            errors.append(validation_error(step, location, f"环境变量不存在: {name}"))
            if validation_mode in {"debug", "run"}:
                for issue in request_issues(config.request, env or {}):
                    error = validation_error(step, issue["location"], issue["message"])
                    if error not in errors:
                        errors.append(error)
            if validation_mode == "run" and not config.assertions:
                errors.append(validation_error(step, ["assertions"], "正式运行前，每个接口步骤至少需要一条断言"))
            seen.add(step.id)
    if validation_mode == "run" and not any(step.kind == "request" for step in steps):
        errors.append({"location": ["steps"], "section": "steps", "error_category": "configuration",
                       "message": "正式运行的场景至少需要一个接口请求步骤"})
    return errors


def value_type(value):
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, (int, float)):
        return "number"
    if isinstance(value, dict):
        return "object"
    if isinstance(value, list):
        return "array"
    return "string"


def same_value(a, b):
    if value_type(a) != value_type(b):
        return False
    if isinstance(a, dict):
        return a.keys() == b.keys() and all(same_value(v, b[k]) for k, v in a.items())
    if isinstance(a, list):
        return len(a) == len(b) and all(same_value(x, y) for x, y in zip(a, b))
    return a == b


def evaluate_assertions(assertions, response, env, outputs, refs):
    results = []
    absent = object()
    for assertion in assertions:
        actual = at_path(response, assertion.path, absent)
        expected = None
        ok, reason = False, ""
        try:
            op = assertion.op
            if op not in {"exists", "not_empty", "is_2xx"}:
                expected = resolve(assertion.expected, env, outputs, refs)
            if op == "exists":
                ok = actual is not absent
            elif actual is absent:
                reason = "字段不存在"
            elif op == "not_empty":
                ok = actual is not None and actual != "" and actual != [] and actual != {}
                if not ok:
                    reason = "字段值为空"
            elif op in {"eq", "ne"}:
                ok = same_value(actual, expected) == (op == "eq")
                if not ok:
                    reason = "类型不匹配" if value_type(actual) != value_type(expected) else "值不相等" if op == "eq" else "值不应相等"
            elif op == "is_2xx":
                ok = type(actual) is int and 200 <= actual < 300
            elif op == "contains":
                if isinstance(actual, list):
                    ok = any(same_value(v, expected) for v in actual)
                elif isinstance(actual, str) and isinstance(expected, str):
                    ok = expected in actual
                elif isinstance(actual, dict) and isinstance(expected, str):
                    ok = expected in actual
            elif op == "type":
                ok = value_type(actual) == expected
            elif op == "length":
                ok = isinstance(actual, (dict, list, str)) and type(expected) is int and len(actual) == expected
            else:
                if value_type(actual) != "number" or value_type(expected) != "number":
                    reason = "大小比较需要数字类型，不能隐式转换"
                else:
                    ok = {
                        "gt": actual > expected,
                        "gte": actual >= expected,
                        "lt": actual < expected,
                        "lte": actual <= expected,
                    }[op]
        except ExecutionError as exc:
            reason = str(exc)
        except (TypeError, ValueError):
            reason = "断言值类型不匹配"
        results.append(
            {
                "id": assertion.id,
                "path": assertion.path,
                "op": assertion.op,
                "actual": None if actual is absent else actual,
                "missing": actual is absent,
                "expected": expected,
                "passed": ok,
                "message": reason or ("通过" if ok else "断言未满足"),
            }
        )
    return results


def assertion_outcome(checks):
    if not checks:
        return "UNCHECKED", "请求已完成，但未配置断言；请添加断言并校验当前响应", "unchecked"
    failed = [check for check in checks if not check["passed"]]
    if failed:
        category = "http" if any(check["path"] == ["status_code"] for check in failed) else "assertion"
        return "FAIL", "断言未全部通过", category
    return "PASS", None, None


def field_tree(value, path=None, budget=None):
    path, budget = path or [], budget if budget is not None else [2000]
    if budget[0] <= 0 or len(path) > 32:
        return []
    budget[0] -= 1
    row = {"path": path, "type": value_type(value)}
    if not isinstance(value, (dict, list)):
        example = value
        row["example"] = example
    children = value.items() if isinstance(value, dict) else enumerate(value) if isinstance(value, list) else []
    row["children"] = []
    for key, child in children:
        if budget[0] <= 0:
            row["truncated"] = True
            break
        row["children"].extend(field_tree(child, path + [key], budget))
    return [row]


def sync_preview(step: Step, config, version):
    """Keep overrides intact and explicitly surface overlapping template changes."""
    old = step.snapshot.model_dump()
    new = config.model_dump()
    changes, conflicts = [], []
    for section in ("request", "assertions", "sensitive_paths"):
        if section == "request":
            for key in set(old[section]) | set(new[section]):
                if old[section].get(key) != new[section].get(key):
                    changes.append(
                        {"path": [section, key], "before": old[section].get(key), "after": new[section].get(key)}
                    )
                    if key in step.overrides.get("request", {}):
                        conflicts.append([section, key])
        elif old[section] != new[section]:
            changes.append({"path": [section], "before": old[section], "after": new[section]})
            if section in step.overrides:
                conflicts.append([section])
    proposal = step.model_copy(deep=True)
    proposal.snapshot, proposal.interface_version = config, version
    proposal.effective()
    return {"step": proposal.model_dump(), "changes": changes, "conflicts": conflicts}


def conflict_key(path):
    """Stable conflict identifier shared with the frontend.

    Section names and request field names never contain dots, so joining is
    unambiguous and avoids depending on JSON whitespace matching.
    """
    return ".".join(str(part) for part in path)


def apply_sync(step, config, version, choices=None):
    """Apply an interface update to a step, honouring per-conflict choices.

    Returns ``(updated_step, unresolved_conflicts)``. Callers must treat a
    non-empty unresolved list as a refusal to write anything: an unresolved
    conflict means the user has not decided whether to keep their local edit.
    """
    preview = sync_preview(step, config, version)
    choices = choices or {}
    unresolved = [
        list(path) for path in preview["conflicts"] if choices.get(conflict_key(path)) not in {"keep", "template"}
    ]
    if unresolved:
        return None, unresolved
    updated = Step.model_validate(preview["step"])
    for path in preview["conflicts"]:
        if choices[conflict_key(path)] == "template":
            if len(path) == 2:
                updated.overrides.get("request", {}).pop(path[1], None)
            else:
                updated.overrides.pop(path[0], None)
    updated.effective()
    return updated, []
