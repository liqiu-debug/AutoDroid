import logging
import re
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query
from sqlmodel import Session, select

from backend.api import deps
from backend.cross_platform_execution import (
    precheck_case_execution,
    restore_device_status_after_execution,
    run_case_with_standard_runner,
)
from backend.database import get_session
from backend.feature_flags import (
    FLAG_CROSS_PLATFORM_RUNNER,
    FLAG_NEW_STEP_MODEL,
    is_flag_enabled,
)
from backend.models import CaseFolder, Device, TestCase, TestCaseStep, User
from backend.runner import register_device_abort, unregister_device_abort
from backend.schemas import (
    PaginatedTestCaseRead,
    Step,
    TestCaseCreate,
    TestCaseRead,
    TestCaseStepRead,
    TestCaseStepWrite,
)
from backend.step_contract import (
    build_legacy_from_standard_steps,
    build_standard_from_legacy_steps,
    normalize_action,
    normalize_error_strategy,
    normalize_execute_on,
    normalize_platform_overrides,
)
from backend.utils.pydantic_compat import dump_model

router = APIRouter()
logger = logging.getLogger(__name__)
PROJECT_ROOT = Path(__file__).resolve().parents[2]
AUTO_TEMPLATE_IMAGE_DIR = PROJECT_ROOT / "static" / "images"
AUTO_TEMPLATE_IMAGE_RE = re.compile(r"^static/images/element_[0-9a-f]+\.png$", re.IGNORECASE)
IMAGE_TEMPLATE_ACTIONS = {"click_image", "assert_image"}


class _CaseSnapshot:
    """Detached-safe testcase payload used by background runners."""

    __slots__ = ("id", "name", "steps", "variables")

    def __init__(
        self,
        *,
        case_id: int,
        name: str,
        steps: List[Any],
        variables: List[Any],
    ) -> None:
        self.id = case_id
        self.name = name
        self.steps = steps
        self.variables = variables


def _normalize_auto_template_image_path(raw: Any) -> Optional[str]:
    if raw in (None, ""):
        return None

    text = str(raw).strip()
    if not text:
        return None

    text = text.replace("\\", "/")
    if text.startswith("./"):
        text = text[2:]

    if Path(text).is_absolute():
        try:
            text = Path(text).resolve().relative_to(PROJECT_ROOT.resolve()).as_posix()
        except Exception:
            return None

    if not AUTO_TEMPLATE_IMAGE_RE.fullmatch(text):
        return None
    return text


def _extract_template_paths_from_platform_overrides(raw: Any) -> List[str]:
    refs: List[str] = []
    if not isinstance(raw, dict):
        return refs

    for override in raw.values():
        if not isinstance(override, dict):
            continue
        by = str(override.get("by") or "").strip().lower()
        if by != "image":
            continue
        normalized = _normalize_auto_template_image_path(override.get("selector"))
        if normalized:
            refs.append(normalized)
    return refs


def _collect_template_paths_from_legacy_steps(steps: List[Any]) -> List[str]:
    refs: List[str] = []
    for step in steps or []:
        raw = dump_model(step)
        if not isinstance(raw, dict):
            continue

        action = normalize_action(raw.get("action"))
        selector_type = str(raw.get("selector_type") or "").strip().lower()
        options = raw.get("options") if isinstance(raw.get("options"), dict) else {}
        candidates: List[Any] = [
            options.get("image_path"),
            options.get("path"),
        ]
        if action in IMAGE_TEMPLATE_ACTIONS or selector_type == "image":
            candidates.extend([raw.get("selector"), raw.get("value")])

        for candidate in candidates:
            normalized = _normalize_auto_template_image_path(candidate)
            if normalized:
                refs.append(normalized)

        refs.extend(_extract_template_paths_from_platform_overrides(raw.get("platform_overrides")))

    return refs


