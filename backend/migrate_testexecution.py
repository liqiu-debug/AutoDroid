import sqlite3

def migrate():
    conn = sqlite3.connect('autodroid.db')
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
