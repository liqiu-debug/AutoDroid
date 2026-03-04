"""
APP 安装包管理 API

提供 APK 文件的上传、解析、列表查询、下载和删除功能。
"""
import os
import uuid
import logging
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, UploadFile, File, Query
from fastapi.responses import FileResponse
from pydantic import BaseModel
from sqlmodel import Session, select, col, func

from backend.database import get_session
from backend.models import AppPackage, User
from backend.schemas import AppPackageRead, PaginatedAppPackageRead
from backend.api.deps import get_current_user
from backend.utils.apk_parser import parse_apk_info

logger = logging.getLogger(__name__)

router = APIRouter()

# 存储目录
UPLOAD_DIR = os.path.join(os.path.dirname(__file__), "..", "..", "uploads", "apps")
os.makedirs(UPLOAD_DIR, exist_ok=True)


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
    saved_path = os.path.join(UPLOAD_DIR, saved_name)

    try:
        content = await file.read()
        with open(saved_path, "wb") as f:
            f.write(content)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"文件保存失败: {e}")

    file_size_mb = round(len(content) / (1024 * 1024), 2)

    # 3. 解析 APK 信息
    apk_info = parse_apk_info(saved_path)

    # 4. 将相同包名的旧包标记为 非最新
    if apk_info.get("package_name"):
        stmt = select(AppPackage).where(
            AppPackage.package_name == apk_info["package_name"],
            AppPackage.is_latest == True  # noqa: E712
        )
        old_packages = session.exec(stmt).all()
        for pkg in old_packages:
            pkg.is_latest = False
            session.add(pkg)

    # 5. 创建新记录
    new_package = AppPackage(
        app_name=apk_info.get("app_name", "Unknown"),
        package_name=apk_info.get("package_name", ""),
        version_name=apk_info.get("version_name", ""),
        version_code=apk_info.get("version_code", ""),
        file_path=saved_path,
        file_size=file_size_mb,
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

    if not os.path.exists(pkg.file_path):
        raise HTTPException(status_code=404, detail="文件已被删除")

    # 生成有意义的下载文件名
    download_name = f"{pkg.app_name}_{pkg.version_name}.apk"
    return FileResponse(
        path=pkg.file_path,
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
    if pkg.file_path and os.path.exists(pkg.file_path):
        try:
            os.remove(pkg.file_path)
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
    import asyncio

    # 1. 查询安装包
    pkg = session.get(AppPackage, package_id)
    if not pkg:
        raise HTTPException(status_code=404, detail="安装包不存在")

    if not os.path.exists(pkg.file_path):
        raise HTTPException(status_code=404, detail="APK 文件已被删除")

    # 2. 校验设备状态
    from backend.models import Device
    device = session.exec(
        select(Device).where(Device.serial == req.serial)
    ).first()

    if not device:
        raise HTTPException(status_code=404, detail=f"设备 {req.serial} 不存在")

    if device.status != "IDLE":
        raise HTTPException(
            status_code=400,
            detail=f"设备 {device.model} 当前状态为 {device.status}，无法安装"
        )

    # 3. 定义 ADB 执行辅助函数
    async def _run_adb(c, timeout=120):
        try:
            process = await asyncio.create_subprocess_shell(
                c,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(
                process.communicate(), timeout=timeout
            )
            return stdout.decode("utf-8", errors="replace") + stderr.decode("utf-8", errors="replace")
        except asyncio.TimeoutError:
            raise HTTPException(status_code=500, detail=f"与设备通信超时（可能设备已断开）")
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"执行命令失败: {e}")

    # 4. 执行 ADB 安装
    cmd = f'adb -s {req.serial} install -r -t -d "{pkg.file_path}"'
    logger.info(f"执行安装命令: {cmd}")
    output = await _run_adb(cmd)
    logger.info(f"ADB 安装输出: {output.strip()}")

    # 5. 降级失败备选方案：自动卸载并重试
    if "INSTALL_FAILED_VERSION_DOWNGRADE" in output and pkg.package_name:
        logger.warning(f"检测到系统拦截降级安装，准备自动卸载旧版本并重试 ({pkg.package_name})")
        uninstall_cmd = f'adb -s {req.serial} uninstall {pkg.package_name}'
        uninstall_output = await _run_adb(uninstall_cmd, timeout=30)
        logger.info(f"ADB 卸载输出: {uninstall_output.strip()}")
        
        logger.info(f"重新执行安装: {cmd}")
        output = await _run_adb(cmd)
        logger.info(f"重试安装输出: {output.strip()}")

    # 6. 解析最终结果 — adb install 即使失败也可能返回 exit code 0

    if "Success" in output:
        return {
            "success": True,
            "msg": f"{pkg.app_name} v{pkg.version_name} 安装成功"
        }
    else:
        # 提取 Failure [REASON] 格式的错误
        import re
        failure_match = re.search(r"Failure\s*\[([^\]]+)\]", output)
        error_reason = failure_match.group(1) if failure_match else output.strip()[-200:]
        raise HTTPException(
            status_code=500,
            detail=f"安装失败: {error_reason}"
        )