def _collect_template_paths_from_standard_steps(steps: List[Any]) -> List[str]:
    refs: List[str] = []
    for step in steps or []:
        raw = dump_model(step)
        if not isinstance(raw, dict):
            continue

        action = normalize_action(raw.get("action"))
        args = raw.get("args") if isinstance(raw.get("args"), dict) else {}
        candidates: List[Any] = [args.get("image_path"), args.get("path")]
        if action in IMAGE_TEMPLATE_ACTIONS:
            candidates.extend([raw.get("selector"), raw.get("value")])

        for candidate in candidates:
            normalized = _normalize_auto_template_image_path(candidate)
            if normalized:
                refs.append(normalized)

        refs.extend(_extract_template_paths_from_platform_overrides(raw.get("platform_overrides")))

    return refs


def _collect_case_template_image_paths(
    case: Optional[TestCase],
    standard_steps: Optional[List[TestCaseStep]] = None,
) -> List[str]:
    refs: List[str] = []
    if case:
        refs.extend(_collect_template_paths_from_legacy_steps(case.steps or []))
    if standard_steps:
        refs.extend(_collect_template_paths_from_standard_steps(standard_steps))
    return refs


def _list_all_referenced_template_images(session: Session) -> set:
    refs = set()
    all_cases = session.exec(select(TestCase)).all()
    for case in all_cases:
        refs.update(_collect_template_paths_from_legacy_steps(case.steps or []))

    all_standard_steps = session.exec(select(TestCaseStep)).all()
    refs.update(_collect_template_paths_from_standard_steps(all_standard_steps))
    return refs


def _list_generated_template_images_on_disk() -> set:
    if not AUTO_TEMPLATE_IMAGE_DIR.exists() or not AUTO_TEMPLATE_IMAGE_DIR.is_dir():
        return set()

    refs = set()
    for file_path in AUTO_TEMPLATE_IMAGE_DIR.glob("element_*.png"):
        if not file_path.is_file():
            continue
        try:
            relative_path = file_path.resolve().relative_to(PROJECT_ROOT.resolve()).as_posix()
        except Exception:
            continue
        normalized = _normalize_auto_template_image_path(relative_path)
        if normalized:
            refs.add(normalized)
    return refs


def _cleanup_unused_template_images(session: Session, candidate_paths: List[Any]) -> List[str]:
    normalized_candidates = {
        normalized
        for normalized in (_normalize_auto_template_image_path(item) for item in candidate_paths or [])
        if normalized
    }
    normalized_candidates.update(_list_generated_template_images_on_disk())
    if not normalized_candidates:
        return []

    active_refs = _list_all_referenced_template_images(session)
    stale_paths = sorted(normalized_candidates - active_refs)
    deleted_paths: List[str] = []

    for relative_path in stale_paths:
        file_path = (PROJECT_ROOT / relative_path).resolve()
        try:
            file_path.relative_to(AUTO_TEMPLATE_IMAGE_DIR.resolve())
        except Exception:
            logger.warning("skip deleting template image outside static/images: %s", relative_path)
            continue

        if not file_path.exists() or not file_path.is_file():
            continue

        try:
            file_path.unlink()
            deleted_paths.append(relative_path)
        except OSError:
            logger.exception("failed to delete unused template image: %s", file_path)

    if deleted_paths:
        logger.info("deleted unused template images: %s", deleted_paths)
    return deleted_paths


def _build_case_snapshot(case: TestCase) -> _CaseSnapshot:
    return _CaseSnapshot(
        case_id=int(case.id or 0),
        name=str(case.name or ""),
        steps=list(case.steps or []),
        variables=list(case.variables or []),
    )


def _update_case_run_status(session: Session, case_id: int, status: str) -> None:
    db_case = session.get(TestCase, case_id)
    if not db_case:
        return
    db_case.last_run_status = status
    db_case.last_run_time = datetime.now()
    session.add(db_case)
    session.commit()


