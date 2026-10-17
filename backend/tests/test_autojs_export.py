"""AutoDroid 导出器 + 设备端回传链路测试。"""
import io
import json
import unittest
import zipfile
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, select

from backend.api import autojs as autojs_api
from backend.api import app_runs as app_runs_api
from backend.core.api_tokens import generate_api_token, hash_api_token, token_display_prefix
from backend.database import get_session
from backend.models import (
    ApiToken,
    Device,
    DeviceRunTask,
    TestCase,
    TestCaseStep,
    TestExecution,
    TestResult,
    TestScenario,
    ScenarioStep,
    User,
)
from backend.autojs_export import packager
from backend.autojs_export.generator import build_case_script, collect_case_variables
from backend.autojs_export.precheck import precheck_case_script


class _Base(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = create_engine(
            "sqlite://",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        SQLModel.metadata.create_all(self.engine)
        self.session = Session(self.engine)

    def tearDown(self) -> None:
        self.session.close()

    # -- helpers -----------------------------------------------------------
    def _create_case(self, name: str = "case-1", variables=None) -> TestCase:
        case = TestCase(name=name, steps=[], variables=variables or [])
        self.session.add(case)
        self.session.commit()
        self.session.refresh(case)
        return case

    def _add_step(
        self,
        case_id: int,
        order: int,
        action: str,
        *,
        args=None,
        value=None,
        platform_overrides=None,
        execute_on=None,
        timeout=10,
        error_strategy="ABORT",
        retry_count=0,
        description=None,
    ) -> TestCaseStep:
        step = TestCaseStep(
            case_id=case_id,
            order=order,
            action=action,
            execute_on=execute_on or ["android", "ios"],
            platform_overrides=platform_overrides or {},
            args=args or {},
            value=value,
            timeout=timeout,
            error_strategy=error_strategy,
            retry_count=retry_count,
            description=description or f"step-{order}",
        )
        self.session.add(step)
        self.session.commit()
        return step


class GeneratorTests(_Base):
    def test_all_actions_compile(self):
        case = self._create_case()
        self._add_step(
            case.id, 1, "click",
            platform_overrides={"android": {"selector": "登录", "by": "text"}},
        )
        self._add_step(case.id, 2, "input", args={"text": "hello"}, platform_overrides={"android": {"selector": "com.x:id/et", "by": "id"}})
        self._add_step(case.id, 3, "wait_until_exists", platform_overrides={"android": {"selector": "首页", "by": "text"}}, timeout=15)
        self._add_step(case.id, 4, "assert_text", args={"expected_text": "成功", "match_mode": "contains"})
        self._add_step(case.id, 5, "sleep", args={"seconds": 1.5})
        self._add_step(case.id, 6, "swipe", args={"direction": "up"})
        self._add_step(case.id, 7, "back")
        self._add_step(case.id, 8, "home")
        self._add_step(case.id, 9, "start_app", args={"app_key": "com.demo.app"})
        self._add_step(case.id, 10, "stop_app", args={"app_key": "com.demo.app"})
        self._add_step(case.id, 11, "extract_by_ocr", args={"region": "[0,0,1,1]", "extract_rule": {"extract_rule": "preset", "preset_type": "number_only"}, "output_var": "PRICE"})

        script = build_case_script(self.session, case)
        actions = [step["action"] for step in script["steps"]]
        self.assertEqual(
            actions,
            ["click", "input", "wait_until_exists", "assert_text", "sleep",
             "swipe", "back", "home", "start_app", "stop_app", "extract_by_ocr"],
        )
        by_action = {step["action"]: step for step in script["steps"]}
        self.assertEqual(by_action["click"]["locators"], [{"selector": "登录", "by": "text"}])
        self.assertEqual(by_action["input"]["args"]["text"], "hello")
        self.assertEqual(by_action["wait_until_exists"]["timeout"], 15)
        self.assertEqual(by_action["assert_text"]["args"]["match_mode"], "contains")
        self.assertEqual(by_action["sleep"]["args"]["seconds"], 1.5)
        self.assertEqual(by_action["swipe"]["args"]["direction"], "up")
        # app_key 形如包名时未映射也可直接透传
        self.assertEqual(by_action["start_app"]["args"]["package"], "com.demo.app")
        self.assertEqual(by_action["extract_by_ocr"]["args"]["region"], [0.0, 0.0, 1.0, 1.0])
        self.assertEqual(by_action["extract_by_ocr"]["args"]["output_var"], "PRICE")

    def test_variables_rendered_at_export(self):
        case = self._create_case(
            variables=[{"key": "USER", "value": "alice"}]
        )
        self._add_step(case.id, 1, "input", args={"text": "{{USER}}"}, platform_overrides={"android": {"selector": "com.x:id/et", "by": "id"}})
        script = build_case_script(self.session, case)
        self.assertEqual(script["steps"][0]["args"]["text"], "alice")
        self.assertEqual(script["variables"]["USER"], "alice")

    def test_unresolved_variable_kept(self):
        case = self._create_case()
        self._add_step(case.id, 1, "input", args={"text": "{{UNKNOWN}}"}, platform_overrides={"android": {"selector": "com.x:id/et", "by": "id"}})
        script = build_case_script(self.session, case)
        self.assertEqual(script["steps"][0]["args"]["text"], "{{UNKNOWN}}")

    def test_image_step_resolves_asset(self):
        case = self._create_case()
        self._add_step(
            case.id, 1, "click_image",
            args={"image_path": "static/images/element_ab12cd34.png"},
        )
        script = build_case_script(self.session, case)
        step = script["steps"][0]
        self.assertEqual(step["args"]["image"], "element_ab12cd34.png")
        self.assertIn(("element_ab12cd34.png", "static/images/element_ab12cd34.png"), script["template_refs"])


class PrecheckTests(_Base):
    def test_xpath_rejected(self):
        case = self._create_case()
        self._add_step(case.id, 1, "click", platform_overrides={"android": {"selector": "//*[@text='x']", "by": "xpath"}})
        script = build_case_script(self.session, case)
        result = precheck_case_script(script, project_root=packager.PROJECT_ROOT)
        self.assertFalse(result["ok"])
        codes = [issue["code"] for issue in result["issues"]]
        self.assertIn("EXPORT_UNSUPPORTED_LOCATOR", codes)

    def test_missing_locator_rejected(self):
        case = self._create_case()
        self._add_step(case.id, 1, "click")
        script = build_case_script(self.session, case)
        result = precheck_case_script(script, project_root=packager.PROJECT_ROOT)
        self.assertFalse(result["ok"])
        self.assertIn("P1003_SELECTOR_MISSING", [i["code"] for i in result["issues"]])

    def test_unmapped_app_key_rejected(self):
        case = self._create_case()
        self._add_step(case.id, 1, "start_app", args={"app_key": "商城APP"})
        script = build_case_script(self.session, case)
        result = precheck_case_script(script, project_root=packager.PROJECT_ROOT)
        self.assertFalse(result["ok"])
        self.assertIn("P1004_APP_MAPPING_MISSING", [i["code"] for i in result["issues"]])

    def test_missing_image_rejected(self):
        case = self._create_case()
        self._add_step(case.id, 1, "click_image", args={"image_path": "static/images/element_ffffffff.png"})
        script = build_case_script(self.session, case)
        result = precheck_case_script(script, project_root=packager.PROJECT_ROOT)
        self.assertFalse(result["ok"])
        self.assertIn("EXPORT_IMAGE_MISSING", [i["code"] for i in result["issues"]])

    def test_unresolved_variable_warning(self):
        case = self._create_case()
        self._add_step(case.id, 1, "input", args={"text": "{{MISSING}}"}, platform_overrides={"android": {"selector": "com.x:id/et", "by": "id"}})
        script = build_case_script(self.session, case)
        result = precheck_case_script(script, project_root=packager.PROJECT_ROOT)
        self.assertTrue(result["ok"])  # warning 不拦截导出
        self.assertIn("EXPORT_UNRESOLVED_VARIABLE", [i["code"] for i in result["issues"]])


class PackagerTests(_Base):
    def test_case_package_zip_structure(self):
        case = self._create_case(name="打包用例")
        self._add_step(case.id, 1, "sleep", args={"seconds": 0})
        package = packager.build_case_package(self.session, case)
        self.assertTrue(package["filename"].endswith(".zip"))
        with zipfile.ZipFile(io.BytesIO(package["content"])) as zf:
            names = set(zf.namelist())
            self.assertIn("manifest.json", names)
            self.assertIn("main.js", names)
            self.assertIn("runtime.js", names)
            self.assertIn("project.json", names)
            self.assertIn("README.txt", names)
            self.assertIn(f"cases/case_{case.id}.js", names)
            manifest = json.loads(zf.read("manifest.json"))
            self.assertEqual(manifest["kind"], "case")
            self.assertEqual(manifest["cases"][0]["id"], case.id)
            project = json.loads(zf.read("project.json"))
            self.assertEqual(project["main"], "main.js")
            main_js = zf.read("main.js").decode("utf-8")
            self.assertIn("__loadModule__", main_js)
            case_module = zf.read(f"cases/case_{case.id}.js").decode("utf-8")
            self.assertIn("module.exports", case_module)
            # 直接运行时（顶层无 module）不应因 module 未定义而崩溃
            self.assertIn('typeof module !== "undefined"', case_module)

    def test_scenario_package_contains_all_cases(self):
        case_a = self._create_case(name="用例A")
        case_b = self._create_case(name="用例B")
        self._add_step(case_a.id, 1, "sleep", args={"seconds": 0})
        self._add_step(case_b.id, 1, "sleep", args={"seconds": 0})
        scenario = TestScenario(name="场景1")
        self.session.add(scenario)
        self.session.commit()
        self.session.refresh(scenario)
        self.session.add(ScenarioStep(scenario_id=scenario.id, case_id=case_a.id, order=1, alias="A"))
        self.session.add(ScenarioStep(scenario_id=scenario.id, case_id=case_b.id, order=2, alias="B"))
        self.session.commit()

        package = packager.build_scenario_package(self.session, scenario)
        with zipfile.ZipFile(io.BytesIO(package["content"])) as zf:
            manifest = json.loads(zf.read("manifest.json"))
            self.assertEqual(manifest["kind"], "scenario")
            self.assertEqual([entry["id"] for entry in manifest["cases"]], [case_a.id, case_b.id])
            self.assertIn(f"cases/case_{case_a.id}.js", zf.namelist())
            self.assertIn(f"cases/case_{case_b.id}.js", zf.namelist())

    def test_single_file_embeds_case(self):
        case = self._create_case(name="单文件")
        self._add_step(case.id, 1, "sleep", args={"seconds": 0})
        result = packager.build_single_file(self.session, case)
        source = result["content"].decode("utf-8")
        self.assertIn("createContext", source)
        self.assertIn('"case_id": %d' % case.id, source)
        self.assertTrue(result["filename"].endswith(".js"))


class DeviceFlowTests(_Base):
    def setUp(self) -> None:
        super().setUp()
        import tempfile
        from pathlib import Path

        # 脚本包写入临时目录，避免污染仓库 uploads/
        self._package_dir = Path(tempfile.mkdtemp(prefix="autodroid_test_packages_"))
        self._package_dir_patch = patch.object(autojs_api, "PACKAGE_DIR", self._package_dir)
        self._package_dir_patch.start()
        self.addCleanup(self._package_dir_patch.stop)
        self.user = User(username="tester", hashed_password="x", role="user", is_active=True)
        self.session.add(self.user)
        self.session.commit()
        self.session.refresh(self.user)
        self.token = generate_api_token()
        self.session.add(ApiToken(
            name="test",
            token_hash=hash_api_token(self.token),
            token_prefix=token_display_prefix(self.token),
            user_id=self.user.id,
            is_active=True,
        ))
        self.session.commit()

        app = FastAPI()
        app.include_router(autojs_api.router, prefix="/api/autojs")
        app.include_router(autojs_api.device_router, prefix="/api/device-runs")
        app.include_router(app_runs_api.router, prefix="/api/app-runs")
        app.dependency_overrides[get_session] = lambda: self.session
        self.client = TestClient(app)
        self.headers = {"Authorization": f"Bearer {self.token}"}

    def _make_case_with_steps(self) -> TestCase:
        case = self._create_case(name="设备端用例")
        self._add_step(
            case.id, 1, "click",
            platform_overrides={"android": {"selector": "登录", "by": "text"}},
        )
        self._add_step(case.id, 2, "assert_text", args={"expected_text": "欢迎", "match_mode": "contains"})
        return case

    def test_register_creates_device(self):
        resp = self.client.post(
            "/api/device-runs/register",
            json={"android_id": "abc123", "model": "Pixel 8", "brand": "Google", "android_version": "14", "resolution": "1080x2400"},
            headers=self.headers,
        )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["device_serial"], "autojs6:abc123")
        device = self.session.exec(select(Device).where(Device.serial == "autojs6:abc123")).first()
        self.assertIsNotNone(device)
        self.assertEqual(device.connection_type, "autojs6")

    def test_full_flow_create_claim_report(self):
        case = self._make_case_with_steps()

        # 1. 创建任务（生成脚本包）
        resp = self.client.post(
            "/api/autojs/tasks",
            json={"kind": "case", "case_id": case.id},
            headers=self.headers,
        )
        self.assertEqual(resp.status_code, 200, resp.text)
        task_id = resp.json()["task_id"]
        self.assertEqual(resp.json()["status"], "PENDING")

        # 2. 设备领取任务
        resp = self.client.get(
            "/api/device-runs/pending",
            params={"serial": "autojs6:abc123"},
            headers=self.headers,
        )
        self.assertEqual(resp.status_code, 200)
        claimed = resp.json()["task"]
        self.assertEqual(claimed["task_id"], task_id)

        # 3. 下载脚本包
        resp = self.client.get(
            f"/api/device-runs/packages/{task_id}", headers=self.headers
        )
        self.assertEqual(resp.status_code, 200)
        with zipfile.ZipFile(io.BytesIO(resp.content)) as zf:
            self.assertIn("main.js", zf.namelist())

        # 4. 上报结果 -> 落库 + 报告 + 通知
        report_payload = {
            "task_id": task_id,
            "kind": "case",
            "case_id": case.id,
            "device": {"model": "Pixel 8", "android_id": "abc123"},
            "started_at": 1000.0,
            "finished_at": 6000.0,
            "success": False,
            "cases": [{
                "case_id": case.id,
                "name": case.name,
                "success": False,
                "steps": [
                    {"order": 1, "action": "click", "status": "PASS", "duration": 1.2},
                    {"order": 2, "action": "assert_text", "status": "FAIL", "duration": 0.8,
                     "error": "断言失败: 期望页面包含 \"欢迎\"，未找到",
                     "error_code": "E2003_ASSERT_TEXT_FAILED", "attempts": 2},
                ],
            }],
            "logs": ["step1 ok"],
        }
        with patch.object(autojs_api.report_generator, "generate_report", return_value="report-test.html") as report_mock, \
             patch.object(autojs_api.NotificationService, "send_report_card") as notify_mock:
            resp = self.client.post(
                "/api/device-runs/report", json=report_payload, headers=self.headers
            )
        self.assertEqual(resp.status_code, 200, resp.text)
        body = resp.json()
        self.assertEqual(body["status"], "FAIL")
        self.assertEqual(body["report_id"], "report-test.html")
        report_mock.assert_called_once()
        notify_mock.assert_called_once()

        # 执行记录与步骤结果
        execution = self.session.get(TestExecution, body["execution_id"])
        self.assertIsNotNone(execution)
        self.assertEqual(execution.device_serial, "autojs6:abc123")
        self.assertEqual(execution.platform, "android")
        self.assertEqual(execution.batch_name, "AutoDroid 设备端执行")
        results = self.session.exec(
            select(TestResult).where(TestResult.execution_id == execution.id).order_by(TestResult.step_order)
        ).all()
        self.assertEqual(len(results), 2)
        self.assertEqual(results[0].status, "PASS")
        self.assertEqual(results[1].status, "FAIL")

        # 任务收尾
        task = self.session.exec(select(DeviceRunTask).where(DeviceRunTask.task_id == task_id)).first()
        self.assertEqual(task.status, "DONE")
        self.assertEqual(task.execution_id, execution.id)
        self.assertEqual(task.result_summary["failed_steps"], 1)

        # 5. 幂等重报：不产生重复执行记录
        with patch.object(autojs_api.report_generator, "generate_report", return_value="report-test-2.html"), \
             patch.object(autojs_api.NotificationService, "send_report_card"):
            resp = self.client.post(
                "/api/device-runs/report", json=report_payload, headers=self.headers
            )
        self.assertEqual(resp.status_code, 200)
        executions = self.session.exec(select(TestExecution)).all()
        self.assertEqual(len(executions), 1)

    def test_claim_returns_none_when_empty(self):
        resp = self.client.get(
            "/api/device-runs/pending",
            params={"serial": "autojs6:none"},
            headers=self.headers,
        )
        self.assertEqual(resp.status_code, 200)
        self.assertIsNone(resp.json()["task"])

    def test_concurrent_claim_only_one_wins(self):
        """两个 Agent 实例轮询同一任务时，只有一个能领到（原子领取）。"""
        case = self._make_case_with_steps()
        resp = self.client.post(
            "/api/autojs/tasks",
            json={"kind": "case", "case_id": case.id},
            headers=self.headers,
        )
        self.assertEqual(resp.status_code, 200, resp.text)
        task_id = resp.json()["task_id"]

        first = self.client.get(
            "/api/device-runs/pending", params={"serial": "autojs6:agent-a"}, headers=self.headers
        ).json()["task"]
        second = self.client.get(
            "/api/device-runs/pending", params={"serial": "autojs6:agent-b"}, headers=self.headers
        ).json()["task"]

        self.assertIsNotNone(first)
        self.assertEqual(first["task_id"], task_id)
        self.assertIsNone(second)

    # ------------------------------------------------------------------ #
    #  App 直跑（无任务）：脚本下发 + 上报
    # ------------------------------------------------------------------ #
    def _app_report_payload(self, case: TestCase, run_id: str, *, notify: bool = True) -> dict:
        return {
            "run_id": run_id,
            "notify": notify,
            "kind": "case",
            "case_id": case.id,
            "device": {"model": "Pixel 8", "android_id": "abc123"},
            "started_at": 1000.0,
            "finished_at": 5000.0,
            "success": True,
            "cases": [{
                "case_id": case.id,
                "name": case.name,
                "success": True,
                "steps": [
                    {"order": 1, "action": "click", "status": "PASS", "duration": 0.5},
                    {"order": 2, "action": "assert_text", "status": "PASS", "duration": 0.3},
                ],
            }],
            "logs": ["ok"],
        }

    def test_app_direct_report_is_idempotent_by_run_id(self):
        case = self._make_case_with_steps()
        run_id = "app-run-0001"
        payload = self._app_report_payload(case, run_id)

        with patch.object(autojs_api.report_generator, "generate_report", return_value="report-app.html"), \
             patch.object(autojs_api.NotificationService, "send_report_card"):
            resp = self.client.post("/api/device-runs/report", json=payload, headers=self.headers)
        self.assertEqual(resp.status_code, 200, resp.text)
        body = resp.json()
        self.assertEqual(body["status"], "PASS")
        self.assertEqual(body["report_id"], "report-app.html")

        execution = self.session.get(TestExecution, body["execution_id"])
        self.assertEqual(execution.batch_id, run_id)
        self.assertEqual(execution.batch_name, "App 设备端执行")
        self.assertEqual(execution.device_serial, "autojs6:abc123")
        self.assertEqual(execution.scenario_name, case.name)

        # 重报同一 run_id：不产生重复执行记录
        with patch.object(autojs_api.report_generator, "generate_report", return_value="report-app-2.html"), \
             patch.object(autojs_api.NotificationService, "send_report_card"):
            resp2 = self.client.post("/api/device-runs/report", json=payload, headers=self.headers)
        self.assertEqual(resp2.status_code, 200)
        executions = self.session.exec(select(TestExecution)).all()
        self.assertEqual(len(executions), 1)

    def test_notify_flag_controls_feishu(self):
        case = self._make_case_with_steps()

        with patch.object(autojs_api.report_generator, "generate_report", return_value="r1.html"), \
             patch.object(autojs_api.NotificationService, "send_report_card") as notify_mock:
            resp = self.client.post(
                "/api/device-runs/report",
                json=self._app_report_payload(case, "app-run-no-notify", notify=False),
                headers=self.headers,
            )
        self.assertEqual(resp.status_code, 200)
        notify_mock.assert_not_called()

        with patch.object(autojs_api.report_generator, "generate_report", return_value="r2.html"), \
             patch.object(autojs_api.NotificationService, "send_report_card") as notify_mock2:
            resp = self.client.post(
                "/api/device-runs/report",
                json=self._app_report_payload(case, "app-run-with-notify", notify=True),
                headers=self.headers,
            )
        self.assertEqual(resp.status_code, 200)
        notify_mock2.assert_called_once()

    def test_app_script_bundle_endpoint(self):
        import base64
        import tempfile
        from pathlib import Path

        case = self._make_case_with_steps()
        # 加一个图像步骤并准备模板图（放到临时 PROJECT_ROOT 下，避免污染仓库）
        with tempfile.TemporaryDirectory() as tmp_root:
            images_dir = Path(tmp_root) / "static" / "images"
            images_dir.mkdir(parents=True)
            (images_dir / "element_ab12cd34.png").write_bytes(b"\x89PNG\r\n\x1a\nfake")
            self._add_step(
                case.id, 3, "click_image",
                args={"image_path": "static/images/element_ab12cd34.png"},
            )
            with patch("backend.autojs_export.packager.PROJECT_ROOT", Path(tmp_root)):
                resp = self.client.get(
                    f"/api/app-runs/cases/{case.id}/script", headers=self.headers
                )
        self.assertEqual(resp.status_code, 200, resp.text)
        bundle = resp.json()
        self.assertEqual(bundle["kind"], "case")
        self.assertIn("runtime_js", bundle)
        self.assertIn("createContext", bundle["runtime_js"])
        self.assertIn("__loadModule__", bundle["main_js"])
        self.assertEqual(len(bundle["case_files"]), 1)
        self.assertTrue(bundle["case_files"][0]["path"].startswith("cases/case_"))
        self.assertIn("element_ab12cd34.png", bundle["assets"])
        decoded = base64.b64decode(bundle["assets"]["element_ab12cd34.png"])
        self.assertTrue(decoded.startswith(b"\x89PNG"))

    def test_app_script_bundle_blocks_on_precheck_error(self):
        case = self._create_case(name="xpath用例")
        self._add_step(
            case.id, 1, "click",
            platform_overrides={"android": {"selector": "//*[@text='x']", "by": "xpath"}},
        )
        resp = self.client.get(f"/api/app-runs/cases/{case.id}/script", headers=self.headers)
        self.assertEqual(resp.status_code, 400)
        detail = resp.json()["detail"]
        codes = [issue["code"] for issue in detail.get("issues") or []]
        self.assertIn("EXPORT_UNSUPPORTED_LOCATOR", codes)

    def test_export_requires_auth(self):
        case = self._make_case_with_steps()
        resp = self.client.post(f"/api/autojs/cases/{case.id}/export")
        self.assertEqual(resp.status_code, 401)


if __name__ == "__main__":
    unittest.main()
