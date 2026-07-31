import logging
import os
import re
from pathlib import Path
from sqlalchemy import event
from sqlmodel import Session, SQLModel, col, create_engine, select

from backend.paths import PROJECT_ROOT

logger = logging.getLogger(__name__)

sqlite_file_name = os.getenv("AUTODROID_DB_PATH", "database.db")
sqlite_path = Path(sqlite_file_name).expanduser()
if not sqlite_path.is_absolute():
    sqlite_path = PROJECT_ROOT / sqlite_path
sqlite_path.parent.mkdir(parents=True, exist_ok=True)
sqlite_url = f"sqlite:///{sqlite_path.as_posix()}"

# 并发写等待锁的时长（毫秒）：团队多人同时执行/操作时避免立刻抛 database is locked
SQLITE_BUSY_TIMEOUT_MS = 30_000


def configure_sqlite_engine(target_engine) -> None:
    """为 SQLite engine 附加并发加固 PRAGMA。

    - WAL：允许读写并行，显著降低团队并发下的锁冲突（对文件库持久生效）
    - synchronous=NORMAL：WAL 模式下的推荐档位，安全且更快
    - busy_timeout：写锁被占时等待而非立即失败
    """

    @event.listens_for(target_engine, "connect")
    def _set_sqlite_pragma(dbapi_connection, connection_record):
        cursor = dbapi_connection.cursor()
        try:
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.execute("PRAGMA synchronous=NORMAL")
            cursor.execute(f"PRAGMA busy_timeout={SQLITE_BUSY_TIMEOUT_MS}")
        finally:
            cursor.close()


# check_same_thread=False is needed for SQLite with multiple threads/FastAPI
engine = create_engine(sqlite_url, connect_args={"check_same_thread": False})
configure_sqlite_engine(engine)


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
        ("device", "connection_type", "VARCHAR"),
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
        ("testcasestep", "retry_count", "INTEGER DEFAULT 0"),
        ("testcasestep", "description", "VARCHAR"),
        ("testexecution", "device_serial", "VARCHAR"),
        ("testexecution", "platform", "VARCHAR"),
        ("testresult", "report_display", "JSON"),
        ("compatibilityrun", "compare_mode", "VARCHAR DEFAULT 'version'"),
        ("compatibilityrun", "baseline_device_serial", "VARCHAR"),
        ("compatibilitycell", "is_baseline", "BOOLEAN DEFAULT 0"),
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
                compare_mode VARCHAR DEFAULT 'version',
                baseline_device_serial VARCHAR,
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
                is_baseline BOOLEAN DEFAULT 0,
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


def _migrate_scenario_folders(cursor) -> None:
    """创建场景目录表并为 testscenario 补充 folder_id 外键列。"""
    if not _table_exists(cursor, "scenariofolder"):
        cursor.execute(
            """
            CREATE TABLE scenariofolder (
                id INTEGER PRIMARY KEY,
                name VARCHAR NOT NULL,
                parent_id INTEGER REFERENCES scenariofolder(id),
                created_at TIMESTAMP NOT NULL
            )
            """
        )
        logger.info("Migration: CREATE TABLE scenariofolder")

    if not _table_exists(cursor, "testscenario"):
        logger.warning("Migration skip: table testscenario not found when adding column folder_id")
        return

    cursor.execute("PRAGMA table_info(testscenario)")
    existing_cols = {row[1] for row in cursor.fetchall()}
    if "folder_id" not in existing_cols:
        cursor.execute(
            "ALTER TABLE testscenario ADD COLUMN folder_id INTEGER REFERENCES scenariofolder(id)"
        )
        logger.info("Migration: ALTER TABLE testscenario ADD COLUMN folder_id")


def _migrate_app_package_platform(cursor) -> None:
    """Add and backfill package platform for Android/iOS artifact dispatch."""
    if not _table_exists(cursor, "apppackage"):
        logger.warning("Migration skip: apppackage table not found when adding platform")
        return

    cursor.execute("PRAGMA table_info(apppackage)")
    existing_cols = {row[1] for row in cursor.fetchall()}
    if "platform" not in existing_cols:
        cursor.execute("ALTER TABLE apppackage ADD COLUMN platform VARCHAR DEFAULT 'android'")
        logger.info("Migration: ALTER TABLE apppackage ADD COLUMN platform")
    cursor.execute(
        "UPDATE apppackage SET platform = 'android' WHERE platform IS NULL OR TRIM(platform) = ''"
    )
    cursor.execute(
        "CREATE INDEX IF NOT EXISTS ix_apppackage_platform ON apppackage(platform)"
    )


def _migrate_inspection_schema(cursor) -> None:
    """巡检表由 SQLModel.create_all 创建；迁移负责为旧库补必要索引。

    create_db_and_tables 会先 create_all 再执行版本迁移，因此这里只做幂等索引，
    同时允许迁移单测在缺少新表时安全跳过。
    """
    indexes = [
        ("inspectionrun", "ix_inspectionrun_status", "status"),
        ("inspectionrun", "ix_inspectionrun_device_serial", "device_serial"),
        ("inspectionstate", "ix_inspectionstate_cluster_key", "cluster_key"),
        ("inspectionstate", "ix_inspectionstate_state_key", "state_key"),
        ("inspectiontransition", "ix_inspectiontransition_action_key", "action_key"),
        ("inspectionfault", "ix_inspectionfault_signature", "signature"),
    ]
    for table, index_name, column in indexes:
        if not _table_exists(cursor, table):
            continue
        cursor.execute(
            f"CREATE INDEX IF NOT EXISTS {index_name} ON {table}({column})"
        )


