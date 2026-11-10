"""Visual compatibility testing for Android production APKs."""
from __future__ import annotations

import asyncio
import io
import logging
import re
import shutil
import shlex
import threading
from datetime import datetime
from difflib import SequenceMatcher
from pathlib import Path
from typing import Annotated, Any, Dict, List, Optional, Sequence, Tuple

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query
from sqlalchemy import or_
from sqlmodel import Session, select, func, col

from backend.api import deps
from backend.api.packages import (
    _resolve_package_file_path,
    _run_adb_command,
    install_app_package_to_device,
)
from backend.artifact_store import (
    AssetCapacityExceeded,
    AssetGone,
    AssetNotFound,
    RETENTION_HOT,
    RETENTION_PINNED,
    content_addressed_assets_enabled,
    ensure_asset_capacity_for_new_run,
    read_asset,
    release_owner_references,
    retention_expiry,
    store_file,
    store_image_bytes,
    store_text,
    upsert_reference,
)
from backend.cross_platform_execution import (
    restore_device_status_after_execution,
    run_case_with_standard_runner,
)
from backend.database import engine, get_session
from backend.device_execution_lease import (
    DeviceExecutionLease,
    legacy_fastbot_device_locked,
)
from backend.models import (
    AppPackage,
    AssetReference,
    CompatPageSet,
    CompatibilityCell,
    CompatibilityPageResult,
    CompatibilityRun,
    Device,
    StoredAsset,
    TestCase,
    User,
)
from backend.paths import project_path
from backend.schemas import (
    CompatPageDefinition,
    CompatPageSetCreate,
    CompatPageSetRead,
    CompatPageSetUpdate,
    CompatibilityCellRead,
    CompatibilityPageResultRead,
    CompatibilityRunCreate,
    CompatibilityRunRead,
    PaginatedCompatibilityRunRead,
)
from backend.utils.pydantic_compat import dump_model
from backend.run_control import register_device_abort, unregister_device_abort

logger = logging.getLogger(__name__)
router = APIRouter()

TERMINAL_STATUSES = {"PASS", "WARNING", "FAIL", "ERROR", "ABORTED"}

_CRASH_PATTERN = re.compile(r"FATAL EXCEPTION|ANR in|Application Not Responding", re.I)
_RUN_ABORT_EVENTS: Dict[int, threading.Event] = {}
_RUN_ABORT_LOCK = threading.Lock()


def _now() -> datetime:
    return datetime.now()


def _dump_page(page: Any) -> Dict[str, Any]:
    raw = dump_model(page)
    return dict(raw or {}) if isinstance(raw, dict) else {}


def _page_key(index: int, page: Dict[str, Any]) -> str:
    candidate = str(page.get("key") or "").strip()
    if candidate:
        return re.sub(r"[^a-zA-Z0-9_.-]+", "_", candidate)[:80]
    name = str(page.get("name") or f"page_{index}").strip()
    safe_name = re.sub(r"[^a-zA-Z0-9_.-]+", "_", name).strip("_")
    return f"{index:02d}_{safe_name or 'page'}"[:80]


def _optional_page_case_id(page: Dict[str, Any]) -> Optional[int]:
    value = page.get("case_id")
    return int(value) if value not in (None, "", 0, "0") else None


def _active_compatibility_asset(
    session: Session,
    asset_id: Optional[str],
) -> Optional[StoredAsset]:
    if not asset_id:
        return None
    row = session.get(StoredAsset, asset_id)
    if row is None or row.status != "ACTIVE":
        return None
    return row


def _normalize_pages(raw_pages: List[Any]) -> List[Dict[str, Any]]:
    pages: List[Dict[str, Any]] = []
    for index, item in enumerate(raw_pages or [], start=1):
        page = _dump_page(item)
        if not page.get("key"):
            page["key"] = _page_key(index, page)
        pages.append(page)
    return pages


def _page_definitions(raw_pages: List[Any]) -> List[CompatPageDefinition]:
    """把历史 page_set_snapshot 归一成页面定义。

    历史行可能来自已下线的巡检快照/链路回放来源，字段形态不完全一致；
    只读展示允许信息变少，但绝不能因为个别脏数据让整份报告读取报错。
    """
    definitions: List[CompatPageDefinition] = []
    for index, page in enumerate(_normalize_pages(raw_pages or []), start=1):
        candidate = dict(page)
        if not str(candidate.get("name") or "").strip():
            candidate["name"] = str(candidate.get("key") or f"页面 {index}")
        try:
            definitions.append(CompatPageDefinition(**candidate))
        except Exception:
            logger.warning("skip unreadable compatibility page snapshot: %s", candidate.get("key"))
    return definitions


