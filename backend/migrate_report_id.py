from sqlmodel import create_engine, text

sqlite_file_name = "database.db"
sqlite_url = f"sqlite:///{sqlite_file_name}"
engine = create_engine(sqlite_url)

with engine.connect() as connection:
    try:
        connection.execute(text("ALTER TABLE testscenario ADD COLUMN last_report_id VARCHAR"))
        print("Migration successful: Added last_report_id to testscenario")
    except Exception as e:
        print(f"Migration failed (maybe column exists?): {e}")
