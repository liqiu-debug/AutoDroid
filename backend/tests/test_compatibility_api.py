import os
import shutil
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from fastapi import BackgroundTasks, HTTPException
from sqlmodel import SQLModel, Session, create_engine, select

from backend.api.compatibility import compare_page_snapshots, create_run, delete_page_set, delete_run, get_run, _execute_cell
from backend.api.packages import install_app_package_to_device
from backend.models import AppPackage, CompatPageSet, CompatibilityCell, CompatibilityPageResult, CompatibilityRun, Device, TestCase, User
from backend.paths import project_path
from backend.schemas import CompatibilityRunCreate


class CompatibilityApiTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
        SQLModel.metadata.create_all(self.engine)
        self.session = Session(self.engine)
        self.temp_files = []

    def tearDown(self) -> None:
        self.session.close()
        for path in self.temp_files:
            try:
                os.remove(path)
            except OSError:
                pass

    def _file(self) -> str:
        handle = tempfile.NamedTemporaryFile(suffix=".apk", delete=False)
        handle.write(b"apk")
        handle.close()
        self.temp_files.append(handle.name)
        return handle.name

    def _user(self) -> User:
        user = User(username="tester", hashed_password="x", full_name="Tester")
        self.session.add(user)
        self.session.commit()
        self.session.refresh(user)
        return user

    def _package(self, package_name: str, version: str) -> AppPackage:
        item = AppPackage(
            app_name="Demo",
            package_name=package_name,
            version_name=version,
            version_code=version.replace(".", ""),
            file_path=self._file(),
        )
        self.session.add(item)
        self.session.commit()
        self.session.refresh(item)
        return item

    def _case_and_page_set(self, pages=None) -> CompatPageSet:
        case = TestCase(name="open page", steps=[], variables=[])
        self.session.add(case)
        self.session.commit()
        self.session.refresh(case)
        page_set = CompatPageSet(
            name="main pages",
            pages=pages if pages is not None else [
                {"name": "Home", "case_id": case.id, "settle_seconds": 0}
            ],
        )
        self.session.add(page_set)
        self.session.commit()
        self.session.refresh(page_set)
        return page_set

    def _device(self, serial="android-1", platform="android", status="IDLE") -> Device:
        device = Device(serial=serial, platform=platform, model="Pixel", status=status)
        self.session.add(device)
        self.session.commit()
        return device

    def test_create_run_rejects_different_package_names(self):
        user = self._user()
        old_pkg = self._package("com.demo.old", "1.0")
        new_pkg = self._package("com.demo.new", "2.0")
        page_set = self._case_and_page_set()
        self._device()

        payload = CompatibilityRunCreate(
            name="compat",
            old_package_id=old_pkg.id,
            new_package_id=new_pkg.id,
            page_set_id=page_set.id,
            device_serials=["android-1"],
        )

        with self.assertRaises(HTTPException) as context:
            create_run(payload, BackgroundTasks(), session=self.session, current_user=user)
        self.assertEqual(context.exception.status_code, 400)
        self.assertIn("package_name", str(context.exception.detail))

    def test_create_run_rejects_ios_device(self):
        user = self._user()
        old_pkg = self._package("com.demo.app", "1.0")
        new_pkg = self._package("com.demo.app", "2.0")
        page_set = self._case_and_page_set()
        self._device(serial="ios-1", platform="ios")

        payload = CompatibilityRunCreate(
            name="compat",
            old_package_id=old_pkg.id,
            new_package_id=new_pkg.id,
            page_set_id=page_set.id,
            device_serials=["ios-1"],
        )

        with self.assertRaises(HTTPException) as context:
            create_run(payload, BackgroundTasks(), session=self.session, current_user=user)
        self.assertEqual(context.exception.status_code, 400)
        self.assertIn("仅支持 Android", str(context.exception.detail))

    def test_create_run_rejects_empty_page_set(self):
        user = self._user()
        old_pkg = self._package("com.demo.app", "1.0")
        new_pkg = self._package("com.demo.app", "2.0")
        page_set = self._case_and_page_set(pages=[])
        self._device()

        payload = CompatibilityRunCreate(
            name="compat",
            old_package_id=old_pkg.id,
            new_package_id=new_pkg.id,
            page_set_id=page_set.id,
            device_serials=["android-1"],
        )

        with self.assertRaises(HTTPException) as context:
            create_run(payload, BackgroundTasks(), session=self.session, current_user=user)
        self.assertEqual(context.exception.status_code, 400)
        self.assertIn("页面集合不能为空", str(context.exception.detail))

    def test_create_run_creates_cells_and_schedules_background_task(self):
        user = self._user()
        old_pkg = self._package("com.demo.app", "1.0")
        new_pkg = self._package("com.demo.app", "2.0")
        page_set = self._case_and_page_set()
        self._device()
        tasks = BackgroundTasks()

        payload = CompatibilityRunCreate(
            name="compat",
            old_package_id=old_pkg.id,
            new_package_id=new_pkg.id,
            page_set_id=page_set.id,
            device_serials=["android-1"],
        )

        result = create_run(payload, tasks, session=self.session, current_user=user)

        self.assertEqual(result.status, "PENDING")
        self.assertEqual(result.total_cells, 1)
        self.assertEqual(result.page_set_name, "main pages")
        self.assertEqual(result.page_set_snapshot[0].name, "Home")
        self.assertEqual(len(tasks.tasks), 1)
        cells = self.session.exec(select(CompatibilityCell)).all()
        self.assertEqual(len(cells), 1)
        self.assertEqual(cells[0].device_serial, "android-1")

    def test_delete_referenced_page_set_detaches_runs_and_preserves_report_snapshot(self):
        user = self._user()
        old_pkg = self._package("com.demo.app", "1.0")
        new_pkg = self._package("com.demo.app", "2.0")
        page_set = self._case_and_page_set()
        self._device()

        payload = CompatibilityRunCreate(
            name="compat",
            old_package_id=old_pkg.id,
            new_package_id=new_pkg.id,
            page_set_id=page_set.id,
            device_serials=["android-1"],
        )
        created = create_run(payload, BackgroundTasks(), session=self.session, current_user=user)

        response = delete_page_set(page_set.id, session=self.session, current_user=user)

        self.assertEqual(response["success"], True)
        self.assertEqual(response["detached_runs"], 1)
        self.assertIsNone(self.session.get(CompatPageSet, page_set.id))
        run_row = self.session.get(CompatibilityRun, created.id)
        self.assertIsNone(run_row.page_set_id)
        self.assertEqual(run_row.page_set_name, "main pages")
        self.assertEqual(run_row.page_set_snapshot[0]["name"], "Home")

        report = get_run(created.id, session=self.session, current_user=user)
        self.assertIsNone(report.page_set_id)
        self.assertEqual(report.page_set.name, "main pages")
        self.assertEqual(report.page_set.pages[0].name, "Home")

    def test_delete_run_removes_records_and_report_artifacts(self):
        user = self._user()
        old_pkg = self._package("com.demo.app", "1.0")
        new_pkg = self._package("com.demo.app", "2.0")
        page_set = self._case_and_page_set()
        self._device()
        created = create_run(
            CompatibilityRunCreate(
                name="compat",
                old_package_id=old_pkg.id,
                new_package_id=new_pkg.id,
                page_set_id=page_set.id,
                device_serials=["android-1"],
            ),
            BackgroundTasks(),
            session=self.session,
            current_user=user,
        )
        run_row = self.session.get(CompatibilityRun, created.id)
        run_row.status = "PASS"
        self.session.add(run_row)
        cell = self.session.exec(select(CompatibilityCell).where(CompatibilityCell.run_id == created.id)).first()
        self.session.add(
            CompatibilityPageResult(
                run_id=created.id,
                cell_id=cell.id,
                page_key="home",
                page_name="Home",
                status="PASS",
            )
        )
        self.session.commit()

        with tempfile.TemporaryDirectory() as temp_root:
            report_dir = Path(temp_root) / "reports" / "compatibility" / str(created.id)
            (report_dir / "diff").mkdir(parents=True)
            (report_dir / "diff" / "home.png").write_bytes(b"png")
            with patch(
                "backend.api.compatibility.project_path",
                side_effect=lambda *parts: Path(temp_root).joinpath(*parts),
            ):
                response = delete_run(created.id, session=self.session, current_user=user)

            self.assertEqual(response["success"], True)
            self.assertEqual(response["deleted_pages"], 1)
            self.assertEqual(response["deleted_cells"], 1)
            self.assertTrue(response["artifacts_deleted"])
            self.assertFalse(report_dir.exists())

        self.assertIsNone(self.session.get(CompatibilityRun, created.id))
        self.assertFalse(self.session.exec(select(CompatibilityCell).where(CompatibilityCell.run_id == created.id)).all())
        self.assertFalse(self.session.exec(select(CompatibilityPageResult).where(CompatibilityPageResult.run_id == created.id)).all())

    def test_delete_running_run_is_rejected_and_keeps_artifacts(self):
        user = self._user()
        old_pkg = self._package("com.demo.app", "1.0")
        new_pkg = self._package("com.demo.app", "2.0")
        page_set = self._case_and_page_set()
        self._device()
        created = create_run(
            CompatibilityRunCreate(
                name="compat",
                old_package_id=old_pkg.id,
                new_package_id=new_pkg.id,
                page_set_id=page_set.id,
                device_serials=["android-1"],
            ),
            BackgroundTasks(),
            session=self.session,
            current_user=user,
        )
        run_row = self.session.get(CompatibilityRun, created.id)
        run_row.status = "RUNNING"
        self.session.add(run_row)
        self.session.commit()

        with tempfile.TemporaryDirectory() as temp_root:
            report_dir = Path(temp_root) / "reports" / "compatibility" / str(created.id)
            report_dir.mkdir(parents=True)
            with patch(
                "backend.api.compatibility.project_path",
                side_effect=lambda *parts: Path(temp_root).joinpath(*parts),
            ):
                with self.assertRaises(HTTPException) as context:
                    delete_run(created.id, session=self.session, current_user=user)

            self.assertEqual(context.exception.status_code, 400)
            self.assertTrue(report_dir.exists())
            self.assertIsNotNone(self.session.get(CompatibilityRun, created.id))

    def test_create_run_allows_current_installed_baseline(self):
        user = self._user()
        new_pkg = self._package("com.demo.app", "2.0")
        page_set = self._case_and_page_set()
        self._device()
        tasks = BackgroundTasks()

        payload = CompatibilityRunCreate(
            name="compat current",
            old_package_id=None,
            new_package_id=new_pkg.id,
            page_set_id=page_set.id,
            device_serials=["android-1"],
        )

        result = create_run(payload, tasks, session=self.session, current_user=user)

        self.assertIsNone(result.old_package_id)
        self.assertEqual(result.new_package_id, new_pkg.id)
        self.assertEqual(result.package_name, "com.demo.app")
        self.assertEqual(len(tasks.tasks), 1)

    async def test_current_installed_baseline_skips_old_package_install(self):
        db = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        db.close()
        self.temp_files.append(db.name)
        engine = create_engine(f"sqlite:///{db.name}", connect_args={"check_same_thread": False})
        SQLModel.metadata.create_all(engine)

        with Session(engine) as session:
            new_pkg = AppPackage(
                app_name="Demo",
                package_name="com.demo.app",
                version_name="2.0",
                version_code="20",
                file_path=self._file(),
            )
            device = Device(serial="android-1", platform="android", model="Pixel", status="IDLE")
            run = CompatibilityRun(
                name="compat current",
                page_set_id=1,
                old_package_id=None,
                new_package_id=1,
                package_name="com.demo.app",
                device_serials=["android-1"],
                thresholds={},
                status="PENDING",
            )
            session.add(new_pkg)
            session.add(device)
            session.add(run)
            session.commit()
            session.refresh(new_pkg)
            run.new_package_id = new_pkg.id
            session.add(run)
            session.commit()
            session.refresh(run)
            cell = CompatibilityCell(run_id=run.id, device_serial="android-1", status="PENDING")
            session.add(cell)
            session.commit()
            session.refresh(cell)
            new_pkg_id = new_pkg.id
            run_id = run.id
            cell_id = cell.id

        pages = [{"key": "home", "name": "Home", "case_id": 1, "settle_seconds": 0}]

        def fake_record_compare(session, run, cell, page, baseline, candidate):
            session.add(
                CompatibilityPageResult(
                    run_id=run.id,
                    cell_id=cell.id,
                    page_key=page["key"],
                    page_name=page["name"],
                    case_id=page["case_id"],
                    status="PASS",
                )
            )
            session.commit()

        async def fake_capture(**kwargs):
            return {"screenshot_path": "compat/base.png", "xml_text": "", "logcat_errors": ""}

        with patch("backend.api.compatibility.engine", engine), \
                patch("backend.api.compatibility._ensure_package_installed", new=AsyncMock()) as ensure_mock, \
                patch("backend.api.compatibility.install_app_package_to_device", new=AsyncMock()) as install_mock, \
                patch("backend.api.compatibility._run_page_capture", new=AsyncMock(side_effect=fake_capture)), \
                patch("backend.api.compatibility._record_compare_result", side_effect=fake_record_compare):
            await _execute_cell(run_id, cell_id, pages, threading.Event())

        ensure_mock.assert_awaited_once_with("android-1", "com.demo.app")
        install_mock.assert_awaited_once()
        self.assertEqual(install_mock.await_args.kwargs["package_id"], new_pkg_id)

    async def test_upgrade_install_failure_does_not_uninstall_retry(self):
        pkg = self._package("com.demo.app", "2.0")
        self._device()
        commands = []

        async def fake_adb(cmd, timeout=120):
            commands.append(cmd)
            return "Failure [INSTALL_FAILED_VERSION_DOWNGRADE]"

        with patch("backend.api.packages._run_adb_command", new=AsyncMock(side_effect=fake_adb)):
            with self.assertRaises(HTTPException):
                await install_app_package_to_device(
                    session=self.session,
                    package_id=pkg.id,
                    serial="android-1",
                    require_idle=False,
                    uninstall_first=False,
                    allow_uninstall_retry=False,
                    allow_downgrade=False,
                )

        self.assertEqual(len(commands), 1)
        self.assertIn(" install -r -t ", commands[0])
        self.assertNotIn(" uninstall ", commands[0])


class CompatibilityCompareTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = project_path("reports", f"compat_test_{next(tempfile._get_candidate_names())}")
        self.root.mkdir(parents=True, exist_ok=True)

    def tearDown(self) -> None:
        shutil.rmtree(self.root, ignore_errors=True)

    def _image(self, name: str, changed: bool = False) -> str:
        from PIL import Image, ImageDraw

        image = Image.new("RGB", (100, 100), "white")
        if changed:
            draw = ImageDraw.Draw(image)
            draw.rectangle((0, 0, 40, 40), fill="black")
        path = self.root / name
        image.save(path)
        return path.relative_to(project_path("reports")).as_posix()

    def test_visual_diff_exceeding_threshold_is_warning(self):
        baseline = {
            "screenshot_path": self._image("base.png"),
            "xml_text": "<root text='A'/>",
            "logcat_errors": "",
        }
        candidate = {
            "screenshot_path": self._image("candidate.png", changed=True),
            "xml_text": "<root text='A'/>",
            "logcat_errors": "",
        }

        result = compare_page_snapshots(
            baseline=baseline,
            candidate=candidate,
            page={"key": "home"},
            thresholds={"pixel_diff_ratio_warn": 0.01, "ssim_warn": 0.99},
            run_id=1,
            cell_id=1,
        )

        self.assertEqual(result["status"], "WARNING")
        self.assertGreater(result["metrics"]["pixel_diff_ratio"], 0.0)

    def test_required_text_missing_is_failure(self):
        image_path = self._image("same.png")
        result = compare_page_snapshots(
            baseline={"screenshot_path": image_path, "xml_text": "<root text='Welcome'/>", "logcat_errors": ""},
            candidate={"screenshot_path": image_path, "xml_text": "<root text='Home'/>", "logcat_errors": ""},
            page={"key": "home", "required_text": "Welcome"},
            thresholds={},
            run_id=2,
            cell_id=2,
        )

        self.assertEqual(result["status"], "FAIL")
        self.assertIn("必需文本", result["reason"])

    def test_required_text_present_in_xml_passes(self):
        image_path = self._image("same.png")
        result = compare_page_snapshots(
            baseline={"screenshot_path": image_path, "xml_text": "<root text='Welcome'/>", "logcat_errors": ""},
            candidate={"screenshot_path": image_path, "xml_text": "<root text='Welcome'/>", "logcat_errors": ""},
            page={"key": "home", "required_text": "Welcome"},
            thresholds={},
            run_id=3,
            cell_id=3,
        )

        self.assertEqual(result["status"], "PASS")
        self.assertFalse(result["metrics"]["required_text_missing"])


if __name__ == "__main__":
    unittest.main()
