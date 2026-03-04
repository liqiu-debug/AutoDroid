from sqlmodel import Session, select, create_engine
from backend.models import TestScenario

sqlite_file_name = "database.db"
sqlite_url = f"sqlite:///{sqlite_file_name}"
engine = create_engine(sqlite_url)

try:
    with Session(engine) as session:
        scenarios = session.exec(select(TestScenario)).all()
        print(f"Successfully retrieved {len(scenarios)} scenarios.")
        for s in scenarios:
            print(f"Scenario: {s.name}, Duration: {s.last_run_duration}")
except Exception as e:
    print(f"Error: {e}")
