"""
设备管理 API - ADB 核心控制接口

提供设备同步、内存级截图、强制释放锁、重启等功能。
所有 ADB 命令通过 asyncio subprocess 异步执行。
"""
import asyncio
import base64
import logging
from datetime import datetime
from typing import List

from fastapi import APIRouter, Depends, HTTPException
from sqlmodel import Session, select

from backend.database import get_session
from backend.models import Device
from backend.schemas import DeviceRead, DeviceSyncResponse, DeviceRenameRequest
from backend.api.deps import get_current_user

logger = logging.getLogger(__name__)

router = APIRouter()


# ==================== ADB 异步工具函数 ====================

async def _run_adb_command(*args: str, timeout: int = 15) -> bytes:
    """异步执行 ADB 命令并返回 stdout 字节流"""
    cmd = ["adb"] + list(args)
    logger.info(f"执行 ADB 命令: {' '.join(cmd)}")
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(
            proc.communicate(), timeout=timeout
        )
        if proc.returncode != 0:
            err_msg = stderr.decode("utf-8", errors="replace").strip()
            logger.warning(f"ADB 命令失败 (rc={proc.returncode}): {err_msg}")
            raise RuntimeError(f"ADB error: {err_msg}")
        return stdout
    except asyncio.TimeoutError:
        logger.error(f"ADB 命令超时: {' '.join(cmd)}")
        raise RuntimeError("ADB command timed out")


async def _get_device_prop(serial: str, prop: str) -> str:
    """获取设备单个属性值"""
    try:
        raw = await _run_adb_command("-s", serial, "shell", "getprop", prop)
        return raw.decode("utf-8", errors="replace").strip()
    except Exception as e:
        logger.warning(f"获取 {serial} 属性 {prop} 失败: {e}")
        return ""


async def _get_device_resolution(serial: str) -> str:
    """获取设备分辨率"""
    try:
        raw = await _run_adb_command("-s", serial, "shell", "wm", "size")
        # 输出格式: "Physical size: 1080x2340"
        text = raw.decode("utf-8", errors="replace").strip()
        if ":" in text:
            return text.split(":")[-1].strip()
        return text
    except Exception:
        return ""


# ==================== API 端点 ====================


@router.get("/", response_model=List[DeviceRead])
async def list_devices(session: Session = Depends(get_session)):
    """获取所有设备列表,并实时检查在线状态"""
    devices = session.exec(select(Device).order_by(Device.status, Device.model)).all()
    
    # 实时检查哪些设备在线
    try:
        raw = await _run_adb_command("devices")
        lines = raw.decode("utf-8", errors="replace").strip().splitlines()
        online_serials = set()
        for line in lines[1:]:
            parts = line.strip().split()
            if len(parts) >= 2 and parts[1] == "device":
                online_serials.add(parts[0])
        
        # 更新设备状态
        status_updated = False
        for device in devices:
            if device.serial in online_serials:
                if device.status == "OFFLINE":
                    device.status = "IDLE"
                    device.updated_at = datetime.now()
                    session.add(device)
                    status_updated = True
            else:
                if device.status != "OFFLINE":
                    device.status = "OFFLINE"
                    device.updated_at = datetime.now()
                    session.add(device)
                    status_updated = True
        
        if status_updated:
            session.commit()
            # 重新查询以获取最新状态
            devices = session.exec(select(Device).order_by(Device.status, Device.model)).all()
    except Exception as e:
        logger.warning(f"检查设备在线状态失败: {e}")
    
    return devices


@router.put("/{serial}/name", response_model=DeviceRead)
def rename_device(
    serial: str,
    req: DeviceRenameRequest,
    session: Session = Depends(get_session),
    current_user=Depends(get_current_user),
):
    """修改设备自定义名称"""
    device = session.exec(select(Device).where(Device.serial == serial)).first()
    if not device:
        raise HTTPException(status_code=404, detail="设备不存在")
    device.custom_name = req.custom_name
    device.updated_at = datetime.now()
    session.add(device)
    session.commit()
    session.refresh(device)
    return device