def _step_ui_status(step_result: Any) -> str:
    if not isinstance(step_result, dict):
        return "FAIL"

    status = str(step_result.get("status") or "").strip().upper()
    if status in {"PASS", "FAIL", "WARNING", "SKIP"}:
        return status

    if step_result.get("is_skipped"):
        return "SKIP"
    if step_result.get("is_warning"):
        return "WARNING"

    success = step_result.get("success")
    if success is True:
        return "PASS"
    if success is False:
        return "FAIL"
    return "FAIL"


def _summarize_case_result(result: Any) -> Dict[str, Any]:
    if not isinstance(result, dict):
        return {"status": "FAIL", "all_skipped": False}

    raw_steps = result.get("steps")
    steps = raw_steps if isinstance(raw_steps, list) else []

    if not steps:
        return {
            "status": "PASS" if bool(result.get("success", True)) else "FAIL",
            "all_skipped": False,
        }

    statuses = [_step_ui_status(step) for step in steps]
    has_fail = any(status == "FAIL" for status in statuses)
    has_warning = any(status == "WARNING" for status in statuses)
    has_pass = any(status == "PASS" for status in statuses)
    has_skip = any(status == "SKIP" for status in statuses)
    all_skipped = has_skip and not has_pass and not has_warning and not has_fail

    if has_fail:
        status = "FAIL"
    elif has_warning or all_skipped:
        status = "WARNING"
    else:
        status = "PASS"

    return {
        "status": status,
        "all_skipped": all_skipped,
    }


def _row_to_standard_step_read(row: TestCaseStep) -> TestCaseStepRead:
    return TestCaseStepRead(
        id=row.id,
        case_id=row.case_id,
        order=row.order,
        action=row.action,
        args=row.args or {},
        value=row.value,
        execute_on=row.execute_on or ["android", "ios"],
        platform_overrides=row.platform_overrides or {},
        timeout=row.timeout,
        error_strategy=row.error_strategy,
        description=row.description,
    )


def _list_standard_steps(session: Session, case_id: int) -> List[TestCaseStep]:
    return session.exec(
        select(TestCaseStep)
        .where(TestCaseStep.case_id == case_id)
        .order_by(TestCaseStep.order, TestCaseStep.id)
    ).all()


def _standard_row_to_write_dict(row: TestCaseStep) -> Dict[str, Any]:
    return {
        "order": row.order,
        "action": row.action,
        "args": row.args or {},
        "value": row.value,
        "execute_on": row.execute_on or ["android", "ios"],
        "platform_overrides": row.platform_overrides or {},
        "timeout": row.timeout,
        "error_strategy": row.error_strategy,
        "description": row.description,
    }


def _validate_standard_step_write(step: TestCaseStepWrite, index: int) -> Dict[str, Any]:
    raw = dump_model(step)

    action = normalize_action(raw.get("action"))
    execute_on = normalize_execute_on(raw.get("execute_on"))
    platform_overrides = normalize_platform_overrides(raw.get("platform_overrides"))
    error_strategy = normalize_error_strategy(raw.get("error_strategy", "ABORT"))

    args = raw.get("args") or {}
    if not isinstance(args, dict):
        raise ValueError("args must be an object")

    timeout = raw.get("timeout", 10)
    try:
        timeout = int(timeout)
    except Exception as exc:
        raise ValueError(f"invalid timeout: {timeout}") from exc
    if timeout < 1:
        raise ValueError("timeout must be >= 1")

    order = raw.get("order", index)
    try:
        order = int(order)
    except Exception as exc:
        raise ValueError(f"invalid order: {order}") from exc
    if order < 0:
        raise ValueError("order must be >= 0")

    return {
        "order": order,
        "action": action,
        "args": args,
        "value": raw.get("value"),
        "execute_on": execute_on,
        "platform_overrides": platform_overrides,
        "timeout": timeout,
        "error_strategy": error_strategy,
        "description": raw.get("description"),
    }