def _migrate_device_execution_lease(cursor) -> None:
    if not _table_exists(cursor, "device"):
        return
    cursor.execute("PRAGMA table_info(device)")
    existing_cols = {row[1] for row in cursor.fetchall()}
    additions = [
        ("lease_task_id", "VARCHAR"),
        ("lease_kind", "VARCHAR"),
        ("lease_acquired_at", "TIMESTAMP"),
    ]
    for column, col_type in additions:
        if column not in existing_cols:
            cursor.execute(f"ALTER TABLE device ADD COLUMN {column} {col_type}")
            logger.info("Migration: ALTER TABLE device ADD COLUMN %s", column)
    cursor.execute(
        "CREATE INDEX IF NOT EXISTS ix_device_lease_task_id ON device(lease_task_id)"
    )
    if _table_exists(cursor, "inspectionprofile"):
        cursor.execute("PRAGMA table_info(inspectionprofile)")
        inspection_cols = {row[1] for row in cursor.fetchall()}
        if "dynamic_text_patterns" not in inspection_cols:
            cursor.execute(
                "ALTER TABLE inspectionprofile "
                "ADD COLUMN dynamic_text_patterns TEXT DEFAULT '[]'"
            )


def _migrate_compatibility_inspection_source(cursor) -> None:
    if not _table_exists(cursor, "compatibilityrun"):
        return
    cursor.execute("PRAGMA table_info(compatibilityrun)")
    existing_cols = {row[1] for row in cursor.fetchall()}
    additions = [
        ("source_type", "VARCHAR DEFAULT 'page_set'"),
        ("inspection_run_id", "INTEGER REFERENCES inspectionrun(id)"),
        ("inspection_state_ids", "TEXT DEFAULT '[]'"),
    ]
    for column, col_type in additions:
        if column not in existing_cols:
            cursor.execute(
                f"ALTER TABLE compatibilityrun ADD COLUMN {column} {col_type}"
            )
    cursor.execute(
        "CREATE INDEX IF NOT EXISTS ix_compatibilityrun_source_type "
        "ON compatibilityrun(source_type)"
    )
    cursor.execute(
        "CREATE INDEX IF NOT EXISTS ix_compatibilityrun_inspection_run_id "
        "ON compatibilityrun(inspection_run_id)"
    )


def _migrate_inspection_thumbnail(cursor) -> None:
    if not _table_exists(cursor, "inspectionstate"):
        return
    cursor.execute("PRAGMA table_info(inspectionstate)")
    existing_cols = {row[1] for row in cursor.fetchall()}
    if "thumbnail_path" not in existing_cols:
        cursor.execute(
            "ALTER TABLE inspectionstate ADD COLUMN thumbnail_path VARCHAR"
        )


def _migrate_device_connection_type(cursor) -> None:
    """补齐后加入 Device 模型、但未被独立版本迁移覆盖的连接类型列。

    已升级过早期 ``_migration_add_columns`` 版本的生产库不会再次执行旧迁移，
    因而仅依赖该通用函数会导致 ORM 查询 Device 时因缺列整体失败。
    """
    if not _table_exists(cursor, "device"):
        return
    cursor.execute("PRAGMA table_info(device)")
    existing_cols = {row[1] for row in cursor.fetchall()}
    if "connection_type" not in existing_cols:
        cursor.execute("ALTER TABLE device ADD COLUMN connection_type VARCHAR")
        logger.info("Migration: ALTER TABLE device ADD COLUMN connection_type")


def _migrate_inspection_transition_relation(cursor) -> None:
    """Add optional transition semantics without reclassifying historical rows."""
    if not _table_exists(cursor, "inspectiontransition"):
        return
    cursor.execute("PRAGMA table_info(inspectiontransition)")
    existing_cols = {row[1] for row in cursor.fetchall()}
    additions = [
        ("relation_type", "VARCHAR"),
        ("relation_confidence", "FLOAT"),
    ]
    for column, col_type in additions:
        if column not in existing_cols:
            cursor.execute(
                f"ALTER TABLE inspectiontransition ADD COLUMN {column} {col_type}"
            )
            logger.info(
                "Migration: ALTER TABLE inspectiontransition ADD COLUMN %s",
                column,
            )


def _add_columns_if_present(cursor, table: str, additions) -> None:
    """Idempotently extend an existing SQLite table without scanning rows."""
    if not _table_exists(cursor, table):
        return
    cursor.execute(f"PRAGMA table_info({table})")
    existing = {row[1] for row in cursor.fetchall()}
    for column, column_type in additions:
        if column in existing:
            continue
        cursor.execute(f"ALTER TABLE {table} ADD COLUMN {column} {column_type}")
        logger.info("Migration: ALTER TABLE %s ADD COLUMN %s", table, column)


def _table_has_columns(cursor, table: str, required) -> bool:
    if not _table_exists(cursor, table):
        return False
    cursor.execute(f"PRAGMA table_info({table})")
    existing = {row[1] for row in cursor.fetchall()}
    return set(required).issubset(existing)


def _unique_index_exists(cursor, table: str, columns) -> bool:
    """Return whether SQLite already enforces exactly this unique key."""
    if not _table_exists(cursor, table):
        return False
    expected = list(columns)
    cursor.execute(f"PRAGMA index_list({table})")
    indexes = cursor.fetchall()
    for index in indexes:
        if not bool(index[2]):
            continue
        index_name = str(index[1]).replace('"', '""')
        cursor.execute(f'PRAGMA index_info("{index_name}")')
        actual = [row[2] for row in cursor.fetchall()]
        if actual == expected:
            return True
    return False


