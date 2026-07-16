"""IPA metadata, provisioning-profile validation, and safe app extraction."""

from __future__ import annotations

import fnmatch
import plistlib
import re
import shutil
import stat
import subprocess
import tempfile
import zipfile
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Dict, List, Optional, Tuple


MAX_IPA_ENTRY_BYTES = 32 * 1024 * 1024
MAX_IPA_UNCOMPRESSED_BYTES = 8 * 1024 * 1024 * 1024
MAX_IPA_ENTRIES = 200_000


class IpaParseError(ValueError):
    """Raised when an IPA cannot be safely parsed or installed."""


def _validate_archive(zf: zipfile.ZipFile) -> Tuple[str, List[zipfile.ZipInfo]]:
    infos = zf.infolist()
    if not infos or len(infos) > MAX_IPA_ENTRIES:
        raise IpaParseError("IPA 文件为空或文件数量异常")

    total_size = 0
    info_plists: List[str] = []
    for info in infos:
        name = str(info.filename or "")
        if not name or "\\" in name or "\x00" in name:
            raise IpaParseError("IPA 包含非法文件路径")
        path = PurePosixPath(name)
        if path.is_absolute() or ".." in path.parts:
            raise IpaParseError("IPA 包含越界文件路径")

        mode = (info.external_attr >> 16) & 0xFFFF
        if stat.S_ISLNK(mode):
            raise IpaParseError("IPA 包含不支持的符号链接")

        total_size += max(0, int(info.file_size or 0))
        if total_size > MAX_IPA_UNCOMPRESSED_BYTES:
            raise IpaParseError("IPA 解压后体积超过安全限制")

        parts = path.parts
        if (
            len(parts) == 3
            and parts[0] == "Payload"
            and parts[1].endswith(".app")
            and parts[2] == "Info.plist"
        ):
            info_plists.append(name)

    if len(info_plists) != 1:
        raise IpaParseError("IPA 必须且只能包含一个 Payload 主应用")
    return info_plists[0], infos


def _read_small_entry(zf: zipfile.ZipFile, name: str) -> bytes:
    try:
        info = zf.getinfo(name)
    except KeyError as exc:
        raise IpaParseError(f"IPA 缺少必要文件: {name}") from exc
    if info.file_size <= 0 or info.file_size > MAX_IPA_ENTRY_BYTES:
        raise IpaParseError(f"IPA 元数据文件大小异常: {name}")
    return zf.read(info)


def _decode_mobileprovision(data: bytes) -> Dict[str, Any]:
    profile_path: Optional[str] = None
    try:
        with tempfile.NamedTemporaryFile(suffix=".mobileprovision", delete=False) as handle:
            handle.write(data)
            profile_path = handle.name
        proc = subprocess.run(
            ["/usr/bin/security", "cms", "-D", "-i", profile_path],
            check=False,
            capture_output=True,
            timeout=15,
        )
        if proc.returncode != 0:
            detail = proc.stderr.decode("utf-8", errors="replace").strip()
            raise IpaParseError(f"无法解析 IPA 描述文件: {detail or 'security cms 失败'}")
        payload = plistlib.loads(proc.stdout)
        if not isinstance(payload, dict):
            raise IpaParseError("IPA 描述文件格式无效")
        return payload
    except subprocess.TimeoutExpired as exc:
        raise IpaParseError("解析 IPA 描述文件超时") from exc
    except plistlib.InvalidFileException as exc:
        raise IpaParseError("IPA 描述文件不是有效的 plist") from exc
    finally:
        if profile_path:
            try:
                Path(profile_path).unlink()
            except FileNotFoundError:
                pass


def _as_utc(value: Any) -> Optional[datetime]:
    if not isinstance(value, datetime):
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _signing_type(profile: Dict[str, Any]) -> str:
    if bool(profile.get("ProvisionsAllDevices")):
        return "enterprise"
    devices = profile.get("ProvisionedDevices") or []
    if devices:
        entitlements = profile.get("Entitlements") or {}
        return "development" if bool(entitlements.get("get-task-allow")) else "adhoc"
    return "appstore"


def _version_tuple(value: str) -> Tuple[int, ...]:
    parts = [int(item) for item in re.findall(r"\d+", str(value or ""))]
    return tuple(parts or [0])


def _profile_matches_bundle(profile: Dict[str, Any], bundle_id: str) -> bool:
    entitlements = profile.get("Entitlements") or {}
    application_identifier = str(
        entitlements.get("application-identifier")
        or entitlements.get("com.apple.application-identifier")
        or ""
    ).strip()
    if not application_identifier:
        return True
    _, separator, profile_bundle_id = application_identifier.partition(".")
    if not separator or not profile_bundle_id:
        return True
    return fnmatch.fnmatchcase(bundle_id, profile_bundle_id)


