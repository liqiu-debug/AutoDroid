import unittest

from fastapi import HTTPException
from sqlmodel import Session, SQLModel, create_engine

from backend.api import folders as folders_api
from backend.api import scenarios as scenarios_api
from backend.models import ScenarioFolder, TestScenario, User
from backend.schemas import CaseFolderCreate, CaseFolderUpdate, TestScenarioCreate


class ScenarioFolderApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
        SQLModel.metadata.create_all(self.engine)
        self.session = Session(self.engine)
        self.user = User(username="owner", hashed_password="x", full_name="Owner")
        self.session.add(self.user)
        self.session.commit()
        self.session.refresh(self.user)

    def tearDown(self) -> None:
        self.session.close()

    def _folder(self, name: str, parent_id=None) -> ScenarioFolder:
        folder = ScenarioFolder(name=name, parent_id=parent_id)
        self.session.add(folder)
        self.session.commit()
        self.session.refresh(folder)
        return folder

    def _scenario(self, name: str, folder_id=None) -> TestScenario:
        scenario = TestScenario(name=name, folder_id=folder_id, user_id=self.user.id)
        self.session.add(scenario)
        self.session.commit()
        self.session.refresh(scenario)
        return scenario

    def test_create_rename_delete_folder(self):
        created = folders_api.create_scenario_folder(
            CaseFolderCreate(name="回归", parent_id=None), session=self.session
        )
        self.assertIsNotNone(created.id)
        self.assertEqual(created.name, "回归")

        renamed = folders_api.rename_scenario_folder(
            created.id, CaseFolderUpdate(name="冒烟"), session=self.session
        )
        self.assertEqual(renamed.name, "冒烟")

        result = folders_api.delete_scenario_folder(created.id, session=self.session)
        self.assertEqual(result["id"], created.id)
        self.assertIsNone(self.session.get(ScenarioFolder, created.id))

    def test_create_folder_rejects_missing_parent(self):
        with self.assertRaises(HTTPException) as ctx:
            folders_api.create_scenario_folder(
                CaseFolderCreate(name="child", parent_id=999), session=self.session
            )
        self.assertEqual(ctx.exception.status_code, 400)

    def test_delete_folder_rejects_children_and_linked_scenarios(self):
        parent = self._folder("parent")
        self._folder("child", parent_id=parent.id)
        with self.assertRaises(HTTPException) as ctx:
            folders_api.delete_scenario_folder(parent.id, session=self.session)
        self.assertEqual(ctx.exception.status_code, 400)

        leaf = self._folder("leaf")
        self._scenario("scenario-in-leaf", folder_id=leaf.id)
        with self.assertRaises(HTTPException) as ctx:
            folders_api.delete_scenario_folder(leaf.id, session=self.session)
        self.assertEqual(ctx.exception.status_code, 400)

    def test_tree_contains_folders_and_scenario_leaves(self):
        root = self._folder("root")
        child = self._folder("child", parent_id=root.id)
        in_child = self._scenario("s-child", folder_id=child.id)
        orphan = self._scenario("s-orphan")

        payload = folders_api.get_scenario_folder_tree(session=self.session)

        all_ids = {node["id"] for node in payload["all_scenarios"]}
        self.assertEqual(all_ids, {f"scenario-{in_child.id}", f"scenario-{orphan.id}"})

        self.assertEqual(len(payload["tree"]), 1)
        root_node = payload["tree"][0]
        self.assertEqual(root_node["folder_id"], root.id)
        child_node = next(n for n in root_node["children"] if n["type"] == "folder")
        self.assertEqual(child_node["folder_id"], child.id)
        leaf = next(n for n in child_node["children"] if n["type"] == "scenario")
        self.assertEqual(leaf["scenario_id"], in_child.id)
        self.assertTrue(leaf["is_leaf"])

    def test_move_scenario_between_folders(self):
        folder = self._folder("target")
        scenario = self._scenario("movable")

        result = folders_api.move_scenario(
            scenario.id, folders_api.MoveItemBody(folder_id=folder.id), session=self.session
        )
        self.assertEqual(result["scenario_id"], scenario.id)
        self.session.refresh(scenario)
        self.assertEqual(scenario.folder_id, folder.id)

        folders_api.move_scenario(
            scenario.id, folders_api.MoveItemBody(folder_id=None), session=self.session
        )
        self.session.refresh(scenario)
        self.assertIsNone(scenario.folder_id)

    def test_move_scenario_rejects_missing_targets(self):
        scenario = self._scenario("movable")
        with self.assertRaises(HTTPException) as ctx:
            folders_api.move_scenario(
                scenario.id, folders_api.MoveItemBody(folder_id=999), session=self.session
            )
        self.assertEqual(ctx.exception.status_code, 400)

        with self.assertRaises(HTTPException) as ctx:
            folders_api.move_scenario(
                999, folders_api.MoveItemBody(folder_id=None), session=self.session
            )
        self.assertEqual(ctx.exception.status_code, 404)

    def test_list_scenarios_filters_by_folder(self):
        folder = self._folder("filter")
        inside = self._scenario("inside", folder_id=folder.id)
        self._scenario("outside")

        response = scenarios_api.list_scenarios(folder_id=folder.id, session=self.session)
        self.assertEqual(response.total, 1)
        self.assertEqual([item.id for item in response.items], [inside.id])
        self.assertEqual(response.items[0].folder_id, folder.id)

        unfiltered = scenarios_api.list_scenarios(session=self.session)
        self.assertEqual(unfiltered.total, 2)

    def test_create_scenario_assigns_folder(self):
        folder = self._folder("new-home")
        created = scenarios_api.create_scenario(
            TestScenarioCreate(name="with-folder", folder_id=folder.id),
            session=self.session,
            current_user=self.user,
        )
        self.assertEqual(created.folder_id, folder.id)
        stored = self.session.get(TestScenario, created.id)
        self.assertEqual(stored.folder_id, folder.id)


if __name__ == "__main__":
    unittest.main()
