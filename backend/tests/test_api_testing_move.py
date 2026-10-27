"""api-testing 条目（接口/场景）目录移动接口回归测试。

背景：接口自动化的目录树此前无法把条目拖拽进文件夹——后端只有
PUT /folders/{id}（改目录自身）没有条目移动接口。新增 PATCH
/{interfaces|scenarios}/{id}/move 与 UI 自动化的 move-case/move-scenario 同语义。
"""
import tempfile
import unittest
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlmodel import SQLModel, Session, create_engine

from backend.api.api_testing import router
from backend.api.deps import get_current_active_user
from backend.api_testing.schemas import Step, literal
from backend.database import get_session
from backend.models import User


class ApiTestingMoveTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="autodroid-move-test-")
        self.engine = create_engine(
            f"sqlite:///{Path(self.tmp.name) / 'test.db'}", connect_args={"check_same_thread": False}
        )
        SQLModel.metadata.create_all(self.engine)
        with Session(self.engine) as s:
            self.user = User(username="mover", hashed_password="unused", role="admin")
            s.add(self.user)
            s.commit()
            s.refresh(self.user)
            s.expunge(self.user)
        app = FastAPI()
        app.include_router(router, prefix="/api/api-testing")

        def session_override():
            with Session(self.engine) as session:
                yield session

        app.dependency_overrides[get_session] = session_override
        app.dependency_overrides[get_current_active_user] = lambda: self.user
        self.client = TestClient(app)

    def tearDown(self):
        self.client.close()
        self.engine.dispose()
        self.tmp.cleanup()

    def call(self, method, path, **kwargs):
        return getattr(self.client, method)("/api/api-testing" + path, **kwargs)

    def folder(self, name, kind):
        resp = self.call("post", "/folders", json={"name": name, "kind": kind, "parent_id": None})
        self.assertEqual(resp.status_code, 200, resp.text)
        return resp.json()

    def interface(self, name="api"):
        step = Step(id="first")
        step.snapshot.request.url = literal("https://example.com/x")
        resp = self.call(
            "post",
            "/interfaces",
            json={"name": name, "config": step.snapshot.model_dump(), "sample": {}},
        )
        self.assertEqual(resp.status_code, 200, resp.text)
        return resp.json()

    def test_move_interface_into_folder_and_back_to_root(self):
        folder = self.folder("登录相关", "interface")
        item = self.interface()

        resp = self.call("patch", f"/interfaces/{item['id']}/move", json={"folder_id": folder["id"]})
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertEqual(resp.json()["folder_id"], folder["id"])

        resp = self.call("patch", f"/interfaces/{item['id']}/move", json={"folder_id": None})
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertIsNone(resp.json()["folder_id"], "folder_id 为空应移出到未分组根节点")

    def test_move_rejects_folder_of_other_kind(self):
        scenario_folder = self.folder("场景目录", "scenario")
        item = self.interface()
        resp = self.call("patch", f"/interfaces/{item['id']}/move", json={"folder_id": scenario_folder["id"]})
        self.assertGreaterEqual(resp.status_code, 400, resp.text)

    def test_move_unknown_item_is_404(self):
        resp = self.call("patch", "/interfaces/99999/move", json={"folder_id": None})
        self.assertEqual(resp.status_code, 404)

    def test_move_scenario_into_folder(self):
        folder = self.folder("回归集", "scenario")
        step = Step(id="first", interface_id=None)
        resp = self.call(
            "post",
            "/scenarios",
            json={"name": "场景A", "steps": [step.model_dump()]},
        )
        self.assertEqual(resp.status_code, 200, resp.text)
        scenario = resp.json()

        resp = self.call("patch", f"/scenarios/{scenario['id']}/move", json={"folder_id": folder["id"]})
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertEqual(resp.json()["folder_id"], folder["id"])


if __name__ == "__main__":
    unittest.main()
