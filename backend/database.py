import logging
import os
from pathlib import Path
from sqlmodel import Session, SQLModel, col, create_engine, select

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
        ("testresult", "report_display", "JSON"),
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


def _migrate_testresult_report_display(cursor) -> None:
    if not _table_exists(cursor, "testresult"):
        return

    cursor.execute("PRAGMA table_info(testresult)")
    existing_cols = {row[1] for row in cursor.fetchall()}
    if "report_display" not in existing_cols:
        cursor.execute("ALTER TABLE testresult ADD COLUMN report_display JSON")
        logger.info("Migration: ALTER TABLE testresult ADD COLUMN report_display")


def _migrate_compatibility_tables(cursor) -> None:
    """Create compatibility-test tables for existing SQLite databases."""
    if not _table_exists(cursor, "compatpageset"):
        cursor.execute(
            """
            CREATE TABLE compatpageset (
                id INTEGER PRIMARY KEY,
                name VARCHAR NOT NULL,
                description VARCHAR,
                pages JSON,
                user_id INTEGER REFERENCES user(id),
                updater_id INTEGER REFERENCES user(id),
                created_at TIMESTAMP NOT NULL,
                updated_at TIMESTAMP
            )
            """
        )
        logger.info("Migration: CREATE TABLE compatpageset")

    if not _table_exists(cursor, "compatibilityrun"):
        cursor.execute(
            """
            CREATE TABLE compatibilityrun (
                id INTEGER PRIMARY KEY,
                name VARCHAR NOT NULL,
                page_set_id INTEGER REFERENCES compatpageset(id),
                page_set_name VARCHAR,
                page_set_snapshot JSON,
                old_package_id INTEGER REFERENCES apppackage(id),
                new_package_id INTEGER NOT NULL REFERENCES apppackage(id),
                package_name VARCHAR DEFAULT '',
                mode VARCHAR DEFAULT 'upgrade',
                env_id INTEGER REFERENCES environment(id),
                device_serials TEXT,
                thresholds JSON,
                status VARCHAR DEFAULT 'PENDING',
                total_cells INTEGER DEFAULT 0,
                total_pages INTEGER DEFAULT 0,
                pass_count INTEGER DEFAULT 0,
                warning_count INTEGER DEFAULT 0,
                fail_count INTEGER DEFAULT 0,
                error_message VARCHAR,
                user_id INTEGER REFERENCES user(id),
                executor_name VARCHAR,
                created_at TIMESTAMP NOT NULL,
                started_at TIMESTAMP,
                finished_at TIMESTAMP
            )
            """
        )
        logger.info("Migration: CREATE TABLE compatibilityrun")

    if not _table_exists(cursor, "compatibilitycell"):
        cursor.execute(
            """
            CREATE TABLE compatibilitycell (
                id INTEGER PRIMARY KEY,
                run_id INTEGER NOT NULL REFERENCES compatibilityrun(id),
                device_serial VARCHAR NOT NULL,
                device_info VARCHAR,
                os_version VARCHAR,
                resolution VARCHAR,
                status VARCHAR DEFAULT 'PENDING',
                current_stage VARCHAR,
                old_install_status VARCHAR,
                new_install_status VARCHAR,
                error_message VARCHAR,
                started_at TIMESTAMP,
                finished_at TIMESTAMP
            )
            """
        )
        logger.info("Migration: CREATE TABLE compatibilitycell")

    if not _table_exists(cursor, "compatibilitypageresult"):
        cursor.execute(
            """
            CREATE TABLE compatibilitypageresult (
                id INTEGER PRIMARY KEY,
                run_id INTEGER NOT NULL REFERENCES compatibilityrun(id),
                cell_id INTEGER NOT NULL REFERENCES compatibilitycell(id),
                page_key VARCHAR DEFAULT '',
                page_name VARCHAR DEFAULT '',
                case_id INTEGER REFERENCES testcase(id),
                status VARCHAR DEFAULT 'PENDING',
                reason VARCHAR,
                required_text VARCHAR,
                baseline_screenshot_path VARCHAR,
                candidate_screenshot_path VARCHAR,
                diff_screenshot_path VARCHAR,
                baseline_xml_path VARCHAR,
                candidate_xml_path VARCHAR,
                baseline_ocr_text VARCHAR,
                candidate_ocr_text VARCHAR,
                baseline_activity VARCHAR,
                candidate_activity VARCHAR,
                metrics JSON,
                created_at TIMESTAMP NOT NULL,
                updated_at TIMESTAMP
            )
            """
        )
        logger.info("Migration: CREATE TABLE compatibilitypageresult")


