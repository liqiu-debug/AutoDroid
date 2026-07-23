"""Short-lived device operations used by the inspection engine."""
from __future__ import annotations

import io
import logging
import threading
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from typing import Any, Dict, Optional, Tuple

from PIL import Image, ImageStat

from backend.inspection.semantics import (
    InspectionAction,
    PageModel,
    build_page_model,
    coordinate_target_key,
    locator_match_count,
    locator_unique_bounds,
    perceptual_hash,
    phash_distance,
    screenshot_sha,
)

logger = logging.getLogger(__name__)


class DeviceDisconnected(RuntimeError):
    pass


class LocatorAmbiguous(RuntimeError):
    pass


class LocatorDrift(RuntimeError):
    """A previously unique locator disappeared before device execution."""


class InspectionAborted(RuntimeError):
    pass


@dataclass
class CapturedPage:
    package_name: str
    activity: str
    xml: str
    screenshot_png: bytes
    screenshot_sha: str
    perceptual_hash: str
    model: PageModel
    stable_by: str


def connect_android(serial: str):
    try:
        import uiautomator2 as u2

        return u2.connect(serial)
    except Exception as exc:
        raise DeviceDisconnected(f"Android 设备连接失败: {serial}: {exc}") from exc


def _check_abort(abort_event: threading.Event) -> None:
    if abort_event.is_set():
        raise InspectionAborted("inspection cancelled")


def _current_app(device) -> Tuple[str, str]:
    try:
        current = device.app_current() or {}
        return (
            str(current.get("package") or ""),
            str(current.get("activity") or ""),
        )
    except Exception as exc:
        raise DeviceDisconnected(f"无法读取前台应用: {exc}") from exc


def _screenshot(device) -> bytes:
    try:
        image = device.screenshot(format="pillow")
        if image is None:
            raise RuntimeError("empty screenshot")
        output = io.BytesIO()
        image.save(output, format="PNG")
        raw = output.getvalue()
        if not raw:
            raise RuntimeError("empty screenshot bytes")
        return raw
    except Exception as exc:
        raise DeviceDisconnected(f"设备截图失败: {exc}") from exc


def capture_quick(device) -> Tuple[str, str, bytes, str, str]:
    package_name, activity = _current_app(device)
    png = _screenshot(device)
    return package_name, activity, png, screenshot_sha(png), perceptual_hash(png)


def exact_parent_matches(
    device,
    *,
    package_name: str,
    activity: str,
    screenshot_sha_value: str,
) -> bool:
    current_package, current_activity, _, current_sha, _ = capture_quick(device)
    return (
        current_package == package_name
        and current_activity == activity
        and current_sha == screenshot_sha_value
    )


def wait_for_stable_page(
    device,
    *,
    expected_package: str,
    abort_event: threading.Event,
    max_wait_seconds: float = 5.0,
    sample_interval_seconds: float = 0.5,
    dynamic_patterns=None,
) -> CapturedPage:
    """Use screenshots for polling and dump hierarchy exactly once at the end."""
    # A zero timeout is intentionally supported for locator re-binding: the
    # caller needs one immediate screenshot/XML sample, not another stability
    # polling window. Normal exploration waits still pass a positive timeout.
    deadline = time.monotonic() + max(0.0, float(max_wait_seconds))
    previous: Optional[Tuple[str, str, bytes, str, str]] = None
    small_animation_samples = 0
    stable_by = "timeout"
    latest: Optional[Tuple[str, str, bytes, str, str]] = None

    while True:
        _check_abort(abort_event)
        latest = capture_quick(device)
        if previous is not None:
            same_context = latest[0] == previous[0] and latest[1] == previous[1]
            if same_context and latest[3] == previous[3]:
                stable_by = "exact"
                break
            if same_context and phash_distance(latest[4], previous[4]) <= 4:
                small_animation_samples += 1
                if small_animation_samples >= 2:
                    stable_by = "perceptual"
                    break
            else:
                small_animation_samples = 0
        if time.monotonic() >= deadline:
            break
        previous = latest
        if abort_event.wait(max(0.5, sample_interval_seconds)):
            raise InspectionAborted("inspection cancelled")

    if latest is None:
        raise DeviceDisconnected("未能采集页面")
    try:
        xml = str(device.dump_hierarchy(compressed=False) or "")
    except TypeError:
        xml = str(device.dump_hierarchy() or "")
    except Exception as exc:
        raise DeviceDisconnected(f"XML hierarchy 获取失败: {exc}") from exc
    if not xml.strip():
        raise DeviceDisconnected("XML hierarchy 为空")
    model = build_page_model(
        xml,
        package_name=latest[0],
        activity=latest[1],
        screenshot_phash=latest[4],
        dynamic_patterns=dynamic_patterns,
    )
    return CapturedPage(
        package_name=latest[0],
        activity=latest[1],
        xml=xml,
        screenshot_png=latest[2],
        screenshot_sha=latest[3],
        perceptual_hash=latest[4],
        model=model,
        stable_by=stable_by,
    )


