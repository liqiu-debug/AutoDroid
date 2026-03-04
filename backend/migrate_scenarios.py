import sqlite3

def migrate():
    conn = sqlite3.connect('autodroid.db')
    cursor = conn.cursor()
    
    # 检查 updater_id
    try:
        cursor.execute("ALTER TABLE testscenario ADD COLUMN updater_id INTEGER")
        print("添加了 updater_id")
    except sqlite3.OperationalError as e:
        print(f"updater_id 可能已存在: {e}")
        
    try:
        cursor.execute("ALTER TABLE testscenario ADD COLUMN creator_name VARCHAR")
    except sqlite3.OperationalError: pass
    try:
        cursor.execute("ALTER TABLE testscenario ADD COLUMN updater_name VARCHAR")
    except sqlite3.OperationalError: pass

    conn.commit()
    conn.close()
    
if __name__ == "__main__":
    migrate()
