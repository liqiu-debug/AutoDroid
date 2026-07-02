import unittest

from sqlalchemy import event
from sqlmodel import Session, SQLModel, create_engine

from backend.api import cases as cases_api
from backend.models import CaseFolder, SystemSetting, TestCase, TestCaseStep, User


class CaseListQueryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
        SQLModel.metadata.create_all(self.engine)
        self.session = Session(self.engine)

    def tearDown(self) -> None:
        self.session.close()

    def test_list_batches_standard_steps_and_feature_flag_queries(self):
        user = User(username="owner", hashed_password="x", full_name="Owner")
        folder = CaseFolder(name="folder")
        self.session.add(user)
        self.session.add(folder)
        self.session.add(SystemSetting(key="new_step_model", value="true"))
        self.session.commit()
        self.session.refresh(user)
        self.session.refresh(folder)

        for index in range(3):
            case = TestCase(
                name=f"case-{index}",
                steps=[],
                variables=[],
                user_id=user.id,
                updater_id=user.id,
                folder_id=folder.id,
            )
            self.session.add(case)
            self.session.commit()
            self.session.refresh(case)
            self.session.add(
                TestCaseStep(
                    case_id=case.id,
                    order=1,
                    action="click",
                    args={},
                    execute_on=["android"],
                    platform_overrides={},
                    timeout=10,
                    error_strategy="ABORT",
                    description=f"standard-{index}",
                )
            )
            self.session.commit()

        statements = []

        def _capture(_conn, _cursor, statement, _parameters, _context, _executemany):
            statements.append(statement.lower())

        event.listen(self.engine, "before_cursor_execute", _capture)
        try:
            response = cases_api.list_test_cases(session=self.session, folder_id=None)
        finally:
            event.remove(self.engine, "before_cursor_execute", _capture)

        self.assertEqual(len(response.items), 3)
        self.assertEqual({item.folder_name for item in response.items}, {"folder"})

        step_queries = [sql for sql in statements if "from testcasestep" in sql]
        flag_queries = [sql for sql in statements if "from systemsetting" in sql]

        self.assertLessEqual(len(step_queries), 1)
        self.assertLessEqual(len(flag_queries), 1)


if __name__ == "__main__":
    unittest.main()
