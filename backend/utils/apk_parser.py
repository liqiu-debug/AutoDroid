"""
APK 解析工具：自动提取 APK 文件的包名、版本号、应用名等信息。

优先使用 androguard 解析，若不可用则降级使用 aapt 命令行。
"""
import re
import subprocess
import logging
from typing import Optional

logger = logging.getLogger(__name__)


def parse_apk_info(apk_path: str) -> dict:
    """
    解析 APK 文件信息。

    返回:
        {
            "package_name": "com.example.app",
            "version_name": "1.0.0",
            "version_code": "1",
            "app_name": "MyApp"
        }
    """
    # 尝试 androguard
    result = _parse_with_androguard(apk_path)
    if result:
        return result

    # 降级 aapt
    result = _parse_with_aapt(apk_path)
    if result:
        return result

    logger.warning(f"无法解析 APK: {apk_path}")
    return {
        "package_name": "",
        "version_name": "",
        "version_code": "",
        "app_name": "Unknown"
    }


def _parse_with_androguard(apk_path: str) -> Optional[dict]:
    """使用 androguard 解析 APK"""
    try:
        from androguard.core.apk import APK
        apk = APK(apk_path)
        return {
            "package_name": apk.get_package() or "",
            "version_name": apk.get_androidversion_name() or "",
            "version_code": str(apk.get_androidversion_code() or ""),
            "app_name": apk.get_app_name() or "Unknown"
        }
    except ImportError:
        logger.debug("androguard 未安装，尝试 aapt")
        return None
    except Exception as e:
        logger.warning(f"androguard 解析失败: {e}")
        return None


def _parse_with_aapt(apk_path: str) -> Optional[dict]:
    """使用 aapt dump badging 命令解析 APK"""
    try:
        output = subprocess.check_output(
            ["aapt", "dump", "badging", apk_path],
            stderr=subprocess.STDOUT,
            timeout=30
        ).decode("utf-8", errors="replace")

        package_name = ""
        version_name = ""
        version_code = ""
        app_name = "Unknown"

        # package: name='com.xxx' versionCode='1' versionName='1.0.0'
        pkg_match = re.search(
            r"package:\s+name='([^']+)'\s+versionCode='([^']*)'\s+.*?versionName='([^']*)'",
            output
        )
        if pkg_match:
            package_name = pkg_match.group(1)
            version_code = pkg_match.group(2)
            version_name = pkg_match.group(3)

        # application-label:'MyApp'
        label_match = re.search(r"application-label(?:-zh(?:-CN)?)?:'([^']+)'", output)
        if label_match:
            app_name = label_match.group(1)

        return {
            "package_name": package_name,
            "version_name": version_name,
            "version_code": version_code,
            "app_name": app_name
        }
    except FileNotFoundError:
        logger.debug("aapt 命令不可用")
        return None
    except subprocess.TimeoutExpired:
        logger.warning("aapt 执行超时")
        return None
    except Exception as e:
        logger.warning(f"aapt 解析失败: {e}")
        return None
