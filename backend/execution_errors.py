"""
执行期错误码与结构化异常（E2xxx）。

对齐 docs/EXECUTION_SPEC.md 第 6 节：
- P1xxx: 预检/执行通用码（见 backend/cross_platform_execution.py）。
- E2xxx: 执行期错误码（本模块），为执行失败提供 `错误码 + 上下文 + 修复建议`。

约定：
- 步骤结果的 `error` 字符串保持原格式不变；结构化信息通过纯增量字段
  `error_code` / `error_context` / `suggestion` 附加（见 cross_platform_runner._result）。
- 驱动层在语义明确的失败点抛 `ExecutionStepError`（或其类型兼容子类）；
  未结构化的异常由 `classify_exception` 兜底归类。
"""
from __future__ import annotations

import re
from typing import Any, Dict, Optional, Tuple

# ------------------------------------------------------------------ #
#  执行期错误码（E2xxx）
# ------------------------------------------------------------------ #

E2001_ELEMENT_NOT_FOUND = "E2001_ELEMENT_NOT_FOUND"
E2002_WAIT_TIMEOUT = "E2002_WAIT_TIMEOUT"
E2003_ASSERT_TEXT_FAILED = "E2003_ASSERT_TEXT_FAILED"
E2004_ASSERT_IMAGE_FAILED = "E2004_ASSERT_IMAGE_FAILED"
E2005_IMAGE_NOT_MATCHED = "E2005_IMAGE_NOT_MATCHED"
E2006_OCR_NO_RESULT = "E2006_OCR_NO_RESULT"
E2007_INPUT_FAILED = "E2007_INPUT_FAILED"
E2008_APP_CONTROL_FAILED = "E2008_APP_CONTROL_FAILED"
E2009_CLICK_NO_EFFECT = "E2009_CLICK_NO_EFFECT"
E2101_DEVICE_CONNECTION_LOST = "E2101_DEVICE_CONNECTION_LOST"
E2102_WDA_SESSION_ERROR = "E2102_WDA_SESSION_ERROR"
E2201_INVALID_ARGS = "E2201_INVALID_ARGS"
E2999_EXECUTION_ERROR = "E2999_EXECUTION_ERROR"

# 用户主动中止不属于需要修复的执行失败，不参与错误码归类。
USER_ABORT_MESSAGE = "执行已被用户中止"

# 集中维护的默认中文修复建议（错误码 -> 建议）。
# 同时收录执行期可能透传的 P1xxx 预检码，便于消费方统一渲染。
DEFAULT_SUGGESTIONS: Dict[str, str] = {
    E2001_ELEMENT_NOT_FOUND: (
        "请检查定位器是否正确、页面是否已加载完成；"
        "可在录制模式重新抓取该元素，或为步骤配置备选定位器。"
    ),
    E2002_WAIT_TIMEOUT: (
        "元素在超时时间内未出现：请确认页面跳转是否完成，"
        "适当增大步骤 timeout，或在该步骤前增加等待。"
    ),
    E2003_ASSERT_TEXT_FAILED: (
        "页面文本与期望不一致：请核对期望文本与页面实际文案是否一致；"
        "若页面加载较慢，可在断言前增加等待。"
    ),
    E2004_ASSERT_IMAGE_FAILED: (
        "图像断言失败：请确认模板图与当前页面一致；"
        "分辨率、主题或文案变化会影响匹配，必要时重新截取模板图。"
    ),
    E2005_IMAGE_NOT_MATCHED: (
        "未在屏幕上匹配到模板图像：请确认目标已出现在屏幕上，"
        "或重新截取更清晰、范围更小的模板图。"
    ),
    E2006_OCR_NO_RESULT: (
        "OCR 未提取到目标文本：请检查识别区域坐标与提取规则是否正确、"
        "区域内是否有清晰文本；可适当扩大区域或增加等待时间。"
    ),
    E2007_INPUT_FAILED: (
        "输入失败：请确认目标为可输入控件且已获得焦点；"
        "可先点击输入框再输入，或检查键盘是否弹出。"
    ),
    E2008_APP_CONTROL_FAILED: (
        "应用启动/停止失败：请检查 app_key 映射的包名/BundleID 是否正确、"
        "应用是否已安装在该设备上。"
    ),
    E2009_CLICK_NO_EFFECT: (
        "点击已执行但页面无变化：请检查元素是否可点击或被遮挡，"
        "必要时改用坐标点击/图像点击。"
    ),
    E2101_DEVICE_CONNECTION_LOST: (
        "Android 设备连接异常：请检查 USB/网络连接与 adb devices 状态，"
        "必要时重新插拔设备或重新初始化 uiautomator2 服务。"
    ),
    E2102_WDA_SESSION_ERROR: (
        "iOS WDA 会话异常：请确认 WebDriverAgent 正在运行且端口转发正常，"
        "必要时重启 WDA 后重试。"
    ),
    E2201_INVALID_ARGS: (
        "步骤参数非法：请检查步骤的 args/value/selector 配置是否符合执行规范。"
    ),
    E2999_EXECUTION_ERROR: (
        "未知执行异常：请结合错误信息与后端日志排查；"
        "若与设备状态相关，可重试或重启设备后再执行。"
    ),
    # 预检/执行通用码（P1xxx）的建议，供执行期透传时统一渲染。
    "P1001_PLATFORM_NOT_ALLOWED": (
        "当前设备平台不在步骤 execute_on 范围内：如需在该平台执行，请调整步骤的 execute_on 配置。"
    ),
    "P1002_ACTION_NOT_SUPPORTED": (
        "当前平台不支持该动作：请检查步骤 action 是否正确，或调整 execute_on 限定平台。"
    ),
    "P1003_SELECTOR_MISSING": (
        "步骤缺少可用定位器：请为当前平台补充 selector/by（可在录制模式抓取元素）。"
    ),
    "P1004_APP_MAPPING_MISSING": (
        "app_key 未映射到当前平台：请在应用映射配置中补充该 app_key 的包名/BundleID。"
    ),
    "P1005_WDA_UNAVAILABLE": (
        "WDA 不可用：请确认 iOS 设备的 WebDriverAgent 已启动且可访问。"
    ),
    "P1006_INVALID_ARGS": (
        "步骤参数结构非法：请检查 args/value 是否按规范填写"
        "（如 input 需要 text，assert_text 需要 expected_text）。"
    ),
}

