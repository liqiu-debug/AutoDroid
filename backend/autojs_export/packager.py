"""脚本包（zip / 单文件）组装。

脚本包结构：
    manifest.json   —— 元信息（用例清单/变量/预检结果/生成器版本）
    main.js         —— 入口（自动生成）
    runtime.js      —— 设备端运行时库（随包分发）
    cases/*.js      —— 各用例的 JSON 步骤清单
    assets/*.png    —— 图像步骤引用的模板图（来自 static/images/）
"""
from __future__ import annotations

import base64
import io
import json
import logging
import os
import zipfile
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from sqlmodel import Session, select

from backend.autojs_export.generator import (
    SCRIPT_FORMAT_VERSION,
    build_case_script,
)
from backend.autojs_export.precheck import precheck_case_package
from backend.step_contract import normalize_platform
from backend.models import ScenarioStep, TestCase, TestScenario

logger = logging.getLogger(__name__)

RUNTIME_JS_PATH = Path(__file__).resolve().parent / "runtime.js"
GENERATOR_NAME = "autodroid/1"

# 打包根目录（与 backend/api/cases.py 的 PROJECT_ROOT 一致）
PROJECT_ROOT = Path(__file__).resolve().parents[2]


def _load_runtime_source() -> str:
    return RUNTIME_JS_PATH.read_text(encoding="utf-8")


def _load_adapter_source(platform: str) -> Optional[str]:
    """iOS 适配层源码（随包下发）。

    设备端执行器（AutoDroid-WDA）只提供原生原语，AutoJs6 API 表面由该适配层用 JS 实现；
    随平台下发的好处是修适配层无需重新签名/安装 WDA。
    """
    if normalize_platform(platform) != "ios":
        return None
    adapter_path = Path(__file__).resolve().parent / "adapters" / "ios_adapter.js"
    if not adapter_path.is_file():
        logger.warning("iOS 适配层缺失: %s", adapter_path)
        return None
    return adapter_path.read_text(encoding="utf-8")


def _case_module_source(case_script: Dict[str, Any]) -> str:
    payload = json.dumps(case_script, ensure_ascii=False, indent=2)
    return (
        '"use strict";\n'
        "// AutoDroid 用例脚本（平台生成，请勿手改；由 main.js 加载执行，请勿单独运行）\n"
        f"// case: {case_script.get('name')}\n"
        f"var CASE_SCRIPT = {payload};\n"
        'if (typeof module !== "undefined" && module.exports) { module.exports = CASE_SCRIPT; }\n'
    )


_MAIN_JS_TEMPLATE = '''"use strict";
// AutoDroid 脚本包入口（平台生成，请勿手改）
// 使用：将脚本包完整解压到同一目录后运行本文件（保留目录结构，勿单独运行包内其他文件）。
var baseDir = (typeof global !== "undefined" && global.__AUTODROID__ && global.__AUTODROID__.baseDir)
    ? global.__AUTODROID__.baseDir
    : files.cwd();
var manifestPath = files.join(baseDir, "manifest.json");
if (!files.exists(manifestPath)) {
    throw new Error("脚本包不完整：缺少 " + manifestPath
        + "。请将 zip 完整解压后运行 main.js（保留目录结构，勿单独运行 cases/ 或 runtime.js）。");
}
function __hasKeys__(obj) {
    for (var key in obj) { return true; }
    return false;
}
function __loadModule__(relativePath) {
    var fullPath = files.join(baseDir, relativePath);
    if (!files.exists(fullPath)) {
        throw new Error("脚本包缺少文件: " + fullPath);
    }
    var source = files.read(fullPath);
    var moduleObj = { exports: {} };
    // 优先内置加载器：CommonJS 语义由脚本包自己保证，不依赖运行引擎模块系统的实现差异
    try {
        var factory = new Function("module", "exports", source);
        factory(moduleObj, moduleObj.exports);
        if (moduleObj.exports && typeof moduleObj.exports === "object" && __hasKeys__(moduleObj.exports)) {
            return moduleObj.exports;
        }
    } catch (e) {
        console.warn("内置加载器失败，回退 require: " + relativePath + " (" + e + ")");
    }
    if (typeof require === "function") {
        var required = require(fullPath);
        if (required && typeof required === "object" && __hasKeys__(required)) {
            return required;
        }
    }
    throw new Error("模块加载失败: " + relativePath);
}
var AUTODROID_RUNTIME = __loadModule__("runtime.js");
var manifest = JSON.parse(files.read(manifestPath));
var ctx = AUTODROID_RUNTIME.createContext(manifest, baseDir);
var modules = [
__REQUIRE_LINES__
];
__RUNNER__
global.__AUTODROID_RESULT__ = AUTODROID_RUNTIME.finish(ctx);
'''

