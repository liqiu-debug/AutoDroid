"""
iOS 设备发现与管理模块

基于 tidevice 原生 Python API 实现 iOS 设备扫描、信息采集和 WDA 健康检查。
不依赖 subprocess 调用命令行，全部通过 tidevice 库的 Python 接口完成。
"""
import logging
from typing import Any, Dict, List, Optional

import requests
from tidevice import Device as TiDevice
from tidevice import Usbmux

logger = logging.getLogger(__name__)

# ==================== iPhone 机型映射表 ====================
# ProductType -> 市场名称（覆盖 iPhone 8 ~ iPhone 16 系列）
IPHONE_MODEL_MAP: Dict[str, str] = {
    # iPhone 8 / X 系列
    "iPhone10,1": "iPhone 8",
    "iPhone10,2": "iPhone 8 Plus",
    "iPhone10,3": "iPhone X",
    "iPhone10,4": "iPhone 8",
    "iPhone10,5": "iPhone 8 Plus",
    "iPhone10,6": "iPhone X",
    # iPhone XS / XR 系列
    "iPhone11,2": "iPhone XS",
    "iPhone11,4": "iPhone XS Max",
    "iPhone11,6": "iPhone XS Max",
    "iPhone11,8": "iPhone XR",
    # iPhone 11 系列
    "iPhone12,1": "iPhone 11",
    "iPhone12,3": "iPhone 11 Pro",
    "iPhone12,5": "iPhone 11 Pro Max",
    "iPhone12,8": "iPhone SE (2nd)",
    # iPhone 12 系列
    "iPhone13,1": "iPhone 12 mini",
    "iPhone13,2": "iPhone 12",
    "iPhone13,3": "iPhone 12 Pro",
    "iPhone13,4": "iPhone 12 Pro Max",
    # iPhone 13 系列
    "iPhone14,2": "iPhone 13 Pro",
    "iPhone14,3": "iPhone 13 Pro Max",
    "iPhone14,4": "iPhone 13 mini",
    "iPhone14,5": "iPhone 13",
    "iPhone14,6": "iPhone SE (3rd)",
    "iPhone14,7": "iPhone 14",
    "iPhone14,8": "iPhone 14 Plus",
    # iPhone 14 Pro 系列
    "iPhone15,2": "iPhone 14 Pro",
    "iPhone15,3": "iPhone 14 Pro Max",
    "iPhone15,4": "iPhone 15",
    "iPhone15,5": "iPhone 15 Plus",
    # iPhone 15 Pro 系列
    "iPhone16,1": "iPhone 15 Pro",
    "iPhone16,2": "iPhone 15 Pro Max",
    # iPhone 16 系列
    "iPhone17,1": "iPhone 16 Pro",
    "iPhone17,2": "iPhone 16 Pro Max",
    "iPhone17,3": "iPhone 16",
    "iPhone17,4": "iPhone 16 Plus",
    # iPad 常见型号（可按需扩充）
    "iPad13,18": "iPad (10th)",
    "iPad13,19": "iPad (10th)",
    "iPad14,3":  "iPad Pro 11-inch (4th)",
    "iPad14,4":  "iPad Pro 11-inch (4th)",
    "iPad14,5":  "iPad Pro 12.9-inch (6th)",
    "iPad14,6":  "iPad Pro 12.9-inch (6th)",
}


