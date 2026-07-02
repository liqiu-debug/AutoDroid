"""
执行限流器 API

提供限流器状态查询接口
"""
from fastapi import APIRouter, Depends
from backend.execution_limiter import get_execution_limiter
from backend.api import deps
from backend.models import User

router = APIRouter()


@router.get("/stats")
def get_limiter_stats(current_user: User = Depends(deps.get_current_user)):
    """
    获取执行限流器统计信息

    Returns:
        {
            "active_tasks": int,  # 当前活跃任务数
            "global_available": int,  # 全局可用槽位
            "active_users": int,  # 有活跃任务的用户数
            "active_devices": list[str],  # 正在使用的设备列表
            "max_global": int,  # 全局最大并发数
            "max_per_user": int,  # 每用户最大并发数
        }
    """
    limiter = get_execution_limiter()
    return limiter.get_stats()


@router.get("/device/{device_serial}/status")
def check_device_status(
    device_serial: str,
    current_user: User = Depends(deps.get_current_user)
):
    """
    检查设备是否正在被使用

    Returns:
        {
            "device_serial": str,
            "is_busy": bool,
            "owner_user_id": int | None,
        }
    """
    limiter = get_execution_limiter()
    is_busy = limiter.is_device_busy(device_serial)
    owner_id = limiter.get_device_owner(device_serial)

    return {
        "device_serial": device_serial,
        "is_busy": is_busy,
        "owner_user_id": owner_id,
    }