def ready_assertion_exists(
    device,
    assertion: Dict[str, Any],
    *,
    abort_event: threading.Event,
) -> bool:
    selector = str(assertion.get("selector") or "").strip()
    by = str(assertion.get("by") or "").strip().lower()
    timeout = max(1.0, float(assertion.get("timeout") or 5))
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        _check_abort(abort_event)
        try:
            if by == "description":
                if bool(device(description=selector).exists):
                    return True
            elif by == "text":
                if bool(device(text=selector).exists):
                    return True
            elif by == "xpath":
                target = device.xpath(selector)
                exists = target.exists
                if callable(exists):
                    exists = exists()
                if bool(exists):
                    return True
            else:
                return False
        except Exception as exc:
            logger.debug("ready assertion sample failed: %s", exc)
        if abort_event.wait(0.25):
            raise InspectionAborted("inspection cancelled")
    return False


_LOCATOR_EXECUTION_TIMEOUT_SECONDS = 0.5


def _is_locator_not_found_error(exc: BaseException) -> bool:
    """Keep optional uiautomator2 exception imports out of module startup."""
    try:
        from uiautomator2.exceptions import (
            UiObjectNotFoundError,
            XPathElementNotFoundError,
        )

        return isinstance(
            exc,
            (UiObjectNotFoundError, XPathElementNotFoundError),
        )
    except Exception:
        return exc.__class__.__name__ in {
            "UiObjectNotFoundError",
            "XPathElementNotFoundError",
        }


def _click_candidate(device, candidate: Dict[str, Any]) -> None:
    selector = str(candidate.get("selector") or "")
    by = str(candidate.get("by") or "").lower()
    try:
        if by == "description":
            device(description=selector).click(
                timeout=_LOCATOR_EXECUTION_TIMEOUT_SECONDS
            )
        elif by == "text":
            device(text=selector).click(
                timeout=_LOCATOR_EXECUTION_TIMEOUT_SECONDS
            )
        elif by == "xpath":
            device.xpath(selector).click(
                timeout=_LOCATOR_EXECUTION_TIMEOUT_SECONDS
            )
        else:
            raise LocatorAmbiguous(f"unsupported inspection locator: {by}")
    except LocatorAmbiguous:
        raise
    except Exception as exc:
        if _is_locator_not_found_error(exc):
            raise LocatorDrift(
                f"定位器执行前已漂移: by={by}, selector={selector!r}"
            ) from exc
        raise


def _unique_candidate(
    xml: str,
    candidates,
) -> Optional[Dict[str, Any]]:
    for candidate in candidates or ():
        if locator_match_count(xml, candidate) == 1:
            return dict(candidate)
    return None


