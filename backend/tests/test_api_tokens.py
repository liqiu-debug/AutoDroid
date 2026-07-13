import unittest
from datetime import datetime, timedelta

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, select

from backend.api import admin, auth, settings as settings_api, tokens
from backend.core.api_tokens import (
    API_TOKEN_PREFIX,
    generate_api_token,
    hash_api_token,
    is_api_token,
    token_display_prefix,
    verify_api_token,
)
from backend.core.security import get_password_hash
from backend.database import get_session
from backend.models import ApiToken, User


class ApiTokenHelperTests(unittest.TestCase):
    def test_generate_token_format(self):
        token = generate_api_token()
        self.assertTrue(token.startswith(API_TOKEN_PREFIX))
        # adk_ + 48 hex chars
        self.assertEqual(len(token), len(API_TOKEN_PREFIX) + 48)
        hex_part = token[len(API_TOKEN_PREFIX):]
        self.assertTrue(all(c in "0123456789abcdef" for c in hex_part))

    def test_generate_token_unique(self):
        tokens_set = {generate_api_token() for _ in range(20)}
        self.assertEqual(len(tokens_set), 20)

    def test_hash_is_sha256_hex_and_stable(self):
        token = "adk_" + "a" * 48
        digest = hash_api_token(token)
        self.assertEqual(len(digest), 64)
        self.assertEqual(digest, hash_api_token(token))
        self.assertNotIn(token, digest)

    def test_verify_api_token_constant_time_compare(self):
        token = generate_api_token()
        stored = hash_api_token(token)
        self.assertTrue(verify_api_token(token, stored))
        self.assertFalse(verify_api_token(generate_api_token(), stored))
        self.assertFalse(verify_api_token(token, ""))
        self.assertFalse(verify_api_token(token, None))

    def test_is_api_token_prefix_detection(self):
        self.assertTrue(is_api_token(generate_api_token()))
        self.assertFalse(is_api_token("eyJhbGciOiJIUzI1NiJ9.payload.sig"))
        self.assertFalse(is_api_token(""))

    def test_token_display_prefix(self):
        token = generate_api_token()
        prefix = token_display_prefix(token)
        self.assertEqual(len(prefix), 12)
        self.assertTrue(prefix.startswith("adk_"))
        self.assertTrue(token.startswith(prefix))


