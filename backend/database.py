import logging
from sqlmodel import Session, SQLModel, create_engine

logger = logging.getLogger(__name__)

sqlite_file_name = "database.db"
sqlite_url = f"sqlite:///{sqlite_file_name}"

# check_same_thread=False is needed for SQLite with multiple threads/FastAPI
engine = create_engine(sqlite_url, connect_args={"check_same_thread": False})


def _run_migrations():
    """对已有表执行 ALTER TABLE 添加新列（SQLite 不支持 IF NOT EXISTS，需先检查）"""
    import sqlite3
    conn = sqlite3.connect(sqlite_file_name)
    cursor = conn.cursor()

    migrations = [
        ("testcase", "folder_id", "INTEGER REFERENCES casefolder(id)"),
        ("device", "brand", "VARCHAR DEFAULT ''"),
        ("device", "custom_name", "VARCHAR(100)"),
        ("device", "market_name", "VARCHAR(100)"),
        ("testscenario", "updater_id", "INTEGER REFERENCES user(id)"),
        ("testscenario", "last_run_duration", "INTEGER"),
        ("testscenario", "last_report_id", "VARCHAR"),
        ("testscenario", "last_execution_id", "INTEGER"),
        ("testscenario", "last_executor", "VARCHAR"),
        ("testscenario", "last_failed_step", "VARCHAR"),
    ]

    for table, column, col_type in migrations:
        cursor.execute(f"PRAGMA table_info({table})")
        existing_cols = {row[1] for row in cursor.fetchall()}
        if column not in existing_cols:
            cursor.execute(f"ALTER TABLE {table} ADD COLUMN {column} {col_type}")
            logger.info(f"Migration: ALTER TABLE {table} ADD COLUMN {column}")

    _migrate_scheduledtask_scenario_id_nullable(cursor)

    conn.commit()
    conn.close()


def _migrate_scheduledtask_scenario_id_nullable(cursor):
    """将 scheduledtask.scenario_id 从 NOT NULL 改为可空"""
    cursor.execute("PRAGMA table_info(scheduledtask)")
    cols = cursor.fetchall()
    scenario_col = next((c for c in cols if c[1] == "scenario_id"), None)
    if scenario_col is None:
        return
    # scenario_col[3] == notnull flag: 1 means NOT NULL
    if scenario_col[3] != 1:
        return

    logger.info("Migration: making scheduledtask.scenario_id nullable")
    cursor.execute("""
        CREATE TABLE scheduledtask_new (
            id INTEGER PRIMARY KEY,
            name VARCHAR NOT NULL,
            scenario_id INTEGER REFERENCES testscenario(id),
            device_serial VARCHAR,
            strategy VARCHAR NOT NULL,
            strategy_config VARCHAR,
            is_active BOOLEAN NOT NULL DEFAULT 1,
            enable_notification BOOLEAN NOT NULL DEFAULT 1,
            next_run_time TIMESTAMP,
            user_id INTEGER REFERENCES user(id),
            created_at TIMESTAMP NOT NULL,
            updated_at TIMESTAMP
        )
    """)
    cursor.execute("""
        INSERT INTO scheduledtask_new
        SELECT id, name, scenario_id, device_serial, strategy, strategy_config,
               is_active, enable_notification, next_run_time, user_id, created_at, updated_at
        FROM scheduledtask
    """)
    cursor.execute("DROP TABLE scheduledtask")
    cursor.execute("ALTER TABLE scheduledtask_new RENAME TO scheduledtask")


def create_db_and_tables():
    SQLModel.metadata.create_all(engine)
    _run_migrations()


def get_session():
    with Session(engine) as session:
        yield session
