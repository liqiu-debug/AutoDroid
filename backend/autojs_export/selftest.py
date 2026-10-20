"""设备端执行器自检（AutoDroid-WDA）。

真机到手后用于一条命令验证四件事，全部不需要平台数据库：

  1. 会话可用      —— WDA 是否在手机上活着（GET /status）；
  2. 脚本包可下发  —— 把本地生成的自检脚本包 POST 到 /autodroid/run；
  3. 步骤可执行    —— 自检包会启动设置应用、截图 + OCR、滑动手势、回桌面；
  4. 结果可回传    —— 轮询 /autodroid/status 拿到执行摘要与日志。

自检脚本包与平台导出器同源：manifest / runtime.js / main.js / cases/*.js 的组装
直接复用 backend/autojs_export/packager.py 的内部实现，因此自检通过即代表
「导出包结构 + runtime.js + iOS 适配层」整条链路可用。

用法：
    # 1) 只生成自检包（放到手机上手动跑，或用于排查）
    python -m backend.autojs_export.selftest --output /tmp/ios_selftest.json

    # 2) 下发到设备并等待结果（WDA 已启动，URL 为手机局域网地址）
    python -m backend.autojs_export.selftest --url http://192.168.1.23:8100

    # 3) 额外校验指定文案（界面语言相关，默认不做）
    python -m backend.autojs_export.selftest --url http://127.0.0.1:8100 --expect-text 设置

注意：iOS 自检默认操作「设置」应用（com.apple.Preferences），不需要任何业务应用。
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
import time
import urllib.error
import urllib.request
from typing import Any, Dict, List, Optional

from backend.autojs_export import packager
from backend.autojs_export.packager import SCRIPT_FORMAT_VERSION, _bundle_payload
from backend.step_contract import normalize_platform

logger = logging.getLogger(__name__)

DEFAULT_IOS_SELFTEST_APP = "com.apple.Preferences"
SELFTEST_CASE_FILE = "cases/case_0.js"


def _step(
    order: int,
    action: str,
    *,
    locators: Optional[List[Dict[str, str]]] = None,
    args: Optional[Dict[str, Any]] = None,
    timeout: int = 10,
    error_strategy: str = "ABORT",
    description: Optional[str] = None,
) -> Dict[str, Any]:
    return {
        "order": order,
        "action": action,
        "description": description or action,
        "timeout": timeout,
        "error_strategy": error_strategy,
        "retry_count": 0,
        "execute_on": ["android", "ios"],
        "locators": locators or [],
        "args": args or {},
    }


def build_selftest_steps(
    *,
    platform: str,
    app_identifier: Optional[str] = None,
    expect_text: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """自检步骤：尽量不依赖界面语言，同时覆盖元素 / 截图 / OCR / 手势 / 导航。"""
    platform = normalize_platform(platform)
    target_app = (app_identifier or "").strip() or (
        DEFAULT_IOS_SELFTEST_APP if platform == "ios" else None
    )
    steps: List[Dict[str, Any]] = []
    order = 1

    if target_app:
        steps.append(
            _step(
                order,
                "start_app",
                args={
                    "app_key": None,
                    "bundle_id": target_app if platform == "ios" else None,
                    "package": target_app if platform != "ios" else None,
                },
                description=f"启动应用 {target_app}",
            )
        )
        order += 1
        steps.append(_step(order, "sleep", args={"seconds": 1.5}, description="等待应用就绪"))
        order += 1

    # 截图 + OCR（任一文本即通过，避免依赖界面语言）
    steps.append(
        _step(
            order,
            "extract_by_ocr",
            args={
                "region": [0, 0, 1, 1],
                # 注意：不能写成 (?s).+ —— JavaScriptCore 不支持内联标志
                "extract_rule": {"extract_rule": "regex", "custom_regex": r"[\s\S]+"},
                "output_var": "SELFTEST_TEXT",
            },
            timeout=15,
            error_strategy="IGNORE" if not expect_text else "ABORT",
            description="截图 + OCR 自检（整屏任意文本）",
        )
    )
    order += 1

    steps.append(
        _step(order, "swipe", args={"direction": "up"}, description="滑动手势自检")
    )
    order += 1

    if expect_text:
        steps.append(
            _step(
                order,
                "wait_until_exists",
                locators=[{"selector": expect_text, "by": "text"}],
                timeout=10,
                description=f"等待元素: {expect_text}",
            )
        )
        order += 1
        steps.append(
            _step(
                order,
                "assert_text",
                args={"expected_text": expect_text, "match_mode": "contains"},
                description=f"断言文案: {expect_text}",
            )
        )
        order += 1

    # iOS Settings starts at its root and has no system Back button. A back
    # action here has no valid destination and must not be treated as success.
    # Back is tested separately after entering a known navigation child page.
    if platform != "ios":
        steps.append(_step(order, "back", description="返回自检"))
        order += 1
    steps.append(_step(order, "home", description="回桌面自检"))
    return steps


def build_selftest_bundle(
    platform: str = "ios",
    *,
    app_identifier: Optional[str] = None,
    expect_text: Optional[str] = None,
) -> Dict[str, Any]:
    """生成与平台导出同结构的自检脚本包（JSON 形式，可直接下发）。"""
    platform = normalize_platform(platform)
    manifest = {
        "format_version": SCRIPT_FORMAT_VERSION,
        "generator": packager.GENERATOR_NAME,
        "kind": "case",
        "platform": platform,
        "name": "AutoDroid 自检",
        "scenario_id": None,
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "cases": [
            {
                "id": 0,
                "name": "AutoDroid 自检",
                "alias": None,
                "order": 1,
                "file": SELFTEST_CASE_FILE,
            }
        ],
        "variables": {},
        "precheck": {"ok": True, "issues": [], "warnings": []},
        "requirements": (
            {"runtime": "autodroid", "platform": "ios", "screenshot": "XCUITest"}
            if platform == "ios"
            else {"runtime": "autodroid", "platform": "android", "min_android_sdk": 24}
        ),
    }
    case_script = {
        "format_version": SCRIPT_FORMAT_VERSION,
        "case_id": 0,
        "name": "AutoDroid 自检",
        "variables": {},
        "steps": build_selftest_steps(
            platform=platform,
            app_identifier=app_identifier,
            expect_text=expect_text,
        ),
        "template_refs": [],
    }
    # 与平台导出同源：复用 _bundle_payload（含 iOS 适配层下发逻辑）
    return _bundle_payload(
        kind="case",
        manifest=manifest,
        case_scripts=[(case_script, SELFTEST_CASE_FILE)],
        template_refs=[],
        precheck=manifest["precheck"],
    )


def _http(method: str, url: str, payload: Optional[dict] = None, timeout: float = 30.0) -> Dict[str, Any]:
    data = None
    headers = {"Accept": "application/json"}
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = response.read().decode("utf-8", errors="replace")
            status = response.status
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        status = exc.code
    except Exception as exc:  # 连接失败等
        return {"ok": False, "error": str(exc)}
    parsed: Any = None
    try:
        parsed = json.loads(body) if body else None
    except Exception:
        parsed = None
    # WDA wraps route payloads in a WebDriver `value` envelope.
    if isinstance(parsed, dict) and isinstance(parsed.get("value"), dict):
        parsed = parsed["value"]
    error = parsed.get("error") if isinstance(parsed, dict) else None
    return {"ok": 200 <= status < 300 and not error, "status": status,
            "body": parsed, "raw": body, "error": error}


def run_selftest(
    base_url: str,
    *,
    platform: str = "ios",
    app_identifier: Optional[str] = None,
    expect_text: Optional[str] = None,
    run_id: Optional[str] = None,
    timeout: float = 180.0,
    poll_interval: float = 2.0,
    verbose: bool = True,
) -> Dict[str, Any]:
    """下发自检包并等待执行结束，返回 {ok, status, result, logs}。"""
    base_url = base_url.rstrip("/")
    run_id = run_id or f"selftest-{int(time.time())}"

    status_response = _http("GET", f"{base_url}/autodroid/status", timeout=10.0)
    if not status_response.get("ok"):
        return {
            "ok": False,
            "stage": "status",
            "message": f"无法访问执行器 /autodroid/status：{status_response.get('error') or status_response.get('raw')}",
        }
    if verbose:
        print(f"[1/4] 执行器在线: {json.dumps(status_response.get('body'), ensure_ascii=False)}")

    bundle = build_selftest_bundle(
        platform,
        app_identifier=app_identifier,
        expect_text=expect_text,
    )
    payload = {
        "bundle": bundle,
        "run_id": run_id,
        "platform": normalize_platform(platform),
        "notify": False,
        "target_bundle_id": app_identifier or (DEFAULT_IOS_SELFTEST_APP if normalize_platform(platform) == "ios" else None),
    }
    run_response = _http("POST", f"{base_url}/autodroid/run", payload, timeout=60.0)
    if not run_response.get("ok"):
        return {
            "ok": False,
            "stage": "run",
            "message": f"下发自检包失败：{run_response.get('error') or run_response.get('raw')}",
        }
    if verbose:
        print(f"[2/4] 自检包已下发: {json.dumps(run_response.get('body'), ensure_ascii=False)}")

    deadline = time.time() + timeout
    last_status: Dict[str, Any] = {}
    while time.time() < deadline:
        time.sleep(poll_interval)
        current = _http("GET", f"{base_url}/autodroid/status", timeout=10.0)
        if not current.get("ok"):
            return {
                "ok": False,
                "stage": "poll",
                "message": f"轮询中断（会话可能已断开）：{current.get('error')}",
                "run_id": run_id,
            }
        last_status = current.get("body") or {}
        if not last_status.get("running", False):
            break
    else:
        return {
            "ok": False,
            "stage": "timeout",
            "message": f"自检超时（{timeout:.0f}s）仍未结束，最后一次状态：{json.dumps(last_status, ensure_ascii=False)}",
            "run_id": run_id,
        }

    logs_response = _http("GET", f"{base_url}/autodroid/logs?since=0", timeout=15.0)
    logs = (logs_response.get("body") or {}).get("lines") or []
    result = last_status.get("last_result") or {}
    if verbose:
        print(f"[3/4] 执行日志（共 {len(logs)} 行）:")
        for line in logs[-40:]:
            print(f"      {line}")
        print(f"[4/4] 执行摘要: {json.dumps(result, ensure_ascii=False)}")

    ok = result.get("success") is True and not result.get("error")
    return {
        "ok": ok,
        "stage": "finished",
        "run_id": run_id,
        "status": last_status,
        "result": result,
        "logs": logs,
        "message": "自检通过" if ok else f"自检失败: {result.get('error') or ('用例未成功完成' if 'success' in result else '未取得执行摘要')}",
    }


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="AutoDroid 设备端执行器自检")
    parser.add_argument("--url", help="执行器地址，例如 http://192.168.1.23:8100")
    parser.add_argument("--platform", default="ios", help="目标平台（默认 ios）")
    parser.add_argument("--app", help="自检启动的应用标识（iOS bundle id）")
    parser.add_argument("--expect-text", help="额外断言的界面文案（可选，默认不校验）")
    parser.add_argument("--output", help="只生成自检包并写入该文件（不下发）")
    parser.add_argument("--run-id", help="自定义 run_id")
    parser.add_argument("--timeout", type=float, default=180.0, help="等待执行结束的秒数")
    args = parser.parse_args(argv)

    if args.output:
        bundle = build_selftest_bundle(
            args.platform,
            app_identifier=args.app,
            expect_text=args.expect_text,
        )
        with open(args.output, "w", encoding="utf-8") as handle:
            json.dump(bundle, handle, ensure_ascii=False, indent=2)
        print(f"自检脚本包已写入 {args.output}")
        return 0

    if not args.url:
        parser.error("需要 --url（或只用 --output 生成脚本包）")

    outcome = run_selftest(
        args.url,
        platform=args.platform,
        app_identifier=args.app,
        expect_text=args.expect_text,
        run_id=args.run_id,
        timeout=args.timeout,
    )
    print(json.dumps({k: v for k, v in outcome.items() if k != "logs"}, ensure_ascii=False, indent=2))
    return 0 if outcome.get("ok") else 1


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
