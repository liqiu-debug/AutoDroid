import os
import shutil
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

from fastapi import BackgroundTasks, HTTPException
from pydantic import ValidationError
from sqlalchemy import event
from sqlmodel import SQLModel, Session, create_engine, select

from backend.api.compatibility import (
    _execute_cell,
    _execute_cell_inspection_version_body,
    _execute_run_async,
    _abort_event_for_run,
    _discard_abort_event,
    _normalize_activity,
    compare_device_pages,
    compare_page_snapshots,
    cancel_run,
    create_run,
    delete_page_set,
    delete_run,
    get_run,
    list_runs,
)
from backend.api.packages import install_app_package_to_device
from backend.artifact_store import AssetCapacityExceeded
from backend.feature_flags import FLAG_MODEL_INSPECTION
from backend.models import (
    AppPackage,
    CompatPageSet,
    CompatibilityCell,
    CompatibilityPageResult,
    CompatibilityRun,
    Device,
    InspectionBranchRun,
    InspectionRun,
    InspectionState,
    SystemSetting,
    TestCase,
    User,
)
from backend.paths import project_path
from backend.schemas import CompatibilityRunCreate


class CompatibilityApiTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
        SQLModel.metadata.create_all(self.engine)
        self.session = Session(self.engine)
        self.temp_files = []
        self.capacity_patch = patch(
            "backend.api.compatibility.ensure_asset_capacity_for_new_run",
            return_value={"can_start": True},
        )
        self.capacity_mock = self.capacity_patch.start()

    def tearDown(self) -> None:
        self.capacity_patch.stop()
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

    def test_list_and_detail_batch_related_rows(self):
        user = self._user()
        page_set = self._case_and_page_set()
        runs = []
        for index in range(5):
            run = CompatibilityRun(
                name=f"batch-{index}",
                page_set_id=page_set.id,
                page_set_name=page_set.name,
                page_set_snapshot=list(page_set.pages),
                package_name="com.demo.app",
                status="PASS",
            )
            self.session.add(run)
            self.session.flush()
            for cell_index in range(3):
                cell = CompatibilityCell(
                    run_id=run.id,
                    device_serial=f"device-{index}-{cell_index}",
                    status="PASS",
                )
                self.session.add(cell)
                self.session.flush()
                self.session.add(
                    CompatibilityPageResult(
                        run_id=run.id,
                        cell_id=cell.id,
                        page_key="home",
                        page_name="Home",
                        status="PASS",
                    )
                )
            runs.append(run)
        self.session.commit()

        statements = []

        def before_cursor_execute(*args):
            statements.append(args[2])

        event.listen(self.engine, "before_cursor_execute", before_cursor_execute)
        try:
            listed = list_runs(
                limit=20,
                session=self.session,
                current_user=user,
            )
            list_query_count = len(statements)
            statements.clear()
            detailed = get_run(
                runs[0].id,
                session=self.session,
                current_user=user,
            )
            detail_query_count = len(statements)
        finally:
            event.remove(self.engine, "before_cursor_execute", before_cursor_execute)

        self.assertEqual(len(listed.items), 5)
        self.assertLessEqual(list_query_count, 3)
        self.assertEqual(len(detailed.cells), 3)
        self.assertEqual(sum(len(cell.pages) for cell in detailed.cells), 3)
        self.assertLessEqual(detail_query_count, 4)

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

    def test_create_run_rejects_critical_asset_storage(self):
        self.capacity_mock.side_effect = AssetCapacityExceeded(
            {"used_percent": 95.1, "can_start": False}
        )
        payload = CompatibilityRunCreate(
            name="blocked",
            new_package_id=1,
            page_set_id=1,
            device_serials=["android-1"],
        )
        with self.assertRaises(HTTPException) as context:
            create_run(
                payload,
                BackgroundTasks(),
                session=self.session,
                current_user=User(username="tester", hashed_password="x"),
            )
        self.assertEqual(context.exception.status_code, 507)
        self.assertEqual(context.exception.detail["storage"]["used_percent"], 95.1)

    def test_create_run_rejects_ios_package(self):
        user = self._user()
        ios_pkg = self._package("com.demo.ios", "2.0")
        ios_pkg.platform = "ios"
        self.session.add(ios_pkg)
        self.session.commit()
        page_set = self._case_and_page_set()
        self._device()

        payload = CompatibilityRunCreate(
            name="compat-ios",
            old_package_id=None,
            new_package_id=ios_pkg.id,
            page_set_id=page_set.id,
            device_serials=["android-1"],
        )
        with self.assertRaises(HTTPException) as context:
            create_run(payload, BackgroundTasks(), session=self.session, current_user=user)
        self.assertEqual(context.exception.status_code, 400)
        self.assertIn("仅支持 Android APK", str(context.exception.detail))

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

    def test_snapshot_from_inspection_copies_sanitized_baseline_into_report(self):
        user = self._user()
        new_pkg = self._package("com.demo.app", "2.0")
        self._device()
        self.session.add(SystemSetting(key=FLAG_MODEL_INSPECTION, value="true"))
        source_run = InspectionRun(
            name="inspection source",
            package_name="com.demo.app",
            device_serial="android-1",
            status="PASS",
            profile_snapshot={
                "branches": {
                    "guest": {
                        "name": "未登录",
                        "prepare_case_id": 1,
                        "entry_case_id": 1,
                        "ready_assertion": {
                            "selector": "首页",
                            "by": "description",
                        },
                    }
                },
                "input_rules": [],
                "sanitizer_rules": [],
                "budgets": {"stable_wait_seconds": 5},
            },
        )
        self.session.add(source_run)
        self.session.flush()
        branch = InspectionBranchRun(
            run_id=source_run.id,
            branch_key="guest",
            branch_name="未登录",
            status="PASS",
        )
        self.session.add(branch)
        self.session.flush()
        state = InspectionState(
            run_id=source_run.id,
            branch_run_id=branch.id,
            branch_key="guest",
            cluster_key="cluster-home",
            state_key="state-home",
            activity=".Home",
            stable_status="STABLE",
            locator_quality="DESCRIPTION",
            selected_for_regression=True,
            screenshot_path=(
                f"inspection/{source_run.id}/guest/home/screenshot.png"
            ),
            xml_path=f"inspection/{source_run.id}/guest/home/hierarchy.xml",
            first_path=[],
        )
        self.session.add(state)
        self.session.commit()
        self.session.refresh(state)

        with tempfile.TemporaryDirectory() as temp_root:
            reports = Path(temp_root) / "reports"
            source_dir = (
                reports
                / "inspection"
                / str(source_run.id)
                / "guest"
                / "home"
            )
            source_dir.mkdir(parents=True)
            (source_dir / "screenshot.png").write_bytes(b"sanitized-png")
            (source_dir / "hierarchy.xml").write_text(
                "<hierarchy/>",
                encoding="utf-8",
            )

            def fake_project_path(*parts):
                return Path(temp_root).joinpath(*parts)

            tasks = BackgroundTasks()
            with patch(
                "backend.api.compatibility.project_path",
                side_effect=fake_project_path,
            ), patch(
                "backend.inspection.engine.project_path",
                side_effect=fake_project_path,
            ):
                created = create_run(
                    CompatibilityRunCreate(
                        name="inspection snapshot",
                        old_package_id=None,
                        new_package_id=new_pkg.id,
                        source_type="inspection",
                        inspection_run_id=source_run.id,
                        inspection_state_ids=[state.id],
                        device_serials=["android-1"],
                        compare_mode="snapshot",
                    ),
                    tasks,
                    session=self.session,
                    current_user=user,
                )

            self.assertEqual(created.source_type, "inspection")
            self.assertEqual(created.compare_mode, "snapshot")
            self.assertEqual(created.inspection_state_ids, [state.id])
            self.assertEqual(len(tasks.tasks), 1)
            copied_page = created.page_set_snapshot[0]
            copied_screenshot = reports / copied_page.baseline_screenshot_path
            copied_xml = reports / copied_page.baseline_xml_path
            self.assertEqual(copied_screenshot.read_bytes(), b"sanitized-png")
            self.assertEqual(copied_xml.read_text(encoding="utf-8"), "<hierarchy/>")
            self.assertIn(
                f"compatibility/{created.id}/inspection_baseline",
                copied_page.baseline_screenshot_path,
            )

    async def test_inspection_version_prepares_only_before_in_place_upgrade(self):
        user = self._user()
        old_pkg = self._package("com.demo.app", "1.0")
        new_pkg = self._package("com.demo.app", "2.0")
        device = self._device()
        run = CompatibilityRun(
            name="inspection version",
            source_type="inspection",
            inspection_run_id=11,
            inspection_state_ids=[21],
            old_package_id=old_pkg.id,
            new_package_id=new_pkg.id,
            package_name="com.demo.app",
            compare_mode="version",
            mode="upgrade",
            device_serials=[device.serial],
            status="RUNNING",
            user_id=user.id,
        )
        self.session.add(run)
        self.session.flush()
        cell = CompatibilityCell(
            run_id=run.id,
            device_serial=device.serial,
            status="RUNNING",
        )
        self.session.add(cell)
        self.session.commit()
        self.session.refresh(run)
        self.session.refresh(cell)
        page = {
            "key": "guest-home",
            "name": "未登录首页",
            "inspection_state_id": 21,
            "branch_key": "guest",
            "branch_config": {
                "prepare_case_id": 1,
                "entry_case_id": 2,
                "ready_assertion": {
                    "selector": "首页",
                    "by": "description",
                },
            },
        }
        install = AsyncMock()
        prepare = Mock()
        capture = AsyncMock(
            side_effect=[
                {"screenshot_path": "old.png", "xml_text": "<old/>"},
                {"screenshot_path": "new.png", "xml_text": "<new/>"},
            ]
        )

        with patch(
            "backend.api.compatibility.install_app_package_to_device",
            new=install,
        ), patch(
            "backend.api.compatibility._prepare_inspection_page_group",
            new=prepare,
        ), patch(
            "backend.api.compatibility._run_page_capture",
            new=capture,
        ), patch(
            "backend.api.compatibility._record_compare_result",
        ):
            await _execute_cell_inspection_version_body(
                self.session,
                run,
                cell,
                [page],
                threading.Event(),
                run.id,
            )

        self.assertEqual(prepare.call_count, 1)
        self.assertEqual(capture.await_count, 2)
        self.assertEqual(install.await_count, 2)
        old_call, new_call = install.await_args_list
        self.assertEqual(old_call.kwargs["package_id"], old_pkg.id)
        self.assertTrue(old_call.kwargs["uninstall_first"])
        self.assertEqual(new_call.kwargs["package_id"], new_pkg.id)
        self.assertFalse(new_call.kwargs["uninstall_first"])
        self.assertFalse(new_call.kwargs["allow_uninstall_retry"])

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

    async def test_cancelled_pending_cell_cannot_be_resurrected_by_worker(self):
        user = self._user()
        old_pkg = self._package("com.demo.app", "1.0")
        new_pkg = self._package("com.demo.app", "2.0")
        page_set = self._case_and_page_set()
        self._device()
        created = create_run(
            CompatibilityRunCreate(
                name="cancel race",
                old_package_id=old_pkg.id,
                new_package_id=new_pkg.id,
                page_set_id=page_set.id,
                device_serials=["android-1"],
            ),
            BackgroundTasks(),
            session=self.session,
            current_user=user,
        )
        cell = self.session.exec(
            select(CompatibilityCell).where(CompatibilityCell.run_id == created.id)
        ).one()
        event = _abort_event_for_run(created.id)
        try:
            response = cancel_run(created.id, session=self.session, current_user=user)
            self.assertEqual(response["status"], "ABORTED")
            with patch("backend.api.compatibility.engine", self.engine):
                await _execute_cell(
                    created.id,
                    cell.id,
                    [],
                    event,
                )
            self.session.refresh(self.session.get(CompatibilityRun, created.id))
            self.session.refresh(self.session.get(CompatibilityCell, cell.id))
            self.assertEqual(
                self.session.get(CompatibilityRun, created.id).status,
                "ABORTED",
            )
            self.assertEqual(
                self.session.get(CompatibilityCell, cell.id).status,
                "ABORTED",
            )
        finally:
            _discard_abort_event(created.id)

    def test_delete_aborted_run_with_running_cell_is_rejected(self):
        user = self._user()
        old_pkg = self._package("com.demo.app", "1.0")
        new_pkg = self._package("com.demo.app", "2.0")
        page_set = self._case_and_page_set()
        self._device()
        created = create_run(
            CompatibilityRunCreate(
                name="aborted cleanup race",
                old_package_id=old_pkg.id,
                new_package_id=new_pkg.id,
                page_set_id=page_set.id,
                device_serials=["android-1"],
            ),
            BackgroundTasks(),
            session=self.session,
            current_user=user,
        )
        row = self.session.get(CompatibilityRun, created.id)
        row.status = "ABORTED"
        cell = self.session.exec(
            select(CompatibilityCell).where(CompatibilityCell.run_id == created.id)
        ).one()
        cell.status = "RUNNING"
        self.session.add(row)
        self.session.add(cell)
        self.session.commit()

        with self.assertRaises(HTTPException) as context:
            delete_run(created.id, session=self.session, current_user=user)
        self.assertEqual(context.exception.status_code, 409)
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


class CompatibilityListRunsFilterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
        SQLModel.metadata.create_all(self.engine)
        self.session = Session(self.engine)
        self.user = User(username="viewer", hashed_password="x")
        self.session.add(self.user)

        package = AppPackage(app_name="Demo", package_name="com.demo.app", file_path="/tmp/demo.apk")
        self.session.add(package)
        self.session.commit()
        self.session.refresh(package)

        runs = [
            CompatibilityRun(name="升级验证-A", package_name="com.demo.app", new_package_id=package.id, status="PASS"),
            CompatibilityRun(name="升级验证-B", package_name="com.demo.app", new_package_id=package.id, status="WARNING"),
            CompatibilityRun(name="夜间巡检", package_name="com.other.app", new_package_id=package.id, status="RUNNING"),
            CompatibilityRun(name="失败任务", package_name="com.other.app", new_package_id=package.id, status="ERROR"),
        ]
        self.session.add_all(runs)
        self.session.commit()

    def tearDown(self) -> None:
        self.session.close()

    def test_keyword_matches_name_or_package(self):
        by_name = list_runs(keyword="升级验证", session=self.session, current_user=self.user)
        self.assertEqual(by_name.total, 2)

        by_package = list_runs(keyword="com.other", session=self.session, current_user=self.user)
        self.assertEqual(by_package.total, 2)
        self.assertEqual({item.package_name for item in by_package.items}, {"com.other.app"})

    def test_status_filter_groups_running_and_fail(self):
        running = list_runs(status="running", session=self.session, current_user=self.user)
        self.assertEqual(running.total, 1)
        self.assertEqual(running.items[0].status, "RUNNING")

        failed = list_runs(status="fail", session=self.session, current_user=self.user)
        self.assertEqual(failed.total, 1)
        self.assertEqual(failed.items[0].status, "ERROR")

        everything = list_runs(status="all", session=self.session, current_user=self.user)
        self.assertEqual(everything.total, 4)

    def test_pagination_keeps_filtered_total(self):
        result = list_runs(keyword="升级验证", skip=1, limit=1, session=self.session, current_user=self.user)
        self.assertEqual(result.total, 2)
        self.assertEqual(len(result.items), 1)