class ApiTokenApiTests(unittest.TestCase):
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
        app.include_router(tokens.router, prefix="/tokens")
        app.include_router(settings_api.router, prefix="/settings")

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
            User(
                username="other",
                hashed_password=get_password_hash("other123"),
                full_name="Other",
                role="user",
            ),
        ]
        self.session.add_all(users)
        self.session.commit()

    def _jwt(self, username: str = "tester", password: str = "tester123") -> str:
        response = self.client.post(
            "/auth/token",
            data={"username": username, "password": password},
        )
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()["access_token"]

    def _jwt_headers(self, username: str = "tester", password: str = "tester123") -> dict:
        return {"Authorization": f"Bearer {self._jwt(username, password)}"}

    def _create_token(self, name: str = "ci-token", username: str = "tester", password: str = "tester123") -> dict:
        response = self.client.post(
            "/tokens/",
            json={"name": name},
            headers=self._jwt_headers(username, password),
        )
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    @staticmethod
    def _token_headers(plaintext: str) -> dict:
        return {"Authorization": f"Bearer {plaintext}"}

    # ---- 创建/列表/吊销 ----

    def test_create_returns_plaintext_once_and_list_has_no_plaintext(self):
        created = self._create_token("jenkins")
        self.assertTrue(created["token"].startswith("adk_"))
        self.assertEqual(created["token_prefix"], created["token"][:12])
        self.assertEqual(created["name"], "jenkins")
        self.assertTrue(created["is_active"])

        list_response = self.client.get("/tokens/", headers=self._jwt_headers())
        self.assertEqual(list_response.status_code, 200, list_response.text)
        items = list_response.json()
        self.assertEqual(len(items), 1)
        self.assertNotIn("token", items[0])
        self.assertNotIn("token_hash", items[0])
        self.assertEqual(items[0]["token_prefix"], created["token_prefix"])

        # 数据库中不存明文
        record = self.session.exec(select(ApiToken)).first()
        self.assertNotEqual(record.token_hash, created["token"])
        self.assertEqual(record.token_hash, hash_api_token(created["token"]))

    def test_create_requires_name(self):
        response = self.client.post(
            "/tokens/",
            json={"name": "   "},
            headers=self._jwt_headers(),
        )
        self.assertEqual(response.status_code, 400)

    def test_list_only_own_tokens_and_admin_all(self):
        self._create_token("mine", "tester", "tester123")
        self._create_token("theirs", "other", "other123")

        own = self.client.get("/tokens/", headers=self._jwt_headers()).json()
        self.assertEqual([t["name"] for t in own], ["mine"])

        # 非 admin 请求 all=1 被拒
        forbidden = self.client.get("/tokens/?all=1", headers=self._jwt_headers())
        self.assertEqual(forbidden.status_code, 403)

        all_items = self.client.get(
            "/tokens/?all=1", headers=self._jwt_headers("admin", "admin123")
        ).json()
        self.assertEqual({t["name"] for t in all_items}, {"mine", "theirs"})
        self.assertEqual(
            {t["username"] for t in all_items}, {"tester", "other"}
        )

    def test_revoke_permissions(self):
        created = self._create_token("mine", "tester", "tester123")
        token_id = created["id"]

        # 其他普通用户不能吊销
        forbidden = self.client.delete(
            f"/tokens/{token_id}", headers=self._jwt_headers("other", "other123")
        )
        self.assertEqual(forbidden.status_code, 403)

        # 属主可吊销
        ok = self.client.delete(f"/tokens/{token_id}", headers=self._jwt_headers())
        self.assertEqual(ok.status_code, 200, ok.text)

        # admin 可吊销他人 token
        other = self._create_token("theirs", "other", "other123")
        admin_ok = self.client.delete(
            f"/tokens/{other['id']}", headers=self._jwt_headers("admin", "admin123")
        )
        self.assertEqual(admin_ok.status_code, 200, admin_ok.text)

        missing = self.client.delete("/tokens/9999", headers=self._jwt_headers())
        self.assertEqual(missing.status_code, 404)

    # ---- 认证路径 ----

    def test_valid_token_passes_business_endpoint(self):
        created = self._create_token()
        response = self.client.get("/settings/", headers=self._token_headers(created["token"]))
        self.assertEqual(response.status_code, 200, response.text)

        me = self.client.get("/auth/users/me", headers=self._token_headers(created["token"]))
        self.assertEqual(me.status_code, 200, me.text)
        self.assertEqual(me.json()["username"], "tester")

    def test_revoked_token_returns_401(self):
        created = self._create_token()
        self.client.delete(f"/tokens/{created['id']}", headers=self._jwt_headers())

        response = self.client.get("/settings/", headers=self._token_headers(created["token"]))
        self.assertEqual(response.status_code, 401)

    def test_unknown_or_malformed_token_returns_401(self):
        unknown = self.client.get(
            "/settings/", headers=self._token_headers("adk_" + "0" * 48)
        )
        self.assertEqual(unknown.status_code, 401)

        malformed = self.client.get("/settings/", headers=self._token_headers("adk_short"))
        self.assertEqual(malformed.status_code, 401)

        not_a_token = self.client.get("/settings/", headers=self._token_headers("garbage"))
        self.assertEqual(not_a_token.status_code, 401)

    def test_token_of_inactive_user_returns_401(self):
        created = self._create_token()
        tester = self.session.exec(select(User).where(User.username == "tester")).first()
        tester.is_active = False
        self.session.add(tester)
        self.session.commit()

        response = self.client.get("/settings/", headers=self._token_headers(created["token"]))
        self.assertEqual(response.status_code, 401)

    # ---- 权限边界 ----

    def test_restricted_endpoints_reject_api_token_with_403(self):
        created = self._create_token(username="admin", password="admin123")
        headers = self._token_headers(created["token"])

        # admin 全部接口
        self.assertEqual(self.client.get("/admin/users", headers=headers).status_code, 403)
        self.assertEqual(
            self.client.get("/admin/registration-settings", headers=headers).status_code, 403
        )

        # settings 写接口
        self.assertEqual(
            self.client.post(
                "/settings/", json=[{"key": "k", "value": "v"}], headers=headers
            ).status_code,
            403,
        )
        self.assertEqual(
            self.client.post(
                "/settings/test-notification",
                json={"webhook_url": "https://example.com"},
                headers=headers,
            ).status_code,
            403,
        )

        # 改密
        self.assertEqual(
            self.client.put(
                "/auth/password",
                json={"current_password": "admin123", "new_password": "x12345"},
                headers=headers,
            ).status_code,
            403,
        )

        # Token 管理接口本身（Token 不能管 Token）
        self.assertEqual(
            self.client.post("/tokens/", json={"name": "nested"}, headers=headers).status_code,
            403,
        )
        self.assertEqual(self.client.get("/tokens/", headers=headers).status_code, 403)
        self.assertEqual(
            self.client.delete(f"/tokens/{created['id']}", headers=headers).status_code, 403
        )

    def test_jwt_path_regression_unchanged(self):
        # JWT 用户仍可读写 settings、admin 用户仍可访问 admin 接口
        headers = self._jwt_headers()
        self.assertEqual(self.client.get("/settings/", headers=headers).status_code, 200)
        self.assertEqual(
            self.client.post(
                "/settings/", json=[{"key": "k", "value": "v"}], headers=headers
            ).status_code,
            200,
        )

        admin_headers = self._jwt_headers("admin", "admin123")
        self.assertEqual(self.client.get("/admin/users", headers=admin_headers).status_code, 200)

        # 非 admin JWT 用户访问 admin 仍是 403
        self.assertEqual(self.client.get("/admin/users", headers=headers).status_code, 403)

        # 无凭证 401
        self.assertEqual(self.client.get("/settings/").status_code, 401)

    # ---- last_used 节流 ----

    def test_last_used_throttling(self):
        created = self._create_token()
        headers = self._token_headers(created["token"])

        self.assertEqual(self.client.get("/settings/", headers=headers).status_code, 200)
        record = self.session.exec(select(ApiToken)).first()
        self.session.refresh(record)
        first_used = record.last_used_at
        self.assertIsNotNone(first_used)

        # 60 秒内再次使用不更新
        self.assertEqual(self.client.get("/settings/", headers=headers).status_code, 200)
        self.session.refresh(record)
        self.assertEqual(record.last_used_at, first_used)

        # 手动回拨 last_used_at 超过节流窗口后会更新
        record.last_used_at = datetime.now() - timedelta(seconds=120)
        self.session.add(record)
        self.session.commit()

        self.assertEqual(self.client.get("/settings/", headers=headers).status_code, 200)
        self.session.refresh(record)
        self.assertGreater(record.last_used_at, datetime.now() - timedelta(seconds=30))


if __name__ == "__main__":
    unittest.main()