def _replace_standard_steps(
    session: Session,
    case_id: int,
    step_items: List[Dict[str, Any]],
) -> List[TestCaseStep]:
    old_steps = session.exec(select(TestCaseStep).where(TestCaseStep.case_id == case_id)).all()
    for old_step in old_steps:
        session.delete(old_step)
    session.flush()

    for item in step_items:
        session.add(
            TestCaseStep(
                case_id=case_id,
                order=item["order"],
                action=item["action"],
                args=item.get("args") or {},
                value=item.get("value"),
                execute_on=item.get("execute_on") or ["android", "ios"],
                platform_overrides=item.get("platform_overrides") or {},
                timeout=item.get("timeout", 10),
                error_strategy=item.get("error_strategy", "ABORT"),
                description=item.get("description"),
            )
        )

    session.commit()
    return _list_standard_steps(session, case_id)


def _sync_standard_steps_from_legacy(
    session: Session,
    case_id: int,
    legacy_steps: List[Any],
) -> List[TestCaseStep]:
    standard_items = build_standard_from_legacy_steps(legacy_steps, case_id=case_id)
    return _replace_standard_steps(session, case_id, standard_items)


def _to_legacy_step_models(standard_steps: List[TestCaseStep]) -> List[Step]:
    legacy_steps = build_legacy_from_standard_steps(standard_steps)
    converted: List[Step] = []
    for item in legacy_steps:
        try:
            converted.append(Step(**item))
        except Exception:
            # Skip malformed rows to keep list/read endpoint available.
            continue
    return converted


def _enrich_case_read(
    case: TestCase,
    session: Session,
    creator_info: Optional[tuple] = None,
    updater_info: Optional[tuple] = None,
) -> TestCaseRead:
    """为 TestCaseRead 补充 folder_name/用户信息，并按开关决定 steps 来源。"""
    case_read = TestCaseRead.from_orm(case)

    if case.id and is_flag_enabled(session, FLAG_NEW_STEP_MODEL, default=False):
        standard_steps = _list_standard_steps(session, case.id)
        if standard_steps:
            case_read.steps = _to_legacy_step_models(standard_steps)

    if creator_info:
        case_read.creator_name = creator_info[0] or creator_info[1] or "Unknown"
    if updater_info:
        case_read.updater_name = updater_info[0] or updater_info[1] or "Unknown"
    if case.folder_id:
        folder = session.get(CaseFolder, case.folder_id)
        if folder:
            case_read.folder_name = folder.name
    return case_read


@router.post("/", response_model=TestCaseRead)
def create_test_case(
    test_case: TestCaseCreate,
    session: Session = Depends(get_session),
    current_user: User = Depends(deps.get_current_user),
):
    """Create a new test case."""
    db_case = TestCase(
        name=test_case.name,
        description=test_case.description,
        steps=test_case.steps,
        variables=test_case.variables,
        tags=test_case.tags,
        folder_id=test_case.folder_id,
        user_id=current_user.id,
        updater_id=current_user.id,
    )
    db_case.updated_at = db_case.created_at
    session.add(db_case)
    session.commit()
    session.refresh(db_case)

    if is_flag_enabled(session, FLAG_NEW_STEP_MODEL, default=False):
        _sync_standard_steps_from_legacy(session, db_case.id, db_case.steps or [])

    return _enrich_case_read(db_case, session)