def _fresh_semantic_bounds(
    xml: str,
    candidate: Dict[str, Any],
) -> Optional[Tuple[int, int, int, int]]:
    """Resolve a bounds-constrained semantic locator from the fresh XML."""
    raw_bounds = candidate.get("bounds")
    if not isinstance(raw_bounds, (list, tuple)) or len(raw_bounds) != 4:
        return None
    try:
        expected_bounds = tuple(int(value) for value in raw_bounds)
    except (TypeError, ValueError):
        return None
    expected_class = str(candidate.get("expected_class") or "")
    expected_desc = str(candidate.get("target_description") or "")
    expected_text = str(candidate.get("target_text") or "")
    matches: list[Tuple[int, int, int, int]] = []
    try:
        root = ET.fromstring(xml)
    except (ET.ParseError, TypeError, ValueError):
        return None
    for node in root.iter():
        attrs = node.attrib
        if expected_class and attrs.get("class") != expected_class:
            continue
        if expected_desc and attrs.get("content-desc") != expected_desc:
            continue
        if not expected_desc and expected_text and attrs.get("text") != expected_text:
            continue
        if attrs.get("bounds") != (
            f"[{expected_bounds[0]},{expected_bounds[1]}]"
            f"[{expected_bounds[2]},{expected_bounds[3]}]"
        ):
            continue
        if attrs.get("visible-to-user", "true").lower() == "false":
            continue
        if attrs.get("enabled", "true").lower() == "false":
            continue
        if attrs.get("clickable", "false").lower() != "true":
            continue
        matches.append(expected_bounds)
    return matches[0] if len(matches) == 1 else None