_EMBEDDED_CODE_PATTERN = re.compile(r"\b([PSE]\d{4}_[A-Z0-9_]+)")


def suggestion_for(code: str) -> str:
    """返回错误码的默认中文修复建议（未知码返回空串）。"""
    return DEFAULT_SUGGESTIONS.get(str(code or "").strip(), "")


# ------------------------------------------------------------------ #
#  结构化异常
# ------------------------------------------------------------------ #


class ExecutionStepError(Exception):
    """
    执行期结构化异常：携带错误码、失败上下文与修复建议。

    `str(exc)` 保持为原始 message（不带错误码前缀），
    以兼容步骤结果 `error` 字符串的现有格式与既有测试断言。
    """

    def __init__(
        self,
        code: str,
        message: str,
        *,
        context: Optional[Dict[str, Any]] = None,
        suggestion: Optional[str] = None,
    ) -> None:
        text = str(message or "")
        super().__init__(text)
        self.code = str(code or "").strip() or E2999_EXECUTION_ERROR
        self.message = text
        self.context: Dict[str, Any] = dict(context) if isinstance(context, dict) else {}
        self.suggestion = str(suggestion).strip() if suggestion else suggestion_for(self.code)

    def __str__(self) -> str:
        return self.message


class ExecutionStepRuntimeError(ExecutionStepError, RuntimeError):
    """RuntimeError 兼容形态：既是 ExecutionStepError 也是 RuntimeError。"""


class ExecutionStepAssertionError(ExecutionStepError, AssertionError):
    """AssertionError 兼容形态：既是 ExecutionStepError 也是 AssertionError。"""


# ------------------------------------------------------------------ #
#  异常归类
# ------------------------------------------------------------------ #

# uiautomator2（Android）异常按类名归类，依据 uiautomator2.exceptions 实际类层次：
# - UiObjectNotFoundError / XPathElementNotFoundError -> 元素未找到
# - AppNotFoundError -> 应用控制失败；InputIMEError -> 输入失败
# - ConnectError / HTTPError(HTTPTimeoutError) / SessionBrokenError /
#   UiAutomation*Error / AdbShellError / DeviceError -> 设备连接异常
_U2_ELEMENT_NOT_FOUND_NAMES = {"UiObjectNotFoundError", "XPathElementNotFoundError"}
_U2_CONNECTION_NAMES = {
    "ConnectError",
    "SessionBrokenError",
    "UiAutomationNotConnectedError",
    "LaunchUiAutomationError",
    "UiAutomationError",
    "HTTPTimeoutError",
    "HTTPError",
    "AdbShellError",
    "AdbBroadcastError",
    "DeviceError",
}

# facebook-wda（iOS）异常按类名归类，依据 wda.exceptions 实际类层次：
# - WDAElementNotFoundError / WDAStaleElementReferenceError /
#   WDAElementNotDisappearError -> 元素未找到
# - WDAKeyboardNotPresentError -> 输入失败
# - WDAInvalidSessionIdError / WDAPossiblyCrashedError / WDAEmptyResponseError /
#   WDABadGateway / WDARequestError / WDAError / Mux*Error / HTTPError -> WDA 会话异常
_WDA_ELEMENT_NOT_FOUND_NAMES = {
    "WDAElementNotFoundError",
    "WDAElementNotDisappearError",
    "WDAStaleElementReferenceError",
}
_WDA_SESSION_NAMES = {
    "WDAInvalidSessionIdError",
    "WDAPossiblyCrashedError",
    "WDAEmptyResponseError",
    "WDABadGateway",
    "WDAUnknownError",
    "WDARequestError",
    "WDAError",
    "MuxConnectError",
    "MuxConnectToUsbmuxdError",
    "MuxError",
    "HTTPError",
}

