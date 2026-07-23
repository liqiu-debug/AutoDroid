"""Small, auditable helpers for installed-version compatibility replay.

This module deliberately contains no package installation, baseline copying or
visual diff code.  It only reads the package currently installed on one
device, validates a frozen inspection plan, and provides the data needed by
the compatibility API's replay worker.
"""
from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
import re
import shlex
from datetime import datetime
from typing import Any, Awaitable, Callable, Dict, Iterable, List, Optional

from sqlmodel import Session, select

from backend.api.packages import _run_adb_command
from backend.models import AppPackage, InspectionBranchRun, InspectionRun, TestCase
from backend.utils.pydantic_compat import dump_model

JSONValue = Any
AdbRunner = Callable[[str, int], Awaitable[str]]


def canonical_json(value: JSONValue) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def digest_json(value: JSONValue) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def package_snapshot_digest(snapshot: Dict[str, Any]) -> str:
    """Digest only stable package identity, excluding capture timestamps."""
    fields = (
        "package_name",
        "version_name",
        "version_code",
        "first_install_time",
        "last_update_time",
        "signing_digest",
        "installed",
    )
    return digest_json({key: snapshot.get(key) for key in fields})


def _parse_line_value(output: str, key: str) -> Optional[str]:
    match = re.search(rf"(?:^|\s){re.escape(key)}=([^\s\r\n]+)", output)
    return match.group(1).strip() if match else None


def _parse_signing_digest(output: str) -> Optional[str]:
    # Android versions print certificate data in several formats. Prefer an
    # explicit SHA-256 value; otherwise hash the normalized signatures line so
    # the value remains a stable, non-secret identifier.
    explicit = re.search(
        r"(?i)(?:sha[- ]?256|certificate(?:Digest|Sha256))\s*[:=]\s*([0-9a-f:]{32,})",
        output,
    )
    if explicit:
        return explicit.group(1).replace(":", "").lower()
    signatures = re.search(r"(?im)^\s*(?:signatures|signingDetails)\s*[:=].*$", output)
    if not signatures:
        return None
    return hashlib.sha256(
        re.sub(r"\s+", " ", signatures.group(0)).strip().encode("utf-8")
    ).hexdigest()


async def read_installed_package(
    serial: str,
    package_name: str,
    *,
    adb_runner: Optional[AdbRunner] = None,
) -> Dict[str, Any]:
    """Read package metadata without changing device state.

    ``adb_runner`` is injectable so preflight and lease revalidation tests do
    not need a real device.  A missing package is represented as
    ``installed=False`` rather than raised, allowing the API to return a
    structured blocker.
    """
    runner = adb_runner or _run_adb_command
    quoted_serial = shlex.quote(str(serial))
    quoted_package = shlex.quote(str(package_name))
    try:
        path_output = await runner(
            f"adb -s {quoted_serial} shell pm path {quoted_package}",
            20,
        )
        path_output = str(path_output or "")
    except Exception as exc:
        return {
            "package_name": package_name,
            "installed": False,
            "known": False,
            "source": "device",
            "error": f"ADB_PACKAGE_QUERY_FAILED: {exc}",
            "captured_at": datetime.now().isoformat(),
        }
    if "package:" not in path_output:
        return {
            "package_name": package_name,
            "installed": False,
            "known": False,
            "source": "device",
            "captured_at": datetime.now().isoformat(),
        }
    try:
        output = str(
            await runner(
                f"adb -s {quoted_serial} shell dumpsys package {quoted_package}",
                20,
            )
            or ""
        )
    except Exception as exc:
        return {
            "package_name": package_name,
            "installed": True,
            "known": False,
            "source": "device",
            "error": f"ADB_PACKAGE_METADATA_FAILED: {exc}",
            "captured_at": datetime.now().isoformat(),
        }
    version_code = _parse_line_value(output, "versionCode")
    if version_code is None:
        version_code = _parse_line_value(output, "longVersionCode")
    snapshot = {
        "package_name": package_name,
        "version_name": _parse_line_value(output, "versionName"),
        "version_code": version_code,
        "first_install_time": _parse_line_value(output, "firstInstallTime"),
        "last_update_time": _parse_line_value(output, "lastUpdateTime"),
        "signing_digest": _parse_signing_digest(output),
        "installed": True,
        "known": bool(version_code or _parse_line_value(output, "versionName")),
        "source": "device",
        "captured_at": datetime.now().isoformat(),
    }
    snapshot["snapshot_digest"] = package_snapshot_digest(snapshot)
    return snapshot


