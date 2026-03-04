from sqlmodel import Session, select
from backend.database import engine
from backend.models import GlobalVariable, TestCase

with Session(engine) as session:
    gvs = session.exec(select(GlobalVariable)).all()
    print("Global Variables:")
    for g in gvs:
        print(f"ID={g.id}, ENV={g.env_id}, KEY='{g.key}', VAL='{g.value}'")
    
    cases = session.exec(select(TestCase)).all()
    print("\nLocal Variables:")
    for c in cases:
        for v in c.variables:
            print(f"CASE={c.id}, KEY='{v.key}', VAL='{v.value}'")