@router.get("/", response_model=PaginatedTestCaseRead)
def list_test_cases(
    skip: int = 0,
    limit: int = 100,
    keyword: Optional[str] = None,
    tag: Optional[str] = None,
    folder_id: Optional[int] = Query(default=None, description="按目录 ID 过滤"),
    session: Session = Depends(get_session),
):
    """List test cases with pagination and filtering."""
    from sqlalchemy import func
    from sqlalchemy.orm import aliased

    creator = aliased(User)
    updater = aliased(User)

    query = (
        session.query(
            TestCase,
            creator.full_name,
            creator.username,
            updater.full_name,
            updater.username,
        )
        .outerjoin(creator, TestCase.user_id == creator.id)
        .outerjoin(updater, TestCase.updater_id == updater.id)
    )

    if keyword:
        query = query.filter(TestCase.name.contains(keyword))
    if folder_id is not None:
        query = query.filter(TestCase.folder_id == folder_id)

    query = query.order_by(TestCase.created_at.desc())

    count_query = session.query(func.count(TestCase.id))
    if keyword:
        count_query = count_query.filter(TestCase.name.contains(keyword))
    if folder_id is not None:
        count_query = count_query.filter(TestCase.folder_id == folder_id)

    total = count_query.scalar()

    results = query.offset(skip).limit(limit).all()

    case_list: List[TestCaseRead] = []
    for case, c_full, c_user, u_full, u_user in results:
        if tag and (not case.tags or tag not in case.tags):
            continue
        case_list.append(
            _enrich_case_read(
                case,
                session,
                creator_info=(c_full, c_user),
                updater_info=(u_full, u_user),
            )
        )

    return PaginatedTestCaseRead(total=total, items=case_list)


@router.get("/{case_id}", response_model=TestCaseRead)
def get_test_case(case_id: int, session: Session = Depends(get_session)):
    """Get a single test case."""
    case = session.get(TestCase, case_id)
    if not case:
        raise HTTPException(status_code=404, detail="Case not found")
    return _enrich_case_read(case, session)


@router.put("/{case_id}", response_model=TestCaseRead)
def update_test_case(
    case_id: int,
    test_case: TestCaseCreate,
    session: Session = Depends(get_session),
    current_user: User = Depends(deps.get_current_user),
):
    """Update a test case."""
    db_case = session.get(TestCase, case_id)
    if not db_case:
        raise HTTPException(status_code=404, detail="Case not found")
    previous_standard_steps = _list_standard_steps(session, case_id)
    previous_image_paths = _collect_case_template_image_paths(db_case, previous_standard_steps)

    db_case.name = test_case.name
    db_case.description = test_case.description
    db_case.steps = test_case.steps
    db_case.variables = test_case.variables
    db_case.tags = test_case.tags
    db_case.folder_id = test_case.folder_id
    db_case.updater_id = current_user.id
    db_case.updated_at = datetime.now()

    session.add(db_case)
    session.commit()
    session.refresh(db_case)

    if is_flag_enabled(session, FLAG_NEW_STEP_MODEL, default=False):
        _sync_standard_steps_from_legacy(session, db_case.id, db_case.steps or [])

    _cleanup_unused_template_images(session, previous_image_paths)

    return _enrich_case_read(db_case, session)


@router.post("/{case_id}/duplicate", response_model=TestCaseRead)
def duplicate_test_case(
    case_id: int,
    session: Session = Depends(get_session),
    current_user: User = Depends(deps.get_current_user),
):
    """Clone a test case."""
    original_case = session.get(TestCase, case_id)
    if not original_case:
        raise HTTPException(status_code=404, detail="Case not found")

    new_case = TestCase(
        name=f"{original_case.name}_copy",
        description=original_case.description,
        steps=original_case.steps,
        variables=original_case.variables,
        tags=original_case.tags,
        folder_id=original_case.folder_id,
        user_id=current_user.id,
        updater_id=current_user.id,
        created_at=datetime.now(),
        updated_at=datetime.now(),
    )

    session.add(new_case)
    session.commit()
    session.refresh(new_case)

    if is_flag_enabled(session, FLAG_NEW_STEP_MODEL, default=False):
        source_standard_steps = _list_standard_steps(session, original_case.id)
        if source_standard_steps:
            payload = [_standard_row_to_write_dict(row) for row in source_standard_steps]
            saved_steps = _replace_standard_steps(session, new_case.id, payload)
            new_case.steps = _to_legacy_step_models(saved_steps)
            session.add(new_case)
            session.commit()
        else:
            _sync_standard_steps_from_legacy(session, new_case.id, new_case.steps or [])

    return _enrich_case_read(new_case, session)