def _rebuild_inspection_state_identity_constraint(cursor) -> None:
    """Replace the legacy table-level semantic key with the v4 instance key.

    SQLite represents ``UNIQUE`` table constraints as auto-indexes, which
    cannot be dropped. Existing databases created by the v2 SQLModel schema
    therefore need a table rebuild; dropping only the later named index would
    leave the old two-column uniqueness rule active.
    """
    legacy_columns = ("branch_run_id", "semantic_key")
    if not _unique_index_exists(cursor, "inspectionstate", legacy_columns):
        return

    cursor.execute(
        "SELECT sql FROM sqlite_master "
        "WHERE type = 'table' AND name = 'inspectionstate'"
    )
    row = cursor.fetchone()
    create_sql = str(row[0] if row else "")
    if not create_sql:
        raise RuntimeError("inspectionstate CREATE TABLE SQL is unavailable")

    identifier = lambda value: (  # noqa: E731 - keeps the DDL pattern legible
        rf'(?:"{value}"|`{value}`|\[{value}\]|{value})'
    )
    constraint_name = r'(?:"[^"]+"|`[^`]+`|\[[^\]]+\]|[A-Za-z_][A-Za-z0-9_]*)'
    legacy_constraint = re.compile(
        rf",\s*(?:CONSTRAINT\s+{constraint_name}\s+)?UNIQUE\s*\(\s*"
        rf"{identifier('branch_run_id')}\s*,\s*"
        rf"{identifier('semantic_key')}\s*\)",
        re.IGNORECASE,
    )
    rebuilt_sql, replacements = legacy_constraint.subn("", create_sql)
    if replacements != 1:
        raise RuntimeError(
            "cannot safely replace legacy inspectionstate uniqueness constraint"
        )

    closing_paren = rebuilt_sql.rfind(")")
    if closing_paren < 0:
        raise RuntimeError("inspectionstate CREATE TABLE SQL is malformed")
    rebuilt_sql = (
        rebuilt_sql[:closing_paren]
        + ", CONSTRAINT uq_inspectionstate_branch_semantic_instance "
        "UNIQUE (branch_run_id, semantic_key, instance_anchor)"
        + rebuilt_sql[closing_paren:]
    )
    opening_paren = rebuilt_sql.find("(")
    if opening_paren < 0:
        raise RuntimeError("inspectionstate CREATE TABLE SQL is malformed")
    rebuilt_sql = (
        "CREATE TABLE inspectionstate_identity_new "
        + rebuilt_sql[opening_paren:]
    )

    cursor.execute(
        "SELECT sql FROM sqlite_master "
        "WHERE type = 'index' AND tbl_name = 'inspectionstate' "
        "AND sql IS NOT NULL "
        "AND name NOT IN (?, ?) ORDER BY name",
        (
            "uq_inspectionstate_branch_semantic",
            "uq_inspectionstate_branch_semantic_instance",
        ),
    )
    index_sql = [str(item[0]) for item in cursor.fetchall()]
    cursor.execute(
        "SELECT sql FROM sqlite_master "
        "WHERE type = 'trigger' AND tbl_name = 'inspectionstate' "
        "AND sql IS NOT NULL ORDER BY name"
    )
    trigger_sql = [str(item[0]) for item in cursor.fetchall()]
    cursor.execute("PRAGMA table_info(inspectionstate)")
    columns = [str(item[1]) for item in cursor.fetchall()]
    quoted_columns = ", ".join(
        f'"{column.replace(chr(34), chr(34) * 2)}"' for column in columns
    )

    cursor.execute(rebuilt_sql)
    cursor.execute(
        f"INSERT INTO inspectionstate_identity_new ({quoted_columns}) "
        f"SELECT {quoted_columns} FROM inspectionstate"
    )
    cursor.execute("DROP TABLE inspectionstate")
    cursor.execute(
        "ALTER TABLE inspectionstate_identity_new RENAME TO inspectionstate"
    )
    for statement in index_sql:
        cursor.execute(statement)
    for statement in trigger_sql:
        cursor.execute(statement)


def _migrate_inspection_identity_observation(cursor) -> None:
    """Add v2 logical identity/observation metadata without touching assets."""
    _add_columns_if_present(
        cursor,
        "inspectionstate",
        [
            ("template_id", "INTEGER REFERENCES inspectionpagetemplate(id)"),
            ("semantic_key", "VARCHAR"),
            ("identity_version", "INTEGER NOT NULL DEFAULT 1"),
            ("representative_observation_id", "INTEGER"),
            ("observation_count", "INTEGER NOT NULL DEFAULT 0"),
            ("last_observed_at", "TIMESTAMP"),
            ("queued_at", "TIMESTAMP"),
            ("expanded_at", "TIMESTAMP"),
        ],
    )
    _add_columns_if_present(
        cursor,
        "inspectiontransition",
        [
            ("topology_type", "VARCHAR"),
            ("source_observation_id", "INTEGER"),
            ("target_observation_id", "INTEGER"),
            ("traversal_count", "INTEGER NOT NULL DEFAULT 1"),
            ("target_was_existing", "BOOLEAN NOT NULL DEFAULT 0"),
        ],
    )
    _add_columns_if_present(
        cursor,
        "inspectionpagetemplate",
        [
            ("activity_family", "VARCHAR"),
            ("page_role", "VARCHAR NOT NULL DEFAULT 'UNKNOWN'"),
            ("is_modal", "BOOLEAN NOT NULL DEFAULT 0"),
            ("structure_signature", "TEXT NOT NULL DEFAULT '[]'"),
            ("action_signature", "TEXT NOT NULL DEFAULT '[]'"),
            ("anchor_signature", "TEXT NOT NULL DEFAULT '[]'"),
            ("control_state_signature", "TEXT NOT NULL DEFAULT '[]'"),
            ("risk_signature", "TEXT NOT NULL DEFAULT '[]'"),
        ],
    )
    _add_columns_if_present(
        cursor,
        "inspectionobservation",
        [("screenshot_phash", "VARCHAR")],
    )
    if _table_has_columns(cursor, "inspectionstate", {"run_id", "template_id"}):
        cursor.execute(
            "CREATE INDEX IF NOT EXISTS ix_inspectionstate_run_template "
            "ON inspectionstate(run_id, template_id)"
        )
    if _table_has_columns(
        cursor,
        "inspectionstate",
        {"branch_run_id", "semantic_key"},
    ):
        cursor.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS uq_inspectionstate_branch_semantic "
            "ON inspectionstate(branch_run_id, semantic_key)"
        )
    if _table_has_columns(
        cursor,
        "inspectiontransition",
        {"run_id", "topology_type"},
    ):
        cursor.execute(
            "CREATE INDEX IF NOT EXISTS ix_inspectiontransition_run_topology "
            "ON inspectiontransition(run_id, topology_type)"
        )


