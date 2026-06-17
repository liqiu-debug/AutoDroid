"""
APP 安装包管理 API

提供 APK 文件的上传、解析、列表查询、下载和删除功能。
"""
import os
import shlex
import uuid
import logging
import asyncio
import re
import json
import shutil
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, UploadFile, File, Query
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from sqlmodel import Session, select, col, func

from backend.database import get_session
from backend.models import AppPackage, Device, User
from backend.paths import project_path, project_relative_path, resolve_project_path
from backend.schemas import AppPackageRead, PaginatedAppPackageRead
from backend.api.deps import get_current_user
from backend.utils.apk_parser import parse_apk_info

logger = logging.getLogger(__name__)

router = APIRouter()

# 存储目录
UPLOAD_DIR = project_path("uploads", "apps")
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
CHUNK_UPLOAD_DIR = UPLOAD_DIR / ".chunks"
CHUNK_UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
PACKAGE_UPLOAD_CHUNK_SIZE = 20 * 1024 * 1024
UPLOAD_SESSION_TTL_SECONDS = 24 * 60 * 60
UPLOAD_READ_SIZE = 1024 * 1024


class PackageUploadSessionCreate(BaseModel):
    filename: str
    file_size: int
    chunk_size: int
    total_chunks: int


class PackageUploadSessionRead(BaseModel):
    upload_id: str
    chunk_size: int
    total_chunks: int
    uploaded_chunks: List[int] = Field(default_factory=list)


class PackageChunkUploadRead(BaseModel):
    upload_id: str
    index: int
    received_bytes: int
    uploaded_chunks_count: int
    total_chunks: int


def _resolve_package_file_path(stored_path: str) -> Path:
    return resolve_project_path(stored_path, anchors=("uploads/apps",))


def _is_admin(user: User) -> bool:
    return (getattr(user, "role", "") or "").strip().lower() == "admin"


def _validate_upload_id(upload_id: str) -> None:
    if not re.fullmatch(r"[0-9a-f]{32}", upload_id or ""):
        raise HTTPException(status_code=404, detail="上传会话不存在")


def _upload_session_dir(upload_id: str) -> Path:
    _validate_upload_id(upload_id)
    return CHUNK_UPLOAD_DIR / upload_id


def _upload_session_meta_path(upload_id: str) -> Path:
    return _upload_session_dir(upload_id) / "meta.json"


def _uploaded_chunk_indexes(session_dir: Path) -> List[int]:
    indexes: List[int] = []
    for path in session_dir.glob("*.part"):
        try:
            indexes.append(int(path.stem))
        except ValueError:
            continue
    return sorted(indexes)


def _remove_path(path: Path) -> None:
    try:
        if path.is_dir():
            shutil.rmtree(path)
        elif path.exists():
            path.unlink()
    except Exception as exc:
        logger.warning("清理路径失败 %s: %s", path, exc)


def _cleanup_stale_upload_sessions() -> None:
    if not CHUNK_UPLOAD_DIR.exists():
        return

    now = time.time()
    for session_dir in CHUNK_UPLOAD_DIR.iterdir():
        if not session_dir.is_dir():
            continue
        try:
            if now - session_dir.stat().st_mtime > UPLOAD_SESSION_TTL_SECONDS:
                _remove_path(session_dir)
        except Exception as exc:
            logger.warning("检查过期上传会话失败 %s: %s", session_dir, exc)


def _load_upload_session(upload_id: str, current_user: User) -> Dict[str, Any]:
    session_dir = _upload_session_dir(upload_id)
    meta_path = session_dir / "meta.json"
    if not meta_path.exists():
        raise HTTPException(status_code=404, detail="上传会话不存在")

    try:
        with open(meta_path, "r", encoding="utf-8") as handle:
            meta = json.load(handle)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"上传会话损坏: {exc}") from exc

    owner_id = meta.get("user_id")
    if not _is_admin(current_user) and owner_id != current_user.id:
        raise HTTPException(status_code=403, detail="无权访问该上传会话")
    return meta


def _expected_total_chunks(file_size: int, chunk_size: int) -> int:
    return (file_size + chunk_size - 1) // chunk_size


