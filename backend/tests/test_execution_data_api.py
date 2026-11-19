"""Execution input snapshots stay read-only and avoid per-case queries."""
import json
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.encoders import jsonable_encoder
from fastapi.testclient import TestClient
from jose import jwt
from sqlalchemy import event
from sqlalchemy.pool import StaticPool
from sqlmodel import SQLModel, Session, create_engine, select

from backend.api import cases, scenarios
from backend.core.api_tokens import generate_api_token, hash_api_token, token_display_prefix
from backend.core.security import ALGORITHM, SECRET_KEY
from backend.cross_platform_execution import load_app_key_mapping
from backend.database import get_session
from backend.models import (
    ApiToken,
    Environment,
    GlobalVariable,
    ScenarioStep,
    SystemSetting,
    TestCase,
    TestCaseStep,
    TestScenario,
    User,
)
from backend.schemas import Step, Variable


class ExecutionDataApiTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine(
            "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
        )
        SQLModel.metadata.create_all(self.engine)
        self.session = Session(self.engine)
        self.user = User(username="reader", hashed_password="x")
        self.session.add(self.user)
        self.session.commit()
        self.session.refresh(self.user)
        self.headers = {
            "Authorization": "Bearer "
            + jwt.encode({"sub": self.user.username}, SECRET_KEY, algorithm=ALGORITHM)
        }
        self.app = FastAPI()
        self.app.include_router(cases.router, prefix="/api/cases")
        self.app.include_router(scenarios.router, prefix="/api/scenarios")

        def session_dependency():
            with Session(self.engine) as session:
                yield session

        self.app.dependency_overrides[get_session] = session_dependency
        self.client = TestClient(self.app)
        self.addCleanup(self.client.close)
        self.addCleanup(self.engine.dispose)
        self.addCleanup(self.session.close)

    def _case(self, *, standard=True, name="登录", legacy=None):
        case = TestCase(
            name=name,
            steps=legacy or [Step(action="sleep", value="1", description="旧步骤")],
            variables=[Variable(key="USER_NAME", value="{{ ENV_USER }}")],
            version=3,
        )
        self.session.add(case)
        self.session.commit()
        self.session.refresh(case)
        if standard:
            self.session.add(
                TestCaseStep(
                    case_id=case.id,
                    order=2,
                    action="input",
                    value="{{USER_NAME}}",
                    args={"clear": True},
                    execute_on=["android", "ios"],
                    platform_overrides={
                        "android": {"by": "id", "selector": "com.demo:id/input"},
                        "ios": {"by": "label", "selector": "用户名"},
                    },
                    retry_count=2,
                    error_strategy="CONTINUE",
                )
            )
            self.session.add(
                TestCaseStep(case_id=case.id, order=1, action="sleep", value="2", args={})
            )
            self.session.commit()
        return case

    def _scenario(self, case_ids):
        scenario = TestScenario(name="下单", version=4)
        self.session.add(scenario)
        self.session.commit()
        self.session.refresh(scenario)
        for index, case_id in enumerate(case_ids, 1):
            self.session.add(
                ScenarioStep(
                    scenario_id=scenario.id,
                    case_id=case_id,
                    order=index,
                    alias=f"节点-{index}",
                )
            )
        self.session.commit()
        return scenario

    def _environment(self):
        env = Environment(name="测试")
        self.session.add(env)
        self.session.commit()
        self.session.refresh(env)
        self.session.add(GlobalVariable(env_id=env.id, key="ENV_USER", value="张三"))
        self.session.add(GlobalVariable(env_id=env.id, key="EMPTY", value=""))
        self.session.add(GlobalVariable(env_id=env.id, key="SECRET", value="secret", is_secret=True))
        self.session.commit()
        return env

    def _get(self, path, headers=None):
        return self.client.get(path, headers=self.headers if headers is None else headers)

    def test_case_inputs_preserve_raw_values_and_standard_serializer(self):
        case = self._case()
        env = self._environment()
        response = self._get(f"/api/cases/{case.id}/execution-data?env_id={env.id}")
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["schema_version"], 1)
        self.assertEqual(data["case"]["id"], case.id)
        self.assertEqual(data["case"]["version"], 3)
        self.assertIsInstance(data["case"]["updated_at"], str)
        self.assertEqual(data["case"]["steps"], jsonable_encoder(case.steps))
        self.assertEqual(data["case"]["variables"], jsonable_encoder(case.variables))
        self.assertEqual(data["env_variables"], {"ENV_USER": "张三", "EMPTY": "", "SECRET": "secret"})
        expected = [
            jsonable_encoder(cases._row_to_standard_step_read(row))
            for row in cases._list_standard_steps(self.session, case.id)
        ]
        self.assertEqual(data["standard_steps"], expected)
        self.assertEqual(data["standard_steps"][1]["value"], "{{USER_NAME}}")
        self.assertEqual(data["standard_steps"][1]["retry_count"], 2)

    def test_scenario_retains_order_repeats_aliases_and_missing_references(self):
        case = self._case()
        scenario = self._scenario([case.id, 9999, case.id])
        response = self._get(f"/api/scenarios/{scenario.id}/execution-data")
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["schema_version"], 1)
        self.assertEqual(data["scenario"]["version"], 4)
        self.assertEqual([row["case_id"] for row in data["scenario_steps"]], [case.id, 9999, case.id])
        self.assertEqual([row["alias"] for row in data["scenario_steps"]], ["节点-1", "节点-2", "节点-3"])
        self.assertEqual(list(data["cases"]), [str(case.id)])
        case_data = self._get(f"/api/cases/{case.id}/execution-data").json()
        self.assertEqual(data["cases"][str(case.id)], {
            "case": case_data["case"], "standard_steps": case_data["standard_steps"]
        })

    def test_same_order_nodes_and_steps_are_sorted_by_id(self):
        case = self._case()
        scenario = self._scenario([case.id, case.id])
        nodes = self.session.exec(select(ScenarioStep).where(ScenarioStep.scenario_id == scenario.id)).all()
        for node in nodes:
            node.order = 1
            self.session.add(node)
        extra = TestCaseStep(case_id=case.id, order=1, action="sleep", value="3")
        self.session.add(extra)
        self.session.commit()
        data = self._get(f"/api/scenarios/{scenario.id}/execution-data").json()
        self.assertEqual([row["id"] for row in data["scenario_steps"]], sorted(node.id for node in nodes))
        steps = data["cases"][str(case.id)]["standard_steps"]
        self.assertEqual([(row["order"], row["id"]) for row in steps], sorted((row["order"], row["id"]) for row in steps))

    def test_empty_scenario_is_a_valid_raw_snapshot(self):
        scenario = self._scenario([])
        data = self._get(f"/api/scenarios/{scenario.id}/execution-data").json()
        self.assertEqual(data["scenario_steps"], [])
        self.assertEqual(data["cases"], {})
        self.assertEqual(data["env_variables"], {})

    def test_missing_targets_and_invalid_environment_are_distinct_404s(self):
        case = self._case()
        scenario = self._scenario([case.id])
        for path, detail in [
            ("/api/cases/9999/execution-data", "用例不存在"),
            ("/api/scenarios/9999/execution-data", "场景不存在"),
            (f"/api/cases/{case.id}/execution-data?env_id=9999", "环境不存在"),
            (f"/api/scenarios/{scenario.id}/execution-data?env_id=9999", "环境不存在"),
            (f"/api/cases/{case.id}/execution-data?env_id=0", "环境不存在"),
        ]:
            with self.subTest(path=path):
                response = self._get(path)
                self.assertEqual(response.status_code, 404)
                self.assertEqual(response.json()["detail"], detail)

    def test_both_endpoints_require_authentication(self):
        case = self._case()
        scenario = self._scenario([case.id])
        for path in [f"/api/cases/{case.id}/execution-data", f"/api/scenarios/{scenario.id}/execution-data"]:
            self.assertEqual(self._get(path, {}).status_code, 401)
            self.assertEqual(self._get(path, {"Authorization": "Bearer bad-token"}).status_code, 401)

    def test_api_token_read_never_commits_usage_or_migrates_legacy(self):
        case = self._case(standard=False)
        token = generate_api_token()
        record = ApiToken(name="ci", token_hash=hash_api_token(token), token_prefix=token_display_prefix(token), user_id=self.user.id)
        self.session.add(record)
        self.session.add(SystemSetting(key="new_step_model", value="true"))
        self.session.commit()
        scenario = self._scenario([case.id])
        statements = []

        def record_statement(_conn, _cursor, statement, _parameters, _context, _executemany):
            statements.append(statement)

        event.listen(self.engine, "before_cursor_execute", record_statement)
        self.addCleanup(event.remove, self.engine, "before_cursor_execute", record_statement)
        with patch.object(Session, "commit", side_effect=AssertionError("read committed")):
            for path in [f"/api/cases/{case.id}/execution-data", f"/api/scenarios/{scenario.id}/execution-data"]:
                response = self._get(path, {"Authorization": f"Bearer {token}"})
                self.assertEqual(response.status_code, 200)
        self.assertTrue(all(sql.lstrip().upper().startswith("SELECT") for sql in statements), statements)
        self.session.refresh(record)
        self.assertIsNone(record.last_used_at)
        self.assertEqual(self.session.exec(select(TestCaseStep).where(TestCaseStep.case_id == case.id)).all(), [])

    def test_revoked_api_token_is_rejected(self):
        case = self._case()
        token = generate_api_token()
        self.session.add(ApiToken(name="revoked", token_hash=hash_api_token(token), token_prefix=token_display_prefix(token), user_id=self.user.id, is_active=False))
        self.session.commit()
        self.assertEqual(self._get(f"/api/cases/{case.id}/execution-data", {"Authorization": f"Bearer {token}"}).status_code, 401)

    def test_unknown_and_expired_or_invalid_jwt_are_rejected(self):
        case = self._case()
        scenario = self._scenario([case.id])
        tokens = [
            jwt.encode({"sub": "unknown"}, SECRET_KEY, algorithm=ALGORITHM),
            jwt.encode({"sub": self.user.username, "exp": datetime.now(timezone.utc) - timedelta(seconds=10)}, SECRET_KEY, algorithm=ALGORITHM),
            jwt.encode({"sub": self.user.username}, SECRET_KEY + "-different", algorithm=ALGORITHM),
        ]
        for path in [f"/api/cases/{case.id}/execution-data", f"/api/scenarios/{scenario.id}/execution-data"]:
            for token in tokens:
                with self.subTest(path=path, token=token[:10]):
                    self.assertEqual(self._get(path, {"Authorization": f"Bearer {token}"}).status_code, 401)

    def test_disabled_user_is_rejected_for_jwt_and_api_token(self):
        case = self._case()
        scenario = self._scenario([case.id])
        token = generate_api_token()
        self.session.add(ApiToken(name="ci", token_hash=hash_api_token(token), token_prefix=token_display_prefix(token), user_id=self.user.id))
        self.user.is_active = False
        self.session.add(self.user)
        self.session.commit()
        for path in [f"/api/cases/{case.id}/execution-data", f"/api/scenarios/{scenario.id}/execution-data"]:
            for headers in [self.headers, {"Authorization": f"Bearer {token}"}]:
                self.assertEqual(self._get(path, headers).status_code, 401)

    def test_unknown_api_token_is_rejected(self):
        case = self._case()
        response = self._get(f"/api/cases/{case.id}/execution-data", {"Authorization": f"Bearer {generate_api_token()}"})
        self.assertEqual(response.status_code, 401)

    def test_standard_and_legacy_selection_is_not_changed_by_feature_flag(self):
        standard = self._case(name="标准")
        legacy = self._case(standard=False, name="旧版")
        flag = SystemSetting(key="new_step_model", value="true")
        self.session.add(flag)
        self.session.commit()
        for enabled in ["true", "false"]:
            flag.value = enabled
            self.session.add(flag)
            self.session.commit()
            for case in [standard, legacy]:
                with self.subTest(flag=enabled, case=case.name):
                    data = self._get(f"/api/cases/{case.id}/execution-data").json()
                    self.assertEqual(bool(data["standard_steps"]), case.id == standard.id)
                    self.assertEqual(data["case"]["steps"], jsonable_encoder(case.steps))
        self.assertEqual(len(self.session.exec(select(TestCaseStep)).all()), 2)

    def test_app_mapping_matches_existing_mapping_and_excludes_other_settings(self):
        case = self._case()
        for key, value in {
            "app_key_map": {"mall": {"android": "com.old.mall", "ios": "com.mall.ios"}},
            "app_key_mapping": {"android": {"extra": "com.extra"}, "mall": {"package_name": "com.new.mall"}},
            "android_app_map": {"mall": "com.final.mall"},
            "ios_app_map": {"extra": "com.extra.ios"},
            "notification_secret": "not-part-of-execution-inputs",
        }.items():
            self.session.add(SystemSetting(key=key, value=json.dumps(value)))
        self.session.commit()
        data = self._get(f"/api/cases/{case.id}/execution-data").json()
        self.assertEqual(data["app_key_mapping"], load_app_key_mapping(self.session))
        self.assertEqual(data["app_key_mapping"], {"mall": {"android": "com.final.mall", "ios": "com.mall.ios"}, "extra": {"android": "com.extra", "ios": "com.extra.ios"}})
        self.assertNotIn("notification_secret", json.dumps(data))

    def test_scenario_query_count_stays_constant_for_one_twelve_and_hundred_cases(self):
        env = self._environment()
        targets = [self._scenario([self._case(name=f"case-{size}-{index}").id for index in range(size)]).id for size in [1, 12, 100]]
        env_id = env.id
        counts = []
        for target in targets:
            statements = []

            def record_statement(_conn, _cursor, statement, _parameters, _context, _executemany):
                statements.append(statement)

            event.listen(self.engine, "before_cursor_execute", record_statement)
            try:
                response = self._get(f"/api/scenarios/{target}/execution-data?env_id={env_id}")
            finally:
                event.remove(self.engine, "before_cursor_execute", record_statement)
            self.assertEqual(response.status_code, 200)
            self.assertTrue(all(sql.lstrip().upper().startswith("SELECT") for sql in statements))
            counts.append(len(statements))
        self.assertEqual(counts, [counts[0]] * 3)
        self.assertLessEqual(counts[0], 8)


if __name__ == "__main__":
    unittest.main()
