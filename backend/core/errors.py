"""Chinese request-validation responses.

FastAPI answers invalid input with a 422 whose ``detail`` lists pydantic
errors in English ("Field required", "Input should be a valid integer"). The
UI is Chinese, so this handler keeps the same shape (``loc`` / ``msg`` /
``type``) and translates the message by error type. Messages raised by our own
validators (``ValueError`` with Chinese text) pass through unchanged.
"""

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

_MESSAGES = {
    "missing": "缺少必填项",
    "extra_forbidden": "存在不支持的字段",
    "json_invalid": "JSON 格式无效",
    "json_type": "请求体必须是 JSON",
    "string_type": "应为文本",
    "int_type": "应为整数",
    "float_type": "应为数字",
    "bool_type": "应为布尔值",
    "list_type": "应为数组",
    "dict_type": "应为对象",
    "model_type": "应为对象",
    "model_attributes_type": "应为对象",
    "int_parsing": "无法解析为整数",
    "int_from_float": "应为整数",
    "float_parsing": "无法解析为数字",
    "bool_parsing": "无法解析为布尔值",
    "literal_error": "取值不在允许范围内",
    "enum": "取值不在允许范围内",
    "string_too_short": "长度不能少于 {min_length}",
    "string_too_long": "长度不能超过 {max_length}",
    "too_short": "数量不能少于 {min_length}",
    "too_long": "数量不能超过 {max_length}",
    "greater_than": "必须大于 {gt}",
    "greater_than_equal": "必须大于或等于 {ge}",
    "less_than": "必须小于 {lt}",
    "less_than_equal": "必须小于或等于 {le}",
    "multiple_of": "必须是 {multiple_of} 的倍数",
    "string_pattern_mismatch": "格式不符合要求",
    "url_parsing": "地址格式无效",
    "url_scheme": "地址协议不受支持",
    "datetime_parsing": "日期时间格式无效",
    "date_parsing": "日期格式无效",
    "finite_number": "必须是有限数字",
}
_VALUE_ERROR_PREFIX = "Value error, "


def translate(error):
    """One pydantic error dict -> Chinese message (without location)."""
    kind = str(error.get("type") or "")
    message = str(error.get("msg") or "")
    if kind in {"value_error", "assertion_error"}:
        text = message[len(_VALUE_ERROR_PREFIX):] if message.startswith(_VALUE_ERROR_PREFIX) else message
        return text or "值无效"
    template = _MESSAGES.get(kind)
    if template is None:
        return "格式无效"
    try:
        return template.format(**(error.get("ctx") or {}))
    except (KeyError, IndexError, ValueError):
        return template.split(" {")[0]


def location(loc):
    """('body', 'steps', 0, 'name') -> 'steps[0].name'."""
    text = ""
    for part in loc:
        if part in ("body", "query", "path", "header") and not text:
            continue
        text += f"[{part}]" if isinstance(part, int) else (f".{part}" if text else str(part))
    return text


def validation_detail(exc: RequestValidationError):
    detail = []
    for error in exc.errors():
        loc = list(error.get("loc") or ())
        where = location(loc)
        message = translate(error)
        detail.append({"loc": loc, "msg": f"{where}：{message}" if where else message, "type": str(error.get("type") or "")})
    return detail


def install_error_handlers(app: FastAPI):
    @app.exception_handler(RequestValidationError)
    async def _validation(_request: Request, exc: RequestValidationError):
        return JSONResponse(status_code=422, content={"detail": validation_detail(exc)})

    return app