class IOSDeviceScanner:
    """
    iOS 设备扫描器

    通过 tidevice 的 Usbmux 协议枚举 USB 连接的 iOS/iPadOS 设备，
    采集设备名称、型号、系统版本等信息，并提供 WDA 运行状态检测。
    """

    def __init__(self, wda_port: int = 8100) -> None:
        """
        初始化扫描器。

        Args:
            wda_port: WebDriverAgent 默认监听端口，用于健康检查。
        """
        self._wda_port = wda_port

    # ------------------------------------------------------------------ #
    #  公开方法
    # ------------------------------------------------------------------ #

    def get_online_devices(self) -> List[Dict[str, Any]]:
        """
        扫描当前通过 USB 连接的所有 iOS 设备。

        Returns:
            统一格式的设备信息字典列表，每项包含：
            - device_id   : 设备 UDID
            - platform    : 固定为 "ios"
            - name        : 设备名称 (DeviceName)
            - model       : 市场友好型号名称
            - os_version  : iOS 系统版本
        """
        devices: List[Dict[str, Any]] = []

        try:
            usbmux = Usbmux()
            device_list = usbmux.device_list()
        except Exception as exc:
            logger.error("枚举 iOS 设备失败 (Usbmux 连接异常): %s", exc)
            return devices

        for dev_info in device_list:
            udid: str = dev_info.udid if hasattr(dev_info, "udid") else str(dev_info)
            try:
                info = self._fetch_device_info(udid)
                devices.append(info)
            except Exception as exc:
                logger.warning("采集设备 %s 信息失败，跳过: %s", udid, exc)

        logger.info("iOS 设备扫描完成，共发现 %d 台在线设备", len(devices))
        return devices

    def check_wda_status(self, udid: str, port: Optional[int] = None) -> Dict[str, Any]:
        """
        检测指定设备上的 WebDriverAgent 是否正常运行。

        通过访问 WDA 的 /status 端点判断服务是否可达。
        tidevice 会自动做 USB → TCP 的端口转发，因此直接请求 localhost 即可。

        Args:
            udid: 目标设备的 UDID。
            port: 可选，自定义 WDA 端口。不传则使用实例默认端口。

        Returns:
            包含检测结果的字典：
            - udid        : 设备 UDID
            - wda_running : bool，WDA 是否在线
            - session_id  : 当前活跃 Session ID（若有）
            - wda_version : WDA 构建信息（若有）
            - error       : 异常信息（若失败）
        """
        target_port = port or self._wda_port
        result: Dict[str, Any] = {
            "udid": udid,
            "wda_running": False,
            "session_id": None,
            "wda_version": None,
            "error": None,
        }

        try:
            resp = requests.get(
                f"http://localhost:{target_port}/status",
                timeout=5,
            )
            resp.raise_for_status()

            data = resp.json()
            # WDA /status 返回结构:  {"value": {"state": "success", ...}, "sessionId": "..."}
            value = data.get("value", {})
            result["wda_running"] = True
            result["session_id"] = data.get("sessionId")
            result["wda_version"] = value.get("build", {}).get("version")

            logger.info("设备 %s WDA 健康检查通过 (port=%d)", udid, target_port)

        except requests.ConnectionError:
            result["error"] = f"无法连接 WDA (localhost:{target_port})，请确认 WDA 已启动且端口转发正常"
            logger.warning("设备 %s WDA 连接失败: %s", udid, result["error"])

        except requests.Timeout:
            result["error"] = f"WDA 请求超时 (localhost:{target_port})"
            logger.warning("设备 %s WDA 请求超时", udid)

        except requests.HTTPError as exc:
            result["error"] = f"WDA 返回异常状态码: {exc.response.status_code}"
            logger.warning("设备 %s WDA HTTP 异常: %s", udid, exc)

        except (ValueError, KeyError) as exc:
            result["error"] = f"WDA 响应解析失败: {exc}"
            logger.warning("设备 %s WDA 响应格式异常: %s", udid, exc)

        return result

    # ------------------------------------------------------------------ #
    #  内部方法
    # ------------------------------------------------------------------ #

    def _fetch_device_info(self, udid: str) -> Dict[str, Any]:
        """
        通过 tidevice 的 lockdown 协议读取单台设备的详细信息。

        Args:
            udid: 设备 UDID。

        Returns:
            标准化的设备信息字典。

        Raises:
            RuntimeError: 设备连接或信息读取失败时抛出。
        """
        try:
            td = TiDevice(udid)
            raw_info: dict = td.device_info()
        except Exception as exc:
            raise RuntimeError(f"连接设备 {udid} 失败: {exc}") from exc

        product_type: str = raw_info.get("ProductType", "Unknown")
        friendly_model = IPHONE_MODEL_MAP.get(product_type, product_type)

        return {
            "device_id": udid,
            "platform": "ios",
            "name": raw_info.get("DeviceName", "Unknown"),
            "model": friendly_model,
            "os_version": raw_info.get("ProductVersion", "Unknown"),
        }

    @staticmethod
    def translate_model(product_type: str) -> str:
        """
        将 Apple 内部型号标识翻译为用户友好的名称。

        Args:
            product_type: Apple 内部 ProductType，如 'iPhone14,2'。

        Returns:
            市场名称，如 'iPhone 13 Pro'。无映射时原样返回。
        """
        return IPHONE_MODEL_MAP.get(product_type, product_type)


# ==================== 便捷入口 ====================

# 模块级单例，避免重复实例化
_scanner = IOSDeviceScanner()

def get_ios_devices() -> List[Dict[str, Any]]:
    """模块级快捷方法：获取所有在线 iOS 设备列表。"""
    return _scanner.get_online_devices()

def check_ios_wda(udid: str, port: Optional[int] = None) -> Dict[str, Any]:
    """模块级快捷方法：检查指定设备的 WDA 状态。"""
    return _scanner.check_wda_status(udid, port)