def read_installed_package_sync(
    serial: str,
    package_name: str,
    *,
    adb_runner: Optional[AdbRunner] = None,
) -> Dict[str, Any]:
    """Synchronous adapter used by the existing sync FastAPI create route."""
    coroutine = read_installed_package(
        serial,
        package_name,
        adb_runner=adb_runner,
    )
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        result = asyncio.run(coroutine)
    else:
        # The route itself is sync and normally runs outside an event loop.
        # Keep this adapter safe for direct async test callers too.
        import threading

        box: List[Dict[str, Any]] = []
        error: List[BaseException] = []

        def run() -> None:
            try:
                box.append(asyncio.run(coroutine))
            except BaseException as exc:  # pragma: no cover - defensive
                error.append(exc)

        thread = threading.Thread(target=run, daemon=True)
        thread.start()
        thread.join()
        if error:
            raise error[0]
        result = box[0]
    result.setdefault("snapshot_digest", package_snapshot_digest(result))
    return result


def source_package_snapshot(
    session: Session,
    source_run: InspectionRun,
) -> Dict[str, Any]:
    package = session.get(AppPackage, source_run.package_id) if source_run.package_id else None
    frozen = (source_run.profile_snapshot or {}).get("package_snapshot")
    if isinstance(frozen, dict):
        snapshot = dict(frozen)
        snapshot.setdefault("package_name", source_run.package_name)
        snapshot.setdefault("source", "inspection_snapshot")
        snapshot.setdefault("known", bool(snapshot.get("version_code")))
        return snapshot
    if package is None:
        return {
            "package_name": source_run.package_name,
            "installed": False,
            "known": False,
            "source": "inspection_snapshot",
        }
    return {
        "package_name": package.package_name or source_run.package_name,
        "app_name": package.app_name,
        "version_name": package.version_name,
        "version_code": package.version_code,
        "installed": False,
        "known": bool(package.version_code or package.version_name),
        "source": "inspection_package",
    }


def _issue(code: str, message: str) -> Dict[str, str]:
    return {"code": code, "message": message}


def entry_case_safety_issues(
    session: Session,
    branch_config: Dict[str, Any],
    *,
    package_name: Optional[str] = None,
) -> List[Dict[str, str]]:
    case_id = branch_config.get("entry_case_id")
    case = session.get(TestCase, int(case_id or 0)) if case_id else None
    if case is None:
        return [_issue("ENTRY_CASE_MISSING", "业务线缺少可回放的 entry 用例")]
    blocked: List[Dict[str, str]] = []
    dangerous_words = re.compile(
        r"登录|登出|密码|支付|付款|下单|删除|卸载|安装|确认购买|授权|外部"
        r"|\b(?:login|logout|password|pay|payment|purchase|place[ _-]?order|"
        r"delete|uninstall|install|authorize|external|checkout|cashier)\b",
        re.IGNORECASE,
    )
    allowed_actions = {
        "click",
        "wait_until_exists",
        "assert_text",
        "assert_image",
        "swipe",
        "back",
        "home",
        "sleep",
        "start_app",
        "stop_app",
    }
    target_package = str(package_name or "").strip()
    for index, raw in enumerate(case.steps or []):
        step = dump_model(raw)
        if not isinstance(step, dict):
            blocked.append(
                _issue("UNSAFE_ENTRY_CASE", f"entry 用例第 {index + 1} 步格式无效")
            )
            continue
        raw_action = step.get("action")
        # Persisted legacy steps may contain the Pydantic ActionType enum
        # rather than its serialized string value. Comparing the enum's
        # repr would incorrectly reject every otherwise safe entry case.
        action_value = getattr(raw_action, "value", raw_action)
        action = str(action_value or "").strip().lower()
        # Include options because modern steps commonly keep the selector or
        # app id there instead of in the legacy top-level fields.
        options = step.get("options")
        options_text = (
            json.dumps(
                options,
                ensure_ascii=False,
                sort_keys=True,
                default=str,
            )
            if isinstance(options, (dict, list))
            else str(options or "")
        )
        haystack = " ".join(
            [
                str(step.get(key) or "")
                for key in ("selector", "value", "description")
            ]
            + [options_text]
        )
        unsafe_reason: Optional[str] = None
        if action not in allowed_actions:
            unsafe_reason = "不支持的动作"
        elif action in {"input", "click_image", "extract_by_ocr"}:
            unsafe_reason = "登录、输入或图像定位动作"
        elif dangerous_words.search(haystack):
            unsafe_reason = "包含风险语义"
        elif action == "sleep":
            raw_seconds = (
                options.get("seconds")
                if isinstance(options, dict) and options.get("seconds") is not None
                else step.get("value", 1)
            )
            try:
                seconds = float(raw_seconds)
            except (TypeError, ValueError):
                seconds = -1
            if not 0 <= seconds <= 60:
                unsafe_reason = "sleep 必须在 0 到 60 秒之间"
        if unsafe_reason is None:
            try:
                step_timeout = float(step.get("timeout") or 10)
            except (TypeError, ValueError):
                step_timeout = -1
            if not 0 < step_timeout <= 60:
                unsafe_reason = "单步 timeout 必须在 1 到 60 秒之间"
        if (
            unsafe_reason is None
            and action in {"start_app", "stop_app"}
            and target_package
        ):
            app_id = str(
                (options.get("app_key") if isinstance(options, dict) else None)
                or step.get("selector")
                or step.get("value")
                or ""
            ).strip()
            if app_id != target_package:
                unsafe_reason = "只能启动或停止巡检目标包"
        if unsafe_reason:
            blocked.append(
                _issue(
                    "UNSAFE_ENTRY_CASE",
                    f"entry 用例第 {index + 1} 步不安全: {unsafe_reason}",
                )
            )
    return blocked