# 消息关键词兜底规则（按顺序匹配，先命中先归类）。
_MESSAGE_RULES: Tuple[Tuple[str, Tuple[str, ...]], ...] = (
    (
        E2006_OCR_NO_RESULT,
        (
            "未识别到文本",
            "正则未匹配到内容",
            "内置模板未匹配到内容",
            "边界提取结果为空",
            "未找到左边界",
            "未找到右边界",
        ),
    ),
    (E2002_WAIT_TIMEOUT, ("等待超时", "元素未出现")),
    (
        E2005_IMAGE_NOT_MATCHED,
        ("图像模板匹配失败", "未匹配到足够置信度目标", "未在屏幕上找到匹配的图像"),
    ),
    (E2009_CLICK_NO_EFFECT, ("tap-no-effect", "点击后页面未变化")),
    (E2001_ELEMENT_NOT_FOUND, ("元素未找到", "no such element")),
    (E2007_INPUT_FAILED, ("input 执行失败", "input_focused 执行失败")),
    (E2008_APP_CONTROL_FAILED, ("start_app 执行失败", "stop_app 执行失败")),
)

_CONNECTION_MESSAGE_TOKENS = (
    "connection refused",
    "connection reset",
    "connection aborted",
    "failed to establish",
    "device offline",
    "设备连接失败",
    "wda 连接失败",
    "rpc error",
)


def _module_root(cls: type) -> str:
    return (getattr(cls, "__module__", "") or "").split(".", 1)[0]


def _connection_code_for_platform(platform: str) -> str:
    return E2102_WDA_SESSION_ERROR if str(platform or "").strip().lower() == "ios" else (
        E2101_DEVICE_CONNECTION_LOST
    )


def _classify_by_exception_type(exc: BaseException, platform: str, action: str) -> str:
    connection_code = _connection_code_for_platform(platform)

    for cls in type(exc).__mro__:
        name = cls.__name__
        root = _module_root(cls)
        if root == "uiautomator2":
            if name in _U2_ELEMENT_NOT_FOUND_NAMES:
                return E2001_ELEMENT_NOT_FOUND
            if name == "AppNotFoundError":
                return E2008_APP_CONTROL_FAILED
            if name == "InputIMEError":
                return E2007_INPUT_FAILED
            if name in _U2_CONNECTION_NAMES:
                return E2101_DEVICE_CONNECTION_LOST
        elif root == "wda":
            if name in _WDA_ELEMENT_NOT_FOUND_NAMES:
                return E2001_ELEMENT_NOT_FOUND
            if name == "WDAKeyboardNotPresentError":
                return E2007_INPUT_FAILED
            if name in _WDA_SESSION_NAMES:
                return E2102_WDA_SESSION_ERROR
        elif root == "requests":
            if "Timeout" in name or name in {"ConnectionError", "ChunkedEncodingError", "RetryError"}:
                return connection_code

    if isinstance(exc, ConnectionError):
        return connection_code
    if isinstance(exc, TimeoutError):
        if str(action or "").strip().lower() == "wait_until_exists":
            return E2002_WAIT_TIMEOUT
        return connection_code
    if isinstance(exc, AssertionError):
        if str(action or "").strip().lower() == "assert_image":
            return E2004_ASSERT_IMAGE_FAILED
        return E2003_ASSERT_TEXT_FAILED
    if isinstance(exc, (ValueError, TypeError, FileNotFoundError)):
        return E2201_INVALID_ARGS
    return ""


def _classify_by_message(message: str, platform: str) -> str:
    text = str(message or "").strip()
    if not text:
        return ""
    lowered = text.lower()

    for code, tokens in _MESSAGE_RULES:
        for token in tokens:
            if token in text or token in lowered:
                return code

    for token in _CONNECTION_MESSAGE_TOKENS:
        if token in lowered:
            return _connection_code_for_platform(platform)

    if "断言失败" in text:
        if "图像" in text:
            return E2004_ASSERT_IMAGE_FAILED
        return E2003_ASSERT_TEXT_FAILED
    return ""


def classify_exception(
    exc: BaseException,
    *,
    platform: str = "",
    action: str = "",
    context: Optional[Dict[str, Any]] = None,  # noqa: ARG001 预留：按上下文细化归类
) -> Tuple[str, str]:
    """
    将执行期异常归类为 (错误码, 修复建议)。

    - `ExecutionStepError` 直接透传自身 code/suggestion。
    - 用户主动中止返回 ("", "")，表示不参与错误码归类。
    - 常见第三方异常（uiautomator2 / facebook-wda / requests / 内置连接与超时）
      按类型归类；否则按消息关键词兜底；仍未命中归为 E2999。
    """
    if isinstance(exc, ExecutionStepError):
        return exc.code, exc.suggestion or suggestion_for(exc.code)

    message = str(exc or "")
    if USER_ABORT_MESSAGE in message:
        return "", ""

    embedded = _EMBEDDED_CODE_PATTERN.search(message)
    if embedded:
        code = embedded.group(1)
        return code, suggestion_for(code)

    code = _classify_by_exception_type(exc, platform=platform, action=action)
    if not code:
        code = _classify_by_message(message, platform=platform)
    if not code:
        code = E2999_EXECUTION_ERROR
    return code, suggestion_for(code)
