"""
IOSDriver App 控制与页面签名 Mixin

从 backend.drivers.ios_driver 拆出：应用启动/停止/状态读取、back/home
多策略返回（侧滑手势、press back、常见返回按钮），以及页面签名抓取与
页面变化等待。
"""
import hashlib
import logging
import time
from typing import Optional

from backend.execution_errors import (
    E2008_APP_CONTROL_FAILED,
    ExecutionStepRuntimeError,
)

# 保持与拆分前一致的 logger 名称（backend.drivers.ios_driver），日志过滤与测试 assertLogs 兼容。
logger = logging.getLogger("backend.drivers.ios_driver")


class IOSAppControlMixin:
    """应用控制、返回策略与页面签名（IOSDriver 组成部分）。"""

    def _capture_page_signature(
        self,
        mode: str = "full",
        screenshot_timeout: Optional[float] = None,
    ) -> str:
        """
        抓取当前页面签名，用于判断 back 后是否发生页面跳转。

        优先级：
        1) 当前前台 app 信息（bundle/activity）
        2) page source 的哈希
        3) screenshot 的哈希（兜底）
        """
        signature_parts = []
        session = self.client.session()
        quick_mode = str(mode or "").strip().lower() in {"quick", "fast", "light"}

        try:
            app_current = getattr(session, "app_current", None)
            if callable(app_current):
                app_current = app_current()
            if isinstance(app_current, dict):
                bundle = str(app_current.get("bundleId") or app_current.get("bundle_id") or "").strip()
                activity = str(app_current.get("activity") or app_current.get("name") or "").strip()
                if bundle:
                    signature_parts.append(f"bundle={bundle}")
                if activity:
                    signature_parts.append(f"activity={activity}")
        except Exception:
            pass

        if not quick_mode:
            source_candidates = [
                lambda: session.source(),
                lambda: getattr(session, "source", None),
                lambda: self.client.source(),
                lambda: getattr(self.client, "source", None),
            ]
            for getter in source_candidates:
                try:
                    raw_value = getter()
                    if callable(raw_value):
                        raw_value = raw_value()
                    source_text = str(raw_value or "").strip()
                    if source_text:
                        digest = hashlib.sha1(
                            source_text.encode("utf-8", errors="ignore")
                        ).hexdigest()[:16]
                        signature_parts.append(f"source={digest}")
                        break
                except Exception:
                    continue

        should_use_screenshot = quick_mode or not signature_parts
        if should_use_screenshot:
            try:
                effective_timeout = max(
                    0.2,
                    float(
                        screenshot_timeout
                        if screenshot_timeout is not None
                        else (1.2 if quick_mode else self._SCREENSHOT_TIMEOUT_SECONDS)
                    ),
                )
                screenshot = self._capture_screenshot_bytes(timeout=effective_timeout)
                if screenshot:
                    digest = hashlib.sha1(screenshot).hexdigest()[:16]
                    signature_parts.append(f"screen={digest}")
            except Exception:
                pass

        return "|".join(signature_parts)

    def _wait_page_changed(
        self,
        before_signature: str,
        timeout: float = 1.2,
        interval: float = 0.25,
        mode: str = "full",
        screenshot_timeout: Optional[float] = None,
    ) -> bool:
        deadline = time.time() + max(timeout, 0.2)
        while time.time() < deadline:
            time.sleep(max(interval, 0.05))
            current_signature = self._capture_page_signature(
                mode=mode,
                screenshot_timeout=screenshot_timeout,
            )
            if current_signature and current_signature != before_signature:
                return True

        current_signature = self._capture_page_signature(
            mode=mode,
            screenshot_timeout=screenshot_timeout,
        )
        return bool(current_signature and current_signature != before_signature)

    def _try_edge_back_swipe(self, y_ratio: float = 0.5) -> bool:
        """
        优先尝试 iOS 常见返回手势：左缘向右滑。
        """
        session = self.client.session()
        size = None
        for getter in (
            lambda: session.window_size(),
            lambda: self.client.window_size(),
        ):
            try:
                payload = getter()
                if payload:
                    size = payload
                    break
            except Exception:
                continue

        width = 390
        height = 844
        if isinstance(size, dict):
            width = int(size.get("width") or size.get("w") or width)
            height = int(size.get("height") or size.get("h") or height)
        elif isinstance(size, (tuple, list)) and len(size) >= 2:
            width = int(size[0] or width)
            height = int(size[1] or height)
        else:
            width = int(getattr(size, "width", width) or width)
            height = int(getattr(size, "height", height) or height)

        # WDA 只要收到 float 就会按百分比处理，必须传 int 绝对坐标。
        # 贴近左边缘，提升 iOS 侧滑返回命中率（最小 1px）。
        start_x = max(1, int(width * 0.005))
        safe_ratio = min(max(float(y_ratio), 0.15), 0.85)
        start_y = max(2, int(height * safe_ratio))
        # 尽量滑到屏幕右侧边缘，提升侧滑返回触发率。
        end_x = max(start_x + 2, int(width * 0.95))
        end_y = start_y

        swipe_candidates = [
            # 优先快速侧滑，再逐步降级到较慢参数。
            lambda: session.swipe(start_x, start_y, end_x, end_y, duration=0.06),
            lambda: session.swipe(start_x, start_y, end_x, end_y, duration=0.12),
            lambda: session.swipe(start_x, start_y, end_x, end_y),
            lambda: session.drag(start_x, start_y, end_x, end_y, duration=0.12),
            lambda: session.drag(start_x, start_y, end_x, end_y),
        ]
        for swipe_call in swipe_candidates:
            try:
                swipe_call()
                logger.info(
                    "iOS.back edge-swipe: (%.1f, %.1f) -> (%.1f, %.1f)",
                    start_x,
                    start_y,
                    end_x,
                    end_y,
                )
                return True
            except Exception:
                continue
        return False

    def _try_press_back(self) -> bool:
        try:
            self.client.press("back")
            return True
        except Exception:
            return False

    def _tap_common_back_buttons(self) -> bool:
        """
        点击常见返回按钮文案/标识。
        """
        candidates = [
            ("name", "Back"),
            ("label", "Back"),
            ("name", "back"),
            ("label", "back"),
            ("name", "返回"),
            ("label", "返回"),
            ("name", "返回上一页"),
            ("label", "返回上一页"),
            ("id", "back"),
            ("id", "nav_back"),
            ("id", "btn_back"),
            ("id", "ic_back"),
            (
                "predicate",
                "type == 'XCUIElementTypeButton' AND (name CONTAINS 'Back' OR label CONTAINS 'Back' OR name CONTAINS '返回' OR label CONTAINS '返回')",
            ),
        ]
        for by, selector in candidates:
            try:
                self.click(selector=selector, by=by)
                return True
            except Exception:
                continue
        return False

    def back(self) -> None:
        """
        iOS 无统一系统 back 键。

        执行顺序：
        1) 左缘右滑手势
        2) press("back")
        3) 点击常见返回按钮

        每一步都会检测页面签名变化，若动作执行后页面未变化则继续尝试下一种。
        """
        started_at = time.time()
        logger.info("iOS.back start")
        before_signature = self._capture_page_signature()
        details = []

        edge_swipe_y_ratios = (0.20, 0.50, 0.80)
        for y_ratio in edge_swipe_y_ratios:
            strategy_name = f"edge-swipe@y={y_ratio:.2f}"
            try:
                acted = bool(self._try_edge_back_swipe(y_ratio=y_ratio))
            except Exception as exc:
                details.append(f"{strategy_name}:error={exc}")
                continue

            if not acted:
                details.append(f"{strategy_name}:not-available")
                continue

            if self._wait_page_changed(before_signature, timeout=0.9):
                self._log_action_success(
                    "back",
                    started_at,
                    strategy=strategy_name,
                    attempts=len(details) + 1,
                )
                return
            details.append(f"{strategy_name}:page-unchanged")

        strategies = [
            ("press-back", self._try_press_back),
            ("tap-back-button", self._tap_common_back_buttons),
        ]
        for strategy_name, action in strategies:
            try:
                acted = bool(action())
            except Exception as exc:
                details.append(f"{strategy_name}:error={exc}")
                continue
            if not acted:
                details.append(f"{strategy_name}:not-available")
                continue

            if self._wait_page_changed(before_signature):
                self._log_action_success(
                    "back",
                    started_at,
                    strategy=strategy_name,
                    attempts=len(details) + 1,
                )
                return
            details.append(f"{strategy_name}:page-unchanged")

        detail_text = "; ".join(details) if details else "no-strategy"
        final_exc = RuntimeError(
            f"iOS back 执行失败：页面未变化或未找到可用返回入口 ({detail_text})"
        )
        self._log_action_failure(
            "back",
            started_at,
            final_exc,
            attempts=len(details),
            detail=detail_text,
        )
        raise final_exc

    def home(self) -> None:
        started_at = time.time()
        logger.info("iOS.home start")
        try:
            self.client.home()
            self._log_action_success("home", started_at)
        except Exception as exc:
            self._log_action_failure("home", started_at, exc)
            raise

    def _read_app_state(self, app_id: str) -> int:
        """
        读取应用状态：
        - 1: not running
        - 2: running in background
        - 3/4: running in foreground (WDA 不同版本返回值有差异)
        """
        raw_state = self.client.app_state(app_id)
        if hasattr(raw_state, "value"):
            raw_state = getattr(raw_state, "value")
        if isinstance(raw_state, dict):
            raw_state = raw_state.get("value")
        return int(raw_state)

    def start_app(self, app_id: str) -> None:
        if not app_id:
            raise ValueError("start_app 需要 app_id（iOS bundleId）")
        started_at = time.time()
        logger.info("iOS.start_app start: app_id=%s", app_id)

        errors = []
        launch_methods = [
            ("app_activate", lambda: self.client.app_activate(app_id)),
            ("app_launch", lambda: self.client.app_launch(app_id, wait_for_quiescence=False)),
        ]
        for method_name, method in launch_methods:
            try:
                method()
                time.sleep(0.35)
                try:
                    state = self._read_app_state(app_id)
                    if state in (2, 3, 4):
                        self._log_action_success(
                            "start_app",
                            started_at,
                            app_id=app_id,
                            strategy=method_name,
                            app_state=state,
                            attempts=len(errors) + 1,
                        )
                        return
                    errors.append(f"{method_name}: unexpected app_state={state}")
                except Exception:
                    # 某些 WDA 版本 app_state 不稳定，启动请求成功即可视为成功。
                    self._log_action_success(
                        "start_app",
                        started_at,
                        app_id=app_id,
                        strategy=method_name,
                        app_state="unknown",
                        attempts=len(errors) + 1,
                    )
                    return
            except Exception as exc:
                errors.append(f"{method_name}: {exc}")

        detail = "; ".join(errors) if errors else "unknown"
        final_exc = ExecutionStepRuntimeError(
            E2008_APP_CONTROL_FAILED,
            f"iOS.start_app 执行失败: {detail}",
            context={"app_id": app_id},
        )
        self._log_action_failure(
            "start_app",
            started_at,
            final_exc,
            app_id=app_id,
            attempts=len(errors),
            detail=detail,
        )
        raise final_exc

    def stop_app(self, app_id: str) -> None:
        if not app_id:
            raise ValueError("stop_app 需要 app_id（iOS bundleId）")
        started_at = time.time()
        logger.info("iOS.stop_app start: app_id=%s", app_id)

        errors = []
        terminate_methods = [
            ("app_terminate", lambda: self.client.app_terminate(app_id)),
            ("session.app_terminate", lambda: self.client.session().app_terminate(app_id)),
        ]
        for method_name, method in terminate_methods:
            try:
                method()
                time.sleep(0.2)
            except Exception as exc:
                errors.append(f"{method_name}: {exc}")

            try:
                state = self._read_app_state(app_id)
                if state == 1:
                    self._log_action_success(
                        "stop_app",
                        started_at,
                        app_id=app_id,
                        strategy=method_name,
                        app_state=state,
                        attempts=len(errors) + 1,
                    )
                    return
            except Exception:
                # 无法读取状态时，调用成功视为 best effort。
                if not errors or not errors[-1].startswith(method_name):
                    self._log_action_success(
                        "stop_app",
                        started_at,
                        app_id=app_id,
                        strategy=method_name,
                        app_state="unknown",
                        attempts=len(errors) + 1,
                    )
                    return

        # 兜底再读一次状态，若已退出则视为成功。
        try:
            if self._read_app_state(app_id) == 1:
                self._log_action_success(
                    "stop_app",
                    started_at,
                    app_id=app_id,
                    strategy="final-app-state-check",
                    app_state=1,
                    attempts=len(errors),
                )
                return
        except Exception:
            pass

        detail = "; ".join(errors) if errors else "unknown"
        final_exc = ExecutionStepRuntimeError(
            E2008_APP_CONTROL_FAILED,
            f"iOS.stop_app 执行失败: {detail}",
            context={"app_id": app_id},
        )
        self._log_action_failure(
            "stop_app",
            started_at,
            final_exc,
            app_id=app_id,
            attempts=len(errors),
            detail=detail,
        )
        raise final_exc
