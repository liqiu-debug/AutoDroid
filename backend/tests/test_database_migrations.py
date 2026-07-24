import os
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from sqlalchemy import create_engine
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, SQLModel, select

from backend import models as backend_models
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

            CREATE TABLE apppackage (
                id INTEGER PRIMARY KEY,
                app_name VARCHAR,
                package_name VARCHAR,
                file_path VARCHAR
            );
            INSERT INTO apppackage(id, app_name, package_name, file_path)
            VALUES (1, 'Legacy', 'com.example.legacy', 'uploads/apps/legacy.apk');

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

    @staticmethod
    def _unique_index_columns(conn, table: str):
        result = []
        for row in conn.execute(f"PRAGMA index_list({table})").fetchall():
            if not bool(row[2]):
                continue
            columns = tuple(
                item[2]
                for item in conn.execute(
                    f'PRAGMA index_info("{str(row[1]).replace(chr(34), chr(34) * 2)}")'
                ).fetchall()
            )
            result.append(columns)
        return result

    def test_migrations_are_versioned_and_idempotent(self):
        _run_migrations_with_conn(self.conn)

        testcase_cols = {c[1] for c in self._table_columns("testcase")}
        self.assertIn("folder_id", testcase_cols)

        testexecution_cols = {c[1] for c in self._table_columns("testexecution")}
        self.assertIn("device_serial", testexecution_cols)
        self.assertIn("platform", testexecution_cols)

        testresult_cols = {c[1] for c in self._table_columns("testresult")}
        self.assertIn("report_display", testresult_cols)

        apppackage_cols = {c[1] for c in self._table_columns("apppackage")}
        self.assertIn("platform", apppackage_cols)
        package_platform = self.conn.execute(
            "SELECT platform FROM apppackage WHERE id = 1"
        ).fetchone()[0]
        self.assertEqual(package_platform, "android")

        fastbotreport_cols = {c[1] for c in self._table_columns("fastbotreport")}
        self.assertIn("jank_data", fastbotreport_cols)
        self.assertIn("jank_events", fastbotreport_cols)
        self.assertIn("trace_artifacts", fastbotreport_cols)

        testcasestep_cols = {c[1] for c in self._table_columns("testcasestep")}
        self.assertIn("step_order", testcasestep_cols)
        self.assertIn("retry_count", testcasestep_cols)
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
        self.assertIn("compare_mode", compatibilityrun_col_names)
        self.assertIn("baseline_device_serial", compatibilityrun_col_names)
        self.assertIn("source_type", compatibilityrun_col_names)
        self.assertIn("inspection_run_id", compatibilityrun_col_names)
        self.assertIn("inspection_state_ids", compatibilityrun_col_names)
        self.assertIn("inspection_observation_ids", compatibilityrun_col_names)
        compatibilitycell_col_names = {c[1] for c in self._table_columns("compatibilitycell")}
        self.assertIn("is_baseline", compatibilitycell_col_names)
        compatibility_result_cols = {
            c[1] for c in self._table_columns("compatibilitypageresult")
        }
        self.assertTrue(
            {
                "baseline_screenshot_asset_id",
                "candidate_screenshot_asset_id",
                "diff_screenshot_asset_id",
                "baseline_xml_asset_id",
                "candidate_xml_asset_id",
            }.issubset(compatibility_result_cols)
        )

        device_cols = {c[1] for c in self._table_columns("device")}
        self.assertIn("lease_task_id", device_cols)
        self.assertIn("lease_kind", device_cols)
        self.assertIn("lease_acquired_at", device_cols)
        self.assertIn("connection_type", device_cols)

        scenario_folder_table = self.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name = 'scenariofolder'"
        ).fetchone()
        self.assertIsNotNone(scenario_folder_table)
        testscenario_cols = {c[1] for c in self._table_columns("testscenario")}
        self.assertIn("folder_id", testscenario_cols)

        rows = self.conn.execute("SELECT version FROM schema_migration ORDER BY version").fetchall()
        self.assertEqual(len(rows), 25)

        # Re-run should be no-op and keep same version records.
        _run_migrations_with_conn(self.conn)
        rows_again = self.conn.execute("SELECT version FROM schema_migration ORDER BY version").fetchall()
        self.assertEqual(rows_again, rows)

    def test_installed_replay_columns_are_nullable_and_idempotent(self):
        _run_migrations_with_conn(self.conn)
        run_columns = {
            row[1]: row for row in self._table_columns("compatibilityrun")
        }
        self.assertIn("execution_mode", run_columns)
        self.assertIn("replay_branch_key", run_columns)
        self.assertIn("replay_plan_digest", run_columns)
        self.assertIn("replay_duration_seconds", run_columns)
        self.assertEqual(run_columns["new_package_id"][3], 0)
        self.assertIn("source_package_snapshot", run_columns)
        self.assertIn("target_package_snapshot", run_columns)
        self.assertIn("source_coverage_snapshot", run_columns)
        cell_columns = {row[1] for row in self._table_columns("compatibilitycell")}
        self.assertIn("preflight_at", cell_columns)
        self.assertIn("installed_package_snapshot", cell_columns)
        result_columns = {
            row[1] for row in self._table_columns("compatibilitypageresult")
        }
        self.assertTrue(
            {
                "path_key",
                "source_state_id",
                "source_observation_id",
                "evidence_level",
                "failure_type",
                "failed_step_index",
                "replay_trace",
            }.issubset(result_columns)
        )
        self.conn.execute(
            "INSERT INTO compatibilityrun("
            "id, name, new_package_id, execution_mode, replay_duration_seconds, created_at"
            ") VALUES (99, 'replay', NULL, 'INSTALLED_REPLAY', 300, CURRENT_TIMESTAMP)"
        )
        self.conn.commit()
        _run_migrations_with_conn(self.conn)
        row = self.conn.execute(
            "SELECT new_package_id, execution_mode, replay_duration_seconds "
            "FROM compatibilityrun WHERE id = 99"
        ).fetchone()
        self.assertEqual(row, (None, "INSTALLED_REPLAY", 300))

    def test_fresh_model_schema_keeps_instance_aware_identity_after_migrations(self):
        fresh = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        fresh.close()
        fresh_engine = create_engine(f"sqlite:///{fresh.name}")
        try:
            # Importing backend.models above registers the complete production
            # metadata used by create_db_and_tables before migrations run.
            self.assertEqual(backend_models.InspectionState.__tablename__, "inspectionstate")
            SQLModel.metadata.create_all(fresh_engine)
            with sqlite3.connect(fresh.name) as conn:
                _run_migrations_with_conn(conn)
                versions = conn.execute(
                    "SELECT version FROM schema_migration ORDER BY version"
                ).fetchall()
                version_names = {row[0] for row in versions}
                self.assertIn(
                    "20260721_021_inspection_frontier_state",
                    version_names,
                )
                self.assertIn(
                    "20260721_022_inspection_exploration_family",
                    version_names,
                )
                self.assertIn(
                    "20260721_023_inspection_coverage_scheduler",
                    version_names,
                )
                self.assertNotIn(
                    "20260721_021_inspection_exploration_family",
                    version_names,
                )
                self.assertNotIn(
                    "20260721_022_inspection_exploration_backfill",
                    version_names,
                )
                _run_migrations_with_conn(conn)
                self.assertEqual(
                    conn.execute(
                        "SELECT version FROM schema_migration ORDER BY version"
                    ).fetchall(),
                    versions,
                )
                unique_keys = self._unique_index_columns(conn, "inspectionstate")
                self.assertIn(
                    ("branch_run_id", "semantic_key", "instance_anchor"),
                    unique_keys,
                )
                self.assertNotIn(
                    ("branch_run_id", "semantic_key"),
                    unique_keys,
                )

            with Session(fresh_engine) as session:
                run = backend_models.InspectionRun(
                    name="fresh",
                    package_name="com.example.fresh",
                    device_serial="fresh-device",
                )
                session.add(run)
                session.flush()
                branch = backend_models.InspectionBranchRun(
                    run_id=run.id,
                    branch_key="guest",
                    branch_name="Guest",
                )
                session.add(branch)
                session.flush()
                session.add_all(
                    [
                        backend_models.InspectionState(
                            run_id=run.id,
                            branch_run_id=branch.id,
                            branch_key="guest",
                            cluster_key="product-a",
                            state_key="product-a",
                            semantic_key="product-detail",
                            instance_anchor="sku-a",
                        ),
                        backend_models.InspectionState(
                            run_id=run.id,
                            branch_run_id=branch.id,
                            branch_key="guest",
                            cluster_key="product-b",
                            state_key="product-b",
                            semantic_key="product-detail",
                            instance_anchor="sku-b",
                        ),
                    ]
                )
                session.commit()
                self.assertEqual(
                    len(
                        session.exec(
                            select(backend_models.InspectionState).where(
                                backend_models.InspectionState.semantic_key
                                == "product-detail"
                            )
                        ).all()
                    ),
                    2,
                )
                session.add(
                    backend_models.InspectionState(
                        run_id=run.id,
                        branch_run_id=branch.id,
                        branch_key="guest",
                        cluster_key="product-a-copy",
                        state_key="product-a-copy",
                        semantic_key="product-detail",
                        instance_anchor="sku-a",
                    )
                )
                with self.assertRaises(IntegrityError):
                    session.commit()
                session.rollback()
        finally:
            fresh_engine.dispose()
            if os.path.exists(fresh.name):
                os.remove(fresh.name)

    def test_development_migration_names_are_adopted_without_reexecution(self):
        _run_migrations_with_conn(self.conn)
        self.conn.execute(
            "DELETE FROM schema_migration WHERE version IN (?, ?)",
            (
                "20260721_021_inspection_frontier_state",
                "20260721_022_inspection_exploration_family",
            ),
        )
        self.conn.executemany(
            "INSERT INTO schema_migration(version) VALUES (?)",
            [
                ("20260721_021_inspection_exploration_family",),
                ("20260721_022_inspection_exploration_backfill",),
            ],
        )
        self.conn.commit()

        with patch(
            "backend.database._migrate_inspection_frontier_state",
            side_effect=AssertionError("frontier migration must not rerun"),
        ), patch(
            "backend.database._migrate_inspection_exploration_family",
            side_effect=AssertionError("family migration must not rerun"),
        ):
            _run_migrations_with_conn(self.conn)

        versions = {
            row[0]
            for row in self.conn.execute("SELECT version FROM schema_migration")
        }
        self.assertTrue(
            {
                "20260721_021_inspection_frontier_state",
                "20260721_022_inspection_exploration_family",
                "20260721_021_inspection_exploration_family",
                "20260721_022_inspection_exploration_backfill",
            }.issubset(versions)
        )

    def test_021_rebuilds_legacy_table_level_semantic_constraint(self):
        self.conn.execute("PRAGMA foreign_keys = ON")
        cursor = self.conn.cursor()
        cursor.executescript(
            """
            CREATE TABLE inspectionstate (
                id INTEGER PRIMARY KEY,
                run_id INTEGER NOT NULL,
                branch_run_id INTEGER NOT NULL,
                branch_key VARCHAR NOT NULL,
                cluster_key VARCHAR NOT NULL,
                state_key VARCHAR NOT NULL,
                semantic_key VARCHAR,
                CONSTRAINT uq_inspectionstate_branch_semantic
                    UNIQUE (branch_run_id, semantic_key)
            );
            CREATE TABLE inspectionpagetemplate (
                id INTEGER PRIMARY KEY
            );
            CREATE INDEX ix_inspectionstate_existing_test
                ON inspectionstate(state_key);
            CREATE TABLE inspectiontransition (
                id INTEGER PRIMARY KEY,
                run_id INTEGER NOT NULL,
                branch_run_id INTEGER NOT NULL,
                from_state_id INTEGER NOT NULL REFERENCES inspectionstate(id),
                sequence INTEGER NOT NULL,
                action_key VARCHAR NOT NULL,
                status VARCHAR NOT NULL,
                reason VARCHAR
            );
            INSERT INTO inspectionstate(
                id, run_id, branch_run_id, branch_key, cluster_key, state_key,
                semantic_key
            ) VALUES (
                1, 1, 7, 'guest', 'product-a', 'product-a', 'product-detail'
            );
            INSERT INTO inspectiontransition(
                id, run_id, branch_run_id, from_state_id, sequence,
                action_key, status
            ) VALUES (1, 1, 7, 1, 1, 'open-product', 'PASS');
            """
        )
        self.conn.commit()

        _run_migrations_with_conn(self.conn)
        self.conn.execute(
            "UPDATE inspectionstate SET instance_anchor = 'sku-a' WHERE id = 1"
        )
        self.conn.execute(
            """
            INSERT INTO inspectionstate(
                id, run_id, branch_run_id, branch_key, cluster_key, state_key,
                semantic_key, instance_anchor
            ) VALUES (
                2, 1, 7, 'guest', 'product-b', 'product-b',
                'product-detail', 'sku-b'
            )
            """
        )
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute(
                """
                INSERT INTO inspectionstate(
                    id, run_id, branch_run_id, branch_key, cluster_key,
                    state_key, semantic_key, instance_anchor
                ) VALUES (
                    3, 1, 7, 'guest', 'product-a-copy', 'product-a-copy',
                    'product-detail', 'sku-a'
                )
                """
            )
        self.assertIn(
            ("branch_run_id", "semantic_key", "instance_anchor"),
            self._unique_index_columns(self.conn, "inspectionstate"),
        )
        self.assertNotIn(
            ("branch_run_id", "semantic_key"),
            self._unique_index_columns(self.conn, "inspectionstate"),
        )
        indexes = {
            row[1]
            for row in self.conn.execute("PRAGMA index_list(inspectionstate)")
        }
        self.assertIn("ix_inspectionstate_existing_test", indexes)
        self.assertEqual(self.conn.execute("PRAGMA foreign_keys").fetchone()[0], 1)
        self.assertEqual(self.conn.execute("PRAGMA foreign_key_check").fetchall(), [])
        self.assertEqual(
            self.conn.execute(
                "SELECT id, semantic_key, instance_anchor "
                "FROM inspectionstate ORDER BY id"
            ).fetchall(),
            [
                (1, "product-detail", "sku-a"),
                (2, "product-detail", "sku-b"),
            ],
        )

        self.conn.commit()
        _run_migrations_with_conn(self.conn)
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) FROM inspectionstate").fetchone()[0],
            2,
        )

    def test_inspection_relation_columns_are_nullable_and_not_backfilled(self):
        cursor = self.conn.cursor()
        cursor.executescript(
            """
            CREATE TABLE inspectiontransition (
                id INTEGER PRIMARY KEY,
                run_id INTEGER,
                branch_run_id INTEGER,
                from_state_id INTEGER,
                sequence INTEGER,
                action_key VARCHAR NOT NULL,
                status VARCHAR NOT NULL,
                reason VARCHAR
            );
            CREATE TABLE inspectionstate (
                id INTEGER PRIMARY KEY,
                run_id INTEGER,
                branch_run_id INTEGER,
                cluster_key VARCHAR,
                state_key VARCHAR,
                queued_at TIMESTAMP,
                expanded_at TIMESTAMP
            );
            INSERT INTO inspectiontransition(
                id, run_id, branch_run_id, from_state_id, sequence,
                action_key, status, reason
            ) VALUES
                (1, 1, 1, 1, 1, 'legacy-action', 'PASS', NULL),
                (2, 1, 1, 1, 2, 'coordinate', 'LOCATOR_DRIFT',
                    '页面像素已变化，拒绝使用采集时保存的坐标'),
                (3, 1, 1, 2, 3, 'runtime', 'LOCATOR_DRIFT',
                    '定位器在采集后、执行前发生漂移'),
                (4, 1, 1, 2, 4, 'cascade', 'UNSTABLE_PARENT',
                    '父状态恢复已失败，本动作组不再重复进入用例');
            INSERT INTO inspectionstate(
                id, run_id, branch_run_id, cluster_key, state_key,
                queued_at, expanded_at
            ) VALUES
                (1, 1, 1, 'expanded-cluster', 'expanded-state',
                    CURRENT_TIMESTAMP, CURRENT_TIMESTAMP),
                (2, 1, 1, 'queued-cluster', 'queued-state',
                    CURRENT_TIMESTAMP, NULL);
            """
        )
        self.conn.commit()

        _run_migrations_with_conn(self.conn)

        columns = {
            row[1]: row
            for row in self._table_columns("inspectiontransition")
        }
        self.assertIn("relation_type", columns)
        self.assertIn("relation_confidence", columns)
        self.assertIn("topology_type", columns)
        self.assertIn("source_observation_id", columns)
        self.assertIn("target_observation_id", columns)
        self.assertIn("traversal_count", columns)
        self.assertIn("action_role_key", columns)
        self.assertIn("execution_disposition", columns)
        self.assertIn("failure_type", columns)
        self.assertIn("coverage_source_transition_id", columns)
        self.assertIn("recovery_attempt_count", columns)
        self.assertIn("coverage_contract_id", columns)
        self.assertIn("action_group_key", columns)
        self.assertIn("sampling_disposition", columns)
        self.assertIn("visual_locator_evidence", columns)
        self.assertEqual(columns["relation_type"][3], 0)
        self.assertEqual(columns["relation_confidence"][3], 0)
        legacy = self.conn.execute(
            "SELECT relation_type, relation_confidence "
            "FROM inspectiontransition WHERE id = 1"
        ).fetchone()
        self.assertEqual(legacy, (None, None))
        versions = {
            row[0]
            for row in self.conn.execute("SELECT version FROM schema_migration")
        }
        self.assertIn(
            "20260720_018_inspection_transition_relation",
            versions,
        )
        self.assertIn(
            "20260720_019_inspection_identity_observation",
            versions,
        )
        self.assertIn(
            "20260721_021_inspection_frontier_state",
            versions,
        )
        self.assertIn(
            "20260721_022_inspection_exploration_family",
            versions,
        )
        self.assertIn(
            "20260721_023_inspection_coverage_scheduler",
            versions,
        )
        state_columns = {row[1] for row in self._table_columns("inspectionstate")}
        self.assertTrue(
            {
                "template_id",
                "semantic_key",
                "identity_version",
                "representative_observation_id",
                "observation_count",
                "queued_at",
                "expanded_at",
                "instance_anchor",
                "exploration_family_id",
                "family_match_confidence",
                "family_match_evidence",
                "exploration_mode",
                "expansion_status",
                "pending_action_count",
                "last_action_cursor",
                "recovery_retry_count",
                "expansion_completed_at",
                "page_subtype",
                "coverage_status",
                "frontier_priority",
                "frontier_reason",
            }.issubset(state_columns)
        )
        tables = {
            row[0]
            for row in self.conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        self.assertIn("inspectionexplorationfamily", tables)
        self.assertIn("inspectionfamilyactioncoverage", tables)
        self.assertIn("inspectioncoveragecontract", tables)
        classified = self.conn.execute(
            "SELECT id, execution_disposition, failure_type "
            "FROM inspectiontransition ORDER BY id"
        ).fetchall()
        self.assertEqual(
            classified,
            [
                (1, "EXECUTED", None),
                (2, "SKIPPED", "COORDINATE_STALE"),
                (3, "FAILED", "LOCATOR_NOT_FOUND"),
                (4, "NOT_REACHED", "PARENT_RECOVERY_CASCADE"),
            ],
        )
        frontier = self.conn.execute(
            "SELECT id, expansion_status, last_action_cursor "
            "FROM inspectionstate ORDER BY id"
        ).fetchall()
        self.assertEqual(
            frontier,
            [(1, "EXPANDED", 2), (2, "DEFERRED", 4)],
        )

        _run_migrations_with_conn(self.conn)
        legacy_again = self.conn.execute(
            "SELECT relation_type, relation_confidence "
            "FROM inspectiontransition WHERE id = 1"
        ).fetchone()
        self.assertEqual(legacy_again, legacy)
        classified_again = self.conn.execute(
            "SELECT id, execution_disposition, failure_type "
            "FROM inspectiontransition ORDER BY id"
        ).fetchall()
        self.assertEqual(classified_again, classified)

    def test_connection_type_added_for_database_already_upgraded_to_016(self):
        """016 已应用但 Device 缺 connection_type 时，017 必须单独补列。"""
        cursor = self.conn.cursor()
        cursor.execute(
            """
            CREATE TABLE schema_migration (
                version VARCHAR PRIMARY KEY,
                applied_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        applied_versions = [
            "20260305_001_add_columns",
            "20260305_002_backfill_testcasestep_order",
            "20260305_003_scheduledtask_scenario_nullable",
            "20260312_004_fastbotreport_jank_fields",
            "20260519_005_testresult_report_display",
            "20260616_006_compatibility_tables",
            "20260616_007_compatibility_current_baseline",
            "20260616_008_compatibility_page_set_snapshot",
            "20260710_009_scenario_folders",
            "20260711_010_testcasestep_retry_count",
            "20260713_011_compatibility_compare_mode",
            "20260716_012_apppackage_platform",
            "20260717_013_model_inspection",
            "20260717_014_device_execution_lease",
            "20260717_015_compatibility_inspection_source",
            "20260717_016_inspection_thumbnail",
        ]
        cursor.executemany(
            "INSERT INTO schema_migration(version) VALUES (?)",
            [(version,) for version in applied_versions],
        )
        self.conn.commit()

        self.assertNotIn(
            "connection_type",
            {c[1] for c in self._table_columns("device")},
        )

        _run_migrations_with_conn(self.conn)

        self.assertIn(
            "connection_type",
            {c[1] for c in self._table_columns("device")},
        )
        versions = {
            row[0]
            for row in self.conn.execute("SELECT version FROM schema_migration")
        }
        self.assertIn("20260720_017_device_connection_type", versions)

    def test_retry_count_added_for_db_upgraded_before_retry_column(self):
        """已应用过 001 的旧库：由 010 版本迁移补 testcasestep.retry_count。"""
        cursor = self.conn.cursor()
        cursor.executescript(
            """
            CREATE TABLE schema_migration (
                version VARCHAR PRIMARY KEY,
                applied_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            """
        )
        applied_versions = [
            "20260305_001_add_columns",
            "20260305_002_backfill_testcasestep_order",
            "20260305_003_scheduledtask_scenario_nullable",
            "20260312_004_fastbotreport_jank_fields",
            "20260519_005_testresult_report_display",
            "20260616_006_compatibility_tables",
            "20260616_007_compatibility_current_baseline",
            "20260616_008_compatibility_page_set_snapshot",
            "20260710_009_scenario_folders",
        ]
        cursor.executemany(
            "INSERT INTO schema_migration(version) VALUES (?)",
            [(version,) for version in applied_versions],
        )
        self.conn.commit()

        self.assertNotIn("retry_count", {c[1] for c in self._table_columns("testcasestep")})

        _run_migrations_with_conn(self.conn)

        testcasestep_cols = {c[1] for c in self._table_columns("testcasestep")}
        self.assertIn("retry_count", testcasestep_cols)
        versions = {
            row[0]
            for row in self.conn.execute("SELECT version FROM schema_migration").fetchall()
        }
        self.assertIn("20260711_010_testcasestep_retry_count", versions)

    def test_compatibility_compare_mode_added_for_existing_compat_tables(self):
        """已应用过 001~010 的旧库：由 011 版本迁移补机型对比列。"""
        cursor = self.conn.cursor()
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
                ('20260616_007_compatibility_current_baseline'),
                ('20260616_008_compatibility_page_set_snapshot'),
                ('20260710_009_scenario_folders'),
                ('20260711_010_testcasestep_retry_count');
            CREATE TABLE compatibilityrun (
                id INTEGER PRIMARY KEY,
                name VARCHAR NOT NULL,
                mode VARCHAR DEFAULT 'upgrade',
                status VARCHAR DEFAULT 'PENDING',
                created_at TIMESTAMP NOT NULL
            );
            CREATE TABLE compatibilitycell (
                id INTEGER PRIMARY KEY,
                run_id INTEGER NOT NULL,
                device_serial VARCHAR NOT NULL,
                status VARCHAR DEFAULT 'PENDING'
            );
            INSERT INTO compatibilityrun(id, name, created_at) VALUES (1, 'legacy', CURRENT_TIMESTAMP);
            INSERT INTO compatibilitycell(id, run_id, device_serial) VALUES (1, 1, 'android-1');
            """
        )
        self.conn.commit()

        self.assertNotIn("compare_mode", {c[1] for c in self._table_columns("compatibilityrun")})

        _run_migrations_with_conn(self.conn)

        run_cols = {c[1] for c in self._table_columns("compatibilityrun")}
        self.assertIn("compare_mode", run_cols)
        self.assertIn("baseline_device_serial", run_cols)
        cell_cols = {c[1] for c in self._table_columns("compatibilitycell")}
        self.assertIn("is_baseline", cell_cols)

        legacy_row = self.conn.execute(
            "SELECT compare_mode, baseline_device_serial FROM compatibilityrun WHERE id = 1"
        ).fetchone()
        self.assertEqual(legacy_row[0], "version")
        self.assertIsNone(legacy_row[1])
        legacy_cell = self.conn.execute(
            "SELECT is_baseline FROM compatibilitycell WHERE id = 1"
        ).fetchone()
        self.assertEqual(legacy_cell[0], 0)
        versions = {
            row[0]
            for row in self.conn.execute("SELECT version FROM schema_migration").fetchall()
        }
        self.assertIn("20260713_011_compatibility_compare_mode", versions)

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