_PACKAGE_README = """AutoDroid 脚本包使用说明
======================

1. 本目录是一个完整的 AutoDroid 脚本项目（含 project.json）：
   - 方式一：把整个目录拷贝到手机 AutoDroid 的脚本目录下；
   - 方式二：在 AutoDroid 中以项目方式导入本目录（或包含本目录的 zip）。
2. 运行入口是 main.js（项目模式下运行项目即执行 main.js）。
3. 请勿单独运行 cases/*.js 或 runtime.js —— 它们由 main.js 加载。
4. 结果回传：
   - 由 autodroid_agent.js 领取执行时，结果自动回传平台；
   - 手动运行时若未配置平台地址/Token，结果写入同目录 report.json。
5. 图像/OCR 步骤需要开启无障碍服务；建议 Android 11+（无障碍截图无弹窗）。
"""


def _main_js_source(manifest: Dict[str, Any]) -> str:
    require_lines = "\n".join(
        f'    __loadModule__("{entry["file"]}")' + ("," if index < len(manifest["cases"]) - 1 else "")
        for index, entry in enumerate(manifest["cases"])
    )
    if manifest["kind"] == "case":
        runner = "AUTODROID_RUNTIME.runCase(ctx, modules[0], manifest.cases[0]);"
    else:
        runner = "AUTODROID_RUNTIME.runScenario(ctx, manifest.cases, modules);"
    return (
        _MAIN_JS_TEMPLATE
        .replace("__REQUIRE_LINES__", require_lines)
        .replace("__RUNNER__", runner)
    )


def _project_json(manifest: Dict[str, Any]) -> str:
    kind = str(manifest.get("kind") or "case")
    identity = kind
    for entry in manifest.get("cases") or []:
        identity = f"{kind}{entry.get('id') or ''}"
        break
    package_name = "com.autodroid.export." + "".join(
        ch for ch in identity.lower() if ch.isalnum()
    )
    return json.dumps({
        "name": manifest.get("name") or "AutoDroid 脚本包",
        "versionName": "1.0.0",
        "versionCode": 1,
        "packageName": package_name,
        "main": "main.js",
    }, ensure_ascii=False, indent=2)


def _build_manifest(
    *,
    kind: str,
    name: str,
    case_entries: List[Dict[str, Any]],
    variables: Dict[str, str],
    scenario_id: Optional[int],
    precheck: Dict[str, Any],
    platform: str = "android",
) -> Dict[str, Any]:
    platform = normalize_platform(platform)
    requirements = (
        {
            "runtime": "autodroid",
            "platform": "ios",
            "accessibility": False,
            "screenshot": "XCUITest (WebDriverAgentRunner)",
            "min_ios_version": 15,
        }
        if platform == "ios"
        else {
            "runtime": "autodroid",
            "platform": "android",
            "min_android_sdk": 24,
            "accessibility": True,
            "screenshot": "automator (Android 11+) 或 MediaProjection",
        }
    )
    return {
        "format_version": SCRIPT_FORMAT_VERSION,
        "generator": GENERATOR_NAME,
        "kind": kind,
        "platform": platform,
        "name": name,
        "scenario_id": scenario_id,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "cases": case_entries,
        "variables": variables,
        "precheck": precheck,
        "requirements": requirements,
    }


