import unittest

from sqlmodel import Session, SQLModel, create_engine, select

from backend.database import backfill_case_steps_to_standard
from backend.models import TestCase, TestCaseStep


class CaseStepsStartupBackfillTests(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
        SQLModel.metadata.create_all(self.engine)
        self.session = Session(self.engine)

    def tearDown(self) -> None:
        self.session.close()

    def _add_case(self, name: str, steps) -> TestCase:
        case = TestCase(name=name, steps=steps, variables=[])
        self.session.add(case)
        self.session.commit()
        self.session.refresh(case)
        return case

    def _standard_steps(self, case_id: int):
        return self.session.exec(
            select(TestCaseStep)
            .where(TestCaseStep.case_id == case_id)
            .order_by(TestCaseStep.order)
        ).all()

    def test_backfills_legacy_steps_into_standard_rows(self):
        case = self._add_case(
            "legacy-case",
            [
                {
                    "action": "click",
                    "selector": "com.demo:id/login",
                    "selector_type": "resourceId",
                    "description": "click login",
                },
                {
                    "action": "input",
                    "selector": "com.demo:id/phone",
                    "selector_type": "resourceId",
                    "value": "18800001111",
                },
            ],
        )

        summary = backfill_case_steps_to_standard(self.session)

        self.assertEqual(summary["migrated_cases"], 1)
        self.assertEqual(summary["created_steps"], 2)
        self.assertEqual(summary["failed_cases"], 0)

        rows = self._standard_steps(case.id)
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0].action, "click")
        self.assertEqual(rows[0].order, 1)
        self.assertEqual(
            rows[0].platform_overrides.get("android"),
            {"selector": "com.demo:id/login", "by": "id"},
        )
        self.assertEqual(rows[1].action, "input")
        self.assertEqual(rows[1].args.get("text"), "18800001111")

    def test_backfill_is_idempotent_and_preserves_existing_rows(self):
        case = self._add_case(
            "already-migrated",
            [{"action": "click", "selector": "登录", "selector_type": "text"}],
        )
        self.session.add(
            TestCaseStep(
                case_id=case.id,
                order=1,
                action="sleep",
                args={"seconds": 2},
                execute_on=["android", "ios"],
                platform_overrides={},
                timeout=10,
                error_strategy="ABORT",
                description="manually curated",
            )
        )
        self.session.commit()

        summary = backfill_case_steps_to_standard(self.session)

        self.assertEqual(summary["migrated_cases"], 0)
        self.assertEqual(summary["skipped_cases"], 1)

        rows = self._standard_steps(case.id)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].action, "sleep")

        # 再跑一遍仍然全部 skip，不产生重复行。
        second = backfill_case_steps_to_standard(self.session)
        self.assertEqual(second["migrated_cases"], 0)
        self.assertEqual(len(self._standard_steps(case.id)), 1)

    def test_backfill_skips_cases_without_legacy_steps(self):
        self._add_case("empty-case", [])

        summary = backfill_case_steps_to_standard(self.session)

        self.assertEqual(summary["migrated_cases"], 0)
        self.assertEqual(summary["skipped_cases"], 1)
        self.assertEqual(summary["failed_cases"], 0)

    def test_backfill_isolates_per_case_failures(self):
        from sqlalchemy import text

        bad_case = self._add_case(
            "bad-case",
            [{"action": "click", "selector": "占位", "selector_type": "text"}],
        )
        good_case = self._add_case(
            "good-case",
            [{"action": "click", "selector": "登录", "selector_type": "text"}],
        )
        bad_case_id = int(bad_case.id)
        good_case_id = int(good_case.id)
        # 模拟历史脏数据：绕过 ORM 校验写入非法 action，读取该 case 时会抛异常。
        self.session.exec(
            text("UPDATE testcase SET steps = :steps WHERE id = :id").bindparams(
                steps='[{"action": "not_a_real_action"}]',
                id=bad_case_id,
            )
        )
        self.session.commit()
        self.session.expire_all()

        summary = backfill_case_steps_to_standard(self.session)

        self.assertEqual(summary["migrated_cases"], 1)
        self.assertEqual(summary["failed_cases"], 1)
        self.assertEqual(self._standard_steps(bad_case_id), [])
        self.assertEqual(len(self._standard_steps(good_case_id)), 1)

    def test_force_replaces_existing_standard_rows(self):
        case = self._add_case(
            "force-case",
            [{"action": "click", "selector": "登录", "selector_type": "text"}],
        )
        self.session.add(
            TestCaseStep(
                case_id=case.id,
                order=1,
                action="sleep",
                args={"seconds": 5},
                execute_on=["android", "ios"],
                platform_overrides={},
                timeout=10,
                error_strategy="ABORT",
            )
        )
        self.session.commit()

        summary = backfill_case_steps_to_standard(self.session, force=True)

        self.assertEqual(summary["migrated_cases"], 1)
        rows = self._standard_steps(case.id)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].action, "click")


if __name__ == "__main__":
    unittest.main()