def perform_action(
    device,
    action: InspectionAction,
    *,
    current_xml: str,
    input_value: Optional[str] = None,
    allow_coordinate_discovery: bool = False,
) -> str:
    """Perform one authorized action.

    Coordinate-only clicks are accepted only when the exploration engine
    explicitly grants discovery access. Replays keep the default-deny path so
    an unrepeatable coordinate can never become a stable regression locator.
    """
    if action.risk_type:
        raise PermissionError(action.blocked_reason or action.risk_type)

    if action.action_type == "back":
        device.press("back")
        return "back"
    if action.action_type == "scroll":
        candidate = _unique_candidate(current_xml, action.locator_candidates)
        if action.locator_candidates and candidate is None:
            raise LocatorAmbiguous("滚动容器定位候选均非唯一或已漂移")
        width, height = device.window_size()
        bounds = None
        bounds_from_locator = False
        if (
            candidate is not None
            and str(candidate.get("expected_class") or "")
            == str(action.target_meta.get("class") or "")
        ):
            bounds = locator_unique_bounds(current_xml, candidate)
            bounds_from_locator = bounds is not None
        if bounds is None:
            raw_bounds = action.target_meta.get("bounds")
            source_size = action.target_meta.get("screen_size")
            if isinstance(raw_bounds, (list, tuple)) and len(raw_bounds) == 4:
                try:
                    source_width = max(1, int(source_size[0]))
                    source_height = max(1, int(source_size[1]))
                    scale_x = width / source_width
                    scale_y = height / source_height
                    bounds = (
                        int(int(raw_bounds[0]) * scale_x),
                        int(int(raw_bounds[1]) * scale_y),
                        int(int(raw_bounds[2]) * scale_x),
                        int(int(raw_bounds[3]) * scale_y),
                    )
                except (TypeError, ValueError, IndexError):
                    bounds = None
        x1, y1, x2, y2 = bounds or (0, 0, width, height)
        center_x = max(0, min(width - 1, (x1 + x2) // 2))
        center_y = max(0, min(height - 1, (y1 + y2) // 2))
        left_x = max(
            0,
            min(width - 1, x1 + max(1, (x2 - x1) // 4)),
        )
        right_x = max(
            0,
            min(width - 1, x1 + max(1, (x2 - x1) * 3 // 4)),
        )
        upper_y = max(
            0,
            min(height - 1, y1 + max(1, (y2 - y1) // 4)),
        )
        lower_y = max(
            0,
            min(height - 1, y1 + max(1, (y2 - y1) * 3 // 4)),
        )
        direction = str(action.target_meta.get("direction") or "up").lower()
        if direction == "left":
            device.swipe(right_x, center_y, left_x, center_y, 0.25)
        elif direction == "right":
            device.swipe(left_x, center_y, right_x, center_y, 0.25)
        elif direction == "down":
            device.swipe(center_x, upper_y, center_x, lower_y, 0.25)
        else:
            device.swipe(center_x, lower_y, center_x, upper_y, 0.25)
        locator_kind = (
            str(candidate.get("by") or "")
            if candidate is not None and bounds_from_locator
            else "coordinate"
        )
        return f"scroll:{direction}:{locator_kind}"
    if action.coordinate_only:
        if not allow_coordinate_discovery or action.action_type != "click":
            raise LocatorAmbiguous("coordinate-only action cannot be replayed")
        if coordinate_target_key(action) is None:
            raise LocatorAmbiguous("coordinate-only action has invalid bounds")
        raw_bounds = action.target_meta.get("bounds")
        source_size = action.target_meta.get("screen_size")
        try:
            width, height = device.window_size()
            width, height = int(width), int(height)
            source_width = int(source_size[0])
            source_height = int(source_size[1])
            if width <= 0 or height <= 0:
                raise ValueError("device window size is invalid")
            x1, y1, x2, y2 = (int(value) for value in raw_bounds)
            center_x = int(((x1 + x2) / 2) * width / source_width)
            center_y = int(((y1 + y2) / 2) * height / source_height)
            if not (0 <= center_x < width and 0 <= center_y < height):
                raise ValueError("scaled coordinate is outside the device window")
        except (TypeError, ValueError, IndexError, ZeroDivisionError) as exc:
            raise LocatorAmbiguous(
                "coordinate-only action has invalid bounds"
            ) from exc
        device.click(center_x, center_y)
        return "coordinate"

    candidate = _unique_candidate(current_xml, action.locator_candidates)
    if candidate is None:
        raise LocatorAmbiguous("所有定位候选均非唯一或已漂移")
    if candidate.get("bounds_constrained"):
        bounds = _fresh_semantic_bounds(current_xml, candidate)
        if bounds is None:
            raise LocatorDrift("语义控件的当前 bounds 已漂移")
        try:
            width, height = device.window_size()
            source_size = action.target_meta.get("screen_size") or (width, height)
            source_width = max(1, int(source_size[0]))
            source_height = max(1, int(source_size[1]))
            x1, y1, x2, y2 = bounds
            center_x = int(((x1 + x2) / 2) * width / source_width)
            center_y = int(((y1 + y2) / 2) * height / source_height)
        except (TypeError, ValueError, IndexError, ZeroDivisionError):
            # Test doubles and older drivers may not expose window_size. Keep
            # the ordinary locator path as a compatibility fallback; a real
            # uiautomator miss is still reported as LocatorDrift below.
            _click_candidate(device, candidate)
            return str(candidate.get("by") or "xpath")
        device.click(center_x, center_y)
        return "semantic-bounds"
    if (
        action.action_type == "click"
        and candidate.get("nearest_clickable_ancestor")
    ):
        bounds = locator_unique_bounds(current_xml, candidate)
        if bounds is None:
            raise LocatorDrift("商品卡最近可点击祖先已漂移")
        x1, y1, x2, y2 = bounds
        if x2 <= x1 or y2 <= y1:
            raise LocatorDrift("商品卡最近可点击祖先范围无效")
        # The point comes from a unique semantic XPath match in the current
        # hierarchy, not from a persisted coordinate captured on an old page.
        device.click((x1 + x2) // 2, (y1 + y2) // 2)
        return str(candidate.get("by") or "xpath")
    if action.action_type == "input":
        if input_value is None:
            raise PermissionError("input value is not authorized")
        _click_candidate(device, candidate)
        try:
            device.clear_text()
        except Exception:
            pass
        device.send_keys(input_value, clear=True)
    else:
        _click_candidate(device, candidate)
    return str(candidate.get("by") or "")


def is_white_screen(png_bytes: bytes) -> bool:
    if not png_bytes:
        return False
    with Image.open(io.BytesIO(png_bytes)) as source:
        image = source.convert("L").resize((64, 64), Image.Resampling.BILINEAR)
        stats = ImageStat.Stat(image)
    return bool(stats.mean and stats.stddev and stats.mean[0] >= 248 and stats.stddev[0] <= 3)