def _write_zip(
    manifest: Dict[str, Any],
    case_scripts: List[Tuple[Dict[str, Any], str]],  # (case_script, module_file)
    template_refs: List[Tuple[str, str]],
    *,
    embedded_assets: bool = False,
) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("project.json", _project_json(manifest))
        zf.writestr("README.txt", _PACKAGE_README)
        zf.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2))
        zf.writestr("main.js", _main_js_source(manifest))
        # iOS 包需要把适配层一起放进 zip（JSON 下发路径放在 adapter_js 字段）
        adapter_source = _load_adapter_source(manifest.get("platform") or "android")
        if adapter_source:
            zf.writestr("adapter.js", adapter_source)
        zf.writestr("runtime.js", _load_runtime_source())
        for case_script, module_file in case_scripts:
            zf.writestr(module_file, _case_module_source(case_script))
        if not embedded_assets:
            for asset_name, rel_path in template_refs:
                abs_path = PROJECT_ROOT / rel_path
                if abs_path.is_file():
                    zf.write(abs_path, f"assets/{asset_name}")
                else:
                    logger.warning("模板图缺失，跳过打包: %s", rel_path)
    return buffer.getvalue()


def _timestamp() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S")


def _safe_name(name: str) -> str:
    cleaned = "".join(ch if ch.isalnum() or ch in "-_." else "_" for ch in str(name or "").strip())
    return cleaned[:48] or "package"


def build_case_package(
    session: Session,
    case: TestCase,
    *,
    env_id: Optional[int] = None,
    platform: str = "android",
) -> Dict[str, Any]:
    """单用例脚本包（zip）。"""
    case_script = build_case_script(session, case, env_id=env_id, platform=platform)
    precheck = precheck_case_package([case_script], project_root=PROJECT_ROOT)
    module_file = f"cases/case_{case.id}.js"
    manifest = _build_manifest(
        kind="case",
        name=case.name,
        case_entries=[{
            "id": case.id,
            "name": case.name,
            "alias": None,
            "file": module_file,
        }],
        variables=case_script.get("variables") or {},
        scenario_id=None,
        precheck=precheck,
        platform=platform,
    )
    content = _write_zip(manifest, [(case_script, module_file)], case_script.get("template_refs") or [])
    return {
        "filename": f"autodroid_case_{case.id}_{_timestamp()}.zip",
        "content": content,
        "manifest": manifest,
        "precheck": precheck,
    }


def collect_scenario_parts(
    session: Session,
    scenario: TestScenario,
    *,
    env_id: Optional[int] = None,
    platform: str = "android",
) -> Dict[str, Any]:
    """收集场景脚本包的组成部件（zip 打包与 App JSON 下发共用）。"""
    scenario_steps = session.exec(
        select(ScenarioStep)
        .where(ScenarioStep.scenario_id == scenario.id)
        .order_by(ScenarioStep.order)
    ).all()

    case_scripts: List[Tuple[Dict[str, Any], str]] = []
    case_entries: List[Dict[str, Any]] = []
    template_refs: List[Tuple[str, str]] = []
    variables: Dict[str, str] = {}
    missing: List[int] = []

    for scenario_step in scenario_steps:
        case = session.get(TestCase, scenario_step.case_id)
        if not case:
            missing.append(scenario_step.case_id)
            continue
        case_script = build_case_script(session, case, env_id=env_id, platform=platform)
        module_file = f"cases/case_{case.id}.js"
        case_scripts.append((case_script, module_file))
        case_entries.append({
            "id": case.id,
            "name": case.name,
            "alias": scenario_step.alias,
            "order": scenario_step.order,
            "file": module_file,
        })
        for ref in case_script.get("template_refs") or []:
            if ref not in template_refs:
                template_refs.append(ref)
        for key, value in (case_script.get("variables") or {}).items():
            variables.setdefault(key, value)

    precheck = precheck_case_package(
        [script for script, _ in case_scripts], project_root=PROJECT_ROOT
    )
    if missing:
        precheck = dict(precheck)
        issues = list(precheck.get("issues") or [])
        for case_id in missing:
            issues.append({
                "step_order": None,
                "severity": "error",
                "code": "EXPORT_CASE_MISSING",
                "message": f"场景引用的用例不存在: {case_id}",
            })
        precheck["issues"] = issues
        precheck["ok"] = False

    return {
        "case_scripts": case_scripts,
        "case_entries": case_entries,
        "template_refs": template_refs,
        "variables": variables,
        "precheck": precheck,
        "missing": missing,
    }


