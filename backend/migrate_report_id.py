from sqlmodel import text

from backend.database import engine

with engine.connect() as connection:
    try:
        connection.execute(text("ALTER TABLE testscenario ADD COLUMN last_report_id VARCHAR"))
        print("Migration successful: Added last_report_id to testscenario")
    except Exception as e:
        print(f"Migration failed (maybe column exists?): {e}")