class DeviceCompareSchemaTests(unittest.TestCase):
    def _payload(self, **overrides):
        data = {
            "name": "机型对比",
            "old_package_id": None,
            "new_package_id": 1,
            "page_set_id": 1,
            "device_serials": ["android-1", "android-2"],
            "compare_mode": "device",
        }
        data.update(overrides)
        return data

    def test_device_mode_rejects_old_package(self):
        with self.assertRaises(ValidationError) as context:
            CompatibilityRunCreate(**self._payload(old_package_id=9))
        self.assertIn("old_package_id", str(context.exception))

    def test_device_mode_requires_two_devices(self):
        with self.assertRaises(ValidationError) as context:
            CompatibilityRunCreate(**self._payload(device_serials=["android-1"]))
        self.assertIn("2 台设备", str(context.exception))

    def test_device_mode_rejects_unknown_baseline(self):
        with self.assertRaises(ValidationError) as context:
            CompatibilityRunCreate(**self._payload(baseline_device_serial="android-9"))
        self.assertIn("基准设备", str(context.exception))

    def test_device_mode_defaults_baseline_to_first_device(self):
        payload = CompatibilityRunCreate(**self._payload())
        self.assertEqual(payload.baseline_device_serial, "android-1")

    def test_version_mode_clears_baseline(self):
        payload = CompatibilityRunCreate(
            **self._payload(compare_mode="version", baseline_device_serial="android-1")
        )
        self.assertIsNone(payload.baseline_device_serial)

    def test_invalid_compare_mode_rejected(self):
        with self.assertRaises(ValidationError):
            CompatibilityRunCreate(**self._payload(compare_mode="matrix"))

    def test_inspection_snapshot_rejects_old_package(self):
        with self.assertRaises(ValidationError):
            CompatibilityRunCreate(
                name="巡检快照",
                old_package_id=9,
                new_package_id=1,
                source_type="inspection",
                inspection_run_id=8,
                device_serials=["android-1"],
                compare_mode="snapshot",
            )

    def test_inspection_version_requires_explicit_old_package_and_upgrade(self):
        base = {
            "name": "巡检版本",
            "new_package_id": 2,
            "source_type": "inspection",
            "inspection_run_id": 8,
            "device_serials": ["android-1"],
            "compare_mode": "version",
        }
        with self.assertRaises(ValidationError):
            CompatibilityRunCreate(**base)
        with self.assertRaises(ValidationError):
            CompatibilityRunCreate(**base, old_package_id=1, mode="clean")
        payload = CompatibilityRunCreate(
            **base,
            old_package_id=1,
            mode="upgrade",
        )
        self.assertEqual(payload.old_package_id, 1)
        self.assertEqual(payload.mode, "upgrade")