def parse_ipa_info(ipa_path: str) -> Dict[str, Any]:
    """Parse a sideloadable Ad Hoc IPA and return normalized package metadata."""
    try:
        with zipfile.ZipFile(ipa_path, "r") as zf:
            info_plist_name, _ = _validate_archive(zf)
            try:
                info = plistlib.loads(_read_small_entry(zf, info_plist_name))
            except plistlib.InvalidFileException as exc:
                raise IpaParseError("IPA Info.plist 格式无效") from exc
            if not isinstance(info, dict):
                raise IpaParseError("IPA Info.plist 内容无效")

            app_root = str(PurePosixPath(info_plist_name).parent)
            profile_name = f"{app_root}/embedded.mobileprovision"
            try:
                profile_data = _read_small_entry(zf, profile_name)
            except IpaParseError as exc:
                raise IpaParseError("IPA 缺少 embedded.mobileprovision，可能是 App Store 包") from exc
            profile = _decode_mobileprovision(profile_data)
    except zipfile.BadZipFile as exc:
        raise IpaParseError("IPA 文件不是有效的 ZIP 归档") from exc
    except OSError as exc:
        raise IpaParseError(f"无法读取 IPA 文件: {exc}") from exc

    bundle_id = str(info.get("CFBundleIdentifier") or "").strip()
    if not bundle_id:
        raise IpaParseError("IPA 缺少 CFBundleIdentifier")
    if not _profile_matches_bundle(profile, bundle_id):
        raise IpaParseError("IPA Bundle ID 与描述文件不匹配")

    signing_type = _signing_type(profile)
    if signing_type != "adhoc":
        labels = {
            "development": "Development",
            "enterprise": "Enterprise/In-House",
            "appstore": "App Store",
        }
        raise IpaParseError(f"当前仅支持 Ad Hoc IPA，检测到 {labels.get(signing_type, signing_type)} 签名")

    expiration = _as_utc(profile.get("ExpirationDate"))
    if not expiration:
        raise IpaParseError("IPA 描述文件缺少有效期")
    if expiration <= datetime.now(timezone.utc):
        raise IpaParseError("IPA 描述文件已过期，请重新打包")

    app_dir_name = PurePosixPath(info_plist_name).parent.name
    fallback_app_name = app_dir_name[:-4] if app_dir_name.endswith(".app") else app_dir_name
    app_name = str(
        info.get("CFBundleDisplayName")
        or info.get("CFBundleName")
        or fallback_app_name
        or "Unknown"
    ).strip()

    return {
        "app_name": app_name or "Unknown",
        "package_name": bundle_id,
        "version_name": str(info.get("CFBundleShortVersionString") or "").strip(),
        "version_code": str(info.get("CFBundleVersion") or "").strip(),
        "minimum_os_version": str(info.get("MinimumOSVersion") or "").strip(),
        "signing_type": signing_type,
        "provisioning_expiration": expiration,
        "provisioned_devices": [str(item) for item in profile.get("ProvisionedDevices") or []],
        "app_root": str(PurePosixPath(info_plist_name).parent),
    }


def validate_ipa_for_device(ipa_path: str, udid: str, os_version: str = "") -> Dict[str, Any]:
    """Validate an uploaded Ad Hoc IPA against one target device."""
    metadata = parse_ipa_info(ipa_path)
    allowed_devices = set(metadata.get("provisioned_devices") or [])
    if str(udid or "").strip() not in allowed_devices:
        raise IpaParseError("目标设备 UDID 未包含在 IPA 的 Ad Hoc 描述文件中")

    minimum_os = str(metadata.get("minimum_os_version") or "").strip()
    installed_os = str(os_version or "").strip()
    if minimum_os and installed_os and _version_tuple(installed_os) < _version_tuple(minimum_os):
        raise IpaParseError(f"目标设备 iOS {installed_os} 低于应用最低要求 iOS {minimum_os}")
    return metadata


def extract_app_bundle(ipa_path: str, destination: Path) -> Path:
    """Safely extract the single Payload app bundle while preserving executable bits."""
    destination = Path(destination)
    destination.mkdir(parents=True, exist_ok=True)
    try:
        with zipfile.ZipFile(ipa_path, "r") as zf:
            info_plist_name, infos = _validate_archive(zf)
            app_root = PurePosixPath(info_plist_name).parent
            app_destination = destination / app_root.name
            prefix = f"{app_root.as_posix()}/"

            for info in infos:
                name = str(info.filename or "")
                if name != app_root.as_posix() and not name.startswith(prefix):
                    continue
                archive_path = PurePosixPath(name)
                relative_parts = archive_path.parts[len(app_root.parts) :]
                target = app_destination.joinpath(*relative_parts)
                mode = (info.external_attr >> 16) & 0xFFFF

                if info.is_dir() or name == app_root.as_posix():
                    target.mkdir(parents=True, exist_ok=True)
                    target.chmod((mode & 0o777) or 0o755)
                    continue

                target.parent.mkdir(parents=True, exist_ok=True)
                with zf.open(info, "r") as source, open(target, "wb") as output:
                    shutil.copyfileobj(source, output, length=1024 * 1024)
                target.chmod((mode & 0o777) or 0o644)

            if not (app_destination / "Info.plist").exists():
                raise IpaParseError("IPA 主应用解压不完整")
            return app_destination
    except zipfile.BadZipFile as exc:
        raise IpaParseError("IPA 文件不是有效的 ZIP 归档") from exc
