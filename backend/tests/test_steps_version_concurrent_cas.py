"""步骤乐观锁原子 CAS 真并发测试（P4 终验必修缺陷 1，blocker）。

复现口径与终验评审一致：每请求独立 Session + threading.Barrier 同刻双发同
If-Match / ?version= / ?expected_updated_at= 令牌 → 修复前两请求都 200（版本
丢失更新，check-then-write 非原子），修复后恰一个 200 一个 409、final version
恰 +1，且并发失败方不得半写步骤行。

覆盖矩阵：cases 与 scenarios × If-Match 版本令牌 / ?version= / ISO
updated_at 令牌（?expected_updated_at=）。
"""
import threading
import unittest
from datetime import datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Callable, List, Tuple

from fastapi import FastAPI
from fastapi.testclient import TestClient
from jose import jwt
from sqlmodel import SQLModel, Session, create_engine, select

from backend.api import cases as cases_api
from backend.api import deps
from backend.api import scenarios as scenarios_api
from backend.core.security import ALGORITHM, SECRET_KEY
from backend.database import configure_sqlite_engine, get_session
from backend.models import ScenarioStep, TestCase, TestCaseStep, TestScenario, User
from backend.version_lock import STEPS_VERSION_CONFLICT_DETAIL


def _case_step(description: str, order: int = 0) -> dict:
    return {"order": order, "action": "click", "args": {}, "description": description}