@router.post("/sync", response_model=DeviceSyncResponse)
async def sync_devices(
    session: Session = Depends(get_session),
    current_user=Depends(get_current_user),
):
    """
    一键同步物理设备

    1. 执行 adb devices 获取在线 serial 列表
    2. 并发获取每个设备的型号、版本、分辨率
    3. 与数据库 Merge（新增/更新/标记离线）
    """
    # Step 1: 解析 adb devices 输出
    try:
        raw = await _run_adb_command("devices")
        lines = raw.decode("utf-8", errors="replace").strip().splitlines()
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"执行 adb devices 失败: {e}")

    online_serials = []
    for line in lines[1:]:  # 跳过 "List of devices attached" 标题行
        parts = line.strip().split()
        if len(parts) >= 2 and parts[1] == "device":
            online_serials.append(parts[0])

    # Step 2: 并发获取设备属性
    async def _fetch_device_info(serial: str) -> dict:
        # 1. 获取基础属性
        model, brand, version, resolution = await asyncio.gather(
            _get_device_prop(serial, "ro.product.model"),
            _get_device_prop(serial, "ro.product.brand"),
            _get_device_prop(serial, "ro.build.version.release"),
            _get_device_resolution(serial),
        )
        
        # 2. 根据厂商特性尝试获取市场名称
        market_name_props = [
            "ro.product.marketname",  # 通用
            "ro.vendor.oplus.market.name",  # OPPO / OnePlus
            "ro.vivo.market.name",  # vivo
            "ro.product.model", # 作为最低限度的 fallback，但我们会优先检查前面的
        ]
        
        market_name = ""
        for prop in market_name_props:
            val = await _get_device_prop(serial, prop)
            if val and val.lower() not in ["null", "unknown"]:
                market_name = val
                break
                
        # 如果 market_name 提取出来和 model 相同，或者提取失败，统一交给前端去 fallback 展示
        if not market_name or market_name == model:
            market_name = None

        return {
            "serial": serial,
            "model": model or "Unknown",
            "brand": (brand or "").upper(),
            "android_version": version,
            "resolution": resolution,
            "market_name": market_name,
        }

    device_infos = await asyncio.gather(
        *[_fetch_device_info(s) for s in online_serials]
    )

    # Step 3: Merge 到数据库
    synced_count = 0
    for info in device_infos:
        existing = session.exec(
            select(Device).where(Device.serial == info["serial"])
        ).first()
        if existing:
            existing.model = info["model"]
            existing.brand = info["brand"]
            existing.android_version = info["android_version"]
            existing.resolution = info["resolution"]
            existing.market_name = info["market_name"]
            existing.status = "IDLE" if existing.status == "OFFLINE" else existing.status
            existing.updated_at = datetime.now()
            session.add(existing)
        else:
            device = Device(
                serial=info["serial"],
                model=info["model"],
                brand=info["brand"],
                android_version=info["android_version"],
                resolution=info["resolution"],
                market_name=info["market_name"],
                status="IDLE",
            )
            session.add(device)
            synced_count += 1

    # 将不在物理列表中的设备标记为 OFFLINE
    all_db_devices = session.exec(select(Device)).all()
    for db_dev in all_db_devices:
        if db_dev.serial not in online_serials and db_dev.status != "OFFLINE":
            db_dev.status = "OFFLINE"
            db_dev.updated_at = datetime.now()
            session.add(db_dev)

    session.commit()

    # 查询最终结果
    all_devices = session.exec(select(Device).order_by(Device.status, Device.model)).all()
    total_offline = sum(1 for d in all_devices if d.status == "OFFLINE")

    return DeviceSyncResponse(
        synced=synced_count,
        online=len(online_serials),
        offline=total_offline,
        devices=all_devices,
    )


@router.get("/{serial}/screenshot")
async def get_screenshot(serial: str):
    """
    内存级屏幕快照

    执行 adb exec-out screencap -p，直接将 stdout 二进制流转为 Base64，
    全程不写磁盘，速度极快。
    """
    try:
        raw_bytes = await _run_adb_command(
            "-s", serial, "exec-out", "screencap", "-p",
            timeout=10,
        )
        if not raw_bytes or len(raw_bytes) < 100:
            raise RuntimeError("截图数据为空或异常")
        base64_img = base64.b64encode(raw_bytes).decode("utf-8")
        return {"base64_img": base64_img}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"截图失败: {e}")


@router.post("/{serial}/unlock")
async def unlock_device(
    serial: str,
    session: Session = Depends(get_session),
    current_user=Depends(get_current_user),
):
    """
    强制释放设备锁

    1. 杀掉设备上可能残留的自动化进程（Fastbot/Monkey/uiautomator2）
    2. 将数据库状态重置为 IDLE
    """
    device = session.exec(select(Device).where(Device.serial == serial)).first()
    if not device:
        raise HTTPException(status_code=404, detail="设备不存在")

    # 杀掉设备上可能残留的自动化进程
    # 注意: monkey 由 app_process 启动，am force-stop 无效，必须用 pkill
    kill_commands = [
        # Fastbot: 杀 monkey 主进程 + app_process 子进程
        f"adb -s {serial} shell pkill -f 'com.android.commands.monkey'",
        f"adb -s {serial} shell pkill -f 'app_process.*monkey'",
        # uiautomator2: 停止 ATX agent 服务
        f"adb -s {serial} shell am force-stop com.github.uiautomator",
        f"adb -s {serial} shell am force-stop com.github.uiautomator.test",
        f"adb -s {serial} shell pkill -f 'uiautomator'",
    ]
    for cmd in kill_commands:
        try:
            proc = await asyncio.create_subprocess_shell(
                cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            await asyncio.wait_for(proc.communicate(), timeout=5)
            logger.info(f"执行清理命令: {cmd.split('shell ')[-1]}")
        except Exception:
            pass  # 进程不存在时命令会失败，忽略

    # 同步清除 Fastbot 内存锁
    try:
        from backend.api.fastbot import _unlock_device as fastbot_unlock
        fastbot_unlock(serial)
    except Exception:
        pass

    # ★ 触发 Python 端中止信号（中断正在执行的 runner 线程）
    try:
        from backend.runner import trigger_device_abort
        trigger_device_abort(serial)
    except Exception:
        pass

    device.status = "IDLE"
    device.updated_at = datetime.now()
    session.add(device)
    session.commit()
    session.refresh(device)
    return {
        "message": f"设备 {serial} 已释放，已终止设备端残留进程",
        "device": DeviceRead.model_validate(device),
    }


@router.post("/{serial}/reboot")
async def reboot_device(
    serial: str,
    session: Session = Depends(get_session),
    current_user=Depends(get_current_user),
):
    """重启设备 - 执行 adb reboot 并标记为 OFFLINE"""
    device = session.exec(select(Device).where(Device.serial == serial)).first()
    if not device:
        raise HTTPException(status_code=404, detail="设备不存在")

    try:
        await _run_adb_command("-s", serial, "reboot", timeout=10)
    except Exception as e:
        logger.warning(f"重启命令执行异常（可能正常）: {e}")

    device.status = "OFFLINE"
    device.updated_at = datetime.now()
    session.add(device)
    session.commit()
    session.refresh(device)
    return {"message": f"设备 {serial} 正在重启", "device": DeviceRead.model_validate(device)}
