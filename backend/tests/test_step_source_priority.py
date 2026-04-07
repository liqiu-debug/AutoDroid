import unittest

from sqlmodel import SQLModel, Session, create_engine

from backend.cross_platform_execution import list_standard_step_payloads
from backend.models import TestCase, TestCaseStep
from backend.schemas import Step


class StepSourcePriorityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
        SQLModel.metadata.create_all(self.engine)
        self.session = Session(self.engine)

    def tearDown(self) -> None:
        self.session.close()

    def _create_case_with_legacy_step(self) -> TestCase:
        case = TestCase(
            name="case-1",
            steps=[
                Step(
                    action="click",
                    selector="com.demo:id/login",
                    selector_type="resourceId",
                    description="legacy click",
                )
            ],
            variables=[],
        )
        self.session.add(case)
        self.session.commit()
        self.session.refresh(case)
        return case

    def test_fallback_to_legacy_when_standard_rows_absent(self):
        case = self._create_case_with_legacy_step()
        payloads = list_standard_step_payloads(self.session, case)

        self.assertEqual(len(payloads), 1)
        self.assertEqual(payloads[0]["action"], "click")
        self.assertIn("platform_overrides", payloads[0])

    def test_standard_rows_take_priority_over_legacy_json(self):
        case = self._create_case_with_legacy_step()

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
                description="standard sleep",
            )
        )
        self.session.commit()

        payloads = list_standard_step_payloads(self.session, case)

        self.assertEqual(len(payloads), 1)
        self.assertEqual(payloads[0]["action"], "sleep")
        self.assertEqual(payloads[0]["args"].get("seconds"), 2)


if __name__ == "__main__":
    unittest.main()
