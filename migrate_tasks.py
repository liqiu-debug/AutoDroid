"""数据库迁移：创建 ScheduledTask 表"""
from sqlmodel import Session, text
from backend.database import engine

def migrate():
    with Session(engine) as session:
        print("检查 scheduledtask 表...")
        try:
            session.exec(text("SELECT id FROM scheduledtask LIMIT 1"))
            print("scheduledtask 表已存在。")
        except Exception:
            print("创建 scheduledtask 表...")
            session.exec(text("""
                CREATE TABLE scheduledtask (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name VARCHAR NOT NULL,
                    scenario_id INTEGER NOT NULL REFERENCES testscenario(id),
                    device_serial VARCHAR,
                    strategy VARCHAR NOT NULL,
                    strategy_config VARCHAR,
                    is_active BOOLEAN DEFAULT 1,
                    next_run_time DATETIME,
                    user_id INTEGER REFERENCES user(id),
                    created_at DATETIME NOT NULL,
                    updated_at DATETIME
                )
            """))
            session.commit()
            print("scheduledtask 表创建成功。")

if __name__ == "__main__":
    migrate()