def _migrate_content_addressed_assets(cursor) -> None:
    """Add CAS references and compatibility asset IDs; never hash legacy files."""
    _add_columns_if_present(
        cursor,
        "compatibilityrun",
        [("inspection_observation_ids", "TEXT NOT NULL DEFAULT '[]'")],
    )
    _add_columns_if_present(
        cursor,
        "compatibilitypageresult",
        [
            ("baseline_screenshot_asset_id", "VARCHAR REFERENCES storedasset(id)"),
            ("candidate_screenshot_asset_id", "VARCHAR REFERENCES storedasset(id)"),
            ("diff_screenshot_asset_id", "VARCHAR REFERENCES storedasset(id)"),
            ("baseline_xml_asset_id", "VARCHAR REFERENCES storedasset(id)"),
            ("candidate_xml_asset_id", "VARCHAR REFERENCES storedasset(id)"),
        ],
    )
    _add_columns_if_present(
        cursor,
        "storedasset",
        [
            ("encoding", "VARCHAR"),
            ("scale", "FLOAT NOT NULL DEFAULT 1.0"),
            ("integrity_status", "VARCHAR NOT NULL DEFAULT 'VERIFIED'"),
            ("last_verified_at", "TIMESTAMP"),
            ("orphaned_at", "TIMESTAMP"),
        ],
    )
    _add_columns_if_present(
        cursor,
        "assetreference",
        [
            ("released_at", "TIMESTAMP"),
            ("grace_until", "TIMESTAMP"),
        ],
    )


def _migrate_inspection_exploration_family(cursor) -> None:
    """Add the v4 exploration frontier and family-coverage schema."""
    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS inspectionexplorationfamily (
            id INTEGER PRIMARY KEY,
            run_id INTEGER NOT NULL REFERENCES inspectionrun(id),
            branch_run_id INTEGER NOT NULL REFERENCES inspectionbranchrun(id),
            family_key VARCHAR NOT NULL,
            fingerprint_version INTEGER NOT NULL DEFAULT 1,
            page_role VARCHAR NOT NULL DEFAULT 'UNKNOWN',
            activity_family VARCHAR,
            representative_state_id INTEGER REFERENCES inspectionstate(id),
            signature JSON NOT NULL DEFAULT '{}',
            member_count INTEGER NOT NULL DEFAULT 0,
            created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP,
            CONSTRAINT uq_inspectionfamily_branch_version_key
                UNIQUE (branch_run_id, fingerprint_version, family_key)
        )
        """
    )
    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS inspectionfamilyactioncoverage (
            id INTEGER PRIMARY KEY,
            family_id INTEGER NOT NULL REFERENCES inspectionexplorationfamily(id),
            action_role_key VARCHAR NOT NULL,
            action_role VARCHAR,
            status VARCHAR NOT NULL DEFAULT 'PENDING',
            source_state_id INTEGER REFERENCES inspectionstate(id),
            source_transition_id INTEGER REFERENCES inspectiontransition(id),
            attempt_count INTEGER NOT NULL DEFAULT 0,
            max_attempts INTEGER NOT NULL DEFAULT 2,
            last_error VARCHAR,
            created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP,
            CONSTRAINT uq_inspectionfamilycoverage_family_role
                UNIQUE (family_id, action_role_key)
        )
        """
    )
    _add_columns_if_present(
        cursor,
        "inspectionstate",
        [
            ("instance_anchor", "VARCHAR"),
            (
                "exploration_family_id",
                "INTEGER REFERENCES inspectionexplorationfamily(id)",
            ),
            ("family_match_confidence", "FLOAT"),
            ("family_match_evidence", "TEXT NOT NULL DEFAULT '{}'"),
            ("exploration_mode", "VARCHAR NOT NULL DEFAULT 'INDEPENDENT'"),
            ("expansion_status", "VARCHAR NOT NULL DEFAULT 'DISCOVERED'"),
            ("pending_action_count", "INTEGER NOT NULL DEFAULT 0"),
            ("last_action_cursor", "INTEGER"),
            ("recovery_retry_count", "INTEGER NOT NULL DEFAULT 0"),
            ("expansion_completed_at", "TIMESTAMP"),
        ],
    )
    if _table_has_columns(
        cursor,
        "inspectionstate",
        {"branch_run_id", "semantic_key", "instance_anchor"},
    ):
        cursor.execute("DROP INDEX IF EXISTS uq_inspectionstate_branch_semantic")
        _rebuild_inspection_state_identity_constraint(cursor)
        if not _unique_index_exists(
            cursor,
            "inspectionstate",
            ("branch_run_id", "semantic_key", "instance_anchor"),
        ):
            cursor.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS "
                "uq_inspectionstate_branch_semantic_instance "
                "ON inspectionstate(branch_run_id, semantic_key, instance_anchor)"
            )
    _add_columns_if_present(
        cursor,
        "inspectiontransition",
        [
            ("action_role_key", "VARCHAR"),
            ("action_role", "VARCHAR"),
            ("execution_disposition", "VARCHAR NOT NULL DEFAULT 'EXECUTED'"),
            ("failure_type", "VARCHAR"),
            (
                "coverage_source_transition_id",
                "INTEGER REFERENCES inspectiontransition(id)",
            ),
            ("recovery_attempt_count", "INTEGER NOT NULL DEFAULT 0"),
        ],
    )
    indexes = [
        (
            "inspectionexplorationfamily",
            "ix_inspectionfamily_run_branch",
            "run_id, branch_run_id",
        ),
        (
            "inspectionexplorationfamily",
            "ix_inspectionfamily_family_key",
            "family_key",
        ),
        (
            "inspectionfamilyactioncoverage",
            "ix_inspectionfamilycoverage_family_status",
            "family_id, status",
        ),
        (
            "inspectionstate",
            "ix_inspectionstate_instance_anchor",
            "instance_anchor",
        ),
        (
            "inspectionstate",
            "ix_inspectionstate_exploration_family_id",
            "exploration_family_id",
        ),
        (
            "inspectionstate",
            "ix_inspectionstate_exploration_mode",
            "exploration_mode",
        ),
        (
            "inspectionstate",
            "ix_inspectionstate_expansion_status",
            "expansion_status",
        ),
        (
            "inspectiontransition",
            "ix_inspectiontransition_action_role_key",
            "action_role_key",
        ),
        (
            "inspectiontransition",
            "ix_inspectiontransition_execution_disposition",
            "execution_disposition",
        ),
        (
            "inspectiontransition",
            "ix_inspectiontransition_failure_type",
            "failure_type",
        ),
        (
            "inspectiontransition",
            "ix_inspectiontransition_coverage_source_transition_id",
            "coverage_source_transition_id",
        ),
    ]
    for table, index_name, columns in indexes:
        if not _table_exists(cursor, table):
            continue
        cursor.execute(
            f"CREATE INDEX IF NOT EXISTS {index_name} ON {table}({columns})"
        )


