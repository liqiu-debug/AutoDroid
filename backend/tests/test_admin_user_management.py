import unittest

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, select

from backend.api import admin, auth
from backend.core.security import get_password_hash
from backend.database import get_session
from backend.models import User


class AdminUserManagementApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = create_engine(
            "sqlite://",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        SQLModel.metadata.create_all(self.engine)
        self.session = Session(self.engine)
        self._seed_users()

        app = FastAPI()
        app.include_router(auth.router, prefix="/auth")
        app.include_router(admin.router, prefix="/admin")

        def override_get_session():
            with Session(self.engine) as session:
                yield session

        app.dependency_overrides[get_session] = override_get_session
        self.client = TestClient(app)

    def tearDown(self) -> None:
        self.session.close()

    def _seed_users(self) -> None:
        users = [
            User(
                username="admin",
                hashed_password=get_password_hash("admin123"),
                full_name="Administrator",
                role="admin",
            ),
            User(
                username="tester",
                hashed_password=get_password_hash("tester123"),
                full_name="Tester",
                role="user",
            ),
        ]
        self.session.add_all(users)
        self.session.commit()

    def _token(self, username: str, password: str) -> str:
        response = self.client.post(
            "/auth/token",
            data={"username": username, "password": password},
        )
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()["access_token"]

    def _auth_headers(self, username: str = "admin", password: str = "admin123") -> dict:
        return {"Authorization": f"Bearer {self._token(username, password)}"}

    def test_registration_defaults_on_and_can_be_disabled(self):
        status_response = self.client.get("/auth/registration-status")
        self.assertEqual(status_response.status_code, 200)
        self.assertEqual(status_response.json(), {"allow_registration": True})

        register_response = self.client.post(
            "/auth/register",
            json={"username": "newbie", "password": "newbie123", "name": "New User"},
        )
        self.assertEqual(register_response.status_code, 200, register_response.text)
        self.assertNotIn("hashed_password", register_response.json())

        disable_response = self.client.put(
            "/admin/registration-settings",
            json={"allow_registration": False},
            headers=self._auth_headers(),
        )
        self.assertEqual(disable_response.status_code, 200, disable_response.text)
        self.assertEqual(disable_response.json(), {"allow_registration": False})

        blocked_response = self.client.post(
            "/auth/register",
            json={"username": "blocked", "password": "blocked123", "name": "Blocked User"},
        )
        self.assertEqual(blocked_response.status_code, 403)

    def test_admin_only_users_api_and_safe_user_responses(self):
        user_headers = self._auth_headers("tester", "tester123")
        forbidden_response = self.client.get("/admin/users", headers=user_headers)
        self.assertEqual(forbidden_response.status_code, 403)

        admin_headers = self._auth_headers()
        me_response = self.client.get("/auth/users/me", headers=admin_headers)
        self.assertEqual(me_response.status_code, 200, me_response.text)
        self.assertEqual(me_response.json()["role"], "admin")
        self.assertNotIn("hashed_password", me_response.json())

        list_response = self.client.get("/admin/users", headers=admin_headers)
        self.assertEqual(list_response.status_code, 200, list_response.text)
        self.assertGreaterEqual(len(list_response.json()), 2)
        self.assertNotIn("hashed_password", list_response.json()[0])
        self.assertNotIn("role", list_response.json()[0])

        create_response = self.client.post(
            "/admin/users",
            json={
                "username": "created",
                "full_name": "Created User",
                "email": "created@example.com",
                "initial_password": "created123",
            },
            headers=admin_headers,
        )
        self.assertEqual(create_response.status_code, 200, create_response.text)
        payload = create_response.json()
        self.assertEqual(payload["username"], "created")
        self.assertNotIn("hashed_password", payload)
        self.assertNotIn("role", payload)

        login_response = self.client.post(
            "/auth/token",
            data={"username": "created", "password": "created123"},
        )
        self.assertEqual(login_response.status_code, 200, login_response.text)

    def test_deactivate_user_blocks_login_and_self_deactivate_is_denied(self):
        admin_headers = self._auth_headers()
        tester = self.session.exec(select(User).where(User.username == "tester")).first()
        admin_user = self.session.exec(select(User).where(User.username == "admin")).first()

        deactivate_response = self.client.patch(
            f"/admin/users/{tester.id}/status",
            json={"is_active": False},
            headers=admin_headers,
        )
        self.assertEqual(deactivate_response.status_code, 200, deactivate_response.text)
        self.assertFalse(deactivate_response.json()["is_active"])

        inactive_login_response = self.client.post(
            "/auth/token",
            data={"username": "tester", "password": "tester123"},
        )
        self.assertEqual(inactive_login_response.status_code, 400)

        self_deactivate_response = self.client.patch(
            f"/admin/users/{admin_user.id}/status",
            json={"is_active": False},
            headers=admin_headers,
        )
        self.assertEqual(self_deactivate_response.status_code, 400)

    def test_change_password_requires_current_password_and_updates_login(self):
        headers = self._auth_headers("tester", "tester123")

        wrong_current_response = self.client.put(
            "/auth/password",
            json={"current_password": "wrong", "new_password": "updated123"},
            headers=headers,
        )
        self.assertEqual(wrong_current_response.status_code, 400)

        update_response = self.client.put(
            "/auth/password",
            json={"current_password": "tester123", "new_password": "updated123"},
            headers=headers,
        )
        self.assertEqual(update_response.status_code, 200, update_response.text)

        old_login_response = self.client.post(
            "/auth/token",
            data={"username": "tester", "password": "tester123"},
        )
        self.assertEqual(old_login_response.status_code, 400)

        new_login_response = self.client.post(
            "/auth/token",
            data={"username": "tester", "password": "updated123"},
        )
        self.assertEqual(new_login_response.status_code, 200, new_login_response.text)


if __name__ == "__main__":
    unittest.main()