def _plan_builder():
    from backend.inspection.replay import build_replay_plan

    return build_replay_plan


def build_replay_plan(
    session: Session,
    *,
    inspection_run_id: int,
    branch_key: str,
    max_chains: int = 20,
) -> Dict[str, Any]:
    plan = _plan_builder()(session, inspection_run_id, branch_key, max_chains=max_chains)
    if inspect.isawaitable(plan):
        plan = asyncio.run(plan)
    if not isinstance(plan, dict):
        raise ValueError("巡检回放计划格式无效")
    normalized = dict(plan)
    normalized.setdefault("plan_version", 1)
    normalized.setdefault("inspection_run_id", inspection_run_id)
    normalized.setdefault("branch_key", branch_key)
    normalized.setdefault("chains", [])
    normalized.setdefault("excluded", {})
    normalized.setdefault("summary", {})
    digest = str(normalized.get("digest") or normalized.get("plan_digest") or "")
    if not digest:
        digest_payload = {key: value for key, value in normalized.items() if key not in {"digest", "plan_digest"}}
        digest = digest_json(digest_payload)
    normalized["digest"] = digest
    normalized["plan_digest"] = digest
    return normalized


def branch_config_for_run(source_run: InspectionRun, branch_key: str) -> Dict[str, Any]:
    branches = (source_run.profile_snapshot or {}).get("branches") or {}
    value = branches.get(branch_key)
    return dict(value) if isinstance(value, dict) else {}


def enrich_chain_for_execution(
    chain: Dict[str, Any],
    *,
    source_run: InspectionRun,
    branch_key: str,
) -> Dict[str, Any]:
    """Add only frozen, non-secret replay context to a planner chain."""
    result = dict(chain)
    result.setdefault("chain_id", result.get("path_key") or str(result.get("endpoint_state_id")))
    result.setdefault("path_key", result.get("chain_id"))
    result["branch_key"] = branch_key
    result["branch_config"] = branch_config_for_run(source_run, branch_key)
    snapshot = source_run.profile_snapshot or {}
    result["input_rules"] = [dict(item) for item in snapshot.get("input_rules") or [] if isinstance(item, dict)]
    result["sanitizer_rules"] = [dict(item) for item in snapshot.get("sanitizer_rules") or [] if isinstance(item, dict)]
    result["dynamic_text_patterns"] = [str(item) for item in snapshot.get("dynamic_text_patterns") or []]
    result["stable_wait_seconds"] = float((snapshot.get("budgets") or {}).get("stable_wait_seconds") or 5.0)
    return result


def select_and_freeze_chains(
    plan: Dict[str, Any],
    selected_chain_ids: Iterable[str],
    *,
    source_run: InspectionRun,
    branch_key: str,
) -> List[Dict[str, Any]]:
    # Materialize once.  The API normally sends a list, but callers may pass a
    # generator; iterating it first for validation and again for freezing would
    # otherwise silently produce an empty plan.
    requested_ids = list(dict.fromkeys(str(item) for item in selected_chain_ids))
    selected = set(requested_ids)
    chains = [dict(item) for item in plan.get("chains") or [] if isinstance(item, dict)]
    by_id: Dict[str, Dict[str, Any]] = {}
    for item in chains:
        # Accept both the public chain id and the historical path key.  The
        # latter is still emitted by older report clients.
        for key in (
            item.get("chain_id"),
            item.get("path_key"),
            item.get("prefix_path_key"),
            item.get("id"),
        ):
            if key not in (None, ""):
                by_id.setdefault(str(key), item)
    missing = sorted(selected - set(by_id))
    if missing:
        raise ValueError(f"回放计划不存在所选链路: {missing}")
    frozen: List[Dict[str, Any]] = []
    frozen_ids: set[str] = set()
    for requested_id in requested_ids:
        item = by_id[requested_id]
        canonical_id = str(
            item.get("chain_id") or item.get("path_key") or item.get("id") or ""
        )
        if canonical_id in frozen_ids:
            continue
        frozen_ids.add(canonical_id)
        frozen.append(
            enrich_chain_for_execution(
                item,
                source_run=source_run,
                branch_key=branch_key,
            )
        )
    return frozen


def plan_digest_matches(plan: Dict[str, Any], expected: str) -> bool:
    return bool(expected) and str(plan.get("digest") or plan.get("plan_digest") or "") == str(expected)


def source_branch_exists(session: Session, inspection_run_id: int, branch_key: str) -> bool:
    return session.exec(
        select(InspectionBranchRun).where(
            InspectionBranchRun.run_id == inspection_run_id,
            InspectionBranchRun.branch_key == branch_key,
        )
    ).first() is not None
