"""导出可导出性预检。

在生成脚本包之前对编译产物做静态检查，错误码沿用平台预检语义
（P1002/P1003/P1004），另有 EXPORT_* 前缀的导出专属提示。
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from backend.autojs_export.generator import (
    LOCATOR_ACTIONS,
    find_unresolved_placeholders,
)

SUPPORTED_BY = {"id", "resourceid", "resource_id", "text", "label", "name", "description", "desc", "content-desc"}


def _issue(
    step_order: Optional[int],
    severity: str,
    code: str,
    message: str,
) -> Dict[str, Any]:
    return {
        "step_order": step_order,
        "severity": severity,
        "code": code,
        "message": message,
    }


def precheck_case_script(
    case_script: Dict[str, Any],
    *,
    project_root: Optional[Path] = None,
) -> Dict[str, Any]:
    """检查单个用例的编译产物。返回 {"ok": bool, "issues": [...]}。"""
    issues: List[Dict[str, Any]] = []
    steps = case_script.get("steps") or []

    if not steps:
        issues.append(_issue(None, "error", "EXPORT_NO_STEPS", "用例没有可导出的步骤"))

    for step in steps:
        order = step.get("order")
        action = step.get("action")
        args = step.get("args") or {}
        locators = step.get("locators") or []

        if action in LOCATOR_ACTIONS and not locators:
            issues.append(
                _issue(order, "error", "P1003_SELECTOR_MISSING", f"{action} 步骤缺少可用定位器")
            )
        for locator in locators:
            by = str(locator.get("by") or "").strip().lower()
            if by == "xpath":
                issues.append(
                    _issue(
                        order,
                        "error",
                        "EXPORT_UNSUPPORTED_LOCATOR",
                        f"{action} 步骤使用 xpath 定位，AutoDroid 不支持（建议改用 text/id/description 定位）",
                    )
                )
            elif by and by not in SUPPORTED_BY:
                issues.append(
                    _issue(order, "error", "P1002_ACTION_NOT_SUPPORTED", f"{action} 步骤定位方式不支持: {by}")
                )

        if action in ("click_image", "assert_image"):
            if not args.get("image"):
                issues.append(
                    _issue(order, "error", "EXPORT_IMAGE_MISSING", f"{action} 步骤缺少有效模板图（image_path 非法或为空）")
                )

        if action in ("start_app", "stop_app"):
            if not args.get("package"):
                app_key = args.get("app_key") or ""
                issues.append(
                    _issue(
                        order,
                        "error",
                        "P1004_APP_MAPPING_MISSING",
                        f"{action} 步骤的 app_key 未映射到 Android 包名: {app_key!r}",
                    )
                )

        if action == "extract_by_ocr" and args.get("region") is None:
            issues.append(
                _issue(order, "error", "P1006_INVALID_ARGS", "extract_by_ocr 步骤缺少有效 region")
            )

        unresolved = find_unresolved_placeholders(
            {"args": args, "locators": locators}
        )
        if unresolved:
            issues.append(
                _issue(
                    order,
                    "warning",
                    "EXPORT_UNRESOLVED_VARIABLE",
                    "存在未解析变量占位符: " + ", ".join(f"{{{{{name}}}}}" for name in unresolved),
                )
            )

    for asset_name, rel_path in case_script.get("template_refs") or []:
        if project_root is not None:
            target = Path(project_root) / rel_path
            if not target.is_file():
                issues.append(
                    _issue(
                        None,
                        "error",
                        "EXPORT_IMAGE_MISSING",
                        f"模板图文件不存在: {rel_path}",
                    )
                )

    ok = not any(item.get("severity") == "error" for item in issues)
    return {"ok": ok, "issues": issues}


def precheck_case_package(
    case_scripts: List[Dict[str, Any]],
    *,
    project_root: Optional[Path] = None,
) -> Dict[str, Any]:
    """批量预检（场景/批量导出复用）。"""
    all_issues: List[Dict[str, Any]] = []
    for case_script in case_scripts:
        result = precheck_case_script(case_script, project_root=project_root)
        for issue in result.get("issues") or []:
            item = dict(issue)
            item["case_id"] = case_script.get("case_id")
            item["case_name"] = case_script.get("name")
            all_issues.append(item)
    ok = not any(item.get("severity") == "error" for item in all_issues)
    return {"ok": ok, "issues": all_issues}