class ConcurrentStepsVersionCasTests(unittest.TestCase):
    """同刻双发同令牌 PUT：恰一胜一 409，版本恰 +1（真并发，非顺序双发）。"""

    def setUp(self) -> None:
        self._tmpdir = TemporaryDirectory()
        db_path = Path(self._tmpdir.name) / "concurrent_cas.db"
        # 文件库 + 每请求独立 Session + WAL/busy_timeout：与 uvicorn 生产形态同构
        self.engine = create_engine(
            f"sqlite:///{db_path.as_posix()}",
            connect_args={"check_same_thread": False},
        )
        configure_sqlite_engine(self.engine)
        SQLModel.metadata.create_all(self.engine)

        with Session(self.engine) as session:
            user = User(username="alice", hashed_password="x", role="user")
            session.add(user)
            session.commit()
            session.refresh(user)
            self.user_id = user.id
            case = TestCase(name="登录用例", steps=[], variables=[], user_id=user.id)
            scenario = TestScenario(name="回归场景", user_id=user.id)
            session.add(case)
            session.add(scenario)
            session.commit()
            session.refresh(case)
            session.refresh(scenario)
            self.case_id = case.id
            self.scenario_id = scenario.id
            self.initial_updated_at = case.updated_at.isoformat()
            self.initial_scenario_updated_at = scenario.updated_at.isoformat()

        app = FastAPI()
        app.include_router(cases_api.router, prefix="/api/cases")
        app.include_router(scenarios_api.router, prefix="/api/scenarios")

        def _per_request_session():
            with Session(self.engine) as session:
                yield session

        app.dependency_overrides[get_session] = _per_request_session
        self.app = app
        self.token = jwt.encode({"sub": "alice"}, SECRET_KEY, algorithm=ALGORITHM)
        self.headers = {"Authorization": f"Bearer {self.token}"}

    def tearDown(self) -> None:
        self.engine.dispose()
        self._tmpdir.cleanup()

    # -- 真并发执行器：Barrier 同刻双发，每线程独立 TestClient/Session --

    def _fire_concurrently(
        self,
        request_fn: Callable[[TestClient, int], "object"],
        workers: int = 2,
    ) -> List[Tuple[int, object]]:
        barrier = threading.Barrier(workers)
        results: List[Tuple[int, object]] = []
        lock = threading.Lock()

        def _worker(index: int) -> None:
            client = TestClient(self.app)
            barrier.wait(timeout=30)
            resp = request_fn(client, index)
            with lock:
                results.append((index, resp))

        threads = [threading.Thread(target=_worker, args=(i,)) for i in range(workers)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=60)
        self.assertEqual(len(results), workers, "并发线程未全部完成")
        return results

    def _assert_exactly_one_winner(self, results: List[Tuple[int, object]]):
        codes = sorted(resp.status_code for _, resp in results)
        self.assertEqual(codes, [200, 409], f"应恰一个 200 一个 409，实际 {codes}")
        for _, resp in results:
            if resp.status_code == 409:
                self.assertEqual(resp.json()["detail"], STEPS_VERSION_CONFLICT_DETAIL)
        winner_index = next(i for i, resp in results if resp.status_code == 200)
        return winner_index

    def _case_rows(self):
        with Session(self.engine) as session:
            return session.exec(
                select(TestCaseStep).where(TestCaseStep.case_id == self.case_id).order_by(TestCaseStep.order)
            ).all()

    def _scenario_rows(self):
        with Session(self.engine) as session:
            return session.exec(
                select(ScenarioStep).where(ScenarioStep.scenario_id == self.scenario_id).order_by(ScenarioStep.order)
            ).all()

    def _case_state(self):
        with Session(self.engine) as session:
            case = session.get(TestCase, self.case_id)
            return case.version, case.updated_at

    def _scenario_state(self):
        with Session(self.engine) as session:
            scenario = session.get(TestScenario, self.scenario_id)
            return scenario.version, scenario.updated_at

    # -- cases：三种令牌通道 --

    def test_concurrent_case_steps_if_match_only_one_200(self):
        results = self._fire_concurrently(
            lambda client, idx: client.put(
                f"/api/cases/{self.case_id}/steps",
                json=[_case_step(f"writer-{idx}")],
                headers={**self.headers, "If-Match": "1"},
            )
        )
        winner = self._assert_exactly_one_winner(results)

        version, _ = self._case_state()
        self.assertEqual(version, 2, "final version 必须恰 +1")
        rows = self._case_rows()
        self.assertEqual([r.description for r in rows], [f"writer-{winner}"], "败者不得半写/覆盖")

    def test_concurrent_case_steps_version_query_only_one_200(self):
        results = self._fire_concurrently(
            lambda client, idx: client.put(
                f"/api/cases/{self.case_id}/steps",
                json=[_case_step(f"writer-{idx}")],
                headers=self.headers,
                params={"version": 1},
            )
        )
        winner = self._assert_exactly_one_winner(results)

        version, _ = self._case_state()
        self.assertEqual(version, 2)
        rows = self._case_rows()
        self.assertEqual([r.description for r in rows], [f"writer-{winner}"])

    def test_concurrent_case_steps_expected_updated_at_only_one_200(self):
        stale_iso = self.initial_updated_at
        results = self._fire_concurrently(
            lambda client, idx: client.put(
                f"/api/cases/{self.case_id}/steps",
                json=[_case_step(f"writer-{idx}")],
                headers=self.headers,
                params={"expected_updated_at": stale_iso},
            )
        )
        winner = self._assert_exactly_one_winner(results)

        version, _ = self._case_state()
        self.assertEqual(version, 2, "ISO 令牌 CAS 同样必须原子递增")
        rows = self._case_rows()
        self.assertEqual([r.description for r in rows], [f"writer-{winner}"])

    def test_concurrent_case_steps_four_writers_single_winner(self):
        """4 线程同刻同令牌：仍恰一胜，version 恰 +1（CAS 不依赖调度运气）。"""
        results = self._fire_concurrently(
            lambda client, idx: client.put(
                f"/api/cases/{self.case_id}/steps",
                json=[_case_step(f"writer-{idx}")],
                headers={**self.headers, "If-Match": "1"},
            ),
            workers=4,
        )
        codes = sorted(resp.status_code for _, resp in results)
        self.assertEqual(codes.count(200), 1, f"恰一个 200，实际 {codes}")
        self.assertEqual(codes.count(409), 3, f"其余全部 409，实际 {codes}")
        for _, resp in results:
            if resp.status_code == 409:
                self.assertEqual(resp.json()["detail"], STEPS_VERSION_CONFLICT_DETAIL)

        version, _ = self._case_state()
        self.assertEqual(version, 2, "final version 必须恰 +1")

    # -- scenarios：三种令牌通道 --

    def test_concurrent_scenario_steps_if_match_only_one_200(self):
        results = self._fire_concurrently(
            lambda client, idx: client.post(
                f"/api/scenarios/{self.scenario_id}/steps",
                json=[{"case_id": idx + 1, "order": 0}],
                headers={**self.headers, "If-Match": "1"},
            )
        )
        winner = self._assert_exactly_one_winner(results)

        version, _ = self._scenario_state()
        self.assertEqual(version, 2, "final version 必须恰 +1")
        rows = self._scenario_rows()
        self.assertEqual([r.case_id for r in rows], [winner + 1], "败者不得半写/覆盖")

    def test_concurrent_scenario_steps_version_query_only_one_200(self):
        results = self._fire_concurrently(
            lambda client, idx: client.put(
                f"/api/scenarios/{self.scenario_id}/steps",
                json=[{"case_id": idx + 1, "order": 0}],
                headers=self.headers,
                params={"version": 1},
            )
        )
        winner = self._assert_exactly_one_winner(results)

        version, _ = self._scenario_state()
        self.assertEqual(version, 2)
        rows = self._scenario_rows()
        self.assertEqual([r.case_id for r in rows], [winner + 1])

    def test_concurrent_scenario_steps_expected_updated_at_only_one_200(self):
        stale_iso = self.initial_scenario_updated_at
        results = self._fire_concurrently(
            lambda client, idx: client.post(
                f"/api/scenarios/{self.scenario_id}/steps",
                json=[{"case_id": idx + 1, "order": 0}],
                headers=self.headers,
                params={"expected_updated_at": stale_iso},
            )
        )
        winner = self._assert_exactly_one_winner(results)

        version, _ = self._scenario_state()
        self.assertEqual(version, 2, "ISO 令牌 CAS 同样必须原子递增")
        rows = self._scenario_rows()
        self.assertEqual([r.case_id for r in rows], [winner + 1])


