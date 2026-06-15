import unittest

from fastapi import HTTPException
from sqlmodel import Session, SQLModel, create_engine, select

from backend.api import cases as case_api
from backend.api import scenarios as scenario_api
from backend.database import backfill_legacy_asset_owners
from backend.models import ScenarioStep, TestCase, TestCaseStep, TestScenario, User


class DeletePermissionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
        SQLModel.metadata.create_all(self.engine)
        self.session = Session(self.engine)

        self.owner = User(username="owner", hashed_password="secret", role="user")
        self.other = User(username="other", hashed_password="secret", role="user")
        self.admin = User(username="admin", hashed_password="secret", role="admin")
        self.session.add(self.owner)
        self.session.add(self.other)
        self.session.add(self.admin)
        self.session.commit()
        self.session.refresh(self.owner)
        self.session.refresh(self.other)
        self.session.refresh(self.admin)

    def tearDown(self) -> None:
        self.session.close()

    def _create_case(self, owner: User) -> TestCase:
        case = TestCase(name="case-owned", steps=[], variables=[], user_id=owner.id)
        self.session.add(case)
        self.session.commit()
        self.session.refresh(case)
        self.session.add(TestCaseStep(case_id=case.id, order=1, action="click"))
        self.session.commit()
        return case

    def _create_scenario(self, owner: User) -> TestScenario:
        case = TestCase(name="scenario-case", steps=[], variables=[], user_id=owner.id)
        scenario = TestScenario(name="scenario-owned", user_id=owner.id)
        self.session.add(case)
        self.session.add(scenario)
        self.session.commit()
        self.session.refresh(case)
        self.session.refresh(scenario)
        self.session.add(ScenarioStep(scenario_id=scenario.id, case_id=case.id, order=1))
        self.session.commit()
        return scenario

    def test_case_owner_can_delete(self):
        case = self._create_case(self.owner)

        payload = case_api.delete_test_case(
            case.id,
            session=self.session,
            current_user=self.owner,
        )

        self.assertEqual(payload, {"message": "Case deleted", "id": case.id})
        self.assertIsNone(self.session.get(TestCase, case.id))
        steps = self.session.exec(
            select(TestCaseStep).where(TestCaseStep.case_id == case.id)
        ).all()
        self.assertEqual(steps, [])

    def test_admin_can_delete_case_owned_by_another_user(self):
        case = self._create_case(self.owner)

        case_api.delete_test_case(case.id, session=self.session, current_user=self.admin)

        self.assertIsNone(self.session.get(TestCase, case.id))

    def test_other_user_cannot_delete_case(self):
        case = self._create_case(self.owner)

        with self.assertRaises(HTTPException) as context:
            case_api.delete_test_case(case.id, session=self.session, current_user=self.other)

        self.assertEqual(context.exception.status_code, 403)
        self.assertEqual(context.exception.detail, "仅创建人或管理员可以删除")
        self.assertIsNotNone(self.session.get(TestCase, case.id))
        steps = self.session.exec(
            select(TestCaseStep).where(TestCaseStep.case_id == case.id)
        ).all()
        self.assertEqual(len(steps), 1)

    def test_scenario_owner_can_delete(self):
        scenario = self._create_scenario(self.owner)

        payload = scenario_api.delete_scenario(
            scenario.id,
            session=self.session,
            current_user=self.owner,
        )

        self.assertEqual(payload, {"message": "Scenario deleted", "id": scenario.id})
        self.assertIsNone(self.session.get(TestScenario, scenario.id))
        steps = self.session.exec(
            select(ScenarioStep).where(ScenarioStep.scenario_id == scenario.id)
        ).all()
        self.assertEqual(steps, [])

    def test_admin_can_delete_scenario_owned_by_another_user(self):
        scenario = self._create_scenario(self.owner)

        scenario_api.delete_scenario(scenario.id, session=self.session, current_user=self.admin)

        self.assertIsNone(self.session.get(TestScenario, scenario.id))

    def test_other_user_cannot_delete_scenario(self):
        scenario = self._create_scenario(self.owner)

        with self.assertRaises(HTTPException) as context:
            scenario_api.delete_scenario(
                scenario.id,
                session=self.session,
                current_user=self.other,
            )

        self.assertEqual(context.exception.status_code, 403)
        self.assertEqual(context.exception.detail, "仅创建人或管理员可以删除")
        self.assertIsNotNone(self.session.get(TestScenario, scenario.id))
        steps = self.session.exec(
            select(ScenarioStep).where(ScenarioStep.scenario_id == scenario.id)
        ).all()
        self.assertEqual(len(steps), 1)


class LegacyAssetOwnerBackfillTests(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
        SQLModel.metadata.create_all(self.engine)
        self.session = Session(self.engine)

        self.admin = User(username="admin", hashed_password="secret", role="admin")
        self.owner = User(username="owner", hashed_password="secret", role="user")
        self.session.add(self.admin)
        self.session.add(self.owner)
        self.session.commit()
        self.session.refresh(self.admin)
        self.session.refresh(self.owner)

    def tearDown(self) -> None:
        self.session.close()

    def test_backfill_assigns_only_unowned_cases_and_scenarios_to_admin(self):
        unowned_case = TestCase(name="legacy-case", steps=[], variables=[])
        owned_case = TestCase(
            name="owned-case",
            steps=[],
            variables=[],
            user_id=self.owner.id,
        )
        unowned_scenario = TestScenario(name="legacy-scenario")
        owned_scenario = TestScenario(name="owned-scenario", user_id=self.owner.id)
        self.session.add(unowned_case)
        self.session.add(owned_case)
        self.session.add(unowned_scenario)
        self.session.add(owned_scenario)
        self.session.commit()
        self.session.refresh(unowned_case)
        self.session.refresh(owned_case)
        self.session.refresh(unowned_scenario)
        self.session.refresh(owned_scenario)

        counts = backfill_legacy_asset_owners(self.session, self.admin.id)

        self.assertEqual(counts, {"cases": 1, "scenarios": 1})
        self.assertEqual(self.session.get(TestCase, unowned_case.id).user_id, self.admin.id)
        self.assertEqual(self.session.get(TestScenario, unowned_scenario.id).user_id, self.admin.id)
        self.assertEqual(self.session.get(TestCase, owned_case.id).user_id, self.owner.id)
        self.assertEqual(self.session.get(TestScenario, owned_scenario.id).user_id, self.owner.id)

        counts_again = backfill_legacy_asset_owners(self.session, self.admin.id)
        self.assertEqual(counts_again, {"cases": 0, "scenarios": 0})


if __name__ == "__main__":
    unittest.main()