def _validate_upload_session_payload(payload: PackageUploadSessionCreate) -> str:
    filename = os.path.basename((payload.filename or "").strip())
    if not filename.lower().endswith(".apk"):
        raise HTTPException(status_code=400, detail="仅支持 .apk 文件")
    if payload.file_size <= 0:
        raise HTTPException(status_code=400, detail="文件大小必须大于 0")
    if payload.chunk_size != PACKAGE_UPLOAD_CHUNK_SIZE:
        raise HTTPException(status_code=400, detail="分片大小必须为 20 MiB")

    expected_chunks = _expected_total_chunks(payload.file_size, payload.chunk_size)
    if payload.total_chunks != expected_chunks:
        raise HTTPException(status_code=400, detail="分片数量与文件大小不匹配")
    return filename


async def _write_upload_file(file: UploadFile, saved_path: Path) -> int:
    total_bytes = 0
    try:
        with open(saved_path, "wb") as handle:
            while True:
                chunk = await file.read(UPLOAD_READ_SIZE)
                if not chunk:
                    break
                total_bytes += len(chunk)
                handle.write(chunk)
    except Exception as exc:
        _remove_path(saved_path)
        raise HTTPException(status_code=500, detail=f"文件保存失败: {exc}") from exc
    return total_bytes


def _record_uploaded_package(
    *,
    saved_path: Path,
    file_size_bytes: int,
    session: Session,
    current_user: User,
) -> AppPackage:
    apk_info = parse_apk_info(str(saved_path))

    if apk_info.get("package_name"):
        stmt = select(AppPackage).where(
            AppPackage.package_name == apk_info["package_name"],
            AppPackage.is_latest == True  # noqa: E712
        )
        old_packages = session.exec(stmt).all()
        for pkg in old_packages:
            pkg.is_latest = False
            session.add(pkg)

    new_package = AppPackage(
        app_name=apk_info.get("app_name", "Unknown"),
        package_name=apk_info.get("package_name", ""),
        version_name=apk_info.get("version_name", ""),
        version_code=apk_info.get("version_code", ""),
        file_path=project_relative_path(saved_path, anchors=("uploads/apps",)),
        file_size=round(file_size_bytes / (1024 * 1024), 2),
        is_latest=True,
        uploader_id=current_user.id,
        uploader_name=current_user.full_name or current_user.username,
    )
    session.add(new_package)
    session.commit()
    session.refresh(new_package)

    logger.info(
        f"APK 上传成功: {apk_info.get('app_name')} "
        f"({apk_info.get('package_name')}) v{apk_info.get('version_name')}"
    )
    return new_package


def _ensure_android_install_device(device: Device) -> None:
    platform = str(device.platform or "android").strip().lower()
    if platform != "android":
        raise HTTPException(
            status_code=400,
            detail="P2002_ADB_ANDROID_ONLY: APK 安装仅支持 Android 设备，iOS 设备仅支持执行。",
        )