class SequentialCasRegressionTests(unittest.TestCase):
    """CAS 收敛后顺序路径回归：失配 409 冻结文案 + 写入半写防护。"""

    def setUp(self) -> None:
        from sqlalchemy.pool import StaticPool

        self.engine = create_engine(
            "sqlite://",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        SQLModel.metadata.create_all(self.engine)
        self.session = Session(self.engine)
        user = User(username="alice", hashed_password="x", role="user")
        self.session.add(user)
        self.session.commit()
        self.session.refresh(user)
        self.case = TestCase(name="c", steps=[], variables=[], user_id=user.id)
        self.session.add(self.case)
        self.session.commit()
        self.session.refresh(self.case)

        app = FastAPI()
        app.include_router(cases_api.router, prefix="/api/cases")
        app.dependency_overrides[get_session] = lambda: self.session
        self.client = TestClient(app)
        self.headers = {"Authorization": f"Bearer {jwt.encode({'sub': 'alice'}, SECRET_KEY, algorithm=ALGORITHM)}"}

    def tearDown(self) -> None:
        self.session.close()
        self.engine.dispose()

    def test_stale_token_conflict_leaves_rows_untouched(self):
        first = self.client.put(
            f"/api/cases/{self.case.id}/steps",
            json=[_case_step("A")],
            headers={**self.headers, "If-Match": "1"},
        )
        self.assertEqual(first.status_code, 200, first.text)

        second = self.client.put(
            f"/api/cases/{self.case.id}/steps",
            json=[_case_step("B")],
            headers={**self.headers, "If-Match": "1"},
        )
        self.assertEqual(second.status_code, 409)
        self.assertEqual(second.json()["detail"], STEPS_VERSION_CONFLICT_DETAIL)

        self.session.refresh(self.case)
        self.assertEqual(self.case.version, 2)
        rows = self.session.exec(select(TestCaseStep).where(TestCaseStep.case_id == self.case.id)).all()
        self.assertEqual([r.description for r in rows], ["A"])

    def test_no_token_write_still_bumps_version(self):
        resp = self.client.put(
            f"/api/cases/{self.case.id}/steps",
            json=[_case_step("A")],
            headers=self.headers,
        )
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertEqual(resp.json()["version"], 2)
        self.assertTrue(resp.json().get("updated_at"))

    def test_updated_at_iso_token_roundtrip(self):
        self.client.put(f"/api/cases/{self.case.id}/steps", json=[_case_step("A")], headers=self.headers)
        self.session.refresh(self.case)
        iso_token = self.case.updated_at.isoformat()

        resp = self.client.put(
            f"/api/cases/{self.case.id}/steps",
            json=[_case_step("B")],
            headers=self.headers,
            params={"expected_updated_at": iso_token},
        )
        self.assertEqual(resp.status_code, 200, resp.text)

        stale = datetime(2020, 1, 1).isoformat()
        resp = self.client.put(
            f"/api/cases/{self.case.id}/steps",
            json=[_case_step("C")],
            headers=self.headers,
            params={"expected_updated_at": stale},
        )
        self.assertEqual(resp.status_code, 409)


if __name__ == "__main__":
    unittest.main()
