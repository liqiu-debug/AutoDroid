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
        case = CaseModel(name=name, steps=steps or [], variables=[], user_id=self.user.id)
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

        case_api.delete_test_case(case.id, session=self.session, current_user=self.user)

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

        case_api.delete_test_case(case.id, session=self.session, current_user=self.user)

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

    def test_cleanup_on_delete_removes_only_case_referenced_images(self):
        """编辑/删除路径只清「被删用例此前引用」的图，绝不全盘扫孤儿——
        全盘扫曾把测试库查不到引用的真实模板图批量误删。"""
        orphan_path = "static/images/element_feedface.png"
        used_path = "static/images/element_deadbabe.png"
        orphan_file = self._write_image(orphan_path)
        used_file = self._write_image(used_path)
        case = self._create_case(
            "case-orphan-sweep",
            steps=[Step(action="click_image", selector=used_path, selector_type="image", description="used")],
        )

        case_api.delete_test_case(case.id, session=self.session, current_user=self.user)

        self.assertFalse(used_file.exists())
        self.assertTrue(orphan_file.exists(), "无关孤儿图不应在用例编辑路径被删除")

    def test_sweep_orphan_template_images_age_gated(self):
        """显式孤儿清扫：无引用的旧图删除、有引用的保留、新图受年龄门槛保护。"""
        orphan_path = "static/images/element_feedface.png"
        used_path = "static/images/element_deadbabe.png"
        fresh_path = "static/images/element_f00d1234.png"
        orphan_file = self._write_image(orphan_path)
        used_file = self._write_image(used_path)
        fresh_file = self._write_image(fresh_path)
        self._create_case(
            "case-sweep-ref",
            steps=[Step(action="click_image", selector=used_path, selector_type="image", description="used")],
        )

        # 年龄门槛默认 24h：全部文件都是新的，一个都不该删
        swept = case_api.sweep_orphan_template_images(self.session)
        self.assertEqual(swept, [])
        self.assertTrue(orphan_file.exists())
        self.assertTrue(fresh_file.exists())

        # 门槛放开后：无引用的旧图/新图都会被清，被引用的保留
        swept = case_api.sweep_orphan_template_images(self.session, min_age_seconds=0)
        self.assertEqual(sorted(swept), sorted([orphan_path, fresh_path]))
        self.assertFalse(orphan_file.exists())
        self.assertTrue(used_file.exists())
        self.assertFalse(fresh_file.exists(), "min_age_seconds=0 时同样年龄的无引用新图会被清")

    def test_cleanup_accepts_pipe_separated_multi_template_paths(self):
        """`|` 连接的多模板兜底引用必须被识别——否则清理器会把两张都当孤儿。"""
        first = "static/images/element_aaaa1111.png"
        second = "static/images/element_bbbb2222.png"
        first_file = self._write_image(first)
        second_file = self._write_image(second)
        case = self._create_case(
            "case-multi-template",
            steps=[Step(action="click_image", selector=f"{first}|{second}", selector_type="image", description="multi")],
        )

        case_api.delete_test_case(case.id, session=self.session, current_user=self.user)

        self.assertFalse(first_file.exists())
        self.assertFalse(second_file.exists(), "多路径引用的兜底模板应随用例删除一并清理")

    def test_cleanup_accepts_locator_candidates_image_refs(self):
        """args.locator_candidates 里的 image 候选也计入引用。"""
        path = "static/images/element_cccc3333.png"
        image_file = self._write_image(path)
        case = self._create_case("case-locator-candidates", steps=[])
        self.session.add(
            CaseStepModel(
                case_id=case.id,
                order=1,
                action="click_image",
                args={"locator_candidates": [{"by": "image", "selector": path}]},
                execute_on=["android", "ios"],
            )
        )
        self.session.commit()

        case_api.delete_test_case(case.id, session=self.session, current_user=self.user)

        self.assertFalse(image_file.exists(), "locator_candidates 引用的模板应被识别并在删除用例时清理")

    def test_cleanup_skips_non_generated_image_paths(self):
        custom_path = "static/images/custom_login.png"
        custom_file = self._write_image(custom_path)
        case = self._create_case(
            "case-custom",
            steps=[Step(action="click_image", selector=custom_path, selector_type="image", description="custom")],
        )

        case_api.delete_test_case(case.id, session=self.session, current_user=self.user)

        self.assertTrue(custom_file.exists())


if __name__ == "__main__":
    unittest.main()