def build_scenario_package(
    session: Session,
    scenario: TestScenario,
    *,
    env_id: Optional[int] = None,
    platform: str = "android",
) -> Dict[str, Any]:
    """场景脚本包：全部子用例 + 场景串联入口。"""
    parts = collect_scenario_parts(session, scenario, env_id=env_id, platform=platform)
    manifest = _build_manifest(
        kind="scenario",
        name=scenario.name,
        case_entries=parts["case_entries"],
        variables=parts["variables"],
        scenario_id=scenario.id,
        precheck=parts["precheck"],
        platform=platform,
    )
    content = _write_zip(manifest, parts["case_scripts"], parts["template_refs"])
    return {
        "filename": f"autodroid_scenario_{scenario.id}_{_timestamp()}.zip",
        "content": content,
        "manifest": manifest,
        "precheck": parts["precheck"],
    }


def _bundle_payload(
    *,
    kind: str,
    manifest: Dict[str, Any],
    case_scripts: List[Tuple[Dict[str, Any], str]],
    template_refs: List[Tuple[str, str]],
    precheck: Dict[str, Any],
) -> Dict[str, Any]:
    """组装 JSON 形式的脚本包（App 直跑下发用，与 zip 打包同源同内容）。"""
    case_files = [
        {"path": module_file, "content": _case_module_source(case_script)}
        for case_script, module_file in case_scripts
    ]
    assets: Dict[str, str] = {}
    for asset_name, rel_path in template_refs:
        abs_path = PROJECT_ROOT / rel_path
        if abs_path.is_file():
            assets[asset_name] = base64.b64encode(abs_path.read_bytes()).decode("ascii")
        else:
            logger.warning("模板图缺失，未随脚本下发: %s", rel_path)
    adapter_source = _load_adapter_source(manifest.get("platform") or "android")
    payload = {
        "format_version": SCRIPT_FORMAT_VERSION,
        "generator": GENERATOR_NAME,
        "kind": kind,
        "manifest": manifest,
        "runtime_js": _load_runtime_source(),
        "main_js": _main_js_source(manifest),
        "case_files": case_files,
        "assets": assets,
        "precheck": precheck,
    }
    if adapter_source:
        payload["adapter_js"] = adapter_source
    return payload


def build_case_bundle(
    session: Session,
    case: TestCase,
    *,
    env_id: Optional[int] = None,
    platform: str = "android",
) -> Dict[str, Any]:
    """单用例的 JSON 脚本包（App 直跑）。"""
    case_script = build_case_script(session, case, env_id=env_id, platform=platform)
    module_file = f"cases/case_{case.id}.js"
    precheck = precheck_case_package([case_script], project_root=PROJECT_ROOT)
    manifest = _build_manifest(
        kind="case",
        name=case.name,
        case_entries=[{
            "id": case.id,
            "name": case.name,
            "alias": None,
            "file": module_file,
        }],
        variables=case_script.get("variables") or {},
        scenario_id=None,
        precheck=precheck,
        platform=platform,
    )
    return _bundle_payload(
        kind="case",
        manifest=manifest,
        case_scripts=[(case_script, module_file)],
        template_refs=case_script.get("template_refs") or [],
        precheck=precheck,
    )


def build_scenario_bundle(
    session: Session,
    scenario: TestScenario,
    *,
    env_id: Optional[int] = None,
    platform: str = "android",
) -> Dict[str, Any]:
    """场景的 JSON 脚本包（App 直跑）。"""
    parts = collect_scenario_parts(session, scenario, env_id=env_id, platform=platform)
    manifest = _build_manifest(
        kind="scenario",
        name=scenario.name,
        case_entries=parts["case_entries"],
        variables=parts["variables"],
        scenario_id=scenario.id,
        precheck=parts["precheck"],
        platform=platform,
    )
    return _bundle_payload(
        kind="scenario",
        manifest=manifest,
        case_scripts=parts["case_scripts"],
        template_refs=parts["template_refs"],
        precheck=parts["precheck"],
    )


