import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from sqlmodel import Session, SQLModel, create_engine

from backend.api import cases as case_api
from backend.models import TestCase as CaseModel, TestCaseStep as CaseStepModel, User
from backend.schemas import Step, TestCaseCreate as CaseWriteModel, TestCaseStepWrite as CaseStepWriteModel


class CaseImageCleanupTests(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
        SQLModel.metadata.create_all(self.engine)
        self.session = Session(self.engine)

        self.tempdir = tempfile.TemporaryDirectory()
        self.project_root = Path(self.tempdir.name)
        self.images_dir = self.project_root / "static" / "images"
        self.images_dir.mkdir(parents=True, exist_ok=True)

        self.project_root_patch = patch.object(case_api, "PROJECT_ROOT", self.project_root)
        self.auto_image_dir_patch = patch.object(case_api, "AUTO_TEMPLATE_IMAGE_DIR", self.images_dir)
        self.project_root_patch.start()
        self.auto_image_dir_patch.start()

        self.user = User(username="tester", hashed_password="secret")
        self.session.add(self.user)
        self.session.commit()
        self.session.refresh(self.user)

    def tearDown(self) -> None:
        self.project_root_patch.stop()
        self.auto_image_dir_patch.stop()
        self.session.close()
        self.tempdir.cleanup()

    def _write_image(self, relative_path: str) -> Path:
        file_path = self.project_root / relative_path
        file_path.parent.mkdir(parents=True, exist_ok=True)
        file_path.write_bytes(b"png")
        return file_path

    def _create_case(self, name: str, steps=None) -> CaseModel:
        case = CaseModel(name=name, steps=steps or [], variables=[])
        self.session.add(case)
        self.session.commit()
        self.session.refresh(case)
        return case

    def _add_standard_image_step(self, case_id: int, image_path: str, order: int = 1) -> None:
        self.session.add(
            CaseStepModel(
                case_id=case_id,
                order=order,
                action="click_image",
                args={"image_path": image_path},
                execute_on=["android", "ios"],
                platform_overrides={"android": {"selector": image_path, "by": "image"}},
                timeout=10,
                error_strategy="ABORT",
                description=f"image-{order}",
            )
        )
        self.session.commit()

    def test_delete_case_removes_unused_auto_template_image(self):
        image_path = "static/images/element_deadbeef.png"
        image_file = self._write_image(image_path)
        case = self._create_case(
            "case-delete",
            steps=[Step(action="click_image", selector=image_path, selector_type="image", description="img")],
        )

        case_api.delete_test_case(case.id, session=self.session)

        self.assertFalse(image_file.exists())

    def test_delete_case_keeps_shared_template_image(self):
        image_path = "static/images/element_cafe1234.png"
        image_file = self._write_image(image_path)
        case = self._create_case(
            "case-legacy",
            steps=[Step(action="click_image", selector=image_path, selector_type="image", description="img")],
        )
        other_case = self._create_case("case-standard", steps=[])
        self._add_standard_image_step(other_case.id, image_path)

        case_api.delete_test_case(case.id, session=self.session)

        self.assertTrue(image_file.exists())

    def test_update_case_deletes_replaced_legacy_template_image(self):
        old_path = "static/images/element_abcd1234.png"
        new_path = "static/images/element_dcba5678.png"
        old_file = self._write_image(old_path)
        new_file = self._write_image(new_path)
        case = self._create_case(
            "case-update",
            steps=[Step(action="click_image", selector=old_path, selector_type="image", description="old")],
        )

        case_api.update_test_case(
            case_id=case.id,
            test_case=CaseWriteModel(
                name="case-update",
                description=None,
                steps=[Step(action="click_image", selector=new_path, selector_type="image", description="new")],
                variables=[],
                tags=[],
                folder_id=None,
            ),
            session=self.session,
            current_user=self.user,
        )

        self.assertFalse(old_file.exists())
        self.assertTrue(new_file.exists())

    def test_replace_standard_steps_deletes_replaced_template_image(self):
        old_path = "static/images/element_1111aaaa.png"
        new_path = "static/images/element_2222bbbb.png"
        old_file = self._write_image(old_path)
        new_file = self._write_image(new_path)
        case = self._create_case("case-standard-replace", steps=[])
        self._add_standard_image_step(case.id, old_path)

        case_api.replace_case_standard_steps(
            case_id=case.id,
            steps=[
                CaseStepWriteModel(
                    order=1,
                    action="click_image",
                    args={"image_path": new_path},
                    execute_on=["android", "ios"],
                    platform_overrides={"android": {"selector": new_path, "by": "image"}},
                    timeout=10,
                    error_strategy="ABORT",
                    description="new-image",
                )
            ],
            session=self.session,
            current_user=self.user,
        )

        self.assertFalse(old_file.exists())
        self.assertTrue(new_file.exists())

    def test_cleanup_also_removes_existing_orphan_template_images(self):
        orphan_path = "static/images/element_feedface.png"
        used_path = "static/images/element_deadbabe.png"
        orphan_file = self._write_image(orphan_path)
        used_file = self._write_image(used_path)
        case = self._create_case(
            "case-orphan-sweep",
            steps=[Step(action="click_image", selector=used_path, selector_type="image", description="used")],
        )

        case_api.delete_test_case(case.id, session=self.session)

        self.assertFalse(used_file.exists())
        self.assertFalse(orphan_file.exists())

    def test_cleanup_skips_non_generated_image_paths(self):
        custom_path = "static/images/custom_login.png"
        custom_file = self._write_image(custom_path)
        case = self._create_case(
            "case-custom",
            steps=[Step(action="click_image", selector=custom_path, selector_type="image", description="custom")],
        )

        case_api.delete_test_case(case.id, session=self.session)

        self.assertTrue(custom_file.exists())


if __name__ == "__main__":
    unittest.main()
