from sqlmodel import Session, select
from backend.database import engine
from backend.models import User, TestCase, TestScenario

def migrate():
    with Session(engine) as session:
        # Get Admin User
        admin = session.exec(select(User).where(User.username == "admin")).first()
        if not admin:
            print("❌ Admin user not found. Please start the backend server at least once to create it.")
            return

        print(f"found Admin User: {admin.username} (ID: {admin.id})")

        # Update TestCases
        cases = session.exec(select(TestCase)).all()
        case_count = 0
        for case in cases:
            if case.user_id != admin.id:
                case.user_id = admin.id
                session.add(case)
                case_count += 1
        
        # Update TestScenarios
        scenarios = session.exec(select(TestScenario)).all()
        scenario_count = 0
        for scenario in scenarios:
            if scenario.user_id != admin.id:
                scenario.user_id = admin.id
                session.add(scenario)
                scenario_count += 1

        session.commit()
        print(f"✅ Updated {case_count} TestCases and {scenario_count} TestScenarios to belong to Admin.")

if __name__ == "__main__":
    migrate()
