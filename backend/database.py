import logging
import os
from pathlib import Path
from sqlmodel import Session, SQLModel, create_engine

from backend.paths import PROJECT_ROOT

logger = logging.getLogger(__name__)

sqlite_file_name = os.getenv("AUTODROID_DB_PATH", "database.db")
sqlite_path = Path(sqlite_file_name).expanduser()
if not sqlite_path.is_absolute():
    sqlite_path = PROJECT_ROOT / sqlite_path
sqlite_path.parent.mkdir(parents=True, exist_ok=True)
sqlite_url = f"sqlite:///{sqlite_path.as_posix()}"

# check_same_thread=False is needed for SQLite with multiple threads/FastAPI
engine = create_engine(sqlite_url, connect_args={"check_same_thread": False})


def _table_exists(cursor, table: str) -> bool:
    cursor.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (table,),
    )
    return cursor.fetchone() is not None


def _ensure_schema_migration_table(cursor) -> None:
    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS schema_migration (
            version VARCHAR PRIMARY KEY,
            applied_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
        """
    )


def _is_migration_applied(cursor, version: str) -> bool:
    cursor.execute(
        "SELECT 1 FROM schema_migration WHERE version = ? LIMIT 1",
        (version,),
    )
    return cursor.fetchone() is not None


def _mark_migration_applied(cursor, version: str) -> None:
    cursor.execute(
        "INSERT INTO schema_migration(version) VALUES (?)",
        (version,),
    )


def _migration_add_columns(cursor) -> None:
    """对已有表执行 ALTER TABLE 添加新列（SQLite 不支持 IF NOT EXISTS，需先检查）"""
    migrations = [
        ("testcase", "folder_id", "INTEGER REFERENCES casefolder(id)"),
        ("device", "brand", "VARCHAR DEFAULT ''"),
        ("device", "custom_name", "VARCHAR(100)"),
        ("device", "market_name", "VARCHAR(100)"),
        ("device", "platform", "VARCHAR DEFAULT 'android'"),
        ("device", "os_version", "VARCHAR DEFAULT ''"),
        ("testscenario", "updater_id", "INTEGER REFERENCES user(id)"),
        ("testscenario", "last_run_duration", "INTEGER"),
        ("testscenario", "last_report_id", "VARCHAR"),
        ("testscenario", "last_execution_id", "INTEGER"),
        ("testscenario", "last_executor", "VARCHAR"),
        ("testscenario", "last_failed_step", "VARCHAR"),
        ("testcasestep", "step_order", "INTEGER DEFAULT 0"),
        ("testcasestep", "args", "JSON"),
        ("testcasestep", "timeout", "INTEGER DEFAULT 10"),
        ("testcasestep", "error_strategy", "VARCHAR DEFAULT 'ABORT'"),
        ("testcasestep", "description", "VARCHAR"),
        ("testexecution", "device_serial", "VARCHAR"),
        ("testexecution", "platform", "VARCHAR"),
    ]

    for table, column, col_type in migrations:
        if not _table_exists(cursor, table):
            logger.warning("Migration skip: table %s not found when adding column %s", table, column)
            continue

        cursor.execute(f"PRAGMA table_info({table})")
        existing_cols = {row[1] for row in cursor.fetchall()}
        if column not in existing_cols:
            cursor.execute(f"ALTER TABLE {table} ADD COLUMN {column} {col_type}")
            logger.info("Migration: ALTER TABLE %s ADD COLUMN %s", table, column)


def _migrate_scheduledtask_scenario_id_nullable(cursor):
    """将 scheduledtask.scenario_id 从 NOT NULL 改为可空"""
    if not _table_exists(cursor, "scheduledtask"):
        return

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


def _migrate_testcasestep_order_to_step_order(cursor):
    """兼容历史列名 `order` -> `step_order`。"""
    if not _table_exists(cursor, "testcasestep"):
        return

    cursor.execute("PRAGMA table_info(testcasestep)")
    cols = cursor.fetchall()
    if not cols:
        return

    col_names = {c[1] for c in cols}
    if "step_order" not in col_names:
        return
    if "order" not in col_names:
        return

    logger.info("Migration: backfilling testcasestep.step_order from legacy order")
    cursor.execute(
        """
        UPDATE testcasestep
        SET step_order = "order"
        WHERE "order" IS NOT NULL
          AND (step_order IS NULL OR step_order = 0)
        """
    )


def _migrate_fastbotreport_jank_fields(cursor) -> None:
    """为 fastbotreport 表补充卡顿监控相关字段。"""
    if not _table_exists(cursor, "fastbotreport"):
        return

    cursor.execute("PRAGMA table_info(fastbotreport)")
    existing_cols = {row[1] for row in cursor.fetchall()}
    additions = [
        ("jank_data", "TEXT"),
        ("jank_events", "TEXT"),
        ("trace_artifacts", "TEXT"),
    ]

    for column, col_type in additions:
        if column not in existing_cols:
            cursor.execute(f"ALTER TABLE fastbotreport ADD COLUMN {column} {col_type}")
            logger.info("Migration: ALTER TABLE fastbotreport ADD COLUMN %s", column)


def _run_migrations_with_conn(conn) -> None:
    cursor = conn.cursor()
    _ensure_schema_migration_table(cursor)

    migration_plan = [
        ("20260305_001_add_columns", _migration_add_columns),
        ("20260305_002_backfill_testcasestep_order", _migrate_testcasestep_order_to_step_order),
        ("20260305_003_scheduledtask_scenario_nullable", _migrate_scheduledtask_scenario_id_nullable),
        ("20260312_004_fastbotreport_jank_fields", _migrate_fastbotreport_jank_fields),
    ]

    for version, migration_func in migration_plan:
        if _is_migration_applied(cursor, version):
            continue
        logger.info("Applying migration: %s", version)
        migration_func(cursor)
        _mark_migration_applied(cursor, version)
        logger.info("Applied migration: %s", version)

    conn.commit()


def _run_migrations():
    import sqlite3

    conn = sqlite3.connect(str(sqlite_path))
    try:
        _run_migrations_with_conn(conn)
    finally:
        conn.close()


def create_db_and_tables():
    SQLModel.metadata.create_all(engine)
    _run_migrations()


def get_session():
    with Session(engine) as session:
        yield session