@router.get("/{case_id}/steps", response_model=List[TestCaseStepRead])
def get_case_standard_steps(case_id: int, session: Session = Depends(get_session)):
    """Read standard cross-platform steps for a case."""
    case = session.get(TestCase, case_id)
    if not case:
        raise HTTPException(status_code=404, detail="Case not found")

    standard_steps = _list_standard_steps(session, case_id)
    if (
        not standard_steps
        and is_flag_enabled(session, FLAG_NEW_STEP_MODEL, default=False)
        and case.steps
    ):
        standard_steps = _sync_standard_steps_from_legacy(session, case_id, case.steps)

    return [_row_to_standard_step_read(item) for item in standard_steps]


@router.put("/{case_id}/steps", response_model=List[TestCaseStepRead])
def replace_case_standard_steps(
    case_id: int,
    steps: List[TestCaseStepWrite],
    session: Session = Depends(get_session),
    current_user: User = Depends(deps.get_current_user),
):
    """Replace all standard steps for a case (source of truth for new model)."""
    case = session.get(TestCase, case_id)
    if not case:
        raise HTTPException(status_code=404, detail="Case not found")
    previous_standard_steps = _list_standard_steps(session, case_id)
    previous_image_paths = _collect_case_template_image_paths(case, previous_standard_steps)

    validated: List[Dict[str, Any]] = []
    try:
        for idx, step in enumerate(steps, start=1):
            validated.append(_validate_standard_step_write(step, idx))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    saved_steps = _replace_standard_steps(session, case_id, validated)

    # Double-write back to legacy steps for existing frontend compatibility.
    case.steps = _to_legacy_step_models(saved_steps)
    case.updater_id = current_user.id
    case.updated_at = datetime.now()
    session.add(case)
    session.commit()
    _cleanup_unused_template_images(session, previous_image_paths)

    return [_row_to_standard_step_read(item) for item in saved_steps]


@router.post("/{case_id}/steps/sync-legacy", response_model=List[TestCaseStepRead])
def sync_case_standard_steps_from_legacy(
    case_id: int,
    session: Session = Depends(get_session),
    current_user: User = Depends(deps.get_current_user),
):
    """One-click migration helper: sync legacy case.steps into standard step rows."""
    case = session.get(TestCase, case_id)
    if not case:
        raise HTTPException(status_code=404, detail="Case not found")
    previous_standard_steps = _list_standard_steps(session, case_id)
    previous_image_paths = _collect_template_paths_from_standard_steps(previous_standard_steps)

    saved_steps = _sync_standard_steps_from_legacy(session, case_id, case.steps or [])

    case.updater_id = current_user.id
    case.updated_at = datetime.now()
    session.add(case)
    session.commit()
    _cleanup_unused_template_images(session, previous_image_paths)

    return [_row_to_standard_step_read(item) for item in saved_steps]


def _run_case_background(
    case_id: int,
    session_factory,
    env_id: Optional[int] = None,
    device_serial: Optional[str] = None,
):
    # We need a new session for the background thread.
    with session_factory() as session:
        case = session.get(TestCase, case_id)
        if not case:
            return
        case_snapshot = _build_case_snapshot(case)

        from backend.runner import TestRunner

        runner = TestRunner(device_serial=device_serial)
        try:
            runner.connect()

            # Prepare optional variables map.
            variables_map = {}
            if env_id:
                from backend.models import GlobalVariable

                global_vars = session.exec(
                    select(GlobalVariable).where(GlobalVariable.env_id == env_id)
                ).all()
                for gv in global_vars:
                    variables_map[gv.key] = gv.value

            result = runner.run_case(case_snapshot, extra_variables=variables_map)

            # Update case status.
            summary = _summarize_case_result(result)
            _update_case_run_status(session, case_id, summary["status"])
        except Exception:
            _update_case_run_status(session, case_id, "FAIL")


