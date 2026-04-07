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

        rows = self.conn.execute("SELECT version FROM schema_migration ORDER BY version").fetchall()
        self.assertEqual(len(rows), 4)

        # Re-run should be no-op and keep same version records.
        _run_migrations_with_conn(self.conn)
        rows_again = self.conn.execute("SELECT version FROM schema_migration ORDER BY version").fetchall()
        self.assertEqual(rows_again, rows)


if __name__ == "__main__":
    unittest.main()