def build_batch_package(
    session: Session,
    cases: List[TestCase],
    *,
    env_id: Optional[int] = None,
    batch_name: str = "batch",
) -> Dict[str, Any]:
    """批量导出：多个独立用例打进一个包（入口按顺序执行，失败不中断）。"""
    case_scripts: List[Tuple[Dict[str, Any], str]] = []
    case_entries: List[Dict[str, Any]] = []
    template_refs: List[Tuple[str, str]] = []
    variables: Dict[str, str] = {}

    for order, case in enumerate(cases, start=1):
        case_script = build_case_script(session, case, env_id=env_id)
        module_file = f"cases/case_{case.id}.js"
        case_scripts.append((case_script, module_file))
        case_entries.append({
            "id": case.id,
            "name": case.name,
            "alias": None,
            "order": order,
            "file": module_file,
        })
        for ref in case_script.get("template_refs") or []:
            if ref not in template_refs:
                template_refs.append(ref)
        for key, value in (case_script.get("variables") or {}).items():
            variables.setdefault(key, value)

    precheck = precheck_case_package(
        [script for script, _ in case_scripts], project_root=PROJECT_ROOT
    )
    manifest = _build_manifest(
        kind="batch",
        name=batch_name,
        case_entries=case_entries,
        variables=variables,
        scenario_id=None,
        precheck=precheck,
    )
    content = _write_zip(manifest, case_scripts, template_refs)
    return {
        "filename": f"autodroid_batch_{_safe_name(batch_name)}_{_timestamp()}.zip",
        "content": content,
        "manifest": manifest,
        "precheck": precheck,
    }


def build_single_file(
    session: Session,
    case: TestCase,
    *,
    env_id: Optional[int] = None,
) -> Dict[str, Any]:
    """单文件模式：runtime + 用例清单 + 模板图 base64 内嵌为一个 .js。

    适合无图像步骤或图像极少的用例快速分享；图像多时请用 zip 脚本包。
    """
    case_script = build_case_script(session, case, env_id=env_id)
    precheck = precheck_case_package([case_script], project_root=PROJECT_ROOT)

    embedded: Dict[str, str] = {}
    for asset_name, rel_path in case_script.get("template_refs") or []:
        abs_path = PROJECT_ROOT / rel_path
        if abs_path.is_file():
            embedded[asset_name] = base64.b64encode(abs_path.read_bytes()).decode("ascii")

    manifest = _build_manifest(
        kind="case",
        name=case.name,
        case_entries=[{
            "id": case.id,
            "name": case.name,
            "alias": None,
            "file": None,
        }],
        variables=case_script.get("variables") or {},
        scenario_id=None,
        precheck=precheck,
    )

    runtime_source = _load_runtime_source()
    source = (
        '"use strict";\n'
        "// AutoDroid 单文件脚本（平台生成，请勿手改）\n"
        f"// case: {case.name}\n"
        f"{runtime_source}\n"
        "var AUTODROID_RUNTIME = RUNTIME_API;\n"
        f"var manifest = {json.dumps(manifest, ensure_ascii=False, indent=2)};\n"
        f"var caseScript = {json.dumps(case_script, ensure_ascii=False, indent=2)};\n"
        f"var EMBEDDED_ASSETS = {json.dumps(embedded, ensure_ascii=False)};\n"
        "var ctx = AUTODROID_RUNTIME.createContext(manifest, files.cwd(), EMBEDDED_ASSETS);\n"
        "AUTODROID_RUNTIME.runCase(ctx, caseScript, manifest.cases[0]);\n"
        "global.__AUTODROID_RESULT__ = AUTODROID_RUNTIME.finish(ctx);\n"
    )
    return {
        "filename": f"autodroid_case_{case.id}_{_timestamp()}.js",
        "content": source.encode("utf-8"),
        "manifest": manifest,
        "precheck": precheck,
    }
