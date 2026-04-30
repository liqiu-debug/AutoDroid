import sqlite3
from backend.database import sqlite_path

def migrate():
    conn = sqlite3.connect(str(sqlite_path))
    cursor = conn.cursor()
    
    try:
        cursor.execute("ALTER TABLE testexecution ADD COLUMN batch_id VARCHAR")
        print("添加了 batch_id")
    except sqlite3.OperationalError as e:
        print(f"batch_id 可能已存在: {e}")
        
    try:
        cursor.execute("ALTER TABLE testexecution ADD COLUMN batch_name VARCHAR")
        print("添加了 batch_name")
    except sqlite3.OperationalError as e:
        print(f"batch_name 可能已存在: {e}")

    conn.commit()
    conn.close()
    
if __name__ == "__main__":
    migrate()
