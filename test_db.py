from sqlmodel import Session, select
from backend.database import engine
from backend.models import User, TestExecution, TestScenario
from sqlalchemy.orm import aliased

with Session(engine) as session:
    Creator = aliased(User)
    Updater = aliased(User)
    query = session.query(TestScenario, Creator.full_name, Creator.username, Updater.full_name, Updater.username).outerjoin(Creator, TestScenario.user_id == Creator.id).outerjoin(Updater, TestScenario.updater_id == Updater.id)
    results = query.all()
    print("Scenarios:")
    for row in results:
        print(row)
        
    print("\nExecutions:")
    query = session.exec(select(TestExecution, User.full_name, User.username).outerjoin(User, TestExecution.executor_id == User.id))
    results = query.all()
    for row in results:
        print(row)
