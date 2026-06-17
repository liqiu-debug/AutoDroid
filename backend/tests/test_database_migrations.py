import os
import sqlite3
import tempfile
import unittest

from backend.database import _run_migrations_with_conn


class DatabaseMigrationsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp.close()
        self.conn = sqlite3.connect(self.tmp.name)
        self._create_legacy_schema(self.conn)

    def tearDown(self) -> None:
        try:
            self.conn.close()
        finally:
            if os.path.exists(self.tmp.name):
                os.remove(self.tmp.name)

    def _create_legacy_schema(self, conn) -> None:
        cursor = conn.cursor()
        cursor.executescript(
            """
            CREATE TABLE casefolder (
                id INTEGER PRIMARY KEY
            );

            CREATE TABLE user (
                id INTEGER PRIMARY KEY
            );

            CREATE TABLE testcase (
                id INTEGER PRIMARY KEY
            );

            CREATE TABLE device (
                id INTEGER PRIMARY KEY
            );

            CREATE TABLE testscenario (
                id INTEGER PRIMARY KEY
            );

            CREATE TABLE testcasestep (
                id INTEGER PRIMARY KEY,
                "order" INTEGER
            );

            CREATE TABLE testexecution (
                id INTEGER PRIMARY KEY
            );

            CREATE TABLE testresult (
                id INTEGER PRIMARY KEY
            );

            CREATE TABLE fastbotreport (
                id INTEGER PRIMARY KEY,
                task_id INTEGER,
                performance_data TEXT,
                crash_events TEXT,
                summary TEXT,
                created_at TIMESTAMP
            );

            CREATE TABLE scheduledtask (
                id INTEGER PRIMARY KEY,
                name VARCHAR NOT NULL,
                scenario_id INTEGER NOT NULL REFERENCES testscenario(id),
                device_serial VARCHAR,
                strategy VARCHAR NOT NULL,
                strategy_config VARCHAR,
                is_active BOOLEAN NOT NULL DEFAULT 1,
                enable_notification BOOLEAN NOT NULL DEFAULT 1,
                next_run_time TIMESTAMP,
                user_id INTEGER REFERENCES user(id),
                created_at TIMESTAMP NOT NULL,
                updated_at TIMESTAMP
            );
            """
        )
        cursor.execute('INSERT INTO testcasestep(id, "order") VALUES (1, 7)')
        conn.commit()

    def _table_columns(self, table: str):
        cursor = self.conn.cursor()
        cursor.execute(f"PRAGMA table_info({table})")
        return cursor.fetchall()

    def test_migrations_are_versioned_and_idempotent(self):
        _run_migrations_with_conn(self.conn)

        testcase_cols = {c[1] for c in self._table_columns("testcase")}
        self.assertIn("folder_id", testcase_cols)

        testexecution_cols = {c[1] for c in self._table_columns("testexecution")}
        self.assertIn("device_serial", testexecution_cols)
        self.assertIn("platform", testexecution_cols)

        testresult_cols = {c[1] for c in self._table_columns("testresult")}
        self.assertIn("report_display", testresult_cols)

        fastbotreport_cols = {c[1] for c in self._table_columns("fastbotreport")}
        self.assertIn("jank_data", fastbotreport_cols)
        self.assertIn("jank_events", fastbotreport_cols)
        self.assertIn("trace_artifacts", fastbotreport_cols)

        testcasestep_cols = {c[1] for c in self._table_columns("testcasestep")}
        self.assertIn("step_order", testcasestep_cols)
        step_order = self.conn.execute("SELECT step_order FROM testcasestep WHERE id = 1").fetchone()[0]
        self.assertEqual(step_order, 7)

        scheduled_cols = self._table_columns("scheduledtask")
        scenario_col = next(c for c in scheduled_cols if c[1] == "scenario_id")
        self.assertEqual(scenario_col[3], 0)  # notnull flag

        compat_tables = {
            row[0]
            for row in self.conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'compat%'"
            ).fetchall()
        }
        self.assertIn("compatpageset", compat_tables)
        self.assertIn("compatibilityrun", compat_tables)
        self.assertIn("compatibilitycell", compat_tables)
        self.assertIn("compatibilitypageresult", compat_tables)

        compatibilityrun_cols = self._table_columns("compatibilityrun")
        old_package_col = next(c for c in compatibilityrun_cols if c[1] == "old_package_id")
        self.assertEqual(old_package_col[3], 0)  # current installed baseline can leave it null
        page_set_col = next(c for c in compatibilityrun_cols if c[1] == "page_set_id")
        self.assertEqual(page_set_col[3], 0)  # page set can be deleted after run snapshot is stored
        compatibilityrun_col_names = {c[1] for c in compatibilityrun_cols}
        self.assertIn("page_set_name", compatibilityrun_col_names)
        self.assertIn("page_set_snapshot", compatibilityrun_col_names)

        rows = self.conn.execute("SELECT version FROM schema_migration ORDER BY version").fetchall()
        self.assertEqual(len(rows), 8)

        # Re-run should be no-op and keep same version records.
        _run_migrations_with_conn(self.conn)
        rows_again = self.conn.execute("SELECT version FROM schema_migration ORDER BY version").fetchall()
        self.assertEqual(rows_again, rows)

    def test_compatibility_page_set_snapshot_migration_backfills_existing_runs(self):
        tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        tmp.close()
        conn = sqlite3.connect(tmp.name)
        try:
            cursor = conn.cursor()
            cursor.executescript(
                """
                CREATE TABLE schema_migration (
                    version VARCHAR PRIMARY KEY,
                    applied_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                INSERT INTO schema_migration(version) VALUES
                    ('20260305_001_add_columns'),
                    ('20260305_002_backfill_testcasestep_order'),
                    ('20260305_003_scheduledtask_scenario_nullable'),
                    ('20260312_004_fastbotreport_jank_fields'),
                    ('20260519_005_testresult_report_display'),
                    ('20260616_006_compatibility_tables'),
                    ('20260616_007_compatibility_current_baseline');
                CREATE TABLE compatpageset (
                    id INTEGER PRIMARY KEY,
                    name VARCHAR NOT NULL,
                    description VARCHAR,
                    pages JSON,
                    user_id INTEGER,
                    updater_id INTEGER,
                    created_at TIMESTAMP NOT NULL,
                    updated_at TIMESTAMP
                );
                CREATE TABLE compatibilityrun (
                    id INTEGER PRIMARY KEY,
                    name VARCHAR NOT NULL,
                    page_set_id INTEGER NOT NULL REFERENCES compatpageset(id),
                    old_package_id INTEGER,
                    new_package_id INTEGER NOT NULL,
                    package_name VARCHAR DEFAULT '',
                    mode VARCHAR DEFAULT 'upgrade',
                    env_id INTEGER,
                    device_serials TEXT,
                    thresholds JSON,
                    status VARCHAR DEFAULT 'PENDING',
                    total_cells INTEGER DEFAULT 0,
                    total_pages INTEGER DEFAULT 0,
                    pass_count INTEGER DEFAULT 0,
                    warning_count INTEGER DEFAULT 0,
                    fail_count INTEGER DEFAULT 0,
                    error_message VARCHAR,
                    user_id INTEGER,
                    executor_name VARCHAR,
                    created_at TIMESTAMP NOT NULL,
                    started_at TIMESTAMP,
                    finished_at TIMESTAMP
                );
                """
            )
            cursor.execute(
                """
                INSERT INTO compatpageset(id, name, pages, created_at)
                VALUES (1, 'main pages', '[{"name":"Home","case_id":9,"settle_seconds":0}]', CURRENT_TIMESTAMP)
                """
            )
            cursor.execute(
                """
                INSERT INTO compatibilityrun(
                    id, name, page_set_id, new_package_id, package_name, created_at
                )
                VALUES (1, 'compat', 1, 2, 'com.demo.app', CURRENT_TIMESTAMP)
                """
            )
            conn.commit()

            _run_migrations_with_conn(conn)

            columns = conn.execute("PRAGMA table_info(compatibilityrun)").fetchall()
            page_set_col = next(c for c in columns if c[1] == "page_set_id")
            self.assertEqual(page_set_col[3], 0)
            col_names = {c[1] for c in columns}
            self.assertIn("page_set_name", col_names)
            self.assertIn("page_set_snapshot", col_names)
            row = conn.execute(
                "SELECT page_set_name, page_set_snapshot FROM compatibilityrun WHERE id = 1"
            ).fetchone()
            self.assertEqual(row[0], "main pages")
            self.assertIn('"Home"', row[1])
        finally:
            conn.close()
            if os.path.exists(tmp.name):
                os.remove(tmp.name)


if __name__ == "__main__":
    unittest.main()