def _run_case_background_cross_platform(
    case_id: int,
    session_factory,
    env_id: Optional[int] = None,
    device_serial: Optional[str] = None,
):
    # Cross-platform path currently requires explicit target device.
    if not device_serial:
        logger.error("cross-platform runner requires device_serial")
        return _run_case_background(
            case_id=case_id,
            session_factory=session_factory,
            env_id=env_id,
            device_serial=device_serial,
        )

    with session_factory() as session:
        case = session.get(TestCase, case_id)
        if not case:
            return
        case_snapshot = _build_case_snapshot(case)

        abort_event = register_device_abort(device_serial)

        try:
            device = session.exec(select(Device).where(Device.serial == device_serial)).first()
            if device:
                device.status = "BUSY"
                device.updated_at = datetime.now()
                session.add(device)
                session.commit()

            result = run_case_with_standard_runner(
                session=session,
                case=case_snapshot,
                device_serial=device_serial,
                env_id=env_id,
                abort_event=abort_event,
            )

            summary = _summarize_case_result(result)
            _update_case_run_status(session, case_id, summary["status"])
        except Exception as exc:
            logger.exception(
                "cross-platform case execution failed: case_id=%s device=%s error=%s",
                case_id,
                device_serial,
                exc,
            )
            _update_case_run_status(session, case_id, "FAIL")
        finally:
            try:
                restore_device_status_after_execution(session, device_serial)
            except Exception:
                logger.exception(
                    "failed to restore device status after case execution: device=%s",
                    device_serial,
                )
            unregister_device_abort(device_serial)


@router.post("/{case_id}/run")
def run_test_case(
    case_id: int,
    background_tasks: BackgroundTasks,
    env_id: Optional[int] = None,
    device_serial: Optional[str] = None,
    session: Session = Depends(get_session),
):
    """Quick run a test case in background."""
    case = session.get(TestCase, case_id)
    if not case:
        raise HTTPException(status_code=404, detail="Case not found")

    # Pass the engine/factory to background task, not the dependency session.
    from backend.database import engine
    from sqlmodel import Session as SQLSession

    def session_factory():
        return SQLSession(engine)

    use_cross_platform_runner = (
        is_flag_enabled(session, FLAG_CROSS_PLATFORM_RUNNER, default=False)
        and bool(device_serial)
    )
    run_func = _run_case_background_cross_platform if use_cross_platform_runner else _run_case_background
    background_tasks.add_task(run_func, case_id, session_factory, env_id, device_serial)

    return {
        "message": "Execution started",
        "case_id": case_id,
        "runner": "cross_platform" if use_cross_platform_runner else "legacy",
    }


@router.get("/{case_id}/precheck")
def precheck_test_case(
    case_id: int,
    device_serial: str,
    env_id: Optional[int] = None,
    session: Session = Depends(get_session),
):
    """Precheck case executability on target device without running the case."""
    case = session.get(TestCase, case_id)
    if not case:
        raise HTTPException(status_code=404, detail="Case not found")
    return precheck_case_execution(
        session=session,
        case=case,
        device_serial=device_serial,
        env_id=env_id,
    )


@router.delete("/{case_id}")
def delete_test_case(case_id: int, session: Session = Depends(get_session)):
    """Delete a test case."""
    case = session.get(TestCase, case_id)
    if not case:
        raise HTTPException(status_code=404, detail="Case not found")

    standard_steps = session.exec(select(TestCaseStep).where(TestCaseStep.case_id == case_id)).all()
    previous_image_paths = _collect_case_template_image_paths(case, standard_steps)
    for step in standard_steps:
        session.delete(step)

    session.delete(case)
    session.commit()
    _cleanup_unused_template_images(session, previous_image_paths)
    return {"message": "Case deleted", "id": case_id}
