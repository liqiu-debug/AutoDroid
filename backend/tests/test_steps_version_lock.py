"""步骤写入乐观锁（P4 功能1）测试：cases / scenarios steps 全量替换并发防护。

契约（编排者裁决，前端 utils/versionToken.js + 桌面保存链路对齐，三端对账）：
- 版本令牌：实体 payload 有数值 version 用它，否则回退 updated_at ISO（原样、不带引号）；
- PUT /api/cases/{id}/steps、POST|PUT /api/scenarios/{id}/steps 接受
  Header If-Match: <token>（不带引号）+ Query version=<int>（仅数值令牌）
  + Query expected_updated_at=<updated_at ISO>；
- 任一令牌存在即进入校验，存在的令牌必须全部匹配（AND），任一不匹配 → 409；
  都缺失 = 跳过校验（旧客户端兼容）；
- 409 detail 固定文案「保存冲突：内容已被其他端修改，请重新拉取后再保存」；
- 校验通过后递增 version / 刷新 updated_at，响应体带新 version/updated_at
  （前端 versionStateFrom 认领顶层字段回填下一次保存令牌）。
"""
import unittest
from datetime import datetime

from fastapi import FastAPI
from fastapi.testclient import TestClient
from jose import jwt
from sqlalchemy.pool import StaticPool
from sqlmodel import SQLModel, Session, create_engine, select

from backend.api import cases as cases_api
from backend.api import deps
from backend.api import scenarios as scenarios_api
from backend.core.security import ALGORITHM, SECRET_KEY
from backend.database import get_session
from backend.models import ScenarioStep, TestCase, TestScenario, User
from backend.version_lock import (
    STEPS_VERSION_CONFLICT_DETAIL,
    VERSION_HEADER,
)

# 409 detail 固定文案（与前端 VERSION_CONFLICT_MESSAGE 同文，供三端对账）
CONFLICT_DETAIL_CONTRACT = "保存冲突：内容已被其他端修改，请重新拉取后再保存"


def _step(action: str = "click", order: int = 0, description: str = "") -> dict:
    return {"order": order, "action": action, "args": {}, "description": description}


class StepsVersionLockApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = create_engine(
            "sqlite://",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        SQLModel.metadata.create_all(self.engine)
        self.session = Session(self.engine)
        self.alice = self._add_user("alice")
        self.case = TestCase(name="登录用例", steps=[], variables=[], user_id=self.alice.id)
        self.scenario = TestScenario(name="回归场景", user_id=self.alice.id)
        self.session.add(self.case)
        self.session.add(self.scenario)
        self.session.commit()
        self.session.refresh(self.case)
        self.session.refresh(self.scenario)

        app = FastAPI()
        app.include_router(cases_api.router, prefix="/api/cases")
        app.include_router(scenarios_api.router, prefix="/api/scenarios")
        app.dependency_overrides[get_session] = lambda: self.session
        self.client = TestClient(app)
        self.token = jwt.encode({"sub": "alice"}, SECRET_KEY, algorithm=ALGORITHM)
        self.headers = {"Authorization": f"Bearer {self.token}"}

    def tearDown(self) -> None:
        self.session.close()
        self.engine.dispose()

    def _add_user(self, username: str) -> User:
        user = User(username=username, hashed_password="x", role="user")
        self.session.add(user)
        self.session.commit()
        self.session.refresh(user)
        return user

    def _put_case_steps(self, steps, headers=None, params=None):
        return self.client.put(
            f"/api/cases/{self.case.id}/steps",
            json=steps,
            headers=headers or self.headers,
            params=params or {},
        )

    def _post_scenario_steps(self, steps, headers=None, params=None):
        return self.client.post(
            f"/api/scenarios/{self.scenario.id}/steps",
            json=steps,
            headers=headers or self.headers,
            params=params or {},
        )

    def _scenario_steps(self):
        return self.session.exec(
            select(ScenarioStep).where(ScenarioStep.scenario_id == self.scenario.id)
        ).all()

    def _case_state(self):
        self.session.refresh(self.case)
        return {"version": self.case.version, "updated_at": self.case.updated_at}

    # -- 版本读写面 --

    def test_case_and_scenario_read_expose_version(self):
        resp = self.client.get(f"/api/cases/{self.case.id}", headers=self.headers)
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertEqual(resp.json()["version"], 1)

        resp = self.client.get(f"/api/scenarios/{self.scenario.id}", headers=self.headers)
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertEqual(resp.json()["version"], 1)

    def test_write_then_read_exposes_updated_at_for_token(self):
        resp = self._put_case_steps([_step("click", 0)])
        self.assertEqual(resp.status_code, 200, resp.text)
        resp = self.client.get(f"/api/cases/{self.case.id}", headers=self.headers)
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertEqual(resp.json()["version"], 2)
        self.assertTrue(resp.json().get("updated_at"))

    def test_save_response_body_carries_new_version_state(self):
        """响应实体带新 version/updated_at：前端回填下一次保存令牌的数据源。"""
        resp = self._put_case_steps([_step("click", 0)])
        self.assertEqual(resp.status_code, 200, resp.text)
        body = resp.json()
        self.assertEqual(body["version"], 2)
        self.assertTrue(body.get("updated_at"))
        self.assertEqual([s["action"] for s in body["steps"]], ["click"])

        resp = self._post_scenario_steps([{"case_id": 1, "order": 0}])
        self.assertEqual(resp.status_code, 200, resp.text)
        body = resp.json()
        self.assertTrue(body["success"])
        self.assertEqual(body["count"], 1)
        self.assertEqual(body["version"], 2)
        self.assertTrue(body.get("updated_at"))

    def test_save_without_version_is_backward_compatible_and_bumps(self):
        """全缺失跳过校验（旧客户端兼容），写入照旧递增 version。"""
        resp = self._put_case_steps([_step("click", 0), _step("input", 1)])
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertEqual(resp.headers.get(VERSION_HEADER), "2")

        self.session.refresh(self.case)
        self.assertEqual(self.case.version, 2)

    # -- 只带 If-Match：命中 / 失配 --

    def test_if_match_matching_version_saves(self):
        resp = self._put_case_steps([_step("click", 0)], headers={**self.headers, "If-Match": "1"})
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertEqual(resp.json()["version"], 2)

    def test_if_match_quoted_and_weak_forms_accepted(self):
        resp = self._put_case_steps([_step("click", 0)], headers={**self.headers, "If-Match": '"1"'})
        self.assertEqual(resp.status_code, 200, resp.text)
        resp = self._put_case_steps([_step("click", 0)], headers={**self.headers, "If-Match": 'W/"2"'})
        self.assertEqual(resp.status_code, 200, resp.text)

    def test_if_match_updated_at_iso_fallback_token_matches(self):
        """令牌回退口径：实体无 version 时用 updated_at ISO，If-Match 该值应命中。"""
        self._put_case_steps([_step("click", 0)])
        self.session.refresh(self.case)
        iso_token = self.case.updated_at.isoformat()
        resp = self._put_case_steps(
            [_step("swipe", 0)], headers={**self.headers, "If-Match": iso_token}
        )
        self.assertEqual(resp.status_code, 200, resp.text)

    def test_if_match_star_is_unconditional(self):
        resp = self._put_case_steps([_step("click", 0)], headers={**self.headers, "If-Match": "*"})
        self.assertEqual(resp.status_code, 200, resp.text)

    def test_stale_if_match_returns_409_with_contract_shape(self):
        self._put_case_steps([_step("click", 0)])
        resp = self._put_case_steps([_step("swipe", 0)], headers={**self.headers, "If-Match": "1"})

        self.assertEqual(resp.status_code, 409)
        self.assertEqual(resp.json()["detail"], CONFLICT_DETAIL_CONTRACT)
        self.assertEqual(resp.json()["detail"], STEPS_VERSION_CONFLICT_DETAIL)
        self.assertEqual(resp.headers.get(VERSION_HEADER), "2")
        # 冲突不覆盖
        self.session.refresh(self.case)
        self.assertEqual(self.case.version, 2)
        steps = self.client.get(f"/api/cases/{self.case.id}/steps", headers=self.headers).json()
        self.assertEqual([s["action"] for s in steps], ["click"])

    # -- 只带 version：命中 / 失配 --

    def test_version_query_param_hit_and_miss(self):
        self._put_case_steps([_step("click", 0)])
        resp = self._put_case_steps([_step("swipe", 0)], params={"version": 1})
        self.assertEqual(resp.status_code, 409)
        self.assertEqual(resp.json()["detail"], CONFLICT_DETAIL_CONTRACT)
        resp = self._put_case_steps([_step("swipe", 0)], params={"version": 2})
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertEqual(resp.json()["version"], 3)

    # -- 只带 expected_updated_at：命中 / 失配 --

    def test_expected_updated_at_hit_and_miss(self):
        # 先写一次建立 updated_at（新档 updated_at 为空，令牌按「实体有 updated_at 时」发送）
        self._put_case_steps([_step("click", 0)])
        state = self._case_state()
        self.assertIsNotNone(state["updated_at"])
        iso_token = state["updated_at"].isoformat()
        stale = datetime(2020, 1, 1, 0, 0, 0).isoformat()

        resp = self._put_case_steps([_step("swipe", 0)], params={"expected_updated_at": stale})
        self.assertEqual(resp.status_code, 409)
        self.assertEqual(resp.json()["detail"], CONFLICT_DETAIL_CONTRACT)

        resp = self._put_case_steps(
            [_step("swipe", 0)], params={"expected_updated_at": iso_token}
        )
        self.assertEqual(resp.status_code, 200, resp.text)

    # -- 双令牌 AND：任一失配即 409 --

    def test_dual_tokens_one_mismatch_returns_409(self):
        """If-Match 命中 + version 失配 → 409；version 命中 + expected_updated_at 失配 → 409。"""
        self._put_case_steps([_step("click", 0)])
        self.session.refresh(self.case)

        resp = self._put_case_steps(
            [_step("swipe", 0)],
            headers={**self.headers, "If-Match": str(self.case.version)},
            params={"version": 1},  # 旧值，失配
        )
        self.assertEqual(resp.status_code, 409)
        self.assertEqual(resp.json()["detail"], CONFLICT_DETAIL_CONTRACT)

        resp = self._put_case_steps(
            [_step("swipe", 0)],
            params={
                "version": self.case.version,  # 命中
                "expected_updated_at": datetime(2020, 1, 1).isoformat(),  # 失配
            },
        )
        self.assertEqual(resp.status_code, 409)

        # 全部命中 → 200
        state = self._case_state()
        resp = self._put_case_steps(
            [_step("swipe", 0)],
            headers={**self.headers, "If-Match": str(state["version"])},
            params={
                "version": state["version"],
                "expected_updated_at": state["updated_at"].isoformat(),
            },
        )
        self.assertEqual(resp.status_code, 200, resp.text)

    def test_if_match_hit_version_miss_is_conflict_not_400(self):
        self._put_case_steps([_step("click", 0)])
        self.session.refresh(self.case)
        resp = self._put_case_steps(
            [_step("swipe", 0)],
            headers={**self.headers, "If-Match": str(self.case.version)},
            params={"version": 1},
        )
        self.assertEqual(resp.status_code, 409)

    # -- 并发双写：一成一 409 --

    def test_concurrent_double_write_one_succeeds_one_conflicts(self):
        """双端同读 version=1 并发保存：先写 200，后写（旧令牌）409，不静默覆盖。"""
        first = self._put_case_steps([_step("click", 0, "A")], headers={**self.headers, "If-Match": "1"})
        self.assertEqual(first.status_code, 200, first.text)

        second = self._put_case_steps([_step("swipe", 0, "B")], headers={**self.headers, "If-Match": "1"})
        self.assertEqual(second.status_code, 409)
        self.assertEqual(second.json()["detail"], CONFLICT_DETAIL_CONTRACT)

        steps = self.client.get(f"/api/cases/{self.case.id}/steps", headers=self.headers).json()
        self.assertEqual(steps[0]["description"], "A")

    def test_concurrent_double_write_via_version_query(self):
        first = self._put_case_steps([_step("click", 0, "A")], params={"version": 1})
        self.assertEqual(first.status_code, 200, first.text)
        second = self._put_case_steps([_step("swipe", 0, "B")], params={"version": 1})
        self.assertEqual(second.status_code, 409)

    # -- scenarios 同一契约 --

    def test_scenario_steps_post_and_put_share_version_lock(self):
        resp = self._post_scenario_steps([{"case_id": 1, "order": 0}], headers={**self.headers, "If-Match": "1"})
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertEqual(resp.json()["version"], 2)

        resp = self._post_scenario_steps([{"case_id": 2, "order": 0}], headers={**self.headers, "If-Match": "1"})
        self.assertEqual(resp.status_code, 409)
        self.assertEqual(resp.json()["detail"], CONFLICT_DETAIL_CONTRACT)
        self.assertEqual(resp.headers.get(VERSION_HEADER), "2")

        # PUT 别名同一契约
        resp = self.client.put(
            f"/api/scenarios/{self.scenario.id}/steps",
            json=[{"case_id": 3, "order": 0}],
            headers={**self.headers, "If-Match": "2"},
        )
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertEqual(resp.json()["version"], 3)
        rows = self._scenario_steps()
        self.assertEqual([r.case_id for r in rows], [3])

        resp = self.client.put(
            f"/api/scenarios/{self.scenario.id}/steps",
            json=[{"case_id": 4, "order": 0}],
            headers={**self.headers, "If-Match": "2"},
        )
        self.assertEqual(resp.status_code, 409)

    def test_scenario_steps_dual_token_and_expected_updated_at(self):
        resp = self._post_scenario_steps([{"case_id": 1, "order": 0}])
        self.assertEqual(resp.status_code, 200, resp.text)
        self.session.refresh(self.scenario)
        iso_token = self.scenario.updated_at.isoformat()

        resp = self._post_scenario_steps(
            [{"case_id": 2, "order": 0}],
            params={"version": self.scenario.version, "expected_updated_at": iso_token},
        )
        self.assertEqual(resp.status_code, 200, resp.text)

        resp = self._post_scenario_steps(
            [{"case_id": 3, "order": 0}],
            params={"version": self.scenario.version, "expected_updated_at": datetime(2020, 1, 1).isoformat()},
        )
        self.assertEqual(resp.status_code, 409)
        self.assertEqual(resp.json()["detail"], CONFLICT_DETAIL_CONTRACT)

    def test_scenario_steps_without_version_backcompat(self):
        resp = self._post_scenario_steps([{"case_id": 1, "order": 0}])
        self.assertEqual(resp.status_code, 200, resp.text)
        self.session.refresh(self.scenario)
        self.assertEqual(self.scenario.version, 2)

    # -- 其它步骤写路径同步 bump --

    def test_update_case_with_steps_bumps_version(self):
        resp = self.client.put(
            f"/api/cases/{self.case.id}",
            json={
                "name": "登录用例",
                "description": None,
                "steps": [_step("click", 0)],
                "variables": [],
                "tags": [],
            },
            headers=self.headers,
        )
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertEqual(resp.json()["version"], 2)

    def test_sync_legacy_bumps_version(self):
        self.session.refresh(self.case)
        self.case.steps = []
        self.session.add(self.case)
        self.session.commit()

        resp = self.client.post(
            f"/api/cases/{self.case.id}/steps/sync-legacy",
            headers=self.headers,
        )
        self.assertEqual(resp.status_code, 200, resp.text)
        self.session.refresh(self.case)
        self.assertEqual(self.case.version, 2)

    def test_get_steps_exposes_current_version_header(self):
        resp = self.client.get(f"/api/cases/{self.case.id}/steps", headers=self.headers)
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertEqual(resp.headers.get(VERSION_HEADER), "1")

        resp = self.client.get(f"/api/scenarios/{self.scenario.id}/steps", headers=self.headers)
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertEqual(resp.headers.get(VERSION_HEADER), "1")


class VersionLockHelperTests(unittest.TestCase):
    class _Entity:
        def __init__(self, version=3, updated_at=None):
            self.version = version
            self.updated_at = updated_at

    def test_all_missing_skips_check(self):
        from backend.version_lock import ensure_version_tokens_match

        self.assertIsNone(ensure_version_tokens_match(self._Entity()))

    def test_token_precedence_version_over_updated_at(self):
        from backend.version_lock import current_version_token

        entity = self._Entity(version=5, updated_at=datetime(2026, 1, 1))
        self.assertEqual(current_version_token(entity), "5")
        entity.version = None
        self.assertEqual(current_version_token(entity), datetime(2026, 1, 1).isoformat())

    def test_match_and_mismatch_matrix(self):
        from fastapi import HTTPException

        from backend.version_lock import ensure_version_tokens_match

        entity = self._Entity(version=3, updated_at=datetime(2026, 1, 1, 12, 0, 0))
        iso = entity.updated_at.isoformat()

        # 命中
        self.assertEqual(
            ensure_version_tokens_match(entity, if_match="3"), 3
        )
        self.assertEqual(ensure_version_tokens_match(entity, version=3), 3)
        self.assertEqual(
            ensure_version_tokens_match(entity, expected_updated_at=iso), 3
        )
        self.assertEqual(
            ensure_version_tokens_match(entity, if_match=iso), 3
        )
        # 双令牌全命中
        self.assertEqual(
            ensure_version_tokens_match(entity, if_match="3", version=3), 3
        )
        # 任一失配 → 409
        for kwargs in (
            {"if_match": "2"},
            {"version": 2},
            {"expected_updated_at": datetime(2020, 1, 1).isoformat()},
            {"if_match": "3", "version": 2},
            {"version": 3, "expected_updated_at": datetime(2020, 1, 1).isoformat()},
        ):
            with self.assertRaises(HTTPException, msg=kwargs) as ctx:
                ensure_version_tokens_match(entity, **kwargs)
            self.assertEqual(ctx.exception.status_code, 409)
            self.assertEqual(ctx.exception.detail, CONFLICT_DETAIL_CONTRACT)

    def test_contract_detail_text_is_frozen(self):
        """409 detail 文案断言（三端对账的固定文案，不得漂移）。"""
        self.assertEqual(STEPS_VERSION_CONFLICT_DETAIL, CONFLICT_DETAIL_CONTRACT)


if __name__ == "__main__":
    unittest.main()