def _page_set_read(row: CompatPageSet) -> CompatPageSetRead:
    return CompatPageSetRead(
        id=row.id,
        name=row.name,
        description=row.description,
        pages=_page_definitions(row.pages or []),
        user_id=row.user_id,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def _page_set_snapshot_read(row: CompatibilityRun) -> Optional[CompatPageSetRead]:
    pages = _page_definitions(row.page_set_snapshot or [])
    if not pages and not row.page_set_name:
        return None
    return CompatPageSetRead(
        id=row.page_set_id or 0,
        name=row.page_set_name or "已删除页面合集",
        description=None,
        pages=pages,
        user_id=row.user_id,
        created_at=row.created_at,
        updated_at=None,
    )


def _page_result_read(row: CompatibilityPageResult) -> CompatibilityPageResultRead:
    return CompatibilityPageResultRead(
        id=row.id,
        run_id=row.run_id,
        cell_id=row.cell_id,
        page_key=row.page_key,
        page_name=row.page_name,
        case_id=row.case_id,
        status=row.status,
        reason=row.reason,
        required_text=row.required_text,
        baseline_screenshot_path=row.baseline_screenshot_path,
        candidate_screenshot_path=row.candidate_screenshot_path,
        diff_screenshot_path=row.diff_screenshot_path,
        baseline_xml_path=row.baseline_xml_path,
        candidate_xml_path=row.candidate_xml_path,
        baseline_screenshot_asset_id=row.baseline_screenshot_asset_id,
        candidate_screenshot_asset_id=row.candidate_screenshot_asset_id,
        diff_screenshot_asset_id=row.diff_screenshot_asset_id,
        baseline_xml_asset_id=row.baseline_xml_asset_id,
        candidate_xml_asset_id=row.candidate_xml_asset_id,
        baseline_activity=row.baseline_activity,
        candidate_activity=row.candidate_activity,
        metrics=row.metrics or {},
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def _cell_read(
    session: Session,
    row: CompatibilityCell,
    include_pages: bool = True,
    *,
    page_rows: Optional[Sequence[CompatibilityPageResult]] = None,
) -> CompatibilityCellRead:
    pages: List[CompatibilityPageResultRead] = []
    if include_pages:
        resolved_page_rows = page_rows
        if resolved_page_rows is None:
            resolved_page_rows = session.exec(
                select(CompatibilityPageResult)
                .where(CompatibilityPageResult.cell_id == row.id)
                .order_by(CompatibilityPageResult.id)
            ).all()
        pages = [_page_result_read(item) for item in resolved_page_rows]

    return CompatibilityCellRead(
        id=row.id,
        run_id=row.run_id,
        device_serial=row.device_serial,
        device_info=row.device_info,
        os_version=row.os_version,
        resolution=row.resolution,
        is_baseline=bool(row.is_baseline),
        status=row.status,
        current_stage=row.current_stage,
        old_install_status=row.old_install_status,
        new_install_status=row.new_install_status,
        preflight_at=row.preflight_at,
        installed_package_snapshot=row.installed_package_snapshot or {},
        error_message=row.error_message,
        started_at=row.started_at,
        finished_at=row.finished_at,
        pages=pages,
    )


def _run_read(
    session: Session,
    row: CompatibilityRun,
    include_detail: bool = False,
    *,
    page_set: Optional[CompatPageSet] = None,
    page_set_loaded: bool = False,
) -> CompatibilityRunRead:
    if not page_set_loaded:
        page_set = session.get(CompatPageSet, row.page_set_id) if row.page_set_id else None
    page_set_read = _page_set_read(page_set) if page_set else _page_set_snapshot_read(row)
    cells: List[CompatibilityCellRead] = []
    if include_detail:
        cell_rows = session.exec(
            select(CompatibilityCell)
            .where(CompatibilityCell.run_id == row.id)
            .order_by(CompatibilityCell.id)
        ).all()
        cell_ids = [int(item.id) for item in cell_rows if item.id is not None]
        pages_by_cell: Dict[int, List[CompatibilityPageResult]] = {
            cell_id: [] for cell_id in cell_ids
        }
        if cell_ids:
            page_rows = session.exec(
                select(CompatibilityPageResult)
                .where(col(CompatibilityPageResult.cell_id).in_(cell_ids))
                .order_by(CompatibilityPageResult.cell_id, CompatibilityPageResult.id)
            ).all()
            for page_row in page_rows:
                pages_by_cell.setdefault(int(page_row.cell_id), []).append(page_row)
        cells = [
            _cell_read(
                session,
                item,
                include_pages=True,
                page_rows=pages_by_cell.get(int(item.id), []),
            )
            for item in cell_rows
        ]

    snapshot_pages = [
        dict(item)
        for item in (row.page_set_snapshot or [])
        if isinstance(item, dict)
    ]
    return CompatibilityRunRead(
        id=row.id,
        name=row.name,
        page_set_id=row.page_set_id,
        page_set_name=row.page_set_name,
        page_set_snapshot=_page_definitions(snapshot_pages),
        source_type=row.source_type or "page_set",
        old_package_id=row.old_package_id,
        new_package_id=row.new_package_id,
        package_name=row.package_name,
        compare_mode=row.compare_mode or "version",
        baseline_device_serial=row.baseline_device_serial,
        mode=row.mode or "upgrade",
        env_id=row.env_id,
        device_serials=row.device_serials or [],
        thresholds=row.thresholds or {},
        status=row.status,
        total_cells=row.total_cells,
        total_pages=row.total_pages,
        pass_count=row.pass_count,
        warning_count=row.warning_count,
        fail_count=row.fail_count,
        error_message=row.error_message,
        executor_name=row.executor_name,
        created_at=row.created_at,
        started_at=row.started_at,
        finished_at=row.finished_at,
        page_set=page_set_read,
        cells=cells,
    )


def _validate_page_set(session: Session, page_set: CompatPageSet) -> List[Dict[str, Any]]:
    pages = _normalize_pages(page_set.pages or [])
    if not pages:
        raise HTTPException(status_code=400, detail="页面集合不能为空")

    for page in pages:
        case_id = int(page.get("case_id") or 0)
        if not case_id or not session.get(TestCase, case_id):
            raise HTTPException(status_code=400, detail=f"页面用例不存在: {case_id}")
    return pages


def _validate_packages(session: Session, old_package_id: Optional[int], new_package_id: int) -> Tuple[Optional[AppPackage], AppPackage]:
    new_pkg = session.get(AppPackage, new_package_id)
    if not new_pkg:
        raise HTTPException(status_code=404, detail="新版安装包不存在")
    if str(new_pkg.platform or "android").strip().lower() != "android":
        raise HTTPException(status_code=400, detail="兼容性测试 v1 仅支持 Android APK")
    if not new_pkg.package_name:
        raise HTTPException(status_code=400, detail="安装包缺少包名，无法执行兼容性测试")
    if not _resolve_package_file_path(new_pkg.file_path).exists():
        raise HTTPException(status_code=404, detail=f"APK 文件已被删除: {new_pkg.version_name or new_pkg.id}")

    if old_package_id is None:
        return None, new_pkg

    old_pkg = session.get(AppPackage, old_package_id)
    if not old_pkg:
        raise HTTPException(status_code=404, detail="旧版安装包不存在")
    if str(old_pkg.platform or "android").strip().lower() != "android":
        raise HTTPException(status_code=400, detail="兼容性测试 v1 仅支持 Android APK")
    if not old_pkg.package_name:
        raise HTTPException(status_code=400, detail="旧版安装包缺少包名，无法执行兼容性测试")
    if old_pkg.package_name != new_pkg.package_name:
        raise HTTPException(status_code=400, detail="旧版和新版 APK 必须属于同一个 package_name")
    if not _resolve_package_file_path(old_pkg.file_path).exists():
        raise HTTPException(status_code=404, detail=f"APK 文件已被删除: {old_pkg.version_name or old_pkg.id}")
    return old_pkg, new_pkg


def _validate_devices(session: Session, serials: List[str]) -> List[Device]:
    devices = session.exec(select(Device).where(col(Device.serial).in_(serials))).all()
    by_serial = {item.serial: item for item in devices}
    missing = [serial for serial in serials if serial not in by_serial]
    if missing:
        raise HTTPException(status_code=404, detail=f"设备不存在: {', '.join(missing)}")

    validated: List[Device] = []
    for serial in serials:
        device = by_serial[serial]
        platform = str(device.platform or "android").strip().lower()
        if platform != "android":
            raise HTTPException(status_code=400, detail=f"兼容性测试 v1 仅支持 Android 设备: {serial}")
        status = str(device.status or "IDLE").strip().upper()
        if (
            status != "IDLE"
            or device.lease_task_id
            or legacy_fastbot_device_locked(serial)
        ):
            raise HTTPException(status_code=400, detail=f"设备非空闲，无法启动兼容性任务: {serial} ({status})")
        validated.append(device)
    return validated


def _abort_event_for_run(run_id: int) -> threading.Event:
    with _RUN_ABORT_LOCK:
        event = _RUN_ABORT_EVENTS.get(run_id)
        if event is None:
            event = threading.Event()
            _RUN_ABORT_EVENTS[run_id] = event
        return event


def _discard_abort_event(run_id: int) -> None:
    with _RUN_ABORT_LOCK:
        _RUN_ABORT_EVENTS.pop(run_id, None)


def _is_cancelled(session: Session, run_id: int, event: threading.Event) -> bool:
    if event.is_set():
        return True
    run = session.get(CompatibilityRun, run_id)
    return bool(run and str(run.status or "").upper() == "ABORTED")


def _set_device_busy(session: Session, serial: str) -> None:
    device = session.exec(select(Device).where(Device.serial == serial)).first()
    if not device:
        return
    device.status = "BUSY"
    device.updated_at = _now()
    session.add(device)
    session.commit()


def _update_run_summary(session: Session, run_id: int, *, final: bool = False) -> None:
    run = session.get(CompatibilityRun, run_id)
    if not run:
        return

    page_rows = session.exec(
        select(CompatibilityPageResult).where(CompatibilityPageResult.run_id == run_id)
    ).all()
    pass_count = sum(1 for item in page_rows if str(item.status).upper() == "PASS")
    warning_count = sum(1 for item in page_rows if str(item.status).upper() == "WARNING")
    fail_count = sum(1 for item in page_rows if str(item.status).upper() in {"FAIL", "ERROR"})

    cells = session.exec(select(CompatibilityCell).where(CompatibilityCell.run_id == run_id)).all()
    cell_statuses = {str(item.status or "").upper() for item in cells}

    run.pass_count = pass_count
    run.warning_count = warning_count
    run.fail_count = fail_count
    run.total_pages = len(page_rows)

    if str(run.status or "").upper() == "ABORTED":
        if final:
            run.finished_at = run.finished_at or _now()
    elif fail_count > 0 or "FAIL" in cell_statuses or "ERROR" in cell_statuses:
        run.status = "FAIL" if final and cell_statuses <= TERMINAL_STATUSES else "RUNNING"
    elif warning_count > 0 or "WARNING" in cell_statuses:
        run.status = "WARNING" if final and cell_statuses <= TERMINAL_STATUSES else "RUNNING"
    elif cells and all(str(item.status or "").upper() == "PASS" for item in cells):
        run.status = "PASS" if final else "RUNNING"
    elif final:
        run.status = "ERROR"
        run.error_message = run.error_message or "兼容性任务未产生有效结果"
    else:
        run.status = "RUNNING"

    if final and not run.finished_at:
        run.finished_at = _now()
    session.add(run)
    session.commit()


def _store_text(path: Path, content: str) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content or "", encoding="utf-8")
    return _report_asset_path(path)


def _store_png_bytes(path: Path, data: bytes) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return _report_asset_path(path)


def _report_asset_path(path: Path) -> str:
    reports_root = project_path("reports").resolve()
    return path.resolve().relative_to(reports_root).as_posix()


def _delete_run_artifacts(run_id: int) -> bool:
    compatibility_root = project_path("reports", "compatibility").resolve()
    target = (compatibility_root / str(run_id)).resolve()
    target.relative_to(compatibility_root)
    if not target.exists():
        return False
    if not target.is_dir():
        raise RuntimeError(f"兼容性报告产物路径异常: {target}")
    shutil.rmtree(target)
    return True


async def _capture_logcat_errors(serial: str, package_name: str) -> str:
    try:
        quoted_serial = shlex.quote(str(serial))
        output = await _run_adb_command(
            f"adb -s {quoted_serial} logcat -d -t 300 {shlex.quote('*:E')}",
            timeout=20,
        )
    except Exception as exc:
        return f"logcat capture failed: {exc}"
    lines = []
    for line in output.splitlines():
        lowered = line.lower()
        if package_name in line or "fatal exception" in lowered or " anr " in lowered or "anr in" in lowered:
            lines.append(line)
    return "\n".join(lines[-80:])


async def _clear_logcat(serial: str) -> None:
    try:
        quoted_serial = shlex.quote(str(serial))
        await _run_adb_command(f"adb -s {quoted_serial} logcat -c", timeout=10)
    except Exception:
        logger.debug("logcat clear failed for %s", serial, exc_info=True)


async def _ensure_package_installed(serial: str, package_name: str) -> None:
    output = await _run_adb_command(
        f"adb -s {shlex.quote(serial)} shell pm path {shlex.quote(package_name)}",
        timeout=20,
    )
    if "package:" not in output:
        raise RuntimeError(f"设备当前未安装 {package_name}，无法使用当前版本作为基线")


async def _capture_activity(serial: str) -> str:
    try:
        quoted_serial = shlex.quote(str(serial))
        output = await _run_adb_command(
            f"adb -s {quoted_serial} shell dumpsys window | grep -E 'mCurrentFocus|mFocusedApp' | head -2",
            timeout=10,
        )
        return output.strip()
    except Exception:
        return ""


async def _capture_snapshot(
    *,
    serial: str,
    package_name: str,
    run_id: int,
    cell_id: int,
    phase: str,
    page: Dict[str, Any],
) -> Dict[str, Any]:
    import uiautomator2 as u2

    page_key = str(page.get("key") or "page")
    base_dir = project_path("reports", "compatibility", str(run_id), str(cell_id), phase, page_key)
    device = u2.connect(serial)
    screenshot = device.screenshot(format="pillow")
    buffer = io.BytesIO()
    screenshot.save(buffer, format="PNG")
    image_bytes = buffer.getvalue()
    xml_text = str(device.dump_hierarchy() or "")
    activity = await _capture_activity(serial)
    logcat_errors = await _capture_logcat_errors(serial, package_name)

    screenshot_path = _store_png_bytes(base_dir / "screenshot.png", image_bytes)
    xml_path = _store_text(base_dir / "hierarchy.xml", xml_text)
    _store_text(base_dir / "logcat_errors.txt", logcat_errors)

    return {
        "screenshot_path": screenshot_path,
        "screenshot_bytes": image_bytes,
        "xml_path": xml_path,
        "xml_text": xml_text,
        "activity": activity,
        "logcat_errors": logcat_errors,
    }


def _persist_snapshot_assets(session: Session, snapshot: Dict[str, Any]) -> Dict[str, Any]:
    if not content_addressed_assets_enabled(session):
        return snapshot

    screenshot_bytes = bytes(snapshot.get("screenshot_bytes") or b"")
    if screenshot_bytes:
        screenshot_asset = store_image_bytes(session, screenshot_bytes, commit=False)
    else:
        screenshot_path = _resolve_report_asset_path(snapshot.get("screenshot_path"))
        screenshot_asset = store_file(session, screenshot_path, commit=False)
    xml_asset = store_text(
        session,
        str(snapshot.get("xml_text") or ""),
        media_type="application/xml",
        suffix="xml",
        commit=False,
    )
    snapshot["screenshot_asset_id"] = screenshot_asset.id
    snapshot["xml_asset_id"] = xml_asset.id
    session.commit()
    return snapshot


def _resolve_report_asset_path(path: Optional[str]) -> Path:
    if not path:
        raise FileNotFoundError("report asset path is empty")
    reports_root = project_path("reports").resolve()
    candidate = (reports_root / str(path)).resolve()
    candidate.relative_to(reports_root)
    return candidate


def _load_cas_asset_bytes(
    asset_id: Optional[str],
    *,
    session: Optional[Session],
) -> Optional[bytes]:
    if not asset_id:
        return None
    if session is not None:
        if not content_addressed_assets_enabled(session):
            return None
        try:
            return read_asset(session, asset_id, transparent=True).body
        except (AssetGone, AssetNotFound):
            logger.warning("CAS asset unavailable; falling back to legacy path: %s", asset_id)
            return None

    with Session(engine) as local_session:
        if not content_addressed_assets_enabled(local_session):
            return None
        try:
            return read_asset(local_session, asset_id, transparent=True).body
        except (AssetGone, AssetNotFound):
            logger.warning("CAS asset unavailable; falling back to legacy path: %s", asset_id)
            return None


def _load_report_asset_bytes(
    path: Optional[str],
    *,
    asset_id: Optional[str] = None,
    session: Optional[Session] = None,
) -> bytes:
    cas_body = _load_cas_asset_bytes(asset_id, session=session)
    if cas_body is not None:
        return cas_body
    if not path:
        return b""
    return _resolve_report_asset_path(path).read_bytes()


def _load_report_text(
    path: Optional[str],
    *,
    asset_id: Optional[str] = None,
    session: Optional[Session] = None,
) -> str:
    try:
        return _load_report_asset_bytes(
            path,
            asset_id=asset_id,
            session=session,
        ).decode("utf-8")
    except Exception:
        return ""


def _normalize_xml(xml_text: str) -> str:
    text = re.sub(r'bounds="[^"]*"', "", xml_text or "")
    text = re.sub(r'(focused|selected|checked|index)="[^"]*"', "", text)
    return text


def _normalize_activity(raw: str) -> str:
    """从 dumpsys window 焦点行提取 `包名/Activity` 组件，剥离窗口 hash 等噪音以便跨设备比较。

    相对写法（com.pkg/.ui.Main）展开为完整组件，不同 ROM 输出风格才可等值比较。
    """
    match = re.search(r'([A-Za-z][A-Za-z0-9_.]*)/(\.?[A-Za-z0-9_.$]+)', raw or "")
    if not match:
        return ""
    package, activity = match.group(1), match.group(2)
    if activity.startswith("."):
        activity = package + activity
    return f"{package}/{activity}"


def compare_page_snapshots(
    *,
    baseline: Dict[str, Any],
    candidate: Dict[str, Any],
    page: Dict[str, Any],
    thresholds: Dict[str, Any],
    run_id: int,
    cell_id: int,
    session: Optional[Session] = None,
) -> Dict[str, Any]:
    try:
        from PIL import Image, ImageChops
        import numpy as np
    except Exception as exc:
        return {
            "status": "FAIL",
            "reason": f"图像对比依赖缺失: {exc}",
            "metrics": {},
            "diff_screenshot_path": None,
        }

    baseline_img = Image.open(
        io.BytesIO(
            _load_report_asset_bytes(
                baseline.get("screenshot_path"),
                asset_id=baseline.get("screenshot_asset_id"),
                session=session,
            )
        )
    ).convert("RGB")
    candidate_img = Image.open(
        io.BytesIO(
            _load_report_asset_bytes(
                candidate.get("screenshot_path"),
                asset_id=candidate.get("screenshot_asset_id"),
                session=session,
            )
        )
    ).convert("RGB")
    size_changed = baseline_img.size != candidate_img.size
    if size_changed:
        candidate_img = candidate_img.resize(baseline_img.size)

    diff = ImageChops.difference(baseline_img, candidate_img)
    diff_arr = np.asarray(diff)
    changed = np.any(diff_arr > 24, axis=2)
    pixel_diff_ratio = float(np.count_nonzero(changed) / max(1, changed.size))
    mean_abs_diff = float(diff_arr.mean() / 255.0)
    visual_similarity = max(0.0, min(1.0, 1.0 - mean_abs_diff))

    try:
        from skimage.metrics import structural_similarity
        import cv2

        gray_a = cv2.cvtColor(np.asarray(baseline_img), cv2.COLOR_RGB2GRAY)
        gray_b = cv2.cvtColor(np.asarray(candidate_img), cv2.COLOR_RGB2GRAY)
        ssim_score = float(structural_similarity(gray_a, gray_b))
    except Exception:
        ssim_score = visual_similarity

    baseline_xml = _normalize_xml(str(baseline.get("xml_text") or ""))
    candidate_xml = _normalize_xml(str(candidate.get("xml_text") or ""))
    xml_similarity = SequenceMatcher(None, baseline_xml, candidate_xml).ratio() if (baseline_xml or candidate_xml) else 1.0
    xml_diff_ratio = 1.0 - xml_similarity

    crash_text = "\n".join([
        str(baseline.get("logcat_errors") or ""),
        str(candidate.get("logcat_errors") or ""),
    ])
    has_crash = bool(_CRASH_PATTERN.search(crash_text))
    required_text = str(page.get("required_text") or "").strip()
    required_text_missing = bool(
        required_text
        and required_text not in candidate_xml
    )

    reasons: List[str] = []
    status = "PASS"
    if has_crash:
        status = "FAIL"
        reasons.append("检测到 Crash/ANR 日志")
    if required_text_missing:
        status = "FAIL"
        reasons.append(f"新版页面缺少必需文本: {required_text}")

    pixel_warn = float(thresholds.get("pixel_diff_ratio_warn", 0.03))
    ssim_warn = float(thresholds.get("ssim_warn", 0.96))
    xml_warn = float(thresholds.get("xml_diff_ratio_warn", 0.35))
    if status != "FAIL":
        if size_changed:
            status = "WARNING"
            reasons.append("截图尺寸发生变化")
        if pixel_diff_ratio > pixel_warn or ssim_score < ssim_warn:
            status = "WARNING"
            reasons.append("视觉差异超过阈值")
        if xml_diff_ratio > xml_warn:
            status = "WARNING"
            reasons.append("UI 层级差异超过阈值")

    metrics = {
        "pixel_diff_ratio": round(pixel_diff_ratio, 6),
        "ssim": round(ssim_score, 6),
        "visual_similarity": round(visual_similarity, 6),
        "xml_diff_ratio": round(xml_diff_ratio, 6),
        "size_changed": size_changed,
        "has_crash_or_anr": has_crash,
        "required_text_missing": required_text_missing,
    }

    diff_path = None
    if status != "PASS":
        overlay_arr = np.asarray(candidate_img).copy()
        overlay_arr[changed] = [255, 64, 64]
        diff_img = Image.fromarray(overlay_arr)
        diff_path = _store_png_bytes(
            project_path(
                "reports",
                "compatibility",
                str(run_id),
                str(cell_id),
                "diff",
                f"{page.get('key')}.png",
            ),
            _image_to_png_bytes(diff_img),
        )

    return {
        "status": status,
        "reason": "；".join(reasons) if reasons else None,
        "metrics": metrics,
        "diff_screenshot_path": diff_path,
    }


def _image_to_png_bytes(image: Any) -> bytes:
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


def _render_on_demand_diff(
    baseline_bytes: bytes,
    candidate_bytes: bytes,
) -> bytes:
    from PIL import Image, ImageChops
    import numpy as np

    baseline = Image.open(io.BytesIO(baseline_bytes)).convert("RGB")
    candidate = Image.open(io.BytesIO(candidate_bytes)).convert("RGB")
    compare_candidate = candidate if candidate.size == baseline.size else candidate.resize(
        baseline.size,
        Image.Resampling.LANCZOS,
    )
    changed = np.any(
        np.asarray(ImageChops.difference(baseline, compare_candidate)) > 24,
        axis=2,
    )
    overlay = np.asarray(compare_candidate).copy()
    overlay[changed] = [255, 64, 64]
    return _image_to_png_bytes(Image.fromarray(overlay))


def _get_or_create_page_diff(
    session: Session,
    row: CompatibilityPageResult,
) -> Dict[str, Any]:
    if row.diff_screenshot_asset_id:
        existing = _active_compatibility_asset(session, row.diff_screenshot_asset_id)
        if existing is not None:
            return {
                "asset_id": existing.id,
                "url": f"/api/assets/{existing.id}",
                "cached": True,
                "expires_at": None,
            }

    now = _now()
    cached_reference = session.exec(
        select(AssetReference).where(
            AssetReference.owner_type == "compatibility_page_result",
            AssetReference.owner_id == row.id,
            AssetReference.role == "on_demand_diff",
            AssetReference.released_at == None,  # noqa: E711
            AssetReference.expires_at != None,  # noqa: E711
            AssetReference.expires_at > now,
        )
    ).first()
    if cached_reference is not None:
        existing = _active_compatibility_asset(session, cached_reference.asset_id)
        if existing is not None:
            return {
                "asset_id": existing.id,
                "url": f"/api/assets/{existing.id}",
                "cached": True,
                "expires_at": cached_reference.expires_at,
            }

    baseline = _load_report_asset_bytes(
        row.baseline_screenshot_path,
        asset_id=row.baseline_screenshot_asset_id,
        session=session,
    )
    candidate = _load_report_asset_bytes(
        row.candidate_screenshot_path,
        asset_id=row.candidate_screenshot_asset_id,
        session=session,
    )
    if not baseline or not candidate:
        raise HTTPException(status_code=410, detail="页面截图资产已不可用")
    diff = store_image_bytes(
        session,
        _render_on_demand_diff(baseline, candidate),
        commit=False,
    )
    expires_at = retention_expiry(RETENTION_HOT, now=now)
    upsert_reference(
        session,
        asset_id=diff.id,
        owner_type="compatibility_page_result",
        owner_id=int(row.id),
        role="on_demand_diff",
        retention_class=RETENTION_HOT,
        expires_at=expires_at,
        commit=False,
    )
    session.commit()
    return {
        "asset_id": diff.id,
        "url": f"/api/assets/{diff.id}",
        "cached": False,
        "expires_at": expires_at,
    }


def compare_device_pages(
    *,
    baseline: Dict[str, Any],
    candidate: Dict[str, Any],
    page: Dict[str, Any],
    thresholds: Dict[str, Any],
    run_id: int,
    cell_id: int,
    session: Optional[Session] = None,
) -> Dict[str, Any]:
    """机型对比：候选设备页面 vs 基准设备页面。

    结构语义为主：Crash/必需文本/Activity 不一致直接 FAIL；归一化 XML 结构差异触发 WARNING；
    像素/SSIM 仅在两台设备分辨率相同时参与判定，跨分辨率仅计算展示（size 差异是预期，不告警）。
    """
    try:
        from PIL import Image, ImageChops
        import numpy as np
    except Exception as exc:
        return {
            "status": "FAIL",
            "reason": f"图像对比依赖缺失: {exc}",
            "metrics": {},
            "diff_screenshot_path": None,
        }

    reasons: List[str] = []
    status = "PASS"

    if candidate.get("has_crash_or_anr"):
        status = "FAIL"
        reasons.append("检测到 Crash/ANR 日志")
    required_text = str(page.get("required_text") or "").strip()
    if candidate.get("required_text_missing"):
        status = "FAIL"
        reasons.append(f"页面缺少必需文本: {required_text}")

    baseline_activity = _normalize_activity(str(baseline.get("activity") or ""))
    candidate_activity = _normalize_activity(str(candidate.get("activity") or ""))
    activity_mismatch = bool(
        baseline_activity and candidate_activity and baseline_activity != candidate_activity
    )
    if activity_mismatch:
        status = "FAIL"
        reasons.append(f"页面 Activity 与基准不一致: {candidate_activity} != {baseline_activity}")

    baseline_img = Image.open(
        io.BytesIO(
            _load_report_asset_bytes(
                baseline.get("screenshot_path"),
                asset_id=baseline.get("screenshot_asset_id"),
                session=session,
            )
        )
    ).convert("RGB")
    candidate_img = Image.open(
        io.BytesIO(
            _load_report_asset_bytes(
                candidate.get("screenshot_path"),
                asset_id=candidate.get("screenshot_asset_id"),
                session=session,
            )
        )
    ).convert("RGB")
    same_resolution = baseline_img.size == candidate_img.size
    compare_img = candidate_img if same_resolution else candidate_img.resize(baseline_img.size)

    diff = ImageChops.difference(baseline_img, compare_img)
    diff_arr = np.asarray(diff)
    changed = np.any(diff_arr > 24, axis=2)
    pixel_diff_ratio = float(np.count_nonzero(changed) / max(1, changed.size))
    mean_abs_diff = float(diff_arr.mean() / 255.0)
    visual_similarity = max(0.0, min(1.0, 1.0 - mean_abs_diff))

    try:
        from skimage.metrics import structural_similarity
        import cv2

        gray_a = cv2.cvtColor(np.asarray(baseline_img), cv2.COLOR_RGB2GRAY)
        gray_b = cv2.cvtColor(np.asarray(compare_img), cv2.COLOR_RGB2GRAY)
        ssim_score = float(structural_similarity(gray_a, gray_b))
    except Exception:
        ssim_score = visual_similarity

    baseline_xml = _normalize_xml(str(baseline.get("xml_text") or ""))
    candidate_xml = _normalize_xml(str(candidate.get("xml_text") or ""))
    xml_similarity = SequenceMatcher(None, baseline_xml, candidate_xml).ratio() if (baseline_xml or candidate_xml) else 1.0
    xml_diff_ratio = 1.0 - xml_similarity

    pixel_warn = float(thresholds.get("pixel_diff_ratio_warn", 0.03))
    ssim_warn = float(thresholds.get("ssim_warn", 0.96))
    xml_warn = float(thresholds.get("xml_diff_ratio_warn", 0.35))
    if status != "FAIL":
        if xml_diff_ratio > xml_warn:
            status = "WARNING"
            reasons.append("UI 层级与基准设备差异超过阈值")
        if same_resolution and (pixel_diff_ratio > pixel_warn or ssim_score < ssim_warn):
            status = "WARNING"
            reasons.append("视觉差异超过阈值")

    metrics = {
        "pixel_diff_ratio": round(pixel_diff_ratio, 6),
        "ssim": round(ssim_score, 6),
        "visual_similarity": round(visual_similarity, 6),
        "xml_diff_ratio": round(xml_diff_ratio, 6),
        "same_resolution": same_resolution,
        "has_crash_or_anr": bool(candidate.get("has_crash_or_anr")),
        "required_text_missing": bool(candidate.get("required_text_missing")),
        "activity_mismatch": activity_mismatch,
        "baseline_device_serial": str(baseline.get("device_serial") or ""),
    }

    diff_path = None
    if same_resolution and status != "PASS":
        overlay_arr = np.asarray(candidate_img).copy()
        overlay_arr[changed] = [255, 64, 64]
        diff_img = Image.fromarray(overlay_arr)
        diff_path = _store_png_bytes(
            project_path(
                "reports",
                "compatibility",
                str(run_id),
                str(cell_id),
                "diff",
                f"{page.get('key')}.png",
            ),
            _image_to_png_bytes(diff_img),
        )

    return {
        "status": status,
        "reason": "；".join(reasons) if reasons else None,
        "metrics": metrics,
        "diff_screenshot_path": diff_path,
    }


async def _run_page_capture(
    *,
    session: Session,
    run: CompatibilityRun,
    cell: CompatibilityCell,
    page: Dict[str, Any],
    phase: str,
    abort_event: threading.Event,
) -> Dict[str, Any]:
    case = session.get(TestCase, int(page.get("case_id") or 0))
    if not case:
        raise RuntimeError(f"页面用例不存在: {page.get('case_id')}")

    await _clear_logcat(cell.device_serial)
    result = await asyncio.to_thread(
        _run_case_for_capture,
        int(page.get("case_id") or 0),
        cell.device_serial,
        run.env_id,
        abort_event,
    )
    if not result.get("success"):
        raise RuntimeError("页面进入用例执行失败")

    settle = max(0, int(page.get("settle_seconds") or 0))
    if settle:
        await asyncio.sleep(settle)

    snapshot = await _capture_snapshot(
        serial=cell.device_serial,
        package_name=run.package_name,
        run_id=run.id,
        cell_id=cell.id,
        phase=phase,
        page=page,
    )
    return _persist_snapshot_assets(session, snapshot)


def _run_case_for_capture(
    case_id: int,
    serial: str,
    env_id: Optional[int],
    abort_event: threading.Event,
) -> Dict[str, Any]:
    from sqlmodel import Session as SQLSession

    with SQLSession(engine) as local_session:
        case = local_session.get(TestCase, case_id)
        if not case:
            raise RuntimeError(f"页面用例不存在: {case_id}")
        return run_case_with_standard_runner(
            session=local_session,
            case=case,
            device_serial=serial,
            env_id=env_id,
            abort_event=abort_event,
        )


async def _execute_cell(run_id: int, cell_id: int, pages: List[Dict[str, Any]], abort_event: threading.Event) -> None:
    from sqlmodel import Session as SQLSession

    lease: Optional[DeviceExecutionLease] = None
    registered_abort = False
    with SQLSession(engine) as session:
        run = session.get(CompatibilityRun, run_id)
        cell = session.get(CompatibilityCell, cell_id)
        if not run or not cell:
            return

        # Cancellation can race with BackgroundTasks starting a cell.  Keep
        # the initial transition and cancel endpoint under the same process
        # lock so a pending/aborted cell can never be resurrected as RUNNING.
        with _RUN_ABORT_LOCK:
            session.refresh(run)
            session.refresh(cell)
            if (
                abort_event.is_set()
                or str(run.status or "").upper() == "ABORTED"
                or str(cell.status or "").upper() == "ABORTED"
            ):
                return
            cell.status = "RUNNING"
            cell.current_stage = "准备设备"
            cell.started_at = _now()
            session.add(cell)
            if not run.started_at:
                run.started_at = _now()
            run.status = "RUNNING"
            session.add(run)
            session.commit()

        try:
            lease = await asyncio.to_thread(
                DeviceExecutionLease.acquire,
                user_id=int(run.user_id or 0),
                serial=cell.device_serial,
                task_id=f"compatibility:{run_id}:{cell_id}",
                kind="compatibility",
                abort_event=abort_event,
                db_engine=engine,
            )
            register_device_abort(cell.device_serial, abort_event)
            registered_abort = True
            if _is_cancelled(session, run_id, abort_event):
                raise asyncio.CancelledError()

            if run.compare_mode == "device":
                await _execute_cell_device_body(session, run, cell, pages, abort_event, run_id)
            else:
                await _execute_cell_version_body(session, run, cell, pages, abort_event, run_id)
        except asyncio.CancelledError:
            cell.status = "ABORTED"
            cell.current_stage = "已取消"
            cell.finished_at = _now()
            session.add(cell)
            session.commit()
        except Exception as exc:
            logger.exception("compatibility cell failed: run=%s cell=%s", run_id, cell_id)
            cell.status = "FAIL"
            cell.current_stage = "失败"
            cell.error_message = str(exc)
            if cell.new_install_status == "RUNNING":
                cell.new_install_status = "FAIL"
            if cell.old_install_status == "RUNNING":
                cell.old_install_status = "FAIL"
            cell.finished_at = _now()
            session.add(cell)
            session.commit()
        finally:
            if registered_abort:
                unregister_device_abort(cell.device_serial)
            if lease:
                try:
                    await asyncio.to_thread(lease.release)
                except Exception:
                    logger.exception(
                        "compatibility device lease release failed: %s",
                        cell.device_serial,
                    )
            _update_run_summary(session, run_id, final=False)


async def _execute_cell_version_body(
    session: Session,
    run: CompatibilityRun,
    cell: CompatibilityCell,
    pages: List[Dict[str, Any]],
    abort_event: threading.Event,
    run_id: int,
) -> None:
    """版本对比（纵向）：同设备安装旧版→采集基线→安装新版→采集并逐页对比。"""
    if run.old_package_id is None:
        cell.current_stage = "检查当前版本"
        cell.old_install_status = "SKIPPED"
        session.add(cell)
        session.commit()
        try:
            await _ensure_package_installed(cell.device_serial, run.package_name)
        except Exception:
            cell.old_install_status = "FAIL"
            session.add(cell)
            session.commit()
            raise
    else:
        cell.current_stage = "安装旧版本"
        cell.old_install_status = "RUNNING"
        session.add(cell)
        session.commit()
        await install_app_package_to_device(
            session=session,
            package_id=run.old_package_id,
            serial=cell.device_serial,
            require_idle=False,
            uninstall_first=True,
            allow_uninstall_retry=True,
            allow_downgrade=True,
        )
        cell.old_install_status = "PASS"
        session.add(cell)
        session.commit()

    baseline_by_key: Dict[str, Dict[str, Any]] = {}
    for page in pages:
        if _is_cancelled(session, run_id, abort_event):
            raise asyncio.CancelledError()
        cell.current_stage = f"采集旧版: {page.get('name')}"
        session.add(cell)
        session.commit()
        try:
            baseline_by_key[str(page.get("key"))] = await _run_page_capture(
                session=session,
                run=run,
                cell=cell,
                page=page,
                phase="baseline",
                abort_event=abort_event,
            )
        except Exception as exc:
            _record_capture_failure(session, run, cell, page, "baseline", exc)

    if _is_cancelled(session, run_id, abort_event):
        raise asyncio.CancelledError()

    cell.current_stage = "安装新版本"
    cell.new_install_status = "RUNNING"
    session.add(cell)
    session.commit()
    await install_app_package_to_device(
        session=session,
        package_id=run.new_package_id,
        serial=cell.device_serial,
        require_idle=False,
        uninstall_first=(run.mode == "clean"),
        allow_uninstall_retry=False,
        allow_downgrade=False,
    )
    cell.new_install_status = "PASS"
    session.add(cell)
    session.commit()

    for page in pages:
        if _is_cancelled(session, run_id, abort_event):
            raise asyncio.CancelledError()
        cell.current_stage = f"采集新版: {page.get('name')}"
        session.add(cell)
        session.commit()
        baseline = baseline_by_key.get(str(page.get("key")))
        try:
            candidate = await _run_page_capture(
                session=session,
                run=run,
                cell=cell,
                page=page,
                phase="candidate",
                abort_event=abort_event,
            )
            if not baseline:
                raise RuntimeError("旧版基线采集失败，无法对比")
            _record_compare_result(session, run, cell, page, baseline, candidate)
        except Exception as exc:
            _record_capture_failure(session, run, cell, page, "candidate", exc, baseline=baseline)

    page_rows = session.exec(
        select(CompatibilityPageResult).where(CompatibilityPageResult.cell_id == cell.id)
    ).all()
    statuses = {str(item.status or "").upper() for item in page_rows}
    if any(item in statuses for item in {"FAIL", "ERROR"}):
        cell.status = "FAIL"
    elif "WARNING" in statuses:
        cell.status = "WARNING"
    else:
        cell.status = "PASS"
    cell.current_stage = "完成"
    cell.finished_at = _now()
    session.add(cell)
    session.commit()


async def _execute_cell_device_body(
    session: Session,
    run: CompatibilityRun,
    cell: CompatibilityCell,
    pages: List[Dict[str, Any]],
    abort_event: threading.Event,
    run_id: int,
) -> None:
    """机型对比（横向）：单次安装测试包，每页采集一次并落 PENDING 页面行；横向对比在所有 cell 完成后统一进行。"""
    cell.current_stage = "安装测试包"
    cell.old_install_status = "SKIPPED"
    cell.new_install_status = "RUNNING"
    session.add(cell)
    session.commit()
    await install_app_package_to_device(
        session=session,
        package_id=run.new_package_id,
        serial=cell.device_serial,
        require_idle=False,
        uninstall_first=(run.mode == "clean"),
        allow_uninstall_retry=(run.mode == "clean"),
        allow_downgrade=True,
    )
    cell.new_install_status = "PASS"
    session.add(cell)
    session.commit()

    for page in pages:
        if _is_cancelled(session, run_id, abort_event):
            raise asyncio.CancelledError()
        cell.current_stage = f"采集页面: {page.get('name')}"
        session.add(cell)
        session.commit()
        try:
            snapshot = await _run_page_capture(
                session=session,
                run=run,
                cell=cell,
                page=page,
                phase="candidate",
                abort_event=abort_event,
            )
            _record_device_capture(session, run, cell, page, snapshot)
        except Exception as exc:
            _record_capture_failure(session, run, cell, page, "candidate", exc)

    # 采集阶段结束：cell 状态暂留 RUNNING，终态由 join 后的横向对比统一收敛
    cell.current_stage = "采集完成，等待横向对比"
    session.add(cell)
    session.commit()


def _store_diff_asset(session: Session, path: Optional[str]) -> Optional[str]:
    if not path or not content_addressed_assets_enabled(session):
        return None
    return store_file(session, _resolve_report_asset_path(path), commit=False).id


def _sync_page_asset_references(
    session: Session,
    row: CompatibilityPageResult,
    *,
    candidate_is_baseline: bool = False,
) -> None:
    if not content_addressed_assets_enabled(session) or row.id is None:
        return

    release_owner_references(
        session,
        owner_type="compatibility_page_result",
        owner_id=row.id,
        commit=False,
    )
    del candidate_is_baseline
    references = (
        (
            "baseline_screenshot",
            row.baseline_screenshot_asset_id,
            RETENTION_PINNED,
        ),
        ("baseline_xml", row.baseline_xml_asset_id, RETENTION_PINNED),
        (
            "candidate_screenshot",
            row.candidate_screenshot_asset_id,
            RETENTION_PINNED,
        ),
        ("candidate_xml", row.candidate_xml_asset_id, RETENTION_PINNED),
        ("diff_screenshot", row.diff_screenshot_asset_id, RETENTION_PINNED),
    )
    for role, asset_id, retention_class in references:
        if not asset_id:
            continue
        upsert_reference(
            session,
            asset_id=asset_id,
            owner_type="compatibility_page_result",
            owner_id=row.id,
            role=role,
            retention_class=retention_class,
            pinned_reason=(
                f"compatibility report evidence {row.id}"
                if retention_class == RETENTION_PINNED
                else None
            ),
            commit=False,
        )


def _record_capture_failure(
    session: Session,
    run: CompatibilityRun,
    cell: CompatibilityCell,
    page: Dict[str, Any],
    phase: str,
    exc: Exception,
    baseline: Optional[Dict[str, Any]] = None,
) -> None:
    existing = session.exec(
        select(CompatibilityPageResult)
        .where(
            CompatibilityPageResult.cell_id == cell.id,
            CompatibilityPageResult.page_key == str(page.get("key")),
        )
    ).first()
    row = existing or CompatibilityPageResult(
        run_id=run.id,
        cell_id=cell.id,
        page_key=str(page.get("key") or ""),
        page_name=str(page.get("name") or ""),
        case_id=_optional_page_case_id(page),
        required_text=page.get("required_text"),
    )
    row.status = "FAIL"
    row.reason = f"{phase} 采集失败: {exc}"
    if baseline:
        row.baseline_screenshot_path = baseline.get("screenshot_path")
        row.baseline_screenshot_asset_id = baseline.get("screenshot_asset_id")
        row.baseline_xml_path = baseline.get("xml_path")
        row.baseline_xml_asset_id = baseline.get("xml_asset_id")
        row.baseline_activity = baseline.get("activity")
    row.updated_at = _now()
    session.add(row)
    session.flush()
    _sync_page_asset_references(session, row)
    session.commit()


def _record_device_capture(
    session: Session,
    run: CompatibilityRun,
    cell: CompatibilityCell,
    page: Dict[str, Any],
    snapshot: Dict[str, Any],
) -> None:
    """机型对比模式：采集即落 PENDING 页面行，并持久化单机自检事实（Crash/必需文本），横向对比在 join 后统一进行。"""
    has_crash = bool(_CRASH_PATTERN.search(str(snapshot.get("logcat_errors") or "")))
    required_text = str(page.get("required_text") or "").strip()
    normalized_xml = _normalize_xml(str(snapshot.get("xml_text") or ""))
    required_text_missing = bool(required_text and required_text not in normalized_xml)

    existing = session.exec(
        select(CompatibilityPageResult)
        .where(
            CompatibilityPageResult.cell_id == cell.id,
            CompatibilityPageResult.page_key == str(page.get("key")),
        )
    ).first()
    row = existing or CompatibilityPageResult(
        run_id=run.id,
        cell_id=cell.id,
        page_key=str(page.get("key") or ""),
        page_name=str(page.get("name") or ""),
        case_id=_optional_page_case_id(page),
        required_text=page.get("required_text"),
    )
    row.status = "PENDING"
    row.reason = None
    row.candidate_screenshot_path = snapshot.get("screenshot_path")
    row.candidate_screenshot_asset_id = snapshot.get("screenshot_asset_id")
    row.candidate_xml_path = snapshot.get("xml_path")
    row.candidate_xml_asset_id = snapshot.get("xml_asset_id")
    row.candidate_activity = _normalize_activity(str(snapshot.get("activity") or ""))
    row.metrics = {
        "has_crash_or_anr": has_crash,
        "required_text_missing": required_text_missing,
        "resolution": cell.resolution or "",
    }
    row.updated_at = _now()
    session.add(row)
    session.flush()
    _sync_page_asset_references(
        session,
        row,
        candidate_is_baseline=bool(cell.is_baseline),
    )
    session.commit()


def _record_compare_result(
    session: Session,
    run: CompatibilityRun,
    cell: CompatibilityCell,
    page: Dict[str, Any],
    baseline: Dict[str, Any],
    candidate: Dict[str, Any],
) -> None:
    comparison = compare_page_snapshots(
        baseline=baseline,
        candidate=candidate,
        page=page,
        thresholds=run.thresholds or {},
        run_id=run.id,
        cell_id=cell.id,
        session=session,
    )
    row = session.exec(
        select(CompatibilityPageResult)
        .where(
            CompatibilityPageResult.cell_id == cell.id,
            CompatibilityPageResult.page_key == str(page.get("key")),
        )
    ).first()
    if row is None:
        row = CompatibilityPageResult(
            run_id=run.id,
            cell_id=cell.id,
            page_key=str(page.get("key") or ""),
            page_name=str(page.get("name") or ""),
            case_id=_optional_page_case_id(page),
            required_text=page.get("required_text"),
        )
    row.status = comparison.get("status") or "FAIL"
    row.reason = comparison.get("reason")
    row.baseline_screenshot_path = baseline.get("screenshot_path")
    row.baseline_screenshot_asset_id = baseline.get("screenshot_asset_id")
    row.candidate_screenshot_path = candidate.get("screenshot_path")
    row.candidate_screenshot_asset_id = candidate.get("screenshot_asset_id")
    row.diff_screenshot_path = comparison.get("diff_screenshot_path")
    row.diff_screenshot_asset_id = _store_diff_asset(
        session,
        row.diff_screenshot_path,
    )
    row.baseline_xml_path = baseline.get("xml_path")
    row.baseline_xml_asset_id = baseline.get("xml_asset_id")
    row.candidate_xml_path = candidate.get("xml_path")
    row.candidate_xml_asset_id = candidate.get("xml_asset_id")
    row.baseline_activity = baseline.get("activity")
    row.candidate_activity = candidate.get("activity")
    row.metrics = comparison.get("metrics") or {}
    row.updated_at = _now()
    session.add(row)
    session.flush()
    _sync_page_asset_references(session, row)
    session.commit()


def _execute_run_background(run_id: int, pages: List[Dict[str, Any]]) -> None:
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        loop.run_until_complete(_execute_run_async(run_id, pages))
    finally:
        loop.close()


async def _execute_run_async(run_id: int, pages: List[Dict[str, Any]]) -> None:
    from sqlmodel import Session as SQLSession

    abort_event = _abort_event_for_run(run_id)
    try:
        with SQLSession(engine) as session:
            run = session.get(CompatibilityRun, run_id)
            compare_mode = str((run.compare_mode if run else None) or "version")
            cells = session.exec(
                select(CompatibilityCell).where(CompatibilityCell.run_id == run_id)
            ).all()

        await asyncio.gather(*[
            _execute_cell(run_id, cell.id, pages, abort_event)
            for cell in cells
            if cell.id is not None
        ])

        if compare_mode == "device":
            await asyncio.to_thread(_run_cross_device_comparison, run_id, pages, abort_event)

        with SQLSession(engine) as session:
            _update_run_summary(session, run_id, final=True)
    except Exception as exc:
        logger.exception("compatibility run failed: %s", run_id)
        with SQLSession(engine) as session:
            run = session.get(CompatibilityRun, run_id)
            if run:
                # A cancellation request is authoritative even if a worker
                # raises while unwinding a device/lease operation.  Do not
                # turn an intentional ABORTED run into ERROR in the outer
                # exception handler.
                if str(run.status or "").upper() != "ABORTED":
                    run.status = "ERROR"
                    run.error_message = str(exc)
                run.finished_at = _now()
                session.add(run)
                session.commit()
    finally:
        _discard_abort_event(run_id)


def _baseline_snapshot_from_row(
    session: Session,
    cell: CompatibilityCell,
    row: CompatibilityPageResult,
) -> Dict[str, Any]:
    return {
        "screenshot_path": row.candidate_screenshot_path,
        "screenshot_asset_id": row.candidate_screenshot_asset_id,
        "xml_path": row.candidate_xml_path,
        "xml_asset_id": row.candidate_xml_asset_id,
        "xml_text": _load_report_text(
            row.candidate_xml_path,
            asset_id=row.candidate_xml_asset_id,
            session=session,
        ),
        "activity": row.candidate_activity or "",
        "device_serial": cell.device_serial,
    }


def _finalize_standalone_page_status(row: CompatibilityPageResult, *, is_baseline: bool) -> None:
    """依据采集期持久化的单机自检事实（Crash/必需文本）收敛页面状态。"""
    metrics = dict(row.metrics or {})
    reasons: List[str] = []
    status = "PASS"
    if metrics.get("has_crash_or_anr"):
        status = "FAIL"
        reasons.append("检测到 Crash/ANR 日志")
    if metrics.get("required_text_missing"):
        status = "FAIL"
        reasons.append(f"页面缺少必需文本: {row.required_text or ''}")
    if is_baseline:
        metrics["is_baseline"] = True
    row.status = status
    row.reason = "；".join(reasons) if reasons else None
    row.metrics = metrics
    row.updated_at = _now()


def _run_cross_device_comparison(run_id: int, pages: List[Dict[str, Any]], abort_event: threading.Event) -> None:
    """机型对比：所有 cell 采集完成后，非基准设备逐页与基准设备横向对比。"""
    from sqlmodel import Session as SQLSession

    with SQLSession(engine) as session:
        run = session.get(CompatibilityRun, run_id)
        if not run:
            return
        cells = session.exec(
            select(CompatibilityCell).where(CompatibilityCell.run_id == run_id).order_by(CompatibilityCell.id)
        ).all()
        baseline_cell = next((item for item in cells if item.is_baseline), None)
        if baseline_cell is None:
            baseline_cell = next(
                (item for item in cells if item.device_serial == run.baseline_device_serial), None
            )

        rows = session.exec(
            select(CompatibilityPageResult).where(CompatibilityPageResult.run_id == run_id)
        ).all()
        rows_by_cell_page: Dict[Tuple[int, str], CompatibilityPageResult] = {
            (item.cell_id, item.page_key): item for item in rows
        }

        cancelled = _is_cancelled(session, run_id, abort_event)

        # 先收敛基准设备自身页面状态（仅单机自检，不与他机对比）
        baseline_rows: Dict[str, CompatibilityPageResult] = {}
        if baseline_cell is not None:
            for page in pages:
                page_key = str(page.get("key") or "")
                row = rows_by_cell_page.get((baseline_cell.id, page_key))
                if row is None:
                    continue
                baseline_rows[page_key] = row
                if str(row.status or "").upper() == "PENDING" and not cancelled:
                    _finalize_standalone_page_status(row, is_baseline=True)
                    session.add(row)
                    session.flush()
                    _sync_page_asset_references(
                        session,
                        row,
                        candidate_is_baseline=True,
                    )
            session.commit()

        for cell in cells:
            if baseline_cell is not None and cell.id == baseline_cell.id:
                continue
            if not cancelled and _is_cancelled(session, run_id, abort_event):
                cancelled = True
            for page in pages:
                page_key = str(page.get("key") or "")
                row = rows_by_cell_page.get((cell.id, page_key))
                if row is None or str(row.status or "").upper() != "PENDING":
                    continue
                if cancelled:
                    continue

                cell_stage_owner = session.get(CompatibilityCell, cell.id)
                if cell_stage_owner and cell_stage_owner.status == "RUNNING":
                    cell_stage_owner.current_stage = f"横向对比: {page.get('name')}"
                    session.add(cell_stage_owner)
                    session.commit()

                baseline_row = baseline_rows.get(page_key)
                if (
                    baseline_cell is None
                    or baseline_row is None
                    or not (
                        baseline_row.candidate_screenshot_asset_id
                        or baseline_row.candidate_screenshot_path
                    )
                    or not (
                        baseline_row.candidate_xml_asset_id
                        or baseline_row.candidate_xml_path
                    )
                ):
                    row.status = "ERROR"
                    row.reason = "基准设备页面采集失败，无法横向对比"
                    row.updated_at = _now()
                    session.add(row)
                    session.flush()
                    _sync_page_asset_references(session, row)
                    session.commit()
                    continue

                baseline_snapshot = _baseline_snapshot_from_row(
                    session,
                    baseline_cell,
                    baseline_row,
                )
                capture_metrics = dict(row.metrics or {})
                candidate_snapshot = {
                    "screenshot_path": row.candidate_screenshot_path,
                    "screenshot_asset_id": row.candidate_screenshot_asset_id,
                    "xml_path": row.candidate_xml_path,
                    "xml_asset_id": row.candidate_xml_asset_id,
                    "xml_text": _load_report_text(
                        row.candidate_xml_path,
                        asset_id=row.candidate_xml_asset_id,
                        session=session,
                    ),
                    "activity": row.candidate_activity or "",
                    "has_crash_or_anr": bool(capture_metrics.get("has_crash_or_anr")),
                    "required_text_missing": bool(capture_metrics.get("required_text_missing")),
                }
                try:
                    comparison = compare_device_pages(
                        baseline=baseline_snapshot,
                        candidate=candidate_snapshot,
                        page=page,
                        thresholds=run.thresholds or {},
                        run_id=run_id,
                        cell_id=cell.id,
                        session=session,
                    )
                    row.status = comparison.get("status") or "FAIL"
                    row.reason = comparison.get("reason")
                    row.diff_screenshot_path = comparison.get("diff_screenshot_path")
                    row.diff_screenshot_asset_id = _store_diff_asset(
                        session,
                        row.diff_screenshot_path,
                    )
                    merged_metrics = dict(capture_metrics)
                    merged_metrics.update(comparison.get("metrics") or {})
                    row.metrics = merged_metrics
                except Exception as exc:
                    logger.exception(
                        "cross-device compare failed: run=%s cell=%s page=%s", run_id, cell.id, page_key
                    )
                    row.status = "FAIL"
                    row.reason = f"横向对比失败: {exc}"
                row.baseline_screenshot_path = baseline_row.candidate_screenshot_path
                row.baseline_screenshot_asset_id = (
                    baseline_row.candidate_screenshot_asset_id
                )
                row.baseline_xml_path = baseline_row.candidate_xml_path
                row.baseline_xml_asset_id = baseline_row.candidate_xml_asset_id
                row.baseline_activity = baseline_row.candidate_activity
                row.updated_at = _now()
                session.add(row)
                session.flush()
                _sync_page_asset_references(session, row)
                session.commit()

        # 收敛各 cell 终态（采集阶段结束时留 RUNNING，由此统一定级）
        for cell in cells:
            current = session.get(CompatibilityCell, cell.id)
            if not current or str(current.status or "").upper() not in {"PENDING", "RUNNING"}:
                continue
            if cancelled:
                current.status = "ABORTED"
                current.current_stage = "已取消"
            else:
                page_rows = session.exec(
                    select(CompatibilityPageResult).where(CompatibilityPageResult.cell_id == current.id)
                ).all()
                statuses = {str(item.status or "").upper() for item in page_rows}
                if not page_rows or any(item in statuses for item in {"FAIL", "ERROR"}):
                    current.status = "FAIL"
                    if not page_rows:
                        current.error_message = current.error_message or "未产生页面结果"
                elif "WARNING" in statuses:
                    current.status = "WARNING"
                elif "PENDING" in statuses:
                    current.status = "FAIL"
                    current.error_message = current.error_message or "横向对比未完成"
                else:
                    current.status = "PASS"
                current.current_stage = "完成"
            current.finished_at = current.finished_at or _now()
            session.add(current)
        session.commit()


@router.get("/page-sets", response_model=List[CompatPageSetRead])
def list_page_sets(
    session: Session = Depends(get_session),
    current_user: User = Depends(deps.get_current_user),
):
    rows = session.exec(select(CompatPageSet).order_by(CompatPageSet.created_at.desc())).all()
    return [_page_set_read(item) for item in rows]


@router.post("/page-sets", response_model=CompatPageSetRead)
def create_page_set(
    payload: CompatPageSetCreate,
    session: Session = Depends(get_session),
    current_user: User = Depends(deps.get_current_user),
):
    pages = _normalize_pages([dump_model(item) for item in payload.pages])
    row = CompatPageSet(
        name=payload.name,
        description=payload.description,
        pages=pages,
        user_id=current_user.id,
        updater_id=current_user.id,
        created_at=_now(),
        updated_at=_now(),
    )
    session.add(row)
    session.commit()
    session.refresh(row)
    return _page_set_read(row)


@router.put("/page-sets/{page_set_id}", response_model=CompatPageSetRead)
def update_page_set(
    page_set_id: int,
    payload: CompatPageSetUpdate,
    session: Session = Depends(get_session),
    current_user: User = Depends(deps.get_current_user),
):
    row = session.get(CompatPageSet, page_set_id)
    if not row:
        raise HTTPException(status_code=404, detail="页面集合不存在")
    row.name = payload.name
    row.description = payload.description
    row.pages = _normalize_pages([dump_model(item) for item in payload.pages])
    row.updater_id = current_user.id
    row.updated_at = _now()
    session.add(row)
    session.commit()
    session.refresh(row)
    return _page_set_read(row)


@router.delete("/page-sets/{page_set_id}")
def delete_page_set(
    page_set_id: int,
    session: Session = Depends(get_session),
    current_user: User = Depends(deps.get_current_user),
):
    row = session.get(CompatPageSet, page_set_id)
    if not row:
        raise HTTPException(status_code=404, detail="页面集合不存在")
    referenced_runs = session.exec(
        select(CompatibilityRun).where(CompatibilityRun.page_set_id == page_set_id)
    ).all()
    snapshot_pages = _normalize_pages(row.pages or [])
    for run in referenced_runs:
        run.page_set_name = run.page_set_name or row.name
        run.page_set_snapshot = run.page_set_snapshot or snapshot_pages
        run.page_set_id = None
        session.add(run)
    session.delete(row)
    session.commit()
    return {"success": True, "detached_runs": len(referenced_runs)}


@router.get("/runs", response_model=PaginatedCompatibilityRunRead)
def list_runs(
    skip: int = 0,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    keyword: Optional[str] = None,
    status: Optional[str] = None,
    session: Session = Depends(get_session),
    current_user: User = Depends(deps.get_current_user),
):
    """分页获取兼容性任务，keyword 匹配任务名/包名，status 支持 pass/warning/fail/running/all"""
    conditions = []
    if keyword:
        conditions.append(
            or_(
                col(CompatibilityRun.name).contains(keyword),
                col(CompatibilityRun.package_name).contains(keyword),
            )
        )
    normalized_status = str(status or "").strip().upper()
    if normalized_status and normalized_status != "ALL":
        if normalized_status == "RUNNING":
            conditions.append(col(CompatibilityRun.status).in_(["RUNNING", "PENDING"]))
        elif normalized_status == "FAIL":
            conditions.append(col(CompatibilityRun.status).in_(["FAIL", "ERROR"]))
        else:
            conditions.append(CompatibilityRun.status == normalized_status)

    count_query = select(func.count(col(CompatibilityRun.id)))
    query = select(CompatibilityRun)
    for condition in conditions:
        count_query = count_query.where(condition)
        query = query.where(condition)

    total = session.exec(count_query).one()
    rows = session.exec(
        query
        .order_by(col(CompatibilityRun.created_at).desc())
        .offset(skip)
        .limit(limit)
    ).all()
    page_set_ids = {
        int(item.page_set_id)
        for item in rows
        if item.page_set_id is not None
    }
    page_sets_by_id = {
        int(item.id): item
        for item in (
            session.exec(
                select(CompatPageSet).where(col(CompatPageSet.id).in_(page_set_ids))
            ).all()
            if page_set_ids
            else []
        )
        if item.id is not None
    }
    return PaginatedCompatibilityRunRead(
        total=total,
        items=[
            _run_read(
                session,
                item,
                include_detail=False,
                page_set=page_sets_by_id.get(int(item.page_set_id or 0)),
                page_set_loaded=True,
            )
            for item in rows
        ],
    )


@router.post("/runs", response_model=CompatibilityRunRead)
def create_run(
    payload: CompatibilityRunCreate,
    background_tasks: BackgroundTasks,
    session: Session = Depends(get_session),
    current_user: User = Depends(deps.get_current_user),
):
    try:
        ensure_asset_capacity_for_new_run(session)
    except AssetCapacityExceeded as exc:
        raise HTTPException(
            status_code=507,
            detail={
                "message": str(exc),
                "storage": exc.status,
            },
        ) from exc
    if payload.source_type != "page_set":
        raise HTTPException(
            status_code=400,
            detail="兼容性测试仅支持手动页面合集来源",
        )
    page_set = session.get(CompatPageSet, payload.page_set_id)
    if not page_set:
        raise HTTPException(status_code=404, detail="页面集合不存在")
    pages = _validate_page_set(session, page_set)
    old_pkg, new_pkg = _validate_packages(
        session,
        payload.old_package_id,
        int(payload.new_package_id or 0),
    )
    devices = _validate_devices(session, payload.device_serials)
    thresholds = dump_model(payload.thresholds) if payload.thresholds is not None else {}

    run = CompatibilityRun(
        name=payload.name,
        page_set_id=page_set.id,
        page_set_name=page_set.name,
        page_set_snapshot=pages,
        source_type="page_set",
        old_package_id=old_pkg.id if old_pkg else None,
        new_package_id=new_pkg.id,
        package_name=new_pkg.package_name,
        compare_mode=payload.compare_mode,
        baseline_device_serial=payload.baseline_device_serial,
        mode=payload.mode,
        env_id=payload.env_id,
        device_serials=payload.device_serials,
        thresholds=thresholds,
        status="PENDING",
        total_cells=len(devices),
        total_pages=0,
        user_id=current_user.id,
        executor_name=current_user.full_name or current_user.username,
        created_at=_now(),
    )
    session.add(run)
    session.commit()
    session.refresh(run)

    for device in devices:
        display = device.custom_name or device.market_name or device.model or device.serial
        session.add(
            CompatibilityCell(
                run_id=run.id,
                device_serial=device.serial,
                device_info=display,
                os_version=device.os_version or device.android_version,
                resolution=device.resolution,
                is_baseline=(
                    payload.compare_mode == "device"
                    and device.serial == payload.baseline_device_serial
                ),
                status="PENDING",
            )
        )
    session.commit()

    background_tasks.add_task(_execute_run_background, run.id, pages)
    return _run_read(session, run, include_detail=True)


@router.get("/runs/{run_id}", response_model=CompatibilityRunRead)
def get_run(
    run_id: int,
    session: Session = Depends(get_session),
    current_user: User = Depends(deps.get_current_user),
):
    row = session.get(CompatibilityRun, run_id)
    if not row:
        raise HTTPException(status_code=404, detail="兼容性任务不存在")
    return _run_read(session, row, include_detail=True)


@router.get("/runs/{run_id}/pages/{page_result_id}/diff")
def get_or_create_page_diff(
    run_id: int,
    page_result_id: int,
    session: Session = Depends(get_session),
    current_user: User = Depends(deps.get_current_user),
):
    del current_user
    run = session.get(CompatibilityRun, run_id)
    row = session.get(CompatibilityPageResult, page_result_id)
    if run is None or row is None or row.run_id != run_id:
        raise HTTPException(status_code=404, detail="兼容性页面结果不存在")
    return _get_or_create_page_diff(session, row)


@router.post("/runs/{run_id}/cancel")
def cancel_run(
    run_id: int,
    session: Session = Depends(get_session),
    current_user: User = Depends(deps.get_current_user),
):
    row = session.get(CompatibilityRun, run_id)
    if not row:
        raise HTTPException(status_code=404, detail="兼容性任务不存在")
    if str(row.status or "").upper() in TERMINAL_STATUSES:
        return {"success": True, "status": row.status}

    event = _abort_event_for_run(run_id)
    # Coordinate the DB transition with _execute_cell's startup transition;
    # otherwise a worker that has just loaded the row can commit RUNNING after
    # this endpoint commits ABORTED.
    with _RUN_ABORT_LOCK:
        event.set()
        row = session.get(CompatibilityRun, run_id)
        if row is None:
            raise HTTPException(status_code=404, detail="兼容性任务不存在")
        row.status = "ABORTED"
        row.finished_at = row.finished_at or _now()
        session.add(row)

        cells = session.exec(
            select(CompatibilityCell).where(
                CompatibilityCell.run_id == run_id,
                col(CompatibilityCell.status).in_(["PENDING", "RUNNING"]),
            )
        ).all()
        for cell in cells:
            if str(cell.status or "").upper() == "PENDING":
                cell.status = "ABORTED"
                cell.current_stage = "已取消"
                cell.finished_at = _now()
                session.add(cell)
        session.commit()
    return {"success": True, "status": "ABORTED"}


@router.delete("/runs/{run_id}")
def delete_run(
    run_id: int,
    session: Session = Depends(get_session),
    current_user: User = Depends(deps.get_current_user),
):
    row = session.get(CompatibilityRun, run_id)
    if not row:
        raise HTTPException(status_code=404, detail="兼容性任务不存在")
    if str(row.status or "").upper() in {"PENDING", "RUNNING"}:
        raise HTTPException(status_code=400, detail="运行中的兼容性任务无法删除")

    # ABORTED is a terminal *requested* state, but the background worker may
    # still be unwinding a device action and writing page results.  Deleting
    # here would race those writes and can leave orphaned assets/rows.  Wait
    # until every cell reaches a terminal state before allowing deletion.
    cells = session.exec(
        select(CompatibilityCell).where(CompatibilityCell.run_id == run_id)
    ).all()
    active_cells = [
        cell
        for cell in cells
        if str(cell.status or "").upper() not in TERMINAL_STATUSES
    ]
    if active_cells and str(row.status or "").upper() == "ABORTED":
        raise HTTPException(
            status_code=409,
            detail="兼容性任务后台执行尚未完全退出，稍后再删除",
        )

    try:
        artifacts_deleted = _delete_run_artifacts(run_id)
    except Exception as exc:
        logger.exception("delete compatibility run artifacts failed: %s", run_id)
        raise HTTPException(status_code=500, detail=f"删除兼容性报告文件失败: {exc}") from exc

    page_results = session.exec(
        select(CompatibilityPageResult).where(CompatibilityPageResult.run_id == run_id)
    ).all()
    cells = session.exec(
        select(CompatibilityCell).where(CompatibilityCell.run_id == run_id)
    ).all()
    for result in page_results:
        release_owner_references(
            session,
            owner_type="compatibility_page_result",
            owner_id=result.id,
            commit=False,
        )
        session.delete(result)
    for cell in cells:
        release_owner_references(
            session,
            owner_type="compatibility_cell",
            owner_id=cell.id,
            commit=False,
        )
        session.delete(cell)
    release_owner_references(
        session,
        owner_type="compatibility_run",
        owner_id=run_id,
        commit=False,
    )
    session.delete(row)
    session.commit()
    _discard_abort_event(run_id)
    return {
        "success": True,
        "deleted_pages": len(page_results),
        "deleted_cells": len(cells),
        "artifacts_deleted": artifacts_deleted,
    }
