import json
import sqlite3
import tempfile
import unittest
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from scripts.maintenance import backfill_haier_business_coverage as backfill


@dataclass
class _Item:
    key: str = "home"
    required: bool = True
    covered: bool = True
    evidence_state_ids: tuple[int, ...] = (10,)

    def to_dict(self):
        return {
            "key": self.key,
            "label": "首页",
            "weight": 1,
            "required": self.required,
            "covered": self.covered,
            "evidence_state_ids": list(self.evidence_state_ids),
            "evidence_transition_ids": [],
            "detail": "fixture",
        }


class HaierBusinessCoverageBackfillTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.database = Path(self.temp.name) / "coverage.db"
        with sqlite3.connect(self.database) as connection:
            connection.executescript(
                """
                CREATE TABLE inspectionrun (
                    id INTEGER PRIMARY KEY,
                    package_name VARCHAR NOT NULL,
                    status VARCHAR NOT NULL,
                    selected_branches JSON,
                    coverage_manifest_id VARCHAR,
                    coverage_manifest_version VARCHAR,
                    coverage_manifest_hash VARCHAR,
                    coverage_manifest_snapshot JSON NOT NULL DEFAULT '{}',
                    coverage_assessment JSON NOT NULL DEFAULT '{}',
                    coverage_verdict VARCHAR NOT NULL DEFAULT 'NOT_EVALUATED',
                    coverage_evaluated_at TIMESTAMP
                );
                CREATE TABLE inspectionstate (
                    id INTEGER PRIMARY KEY,
                    run_id INTEGER NOT NULL,
                    coverage_status VARCHAR NOT NULL DEFAULT 'DISCOVERED'
                );
                """
            )

    def tearDown(self) -> None:
        self.temp.cleanup()

    @staticmethod
    def _report(*, passed: bool = True):
        return SimpleNamespace(
            items=[_Item()],
            passed=passed,
            covered_weight=1,
            total_weight=1,
            weighted_coverage=1.0,
            xml_missing_count=0,
        )

    def _insert_run(
        self,
        run_id: int,
        *,
        branches: list[str],
        manifest_id: str | None = None,
        assessment: dict | None = None,
    ) -> None:
        with sqlite3.connect(self.database) as connection:
            connection.execute(
                "INSERT INTO inspectionrun "
                "(id, package_name, status, selected_branches, coverage_manifest_id, "
                "coverage_assessment) VALUES (?, ?, 'WARNING', ?, ?, ?)",
                (
                    run_id,
                    "com.ehaier.zgq.shop.mall",
                    json.dumps(branches),
                    manifest_id,
                    json.dumps(assessment or {}),
                ),
            )
            connection.execute(
                "INSERT INTO inspectionstate (id, run_id) VALUES (?, ?)",
                (10, run_id),
            )

    def test_force_never_downgrades_a_v2_run(self):
        frozen = {"manifest": {"id": "haier-mall-v2"}}
        self._insert_run(
            70,
            branches=["authenticated"],
            manifest_id="haier-mall-v2",
            assessment=frozen,
        )

        with patch.object(backfill, "audit_haier_coverage") as audit:
            exit_code = backfill.main(
                ["--database", str(self.database), "--run-id", "70", "--force"]
            )

        self.assertEqual(exit_code, 0)
        audit.assert_not_called()
        with sqlite3.connect(self.database) as connection:
            row = connection.execute(
                "SELECT coverage_manifest_id, coverage_assessment "
                "FROM inspectionrun WHERE id = 70"
            ).fetchone()
        self.assertEqual(row[0], "haier-mall-v2")
        self.assertEqual(json.loads(row[1]), frozen)

    def test_backfills_authenticated_run_with_frozen_v1_conclusion(self):
        self._insert_run(44, branches=["authenticated"])

        with patch.object(
            backfill,
            "audit_haier_coverage",
            return_value=self._report(),
        ):
            exit_code = backfill.main(
                ["--database", str(self.database), "--run-id", "44"]
            )

        self.assertEqual(exit_code, 0)
        with sqlite3.connect(self.database) as connection:
            row = connection.execute(
                "SELECT coverage_manifest_id, coverage_manifest_hash, "
                "coverage_assessment, coverage_verdict FROM inspectionrun WHERE id = 44"
            ).fetchone()
            state_status = connection.execute(
                "SELECT coverage_status FROM inspectionstate WHERE run_id = 44"
            ).fetchone()[0]
        assessment = json.loads(row[2])
        self.assertEqual(row[0], "haier-mall-v1")
        self.assertEqual(row[1], backfill.v1_manifest_hash())
        self.assertEqual(row[3], "INCOMPLETE")
        self.assertEqual(assessment["selected_scope_verdict"], "COMPLETE")
        self.assertEqual(assessment["full_app_verdict"], "INCOMPLETE")
        self.assertEqual(state_status, "REQUIRED_EVIDENCE")

    def test_guest_selected_in_v1_is_explicitly_inconclusive(self):
        assessment = backfill._assessment(
            self._report(),
            ["authenticated", "guest"],
        )

        self.assertEqual(assessment["selected_scope_verdict"], "INCONCLUSIVE")
        guest = next(
            item for item in assessment["branches"] if item["branch_key"] == "guest"
        )
        self.assertTrue(guest["selected"])
        self.assertEqual(guest["verdict"], "INCONCLUSIVE")
        self.assertTrue(
            any(
                item["type"] == "LEGACY_BRANCH_UNSUPPORTED"
                for item in assessment["blind_spots"]
            )
        )


if __name__ == "__main__":
    unittest.main()
