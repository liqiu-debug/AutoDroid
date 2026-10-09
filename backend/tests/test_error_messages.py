import tempfile
import unittest
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlmodel import SQLModel, Session, create_engine

from backend.api import auth
from backend.api.api_testing import router as api_testing_router
from backend.api.deps import get_current_active_user
from backend.core.errors import install_error_handlers, location, translate
from backend.core.security import get_password_hash
from backend.database import get_session
from backend.models import User


class ErrorMessageTests(unittest.TestCase):
    """Every message a user can see must be Chinese and keep its structure."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="autodroid-errors-")
        self.engine = create_engine(f"sqlite:///{Path(self.tmp.name) / 'test.db'}", connect_args={"check_same_thread": False})
        SQLModel.metadata.create_all(self.engine)
        with Session(self.engine) as session:
            self.user = User(username="tester", hashed_password=get_password_hash("right"), role="admin")
            session.add(self.user)
            session.commit()
            session.refresh(self.user)
            session.expunge(self.user)
        self.app = install_error_handlers(FastAPI())
        self.app.include_router(auth.router, prefix="/api/auth")
        self.app.include_router(api_testing_router, prefix="/api/api-testing")

        def session_override():
            with Session(self.engine) as session:
                yield session

        self.app.dependency_overrides[get_session] = session_override
        self.app.dependency_overrides[get_current_active_user] = lambda: self.user
        self.client = TestClient(self.app)

    def tearDown(self):
        self.client.close()
        self.engine.dispose()
        self.tmp.cleanup()

    def test_login_failure_is_a_chinese_400_not_a_401_redirect(self):
        response = self.client.post("/api/auth/token", data={"username": "tester", "password": "wrong"})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["detail"], "用户名或密码错误")

    def test_request_validation_keeps_shape_and_speaks_chinese(self):
        response = self.client.post("/api/api-testing/folders", json={"name": "", "kind": "interface"})
        self.assertEqual(response.status_code, 422)
        detail = response.json()["detail"]
        self.assertEqual(detail[0]["loc"], ["body", "name"])
        self.assertEqual(detail[0]["msg"], "name：长度不能少于 1")
        self.assertEqual(detail[0]["type"], "string_too_short")

        response = self.client.post("/api/api-testing/folders", json={"kind": "nope"})
        messages = {item["loc"][-1]: item["msg"] for item in response.json()["detail"]}
        self.assertEqual(messages["name"], "name：缺少必填项")
        self.assertEqual(messages["kind"], "kind：取值不在允许范围内")

        # Our own validators already raise Chinese text; it must not be wrapped.
        response = self.client.post(
            "/api/api-testing/precheck",
            json={"name": "x", "steps": [{"id": "a", "snapshot": {"request": {"url": {"kind": "env", "name": ""}}}}]},
        )
        self.assertEqual(response.status_code, 422)
        self.assertTrue(any("环境变量名称不能为空" in item["msg"] for item in response.json()["detail"]), response.text)
        self.assertFalse(any("Value error" in item["msg"] for item in response.json()["detail"]))

    def test_translation_helpers_cover_nested_locations_and_unknown_types(self):
        self.assertEqual(location(("body", "steps", 0, "name")), "steps[0].name")
        self.assertEqual(location(("query", "limit")), "limit")
        self.assertEqual(translate({"type": "greater_than_equal", "msg": "x", "ctx": {"ge": 1}}), "必须大于或等于 1")
        self.assertEqual(translate({"type": "something_new", "msg": "whatever"}), "格式无效")
        self.assertEqual(translate({"type": "too_long", "msg": "x"}), "数量不能超过")