async def _run_adb_command(cmd: str, timeout: int = 120) -> str:
    try:
        process = await asyncio.create_subprocess_shell(
            cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(
            process.communicate(), timeout=timeout
        )
        return stdout.decode("utf-8", errors="replace") + stderr.decode("utf-8", errors="replace")
    except asyncio.TimeoutError as exc:
        raise RuntimeError("与设备通信超时（可能设备已断开）") from exc
    except Exception as exc:
        raise RuntimeError(f"执行命令失败: {exc}") from exc


def _parse_adb_install_error(output: str) -> str:
    failure_match = re.search(r"Failure\s*\[([^\]]+)\]", output or "")
    return failure_match.group(1) if failure_match else str(output or "").strip()[-200:]


async def install_app_package_to_device(
    *,
    session: Session,
    package_id: int,
    serial: str,
    require_idle: bool = True,
    uninstall_first: bool = False,
    allow_uninstall_retry: bool = True,
    allow_downgrade: bool = True,
) -> dict:
    """Install an uploaded APK onto an Android device and return install metadata."""
    pkg = session.get(AppPackage, package_id)
    if not pkg:
        raise HTTPException(status_code=404, detail="安装包不存在")

    file_path = _resolve_package_file_path(pkg.file_path)
    if not file_path.exists():
        raise HTTPException(status_code=404, detail="APK 文件已被删除")

    device = session.exec(select(Device).where(Device.serial == serial)).first()
    if not device:
        raise HTTPException(status_code=404, detail=f"设备 {serial} 不存在")

    _ensure_android_install_device(device)

    if require_idle and device.status != "IDLE":
        raise HTTPException(
            status_code=400,
            detail=f"设备 {device.model} 当前状态为 {device.status}，无法安装"
        )

    quoted_serial = shlex.quote(serial)
    if uninstall_first and pkg.package_name:
        uninstall_cmd = f"adb -s {quoted_serial} uninstall {shlex.quote(pkg.package_name)}"
        logger.info("兼容性/包管理执行卸载命令: %s", uninstall_cmd)
        try:
            await _run_adb_command(uninstall_cmd, timeout=45)
        except RuntimeError as exc:
            logger.warning("卸载旧包失败但继续安装: serial=%s package=%s error=%s", serial, pkg.package_name, exc)

    install_flags = "-r -t"
    if allow_downgrade:
        install_flags += " -d"
    cmd = f"adb -s {quoted_serial} install {install_flags} {shlex.quote(str(file_path))}"
    logger.info("执行安装命令: %s", cmd)
    output = await _run_adb_command(cmd)
    logger.info("ADB 安装输出: %s", output.strip())

    retried_after_uninstall = False
    if (
        allow_uninstall_retry
        and "INSTALL_FAILED_VERSION_DOWNGRADE" in output
        and pkg.package_name
    ):
        retried_after_uninstall = True
        logger.warning("检测到系统拦截降级安装，准备自动卸载旧版本并重试 (%s)", pkg.package_name)
        uninstall_cmd = f"adb -s {quoted_serial} uninstall {shlex.quote(pkg.package_name)}"
        uninstall_output = await _run_adb_command(uninstall_cmd, timeout=30)
        logger.info("ADB 卸载输出: %s", uninstall_output.strip())

        logger.info("重新执行安装: %s", cmd)
        output = await _run_adb_command(cmd)
        logger.info("重试安装输出: %s", output.strip())

    if "Success" not in output:
        raise HTTPException(
            status_code=500,
            detail=f"安装失败: {_parse_adb_install_error(output)}"
        )

    return {
        "success": True,
        "msg": f"{pkg.app_name} v{pkg.version_name} 安装成功",
        "package_id": pkg.id,
        "package_name": pkg.package_name,
        "version_name": pkg.version_name,
        "version_code": pkg.version_code,
        "output": output,
        "retried_after_uninstall": retried_after_uninstall,
    }


@router.post("/upload", response_model=AppPackageRead, summary="上传 APK 文件")
async def upload_package(
    file: UploadFile = File(...),
    session: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
):
    """
    接收 APK 文件，保存到本地并自动解析包名、版本号等元数据。
    同一包名的旧版本会自动标记为非最新。
    """
    # 1. 校验文件类型
    if not file.filename or not file.filename.lower().endswith(".apk"):
        raise HTTPException(status_code=400, detail="仅支持 .apk 文件")

    # 2. 保存文件 (UUID 重命名防冲突)
    ext = os.path.splitext(file.filename)[1]
    saved_name = f"{uuid.uuid4().hex}{ext}"
    saved_path = UPLOAD_DIR / saved_name

    file_size_bytes = await _write_upload_file(file, saved_path)
    return _record_uploaded_package(
        saved_path=saved_path,
        file_size_bytes=file_size_bytes,
        session=session,
        current_user=current_user,
    )


@router.post(
    "/upload-sessions",
    response_model=PackageUploadSessionRead,
    summary="创建 APK 分片上传会话",
)
def create_package_upload_session(
    payload: PackageUploadSessionCreate,
    current_user: User = Depends(get_current_user),
):
    filename = _validate_upload_session_payload(payload)
    _cleanup_stale_upload_sessions()

    upload_id = uuid.uuid4().hex
    session_dir = _upload_session_dir(upload_id)
    session_dir.mkdir(parents=True, exist_ok=False)

    meta = {
        "upload_id": upload_id,
        "filename": filename,
        "file_size": payload.file_size,
        "chunk_size": payload.chunk_size,
        "total_chunks": payload.total_chunks,
        "user_id": current_user.id,
        "created_at": time.time(),
    }

    try:
        with open(_upload_session_meta_path(upload_id), "w", encoding="utf-8") as handle:
            json.dump(meta, handle, ensure_ascii=False, indent=2)
    except Exception as exc:
        _remove_path(session_dir)
        raise HTTPException(status_code=500, detail=f"创建上传会话失败: {exc}") from exc

    return PackageUploadSessionRead(
        upload_id=upload_id,
        chunk_size=payload.chunk_size,
        total_chunks=payload.total_chunks,
        uploaded_chunks=[],
    )


@router.post(
    "/upload-sessions/{upload_id}/chunks/{index}",
    response_model=PackageChunkUploadRead,
    summary="上传 APK 分片",
)
async def upload_package_chunk(
    upload_id: str,
    index: int,
    file: UploadFile = File(...),
    current_user: User = Depends(get_current_user),
):
    meta = _load_upload_session(upload_id, current_user)
    total_chunks = int(meta["total_chunks"])
    if index < 0 or index >= total_chunks:
        raise HTTPException(status_code=400, detail="分片索引越界")

    file_size = int(meta["file_size"])
    chunk_size = int(meta["chunk_size"])
    expected_max_size = min(chunk_size, file_size - index * chunk_size)
    if expected_max_size <= 0:
        raise HTTPException(status_code=400, detail="分片索引越界")

    session_dir = _upload_session_dir(upload_id)
    part_path = session_dir / f"{index}.part"
    temp_path = session_dir / f"{index}.part.tmp"
    received_bytes = 0

    try:
        with open(temp_path, "wb") as handle:
            while True:
                chunk = await file.read(UPLOAD_READ_SIZE)
                if not chunk:
                    break
                received_bytes += len(chunk)
                if received_bytes > expected_max_size:
                    raise HTTPException(status_code=400, detail="分片大小超过限制")
                handle.write(chunk)
        if received_bytes <= 0:
            raise HTTPException(status_code=400, detail="分片内容不能为空")
        os.replace(temp_path, part_path)
    except HTTPException:
        _remove_path(temp_path)
        raise
    except Exception as exc:
        _remove_path(temp_path)
        raise HTTPException(status_code=500, detail=f"分片保存失败: {exc}") from exc

    uploaded_chunks = _uploaded_chunk_indexes(session_dir)
    return PackageChunkUploadRead(
        upload_id=upload_id,
        index=index,
        received_bytes=received_bytes,
        uploaded_chunks_count=len(uploaded_chunks),
        total_chunks=total_chunks,
    )


@router.post(
    "/upload-sessions/{upload_id}/complete",
    response_model=AppPackageRead,
    summary="完成 APK 分片上传",
)
def complete_package_upload(
    upload_id: str,
    session: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
):
    meta = _load_upload_session(upload_id, current_user)
    session_dir = _upload_session_dir(upload_id)
    file_size = int(meta["file_size"])
    total_chunks = int(meta["total_chunks"])
    chunk_size = int(meta["chunk_size"])

    part_paths: List[Path] = []
    total_bytes = 0
    missing_indexes: List[int] = []
    for index in range(total_chunks):
        part_path = session_dir / f"{index}.part"
        if not part_path.exists():
            missing_indexes.append(index)
            continue
        part_size = part_path.stat().st_size
        expected_max_size = min(chunk_size, file_size - index * chunk_size)
        if part_size <= 0 or part_size > expected_max_size:
            raise HTTPException(status_code=400, detail=f"分片 {index} 大小不匹配")
        total_bytes += part_size
        part_paths.append(part_path)

    if missing_indexes:
        preview = ", ".join(str(i) for i in missing_indexes[:5])
        raise HTTPException(status_code=400, detail=f"缺少分片: {preview}")
    if total_bytes != file_size:
        raise HTTPException(status_code=400, detail="分片总大小与文件大小不匹配")

    saved_path = UPLOAD_DIR / f"{uuid.uuid4().hex}.apk"
    try:
        with open(saved_path, "wb") as output:
            for part_path in part_paths:
                with open(part_path, "rb") as source:
                    shutil.copyfileobj(source, output, length=UPLOAD_READ_SIZE)
        if saved_path.stat().st_size != file_size:
            _remove_path(saved_path)
            raise HTTPException(status_code=400, detail="合并后的文件大小不匹配")

        new_package = _record_uploaded_package(
            saved_path=saved_path,
            file_size_bytes=file_size,
            session=session,
            current_user=current_user,
        )
    except HTTPException:
        _remove_path(saved_path)
        raise
    except Exception as exc:
        _remove_path(saved_path)
        raise HTTPException(status_code=500, detail=f"完成上传失败: {exc}") from exc

    _remove_path(session_dir)
    return new_package


@router.delete("/upload-sessions/{upload_id}", summary="取消 APK 分片上传")
def cancel_package_upload(
    upload_id: str,
    current_user: User = Depends(get_current_user),
):
    _load_upload_session(upload_id, current_user)
    _remove_path(_upload_session_dir(upload_id))
    return {"message": "已取消上传"}


@router.get("/", response_model=PaginatedAppPackageRead, summary="获取安装包列表")
def list_packages(
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    keyword: Optional[str] = Query(None, description="按应用名/包名搜索"),
    session: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
):
    """分页返回安装包列表，按上传时间倒序排列。"""
    query = select(AppPackage)

    if keyword:
        query = query.where(
            (col(AppPackage.app_name).contains(keyword))
            | (col(AppPackage.package_name).contains(keyword))
        )

    # 总数
    count_query = select(func.count()).select_from(query.subquery())
    total = session.exec(count_query).one()

    # 分页
    items = session.exec(
        query.order_by(col(AppPackage.upload_time).desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
    ).all()

    return PaginatedAppPackageRead(total=total, items=items)


@router.get("/{package_id}/download", summary="下载安装包")
def download_package(
    package_id: int,
    session: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
):
    """根据 ID 下载 APK 文件。"""
    pkg = session.get(AppPackage, package_id)
    if not pkg:
        raise HTTPException(status_code=404, detail="安装包不存在")

    file_path = _resolve_package_file_path(pkg.file_path)
    if not file_path.exists():
        raise HTTPException(status_code=404, detail="文件已被删除")

    # 生成有意义的下载文件名
    download_name = f"{pkg.app_name}_{pkg.version_name}.apk"
    return FileResponse(
        path=str(file_path),
        filename=download_name,
        media_type="application/vnd.android.package-archive",
    )


@router.delete("/{package_id}", summary="删除安装包")
def delete_package(
    package_id: int,
    session: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
):
    """删除安装包记录及对应的文件。"""
    pkg = session.get(AppPackage, package_id)
    if not pkg:
        raise HTTPException(status_code=404, detail="安装包不存在")

    # 删除物理文件
    file_path = _resolve_package_file_path(pkg.file_path) if pkg.file_path else None
    if file_path and file_path.exists():
        try:
            file_path.unlink()
        except Exception as e:
            logger.warning(f"删除文件失败 {pkg.file_path}: {e}")

    # 如果删除的是最新包，则将同包名的最近一个设为最新
    if pkg.is_latest and pkg.package_name:
        next_latest = session.exec(
            select(AppPackage)
            .where(
                AppPackage.package_name == pkg.package_name,
                AppPackage.id != pkg.id,
            )
            .order_by(col(AppPackage.upload_time).desc())
            .limit(1)
        ).first()
        if next_latest:
            next_latest.is_latest = True
            session.add(next_latest)

    session.delete(pkg)
    session.commit()

    return {"message": "删除成功"}


class InstallRequest(BaseModel):
    serial: str


@router.post("/{package_id}/install", summary="安装到设备")
async def install_package(
    package_id: int,
    req: InstallRequest,
    session: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
):
    """
    通过 ADB 将 APK 推送安装到指定设备。
    使用 -r (覆盖安装) -t (允许测试包) 参数。
    """
    try:
        return await install_app_package_to_device(
            session=session,
            package_id=package_id,
            serial=req.serial,
            require_idle=True,
            uninstall_first=False,
            allow_uninstall_retry=True,
            allow_downgrade=True,
        )
    except RuntimeError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
