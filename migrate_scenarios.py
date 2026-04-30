import sqlite3
from backend.database import sqlite_path

def migrate():
    conn = sqlite3.connect(str(sqlite_path))
    cursor = conn.cursor()
    
    try:
        cursor.execute("ALTER TABLE testscenario ADD COLUMN last_executor VARCHAR")
        print("Added last_executor to testscenario")
    except sqlite3.OperationalError as e:
        print(f"Skipped adding last_executor: {e}")
        
    try:
        cursor.execute("ALTER TABLE testscenario ADD COLUMN last_failed_step VARCHAR")
        print("Added last_failed_step to testscenario")
    except sqlite3.OperationalError as e:
        print(f"Skipped adding last_failed_step: {e}")

    conn.commit()
    conn.close()
    print("Migration complete.")

if __name__ == "__main__":
    migrate()