def _migrate_compatibility_old_package_nullable(cursor) -> None:
    """Allow compatibility runs to use the currently installed app as baseline."""
    if not _table_exists(cursor, "compatibilityrun"):
        return

    cursor.execute("PRAGMA table_info(compatibilityrun)")
    cols = cursor.fetchall()
    old_package_col = next((col for col in cols if col[1] == "old_package_id"), None)
    if old_package_col is None or old_package_col[3] == 0:
        return

    logger.info("Migration: making compatibilityrun.old_package_id nullable")
    cursor.execute("DROP TABLE IF EXISTS compatibilityrun_new")
    cursor.execute(
        """
        CREATE TABLE compatibilityrun_new (
            id INTEGER PRIMARY KEY,
            name VARCHAR NOT NULL,
            page_set_id INTEGER REFERENCES compatpageset(id),
            page_set_name VARCHAR,
            page_set_snapshot JSON,
            old_package_id INTEGER REFERENCES apppackage(id),
            new_package_id INTEGER NOT NULL REFERENCES apppackage(id),
            package_name VARCHAR DEFAULT '',
            mode VARCHAR DEFAULT 'upgrade',
            env_id INTEGER REFERENCES environment(id),
            device_serials TEXT,
            thresholds JSON,
            status VARCHAR DEFAULT 'PENDING',
            total_cells INTEGER DEFAULT 0,
            total_pages INTEGER DEFAULT 0,
            pass_count INTEGER DEFAULT 0,
            warning_count INTEGER DEFAULT 0,
            fail_count INTEGER DEFAULT 0,
            error_message VARCHAR,
            user_id INTEGER REFERENCES user(id),
            executor_name VARCHAR,
            created_at TIMESTAMP NOT NULL,
            started_at TIMESTAMP,
            finished_at TIMESTAMP
        )
        """
    )
    cursor.execute(
        """
        INSERT INTO compatibilityrun_new (
            id, name, page_set_id, page_set_name, page_set_snapshot, old_package_id,
            new_package_id, package_name, mode, env_id, device_serials, thresholds,
            status, total_cells, total_pages, pass_count, warning_count, fail_count,
            error_message, user_id, executor_name, created_at, started_at, finished_at
        )
        SELECT
            id, name, page_set_id,
            (SELECT name FROM compatpageset WHERE compatpageset.id = compatibilityrun.page_set_id),
            COALESCE((SELECT pages FROM compatpageset WHERE compatpageset.id = compatibilityrun.page_set_id), '[]'),
            old_package_id, new_package_id, package_name, mode, env_id, device_serials,
            thresholds, status, total_cells, total_pages, pass_count, warning_count,
            fail_count, error_message, user_id, executor_name, created_at, started_at,
            finished_at
        FROM compatibilityrun
        """
    )
    cursor.execute("DROP TABLE compatibilityrun")
    cursor.execute("ALTER TABLE compatibilityrun_new RENAME TO compatibilityrun")