def _migrate_inspection_coverage_scheduler(cursor) -> None:
    """Add v5 coverage contracts and priority-frontier diagnostics."""
    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS inspectioncoveragecontract (
            id INTEGER PRIMARY KEY,
            run_id INTEGER NOT NULL REFERENCES inspectionrun(id),
            branch_run_id INTEGER NOT NULL REFERENCES inspectionbranchrun(id),
            contract_key VARCHAR NOT NULL,
            scope VARCHAR NOT NULL DEFAULT 'FAMILY_ACTION',
            source_family_id INTEGER REFERENCES inspectionexplorationfamily(id),
            source_page_subtype VARCHAR NOT NULL DEFAULT 'UNKNOWN',
            action_group_key VARCHAR NOT NULL,
            action_role VARCHAR,
            target_family_id INTEGER REFERENCES inspectionexplorationfamily(id),
            target_page_role VARCHAR,
            status VARCHAR NOT NULL DEFAULT 'PENDING',
            required_samples INTEGER NOT NULL DEFAULT 2,
            success_count INTEGER NOT NULL DEFAULT 0,
            failure_count INTEGER NOT NULL DEFAULT 0,
            source_instance_anchors JSON NOT NULL DEFAULT '[]',
            sample_transition_ids JSON NOT NULL DEFAULT '[]',
            risk_signature VARCHAR,
            control_signature VARCHAR,
            last_error VARCHAR,
            created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP,
            CONSTRAINT uq_inspectioncoveragecontract_branch_key
                UNIQUE (branch_run_id, contract_key)
        )
        """
    )
    _add_columns_if_present(
        cursor,
        "inspectionstate",
        [
            ("page_subtype", "VARCHAR NOT NULL DEFAULT 'UNKNOWN'"),
            ("coverage_status", "VARCHAR NOT NULL DEFAULT 'DISCOVERED'"),
            ("frontier_priority", "INTEGER NOT NULL DEFAULT 700"),
            ("frontier_reason", "VARCHAR"),
        ],
    )
    _add_columns_if_present(
        cursor,
        "inspectiontransition",
        [
            (
                "coverage_contract_id",
                "INTEGER REFERENCES inspectioncoveragecontract(id)",
            ),
            ("action_group_key", "VARCHAR"),
            ("sampling_disposition", "VARCHAR"),
            ("visual_locator_evidence", "JSON NOT NULL DEFAULT '{}'"),
        ],
    )
    indexes = [
        (
            "inspectioncoveragecontract",
            "ix_inspectioncoveragecontract_run_status",
            "run_id, status",
        ),
        (
            "inspectioncoveragecontract",
            "ix_inspectioncoveragecontract_source_action",
            "source_family_id, action_group_key",
        ),
        (
            "inspectionstate",
            "ix_inspectionstate_page_subtype",
            "page_subtype",
        ),
        (
            "inspectionstate",
            "ix_inspectionstate_coverage_status",
            "coverage_status",
        ),
        (
            "inspectionstate",
            "ix_inspectionstate_frontier_priority",
            "frontier_priority",
        ),
        (
            "inspectiontransition",
            "ix_inspectiontransition_coverage_contract_id",
            "coverage_contract_id",
        ),
        (
            "inspectiontransition",
            "ix_inspectiontransition_action_group_key",
            "action_group_key",
        ),
        (
            "inspectiontransition",
            "ix_inspectiontransition_sampling_disposition",
            "sampling_disposition",
        ),
    ]
    for table, index_name, columns in indexes:
        if _table_exists(cursor, table):
            cursor.execute(
                f"CREATE INDEX IF NOT EXISTS {index_name} ON {table}({columns})"
            )


def _migrate_inspection_exploration_backfill(cursor) -> None:
    """Classify legacy frontier and transition outcomes without rewriting v3 fields."""
    if _table_has_columns(
        cursor,
        "inspectionstate",
        {
            "queued_at",
            "expanded_at",
            "expansion_status",
            "expansion_completed_at",
        },
    ):
        cursor.execute(
            """
            UPDATE inspectionstate
            SET expansion_status = CASE
                    WHEN expanded_at IS NOT NULL THEN 'EXPANDED'
                    WHEN queued_at IS NOT NULL THEN 'DEFERRED'
                    ELSE 'DISCOVERED'
                END,
                expansion_completed_at = CASE
                    WHEN expanded_at IS NOT NULL
                        THEN COALESCE(expansion_completed_at, expanded_at)
                    ELSE expansion_completed_at
                END
            """
        )
    if _table_has_columns(
        cursor,
        "inspectionstate",
        {"last_action_cursor", "id"},
    ) and _table_has_columns(
        cursor,
        "inspectiontransition",
        {"from_state_id", "sequence"},
    ):
        cursor.execute(
            """
            UPDATE inspectionstate
            SET last_action_cursor = (
                SELECT MAX(inspectiontransition.sequence)
                FROM inspectiontransition
                WHERE inspectiontransition.from_state_id = inspectionstate.id
            )
            WHERE EXISTS (
                SELECT 1
                FROM inspectiontransition
                WHERE inspectiontransition.from_state_id = inspectionstate.id
            )
            """
        )

    required = {"status", "execution_disposition", "failure_type"}
    if not _table_has_columns(cursor, "inspectiontransition", required):
        return
    cursor.execute("PRAGMA table_info(inspectiontransition)")
    columns = {row[1] for row in cursor.fetchall()}
    reason = "COALESCE(reason, '')" if "reason" in columns else "''"
    cursor.execute(
        f"""
        UPDATE inspectiontransition
        SET execution_disposition = CASE
                WHEN status = 'LOCATOR_DRIFT'
                     AND {reason} LIKE '%页面像素已变化%'
                    THEN 'SKIPPED'
                WHEN status = 'LOCATOR_DRIFT' THEN 'FAILED'
                WHEN status IN ('UNSTABLE_PARENT', 'PATH_DIVERGED', 'NOT_REACHED')
                    THEN 'NOT_REACHED'
                WHEN status IN (
                    'BLOCKED', 'COORDINATE_ONLY', 'AMBIGUOUS', 'SKIPPED',
                    'VARIANT_LIMIT', 'BUDGET_LIMIT'
                ) THEN 'SKIPPED'
                WHEN status IN (
                    'ERROR', 'ACTION_ERROR', 'APP_EXIT', 'EXTERNAL_APP'
                ) THEN 'FAILED'
                ELSE COALESCE(NULLIF(execution_disposition, ''), 'EXECUTED')
            END,
            failure_type = CASE
                WHEN failure_type IS NOT NULL AND failure_type != '' THEN failure_type
                WHEN status = 'LOCATOR_DRIFT'
                     AND {reason} LIKE '%页面像素已变化%'
                    THEN 'COORDINATE_STALE'
                WHEN status = 'LOCATOR_DRIFT' THEN 'LOCATOR_NOT_FOUND'
                WHEN status = 'PATH_DIVERGED' THEN 'PATH_DIVERGED'
                WHEN status = 'UNSTABLE_PARENT'
                     AND {reason} LIKE '%本动作组不再重复%'
                    THEN 'PARENT_RECOVERY_CASCADE'
                WHEN status = 'UNSTABLE_PARENT' THEN 'PARENT_RECOVERY_FAILED'
                WHEN status IN ('ERROR', 'ACTION_ERROR') THEN 'ACTION_ERROR'
                WHEN status IN ('APP_EXIT', 'EXTERNAL_APP') THEN status
                ELSE NULL
            END
        """
    )


def _migrate_inspection_frontier_state(cursor) -> None:
    """Install and classify the v4 state frontier before family finalization."""
    # The state schema contains an optional family FK. SQLite permits the
    # referenced table to be created in the same migration sequence, so keep
    # the existing idempotent schema installer intact and finalize the family
    # version separately below.
    _migrate_inspection_exploration_family(cursor)
    _migrate_inspection_exploration_backfill(cursor)


def _migrate_compatibility_installed_replay(cursor) -> None:
    """Add the lightweight installed-version replay contract.

    ``new_package_id`` was historically required by every compatibility run.
    Installed replay deliberately has no uploaded APK, so SQLite needs a
    bounded table rebuild to remove that single NOT NULL constraint. The
    rebuild preserves every column and explicit index already present; no
    report rows or assets are scanned.
    """
    if not _table_exists(cursor, "compatibilityrun"):
        return

    _add_columns_if_present(
        cursor,
        "compatibilityrun",
        [
            ("execution_mode", "VARCHAR NOT NULL DEFAULT 'COMPARISON'"),
            ("replay_branch_key", "VARCHAR"),
            ("replay_plan_version", "INTEGER"),
            ("replay_plan_digest", "VARCHAR"),
            ("replay_duration_seconds", "INTEGER NOT NULL DEFAULT 3600"),
            ("source_package_snapshot", "JSON NOT NULL DEFAULT '{}'"),
            ("target_package_snapshot", "JSON NOT NULL DEFAULT '{}'"),
            ("manual_install_confirmed_at", "TIMESTAMP"),
        ],
    )
    _add_columns_if_present(
        cursor,
        "compatibilitycell",
        [
            ("preflight_at", "TIMESTAMP"),
            ("installed_package_snapshot", "JSON NOT NULL DEFAULT '{}'"),
        ],
    )
    _add_columns_if_present(
        cursor,
        "compatibilitypageresult",
        [
            ("path_key", "VARCHAR"),
            ("source_state_id", "INTEGER REFERENCES inspectionstate(id)"),
            (
                "source_observation_id",
                "INTEGER REFERENCES inspectionobservation(id)",
            ),
            ("evidence_level", "VARCHAR"),
            ("failure_type", "VARCHAR"),
            ("failed_step_index", "INTEGER"),
            ("replay_trace", "JSON NOT NULL DEFAULT '[]'"),
        ],
    )

    cursor.execute("PRAGMA table_info(compatibilityrun)")
    run_columns = cursor.fetchall()
    new_package_column = next(
        (column for column in run_columns if column[1] == "new_package_id"),
        None,
    )
    if new_package_column is not None and bool(new_package_column[3]):
        create_row = cursor.execute(
            "SELECT sql FROM sqlite_master "
            "WHERE type = 'table' AND name = 'compatibilityrun'"
        ).fetchone()
        create_sql = str(create_row[0] if create_row else "")
        nullable_sql = re.sub(
            r"(\bnew_package_id\b\s+[A-Z0-9_()]+)\s+NOT\s+NULL\b",
            r"\1",
            create_sql,
            count=1,
            flags=re.IGNORECASE,
        )
        if nullable_sql == create_sql:
            raise RuntimeError(
                "Migration could not make compatibilityrun.new_package_id nullable"
            )
        replacement_sql = nullable_sql.replace(
            "CREATE TABLE compatibilityrun",
            "CREATE TABLE compatibilityrun_new",
            1,
        )
        if replacement_sql == nullable_sql:
            replacement_sql = re.sub(
                r"^CREATE TABLE\s+[\"`\[]?compatibilityrun[\"`\]]?",
                "CREATE TABLE compatibilityrun_new",
                nullable_sql,
                count=1,
                flags=re.IGNORECASE,
            )
        index_sql = [
            str(row[0])
            for row in cursor.execute(
                "SELECT sql FROM sqlite_master "
                "WHERE type = 'index' AND tbl_name = 'compatibilityrun' "
                "AND sql IS NOT NULL"
            ).fetchall()
            if row[0]
        ]
        quoted_columns = ", ".join(
            f'"{str(column[1]).replace(chr(34), chr(34) * 2)}"'
            for column in run_columns
        )
        cursor.execute("DROP TABLE IF EXISTS compatibilityrun_new")
        cursor.execute(replacement_sql)
        cursor.execute(
            f"INSERT INTO compatibilityrun_new ({quoted_columns}) "
            f"SELECT {quoted_columns} FROM compatibilityrun"
        )
        cursor.execute("DROP TABLE compatibilityrun")
        cursor.execute(
            "ALTER TABLE compatibilityrun_new RENAME TO compatibilityrun"
        )
        for statement in index_sql:
            cursor.execute(statement)

    cursor.execute(
        "CREATE INDEX IF NOT EXISTS ix_compatibilityrun_execution_mode "
        "ON compatibilityrun(execution_mode)"
    )
    cursor.execute(
        "CREATE INDEX IF NOT EXISTS ix_compatibilityrun_replay_branch_key "
        "ON compatibilityrun(replay_branch_key)"
    )
    cursor.execute(
        "CREATE INDEX IF NOT EXISTS ix_compatibilityrun_replay_plan_digest "
        "ON compatibilityrun(replay_plan_digest)"
    )
    if _table_exists(cursor, "compatibilitypageresult"):
        cursor.execute(
            "CREATE INDEX IF NOT EXISTS ix_compatibilitypageresult_path_key "
            "ON compatibilitypageresult(path_key)"
        )
        cursor.execute(
            "CREATE INDEX IF NOT EXISTS ix_compatibilitypageresult_failure_type "
            "ON compatibilitypageresult(failure_type)"
        )


def _migrate_inspection_business_coverage_v2(cursor) -> None:
    """Persist immutable manifests separately from execution health."""
    _add_columns_if_present(
        cursor,
        "inspectionrun",
        [
            ("coverage_manifest_id", "VARCHAR"),
            ("coverage_manifest_version", "VARCHAR"),
            ("coverage_manifest_hash", "VARCHAR"),
            ("coverage_manifest_snapshot", "JSON NOT NULL DEFAULT '{}'"),
            ("coverage_assessment", "JSON NOT NULL DEFAULT '{}'"),
            (
                "coverage_verdict",
                "VARCHAR NOT NULL DEFAULT 'NOT_EVALUATED'",
            ),
            ("coverage_evaluated_at", "TIMESTAMP"),
        ],
    )
    _add_columns_if_present(
        cursor,
        "compatibilityrun",
        [("source_coverage_snapshot", "JSON NOT NULL DEFAULT '{}'")],
    )
    if not _table_exists(cursor, "inspectionrun"):
        return
    cursor.execute(
        "CREATE INDEX IF NOT EXISTS ix_inspectionrun_coverage_manifest_id "
        "ON inspectionrun(coverage_manifest_id)"
    )
    cursor.execute(
        "CREATE INDEX IF NOT EXISTS ix_inspectionrun_coverage_manifest_hash "
        "ON inspectionrun(coverage_manifest_hash)"
    )
    cursor.execute(
        "CREATE INDEX IF NOT EXISTS ix_inspectionrun_coverage_verdict "
        "ON inspectionrun(coverage_verdict)"
    )


def _migrate_inspection_app_map(cursor) -> None:
    """Add the cross-run surface identity columns and app-map indexes.

    The two app-map tables themselves are created by ``SQLModel.create_all``,
    which runs before the migration plan; this only backfills the new
    ``inspectionstate`` columns on existing databases and adds indexes.
    """
    _add_columns_if_present(
        cursor,
        "inspectionstate",
        [
            ("surface_key", "VARCHAR"),
            ("surface_fingerprint_version", "INTEGER NOT NULL DEFAULT 1"),
        ],
    )
    if _table_exists(cursor, "inspectionstate"):
        cursor.execute(
            "CREATE INDEX IF NOT EXISTS ix_inspectionstate_surface_key "
            "ON inspectionstate(surface_key)"
        )
        cursor.execute(
            "CREATE INDEX IF NOT EXISTS ix_inspectionstate_run_surface "
            "ON inspectionstate(run_id, surface_key)"
        )
    if _table_exists(cursor, "inspectionappsurface"):
        cursor.execute(
            "CREATE INDEX IF NOT EXISTS ix_inspectionappsurface_package_subtype "
            "ON inspectionappsurface(package_name, page_subtype)"
        )
    if _table_exists(cursor, "inspectionappaction"):
        cursor.execute(
            "CREATE INDEX IF NOT EXISTS ix_inspectionappaction_package_covered "
            "ON inspectionappaction(package_name, last_covered_at)"
        )


def _adopt_inspection_migration_aliases(cursor) -> None:
    """Map unreleased development migration names to their formal versions."""
    aliases = (
        (
            "20260721_022_inspection_exploration_backfill",
            "20260721_021_inspection_frontier_state",
        ),
        (
            "20260721_021_inspection_exploration_family",
            "20260721_022_inspection_exploration_family",
        ),
    )
    for development_version, formal_version in aliases:
        if not _is_migration_applied(cursor, development_version):
            continue
        if _is_migration_applied(cursor, formal_version):
            continue
        _mark_migration_applied(cursor, formal_version)
        logger.info(
            "Adopted migration alias: %s -> %s",
            development_version,
            formal_version,
        )


def _run_migrations_with_conn(conn) -> None:
    cursor = conn.cursor()
    cursor.execute("PRAGMA foreign_keys")
    restore_foreign_keys = bool(cursor.fetchone()[0])
    if restore_foreign_keys:
        # SQLite cannot rebuild a referenced table while FK enforcement is on.
        # This must happen outside a transaction; the migration runner owns and
        # commits the supplied connection already.
        if getattr(conn, "in_transaction", False):
            conn.commit()
        cursor.execute("PRAGMA foreign_keys = OFF")

    try:
        _ensure_schema_migration_table(cursor)
        _adopt_inspection_migration_aliases(cursor)

        migration_plan = [
            ("20260305_001_add_columns", _migration_add_columns),
            ("20260305_002_backfill_testcasestep_order", _migrate_testcasestep_order_to_step_order),
            ("20260305_003_scheduledtask_scenario_nullable", _migrate_scheduledtask_scenario_id_nullable),
            ("20260312_004_fastbotreport_jank_fields", _migrate_fastbotreport_jank_fields),
            ("20260519_005_testresult_report_display", _migrate_testresult_report_display),
            ("20260616_006_compatibility_tables", _migrate_compatibility_tables),
            ("20260616_007_compatibility_current_baseline", _migrate_compatibility_old_package_nullable),
            ("20260616_008_compatibility_page_set_snapshot", _migrate_compatibility_run_page_set_snapshot),
            ("20260710_009_scenario_folders", _migrate_scenario_folders),
            # 复用 _migration_add_columns（幂等按列检查），为已应用过 001 的库补 retry_count 列
            ("20260711_010_testcasestep_retry_count", _migration_add_columns),
            # 复用 _migration_add_columns，为已有库补兼容性机型对比列（compare_mode/baseline_device_serial/is_baseline）
            ("20260713_011_compatibility_compare_mode", _migration_add_columns),
            ("20260716_012_apppackage_platform", _migrate_app_package_platform),
            ("20260717_013_model_inspection", _migrate_inspection_schema),
            ("20260717_014_device_execution_lease", _migrate_device_execution_lease),
            (
                "20260717_015_compatibility_inspection_source",
                _migrate_compatibility_inspection_source,
            ),
            ("20260717_016_inspection_thumbnail", _migrate_inspection_thumbnail),
            (
                "20260720_017_device_connection_type",
                _migrate_device_connection_type,
            ),
            (
                "20260720_018_inspection_transition_relation",
                _migrate_inspection_transition_relation,
            ),
            (
                "20260720_019_inspection_identity_observation",
                _migrate_inspection_identity_observation,
            ),
            (
                "20260720_020_content_addressed_assets",
                _migrate_content_addressed_assets,
            ),
            (
                "20260721_021_inspection_frontier_state",
                _migrate_inspection_frontier_state,
            ),
            (
                "20260721_022_inspection_exploration_family",
                _migrate_inspection_exploration_family,
            ),
            (
                "20260721_023_inspection_coverage_scheduler",
                _migrate_inspection_coverage_scheduler,
            ),
            (
                "20260722_024_compatibility_installed_replay",
                _migrate_compatibility_installed_replay,
            ),
            (
                "20260723_025_inspection_business_coverage_v2",
                _migrate_inspection_business_coverage_v2,
            ),
            (
                "20260730_026_inspection_app_map",
                _migrate_inspection_app_map,
            ),
        ]

        for version, migration_func in migration_plan:
            if _is_migration_applied(cursor, version):
                continue
            logger.info("Applying migration: %s", version)
            migration_func(cursor)
            _mark_migration_applied(cursor, version)
            logger.info("Applied migration: %s", version)

        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        if restore_foreign_keys:
            cursor.execute("PRAGMA foreign_keys = ON")


def _run_migrations():
    import sqlite3

    conn = sqlite3.connect(str(sqlite_path))
    try:
        _run_migrations_with_conn(conn)
    finally:
        conn.close()


def backfill_case_steps_to_standard(session: Session, *, force: bool = False) -> dict:
    """Idempotent backfill: convert legacy TestCase.steps JSON into TestCaseStep rows.

    - 默认（force=False）：仅处理"无标准步骤记录且存在 legacy 步骤"的用例；
      已有标准步骤或无 legacy 步骤的用例跳过，可安全地在每次启动时执行。
    - force=True（CLI 使用）：替换所有用例的标准步骤。
    - 单个用例转换失败仅记录并跳过，不阻塞其他用例/启动流程。
    """
    from backend.models import TestCase, TestCaseStep
    from backend.step_contract import build_standard_from_legacy_steps

    migrated_cases = 0
    skipped_cases = 0
    failed_cases = 0
    created_steps = 0

    case_ids = session.exec(select(TestCase.id)).all()
    for case_id in case_ids:
        try:
            case = session.get(TestCase, case_id)
            if not case:
                skipped_cases += 1
                continue

            legacy_steps = list(case.steps or [])
            existing = session.exec(
                select(TestCaseStep).where(TestCaseStep.case_id == case.id)
            ).all()

            if existing and not force:
                skipped_cases += 1
                continue
            if not existing and not legacy_steps:
                skipped_cases += 1
                continue

            if existing and force:
                for row in existing:
                    session.delete(row)
                session.flush()

            payload = build_standard_from_legacy_steps(legacy_steps, case_id=case.id)
            for item in payload:
                session.add(
                    TestCaseStep(
                        case_id=case.id,
                        order=item["order"],
                        action=item["action"],
                        args=item.get("args") or {},
                        value=item.get("value"),
                        execute_on=item.get("execute_on") or ["android", "ios"],
                        platform_overrides=item.get("platform_overrides") or {},
                        timeout=item.get("timeout", 10),
                        error_strategy=item.get("error_strategy", "ABORT"),
                        retry_count=item.get("retry_count", 0) or 0,
                        description=item.get("description"),
                    )
                )
            session.commit()
            migrated_cases += 1
            created_steps += len(payload)
        except Exception as exc:
            session.rollback()
            failed_cases += 1
            logger.warning(
                "case step backfill failed, skipped: case_id=%s error=%s", case_id, exc
            )

    summary = {
        "migrated_cases": migrated_cases,
        "skipped_cases": skipped_cases,
        "failed_cases": failed_cases,
        "created_steps": created_steps,
        "force": force,
    }
    logger.info("case step backfill finished: %s", summary)
    return summary


def create_db_and_tables():
    SQLModel.metadata.create_all(engine)
    _run_migrations()
    try:
        with Session(engine) as session:
            backfill_case_steps_to_standard(session)
    except Exception:
        logger.exception("startup case step backfill failed")


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