class NormalizeActivityTests(unittest.TestCase):
    def test_extracts_component_from_focus_line_with_hash(self):
        raw = "mCurrentFocus=Window{a1b2c3 u0 com.demo.app/com.demo.app.ui.MainActivity}"
        self.assertEqual(_normalize_activity(raw), "com.demo.app/com.demo.app.ui.MainActivity")

    def test_expands_relative_activity(self):
        raw = "  mFocusedApp=ActivityRecord{deadbeef u0 com.demo.app/.ui.MainActivity t123}"
        self.assertEqual(_normalize_activity(raw), "com.demo.app/com.demo.app.ui.MainActivity")

    def test_returns_empty_without_component(self):
        self.assertEqual(_normalize_activity("mCurrentFocus=Window{abc u0 NotificationShade}"), "")
        self.assertEqual(_normalize_activity(""), "")


class CompatibilityDeviceCompareTests(unittest.TestCase):
    RUN_ID = 990001

    def setUp(self) -> None:
        self.root = project_path("reports", f"compat_test_{next(tempfile._get_candidate_names())}")
        self.root.mkdir(parents=True, exist_ok=True)

    def tearDown(self) -> None:
        shutil.rmtree(self.root, ignore_errors=True)
        shutil.rmtree(project_path("reports", "compatibility", str(self.RUN_ID)), ignore_errors=True)

    def _image(self, name: str, size=(100, 100), changed: bool = False) -> str:
        from PIL import Image, ImageDraw

        image = Image.new("RGB", size, "white")
        if changed:
            draw = ImageDraw.Draw(image)
            draw.rectangle((0, 0, size[0] // 2, size[1] // 2), fill="black")
        path = self.root / name
        image.save(path)
        return path.relative_to(project_path("reports")).as_posix()

    def _baseline(self, **overrides):
        data = {
            "screenshot_path": self._image("base.png"),
            "xml_text": "<root text='A'/>",
            "activity": "mCurrentFocus=Window{abc u0 com.demo/com.demo.MainActivity}",
            "device_serial": "base-1",
        }
        data.update(overrides)
        return data

    def _candidate(self, **overrides):
        data = {
            "xml_text": "<root text='A'/>",
            "activity": "mCurrentFocus=Window{def u0 com.demo/com.demo.MainActivity}",
            "has_crash_or_anr": False,
            "required_text_missing": False,
        }
        data.update(overrides)
        if not data.get("screenshot_path"):
            data["screenshot_path"] = self._image("cand.png")
        return data

    def _compare(self, baseline, candidate, page=None, thresholds=None):
        return compare_device_pages(
            baseline=baseline,
            candidate=candidate,
            page=page or {"key": "home"},
            thresholds=thresholds or {},
            run_id=self.RUN_ID,
            cell_id=1,
        )

    def test_same_resolution_visual_diff_is_warning_with_diff_image(self):
        result = self._compare(
            self._baseline(),
            self._candidate(screenshot_path=self._image("cand.png", changed=True)),
            thresholds={"pixel_diff_ratio_warn": 0.01, "ssim_warn": 0.99},
        )
        self.assertEqual(result["status"], "WARNING")
        self.assertTrue(result["metrics"]["same_resolution"])
        self.assertIsNotNone(result["diff_screenshot_path"])

    def test_cross_resolution_visual_diff_does_not_gate(self):
        result = self._compare(
            self._baseline(),
            self._candidate(screenshot_path=self._image("cand.png", size=(80, 160), changed=True)),
            thresholds={"pixel_diff_ratio_warn": 0.0001, "ssim_warn": 0.9999},
        )
        self.assertEqual(result["status"], "PASS")
        self.assertFalse(result["metrics"]["same_resolution"])
        self.assertIsNone(result["diff_screenshot_path"])
        self.assertIn("pixel_diff_ratio", result["metrics"])

    def test_crash_is_fail(self):
        result = self._compare(self._baseline(), self._candidate(has_crash_or_anr=True))
        self.assertEqual(result["status"], "FAIL")
        self.assertIn("Crash", result["reason"])

    def test_required_text_missing_is_fail(self):
        result = self._compare(
            self._baseline(),
            self._candidate(required_text_missing=True),
            page={"key": "home", "required_text": "Welcome"},
        )
        self.assertEqual(result["status"], "FAIL")
        self.assertIn("必需文本", result["reason"])

    def test_activity_mismatch_is_fail(self):
        result = self._compare(
            self._baseline(),
            self._candidate(activity="mCurrentFocus=Window{xyz u0 com.demo/com.demo.ErrorActivity}"),
        )
        self.assertEqual(result["status"], "FAIL")
        self.assertIn("Activity", result["reason"])
        self.assertTrue(result["metrics"]["activity_mismatch"])

    def test_relative_activity_component_matches_full_form(self):
        result = self._compare(
            self._baseline(activity="mFocusedApp=ActivityRecord{aa u0 com.demo/.MainActivity t7}"),
            self._candidate(activity="mCurrentFocus=Window{bb u0 com.demo/com.demo.MainActivity}"),
        )
        self.assertEqual(result["status"], "PASS")
        self.assertFalse(result["metrics"]["activity_mismatch"])

    def test_xml_structure_diff_is_warning_across_resolutions(self):
        result = self._compare(
            self._baseline(xml_text="<root><node text='A'/><node text='B'/><node text='C'/></root>"),
            self._candidate(
                screenshot_path=self._image("cand.png", size=(80, 160)),
                xml_text="<other><widget text='X'/></other>",
            ),
            thresholds={"xml_diff_ratio_warn": 0.2},
        )
        self.assertEqual(result["status"], "WARNING")
        self.assertIn("UI 层级", result["reason"])


class CompatibilityDeviceRunTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.assets_root = project_path("reports", f"compat_test_{next(tempfile._get_candidate_names())}")
        self.assets_root.mkdir(parents=True, exist_ok=True)
        db = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        db.close()
        self.db_path = db.name
        self.engine = create_engine(f"sqlite:///{db.name}", connect_args={"check_same_thread": False})
        SQLModel.metadata.create_all(self.engine)
        self.run_id = None

    def tearDown(self) -> None:
        shutil.rmtree(self.assets_root, ignore_errors=True)
        if self.run_id:
            shutil.rmtree(project_path("reports", "compatibility", str(self.run_id)), ignore_errors=True)
        if os.path.exists(self.db_path):
            os.remove(self.db_path)

    def _write_snapshot_assets(self, name: str, size=(100, 100), xml_text: str = "<root text='A'/>"):
        from PIL import Image

        image_path = self.assets_root / f"{name}.png"
        Image.new("RGB", size, "white").save(image_path)
        xml_path = self.assets_root / f"{name}.xml"
        xml_path.write_text(xml_text, encoding="utf-8")
        reports_root = project_path("reports")
        return {
            "screenshot_path": image_path.relative_to(reports_root).as_posix(),
            "xml_path": xml_path.relative_to(reports_root).as_posix(),
            "xml_text": xml_text,
            "activity": "mCurrentFocus=Window{abc u0 com.demo/com.demo.MainActivity}",
            "logcat_errors": "",
        }

    def _seed_device_run(self, session: Session):
        package = AppPackage(
            app_name="Demo",
            package_name="com.demo.app",
            version_name="2.0",
            version_code="20",
            file_path="/tmp/demo.apk",
        )
        session.add(package)
        session.add(Device(serial="base-1", platform="android", model="Pixel", status="IDLE", resolution="100x100"))
        session.add(Device(serial="other-1", platform="android", model="Mi", status="IDLE", resolution="80x160"))
        session.commit()
        session.refresh(package)

        run = CompatibilityRun(
            name="机型对比",
            page_set_id=None,
            old_package_id=None,
            new_package_id=package.id,
            package_name="com.demo.app",
            compare_mode="device",
            baseline_device_serial="base-1",
            mode="clean",
            device_serials=["base-1", "other-1"],
            thresholds={},
            status="PENDING",
            total_cells=2,
        )
        session.add(run)
        session.commit()
        session.refresh(run)
        baseline_cell = CompatibilityCell(
            run_id=run.id, device_serial="base-1", resolution="100x100", is_baseline=True, status="PENDING"
        )
        other_cell = CompatibilityCell(
            run_id=run.id, device_serial="other-1", resolution="80x160", is_baseline=False, status="PENDING"
        )
        session.add(baseline_cell)
        session.add(other_cell)
        session.commit()
        session.refresh(baseline_cell)
        session.refresh(other_cell)
        return run.id, baseline_cell.id, other_cell.id

    async def test_device_run_cross_compares_against_baseline(self):
        with Session(self.engine) as session:
            run_id, baseline_cell_id, other_cell_id = self._seed_device_run(session)
        self.run_id = run_id

        snapshots = {
            "base-1": self._write_snapshot_assets("base", size=(100, 100)),
            "other-1": self._write_snapshot_assets("other", size=(80, 160)),
        }

        async def fake_capture(**kwargs):
            return snapshots[kwargs["cell"].device_serial]

        pages = [{"key": "home", "name": "Home", "case_id": 1, "settle_seconds": 0}]
        with patch("backend.api.compatibility.engine", self.engine), \
                patch("backend.api.compatibility.install_app_package_to_device", new=AsyncMock()) as install_mock, \
                patch("backend.api.compatibility._run_page_capture", new=AsyncMock(side_effect=fake_capture)):
            await _execute_run_async(run_id, pages)

        self.assertEqual(install_mock.await_count, 2)
        for call in install_mock.await_args_list:
            self.assertTrue(call.kwargs["uninstall_first"])

        with Session(self.engine) as session:
            run = session.get(CompatibilityRun, run_id)
            self.assertEqual(run.status, "PASS")

            baseline_cell = session.get(CompatibilityCell, baseline_cell_id)
            other_cell = session.get(CompatibilityCell, other_cell_id)
            self.assertEqual(baseline_cell.status, "PASS")
            self.assertEqual(other_cell.status, "PASS")
            self.assertEqual(baseline_cell.current_stage, "完成")

            baseline_row = session.exec(
                select(CompatibilityPageResult).where(CompatibilityPageResult.cell_id == baseline_cell_id)
            ).first()
            self.assertEqual(baseline_row.status, "PASS")
            self.assertTrue(baseline_row.metrics.get("is_baseline"))
            self.assertIsNone(baseline_row.baseline_screenshot_path)

            other_row = session.exec(
                select(CompatibilityPageResult).where(CompatibilityPageResult.cell_id == other_cell_id)
            ).first()
            self.assertEqual(other_row.status, "PASS")
            self.assertEqual(other_row.baseline_screenshot_path, snapshots["base-1"]["screenshot_path"])
            self.assertEqual(other_row.candidate_screenshot_path, snapshots["other-1"]["screenshot_path"])
            self.assertFalse(other_row.metrics.get("same_resolution"))
            self.assertEqual(other_row.metrics.get("baseline_device_serial"), "base-1")
            self.assertIsNone(other_row.diff_screenshot_path)

    async def test_device_run_baseline_capture_failure_propagates_error(self):
        with Session(self.engine) as session:
            run_id, baseline_cell_id, other_cell_id = self._seed_device_run(session)
        self.run_id = run_id

        other_snapshot = self._write_snapshot_assets("other", size=(80, 160))

        async def fake_capture(**kwargs):
            if kwargs["cell"].device_serial == "base-1":
                raise RuntimeError("baseline device offline")
            return other_snapshot

        pages = [{"key": "home", "name": "Home", "case_id": 1, "settle_seconds": 0}]
        with patch("backend.api.compatibility.engine", self.engine), \
                patch("backend.api.compatibility.install_app_package_to_device", new=AsyncMock()), \
                patch("backend.api.compatibility._run_page_capture", new=AsyncMock(side_effect=fake_capture)):
            await _execute_run_async(run_id, pages)

        with Session(self.engine) as session:
            run = session.get(CompatibilityRun, run_id)
            self.assertEqual(run.status, "FAIL")

            baseline_row = session.exec(
                select(CompatibilityPageResult).where(CompatibilityPageResult.cell_id == baseline_cell_id)
            ).first()
            self.assertEqual(baseline_row.status, "FAIL")

            other_row = session.exec(
                select(CompatibilityPageResult).where(CompatibilityPageResult.cell_id == other_cell_id)
            ).first()
            self.assertEqual(other_row.status, "ERROR")
            self.assertIn("基准设备", other_row.reason)

            baseline_cell = session.get(CompatibilityCell, baseline_cell_id)
            other_cell = session.get(CompatibilityCell, other_cell_id)
            self.assertEqual(baseline_cell.status, "FAIL")
            self.assertEqual(other_cell.status, "FAIL")


class DeviceCompareCreateRunTests(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
        SQLModel.metadata.create_all(self.engine)
        self.session = Session(self.engine)
        self.temp_files = []
        self.capacity_patch = patch(
            "backend.api.compatibility.ensure_asset_capacity_for_new_run",
            return_value={"can_start": True},
        )
        self.capacity_patch.start()

    def tearDown(self) -> None:
        self.capacity_patch.stop()
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

    def test_create_device_run_marks_baseline_cell(self):
        user = User(username="tester", hashed_password="x")
        self.session.add(user)
        package = AppPackage(
            app_name="Demo",
            package_name="com.demo.app",
            version_name="2.0",
            file_path=self._file(),
        )
        self.session.add(package)
        case = TestCase(name="open page", steps=[], variables=[])
        self.session.add(case)
        self.session.commit()
        self.session.refresh(package)
        self.session.refresh(case)
        page_set = CompatPageSet(
            name="main pages",
            pages=[{"name": "Home", "case_id": case.id, "settle_seconds": 0}],
        )
        self.session.add(page_set)
        self.session.add(Device(serial="android-1", platform="android", model="Pixel", status="IDLE"))
        self.session.add(Device(serial="android-2", platform="android", model="Mi", status="IDLE"))
        self.session.commit()
        self.session.refresh(page_set)

        payload = CompatibilityRunCreate(
            name="机型对比",
            old_package_id=None,
            new_package_id=package.id,
            page_set_id=page_set.id,
            device_serials=["android-1", "android-2"],
            compare_mode="device",
            baseline_device_serial="android-2",
        )
        result = create_run(payload, BackgroundTasks(), session=self.session, current_user=user)

        self.assertEqual(result.compare_mode, "device")
        self.assertEqual(result.baseline_device_serial, "android-2")
        cells = self.session.exec(select(CompatibilityCell)).all()
        flags = {cell.device_serial: cell.is_baseline for cell in cells}
        self.assertEqual(flags, {"android-1": False, "android-2": True})
        baseline_cells = [cell for cell in result.cells if cell.is_baseline]
        self.assertEqual(len(baseline_cells), 1)
        self.assertEqual(baseline_cells[0].device_serial, "android-2")


if __name__ == "__main__":
    unittest.main()