def _migrate_compatibility_run_page_set_snapshot(cursor) -> None:
    """Store page-set data on each compatibility run and allow deleting source page sets."""
    if not _table_exists(cursor, "compatibilityrun"):
        return

    cursor.execute("PRAGMA table_info(compatibilityrun)")
    cols = cursor.fetchall()
    existing_cols = {col[1] for col in cols}
    page_set_col = next((col for col in cols if col[1] == "page_set_id"), None)
    needs_rebuild = (
        "page_set_name" not in existing_cols
        or "page_set_snapshot" not in existing_cols
        or (page_set_col is not None and page_set_col[3] == 1)
    )
    if not needs_rebuild:
        cursor.execute(
            """
            UPDATE compatibilityrun
            SET
                page_set_name = COALESCE(page_set_name, (
                    SELECT name FROM compatpageset WHERE compatpageset.id = compatibilityrun.page_set_id
                )),
                page_set_snapshot = COALESCE(page_set_snapshot, (
                    SELECT pages FROM compatpageset WHERE compatpageset.id = compatibilityrun.page_set_id
                ), '[]')
            WHERE page_set_id IS NOT NULL
            """
        )
        return

    logger.info("Migration: snapshot compatibilityrun page-set data and make page_set_id nullable")
    page_set_name_expr = (
        "COALESCE(page_set_name, "
        "(SELECT name FROM compatpageset WHERE compatpageset.id = compatibilityrun.page_set_id))"
        if "page_set_name" in existing_cols
        else "(SELECT name FROM compatpageset WHERE compatpageset.id = compatibilityrun.page_set_id)"
    )
    page_set_snapshot_expr = (
        "COALESCE(page_set_snapshot, "
        "(SELECT pages FROM compatpageset WHERE compatpageset.id = compatibilityrun.page_set_id), '[]')"
        if "page_set_snapshot" in existing_cols
        else "COALESCE((SELECT pages FROM compatpageset WHERE compatpageset.id = compatibilityrun.page_set_id), '[]')"
    )

    cursor.execute("DROP TABLE IF EXISTS compatibilityrun_new")
    cursor.execute(
        """
        CREATE TABLE compatibilityrun_new (
            id INTEGER PRIMARY KEY,
            name VARCHAR NOT NULL,
            page_set_id INTEGER REFERENCES compatpageset(id),
            page_set_name VARCHAR,
            page_set_snapshot JSON,
            old_package_id INTEGER REFERENCES apppackage(id),
            new_package_id INTEGER NOT NULL REFERENCES apppackage(id),
            package_name VARCHAR DEFAULT '',
            mode VARCHAR DEFAULT 'upgrade',
            env_id INTEGER REFERENCES environment(id),
            device_serials TEXT,
            thresholds JSON,
            status VARCHAR DEFAULT 'PENDING',
            total_cells INTEGER DEFAULT 0,
            total_pages INTEGER DEFAULT 0,
            pass_count INTEGER DEFAULT 0,
            warning_count INTEGER DEFAULT 0,
            fail_count INTEGER DEFAULT 0,
            error_message VARCHAR,
            user_id INTEGER REFERENCES user(id),
            executor_name VARCHAR,
            created_at TIMESTAMP NOT NULL,
            started_at TIMESTAMP,
            finished_at TIMESTAMP
        )
        """
    )
    cursor.execute(
        f"""
        INSERT INTO compatibilityrun_new (
            id, name, page_set_id, page_set_name, page_set_snapshot, old_package_id,
            new_package_id, package_name, mode, env_id, device_serials, thresholds,
            status, total_cells, total_pages, pass_count, warning_count, fail_count,
            error_message, user_id, executor_name, created_at, started_at, finished_at
        )
        SELECT
            id, name, page_set_id, {page_set_name_expr}, {page_set_snapshot_expr},
            old_package_id, new_package_id, package_name, mode, env_id, device_serials,
            thresholds, status, total_cells, total_pages, pass_count, warning_count,
            fail_count, error_message, user_id, executor_name, created_at, started_at,
            finished_at
        FROM compatibilityrun
        """
    )
    cursor.execute("DROP TABLE compatibilityrun")
    cursor.execute("ALTER TABLE compatibilityrun_new RENAME TO compatibilityrun")


def _run_migrations_with_conn(conn) -> None:
    cursor = conn.cursor()
    _ensure_schema_migration_table(cursor)

    migration_plan = [
        ("20260305_001_add_columns", _migration_add_columns),
        ("20260305_002_backfill_testcasestep_order", _migrate_testcasestep_order_to_step_order),
        ("20260305_003_scheduledtask_scenario_nullable", _migrate_scheduledtask_scenario_id_nullable),
        ("20260312_004_fastbotreport_jank_fields", _migrate_fastbotreport_jank_fields),
        ("20260519_005_testresult_report_display", _migrate_testresult_report_display),
        ("20260616_006_compatibility_tables", _migrate_compatibility_tables),
        ("20260616_007_compatibility_current_baseline", _migrate_compatibility_old_package_nullable),
        ("20260616_008_compatibility_page_set_snapshot", _migrate_compatibility_run_page_set_snapshot),
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


def backfill_legacy_asset_owners(session: Session, admin_user_id: int) -> dict:
    """Assign legacy cases/scenarios without owners to the default admin."""
    from backend.models import TestCase, TestScenario

    if not admin_user_id:
        return {"cases": 0, "scenarios": 0}

    cases = session.exec(
        select(TestCase).where(col(TestCase.user_id).is_(None))
    ).all()
    scenarios = session.exec(
        select(TestScenario).where(col(TestScenario.user_id).is_(None))
    ).all()

    for case in cases:
        case.user_id = admin_user_id
        session.add(case)
    for scenario in scenarios:
        scenario.user_id = admin_user_id
        session.add(scenario)

    if cases or scenarios:
        session.commit()

    return {"cases": len(cases), "scenarios": len(scenarios)}


def get_session():
    with Session(engine) as session:
        yield session
