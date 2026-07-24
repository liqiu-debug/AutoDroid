#!/usr/bin/env python3
"""Backfill historical Haier runs with the frozen v1 coverage conclusion.

Historical evidence is never reinterpreted with the stricter v2 contract.
The script is idempotent and defaults to rows without an assessment.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Sequence

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.inspection.haier_coverage import (  # noqa: E402
    CoverageAuditError,
    MANIFEST_VERSION,
    audit_haier_coverage,
    serialize_v1_manifest,
    v1_manifest_hash,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Backfill Haier inspection business coverage using v1 only",
    )
    parser.add_argument(
        "--database",
        type=Path,
        default=ROOT / "database.db",
    )
    parser.add_argument("--run-id", type=int, action="append", default=[])
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    return parser


def _ensure_columns(connection: sqlite3.Connection) -> None:
    columns = {
        row[1]
        for row in connection.execute("PRAGMA table_info(inspectionrun)").fetchall()
    }
    additions = (
        ("coverage_manifest_id", "VARCHAR"),
        ("coverage_manifest_version", "VARCHAR"),
        ("coverage_manifest_hash", "VARCHAR"),
        ("coverage_manifest_snapshot", "JSON NOT NULL DEFAULT '{}'"),
        ("coverage_assessment", "JSON NOT NULL DEFAULT '{}'"),
        ("coverage_verdict", "VARCHAR NOT NULL DEFAULT 'NOT_EVALUATED'"),
        ("coverage_evaluated_at", "TIMESTAMP"),
    )
    for name, column_type in additions:
        if name not in columns:
            connection.execute(
                f"ALTER TABLE inspectionrun ADD COLUMN {name} {column_type}"
            )


def _assessment(report, selected_branches: Sequence[str]) -> dict[str, Any]:
    selected = list(dict.fromkeys(str(value) for value in selected_branches))
    selected_set = set(selected)
    authenticated_selected = "authenticated" in selected_set
    guest_selected = "guest" in selected_set
    required = [item for item in report.items if item.required]
    covered_required = sum(item.covered for item in required)
    authenticated_verdict = (
        "COMPLETE"
        if report.passed
        else "PARTIAL"
        if covered_required
        else "INCOMPLETE"
    )
    selected_verdict = (
        "INCONCLUSIVE"
        if guest_selected or not authenticated_selected
        else authenticated_verdict
    )
    items = [
        {
            **item.to_dict(),
            "branch_key": "authenticated",
            "status": "COVERED" if item.covered else "MISSING",
            "reason_code": "" if item.covered else "V1_EVIDENCE_MISSING",
        }
        for item in report.items
    ]
    blind_spots = [
        {
            "type": "LEGACY_V1_ASSESSMENT",
            "severity": "MEDIUM",
            "message": "历史 Run 按 haier-mall-v1 冻结，不使用 v2 追溯判定",
        }
    ]
    if guest_selected:
        blind_spots.append(
            {
                "type": "LEGACY_BRANCH_UNSUPPORTED",
                "branch_key": "guest",
                "severity": "HIGH",
                "message": "haier-mall-v1 未定义 guest 旅程，所选范围无法判定完整",
            }
        )
    else:
        blind_spots.append(
            {
                "type": "UNRUN_BRANCH",
                "branch_key": "guest",
                "severity": "HIGH",
                "message": "未运行 guest 业务线",
            }
        )
    if not authenticated_selected:
        blind_spots.append(
            {
                "type": "UNRUN_BRANCH",
                "branch_key": "authenticated",
                "severity": "HIGH",
                "message": "未运行 authenticated 业务线",
            }
        )
    snapshot = serialize_v1_manifest()
    digest = v1_manifest_hash()
    return {
        "schema_version": 2,
        "assessment_origin": "BACKFILLED_V1",
        "manifest": {"id": snapshot["id"], "version": 1, "hash": digest},
        "selected_branches": selected,
        "selected_scope_verdict": selected_verdict,
        "full_app_verdict": "INCOMPLETE",
        "coverage_verdict": "INCOMPLETE",
        "summary": {
            "covered_required": covered_required if authenticated_selected else 0,
            "total_required": len(required) if authenticated_selected else 0,
            "required_ratio": (
                round(covered_required / len(required), 4)
                if required and authenticated_selected
                else 0.0
            ),
            "covered_weight": report.covered_weight,
            "total_weight": report.total_weight,
            "weighted_coverage": round(report.weighted_coverage, 6),
            "scope_branches_selected": len(
                set(selected).intersection({"guest", "authenticated"})
            ),
            "scope_branches_covered": (
                1 if authenticated_selected and authenticated_verdict == "COMPLETE" else 0
            ),
            "scope_branches_total": 2,
            "evidence_quality": "MEDIUM" if report.xml_missing_count == 0 else "LOW",
        },
        "blind_spots": blind_spots,
        "branches": [
            {
                "branch_key": "guest",
                "selected": guest_selected,
                "verdict": "INCONCLUSIVE" if guest_selected else "NOT_IN_SCOPE",
                "covered_required": 0,
                "total_required": 0,
                "items": [],
            },
            {
                "branch_key": "authenticated",
                "selected": authenticated_selected,
                "verdict": (
                    authenticated_verdict if authenticated_selected else "NOT_IN_SCOPE"
                ),
                "covered_required": covered_required if authenticated_selected else 0,
                "total_required": len(required) if authenticated_selected else 0,
                "items": items if authenticated_selected else [],
            },
        ],
        "evaluated_at": datetime.now().isoformat(),
    }


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    database = args.database.expanduser().resolve()
    if not database.is_file():
        print(json.dumps({"error": f"database does not exist: {database}"}, ensure_ascii=False))
        return 2
    connection = sqlite3.connect(database)
    connection.row_factory = sqlite3.Row
    try:
        _ensure_columns(connection)
        query = (
            "SELECT id, selected_branches, coverage_manifest_id, coverage_assessment "
            "FROM inspectionrun WHERE package_name = ? "
            "AND status IN ('PASS', 'SUCCESS', 'WARNING', 'FAIL')"
        )
        params: list[Any] = ["com.ehaier.zgq.shop.mall"]
        if args.run_id:
            placeholders = ",".join("?" for _ in args.run_id)
            query += f" AND id IN ({placeholders})"
            params.extend(args.run_id)
        rows = connection.execute(query, params).fetchall()
        updated: list[dict[str, Any]] = []
        skipped: list[dict[str, Any]] = []
        errors: list[dict[str, Any]] = []
        for row in rows:
            existing_manifest_id = str(row["coverage_manifest_id"] or "").strip()
            if existing_manifest_id and existing_manifest_id != MANIFEST_VERSION:
                skipped.append(
                    {
                        "run_id": int(row["id"]),
                        "reason": (
                            "已冻结为其他版本清单，历史 v1 回填禁止覆盖: "
                            f"{existing_manifest_id}"
                        ),
                    }
                )
                continue
            existing = str(row["coverage_assessment"] or "").strip()
            if not args.force and existing not in {"", "{}", "null"}:
                continue
            try:
                report = audit_haier_coverage(database, int(row["id"]))
            except CoverageAuditError as exc:
                errors.append({"run_id": int(row["id"]), "error": str(exc)})
                continue
            try:
                selected = json.loads(row["selected_branches"] or "[]")
            except (TypeError, json.JSONDecodeError):
                selected = ["authenticated"]
            assessment = _assessment(report, selected)
            updated.append(
                {
                    "run_id": int(row["id"]),
                    "selected_scope_verdict": assessment["selected_scope_verdict"],
                    "full_app_verdict": assessment["full_app_verdict"],
                }
            )
            if args.dry_run:
                continue
            snapshot = serialize_v1_manifest()
            digest = v1_manifest_hash()
            connection.execute(
                "UPDATE inspectionrun SET coverage_manifest_id = ?, "
                "coverage_manifest_version = ?, coverage_manifest_hash = ?, "
                "coverage_manifest_snapshot = ?, coverage_assessment = ?, "
                "coverage_verdict = ?, coverage_evaluated_at = ? WHERE id = ?",
                (
                    snapshot["id"],
                    "1",
                    digest,
                    json.dumps(snapshot, ensure_ascii=False),
                    json.dumps(assessment, ensure_ascii=False),
                    assessment["coverage_verdict"],
                    assessment["evaluated_at"],
                    int(row["id"]),
                ),
            )
            connection.execute(
                "UPDATE inspectionstate SET coverage_status = 'EXPLORED' WHERE run_id = ?",
                (int(row["id"]),),
            )
            for item in report.items:
                status = "REQUIRED_EVIDENCE" if item.required else "OPTIONAL_EVIDENCE"
                for state_id in item.evidence_state_ids:
                    connection.execute(
                        "UPDATE inspectionstate SET coverage_status = ? "
                        "WHERE run_id = ? AND id = ?",
                        (status, int(row["id"]), int(state_id)),
                    )
        if args.dry_run:
            connection.rollback()
        else:
            connection.commit()
        print(
            json.dumps(
                {
                    "database": str(database),
                    "dry_run": bool(args.dry_run),
                    "updated": updated,
                    "skipped": skipped,
                    "errors": errors,
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 1 if errors else 0
    except sqlite3.Error as exc:
        connection.rollback()
        print(json.dumps({"error": str(exc)}, ensure_ascii=False))
        return 2
    finally:
        connection.close()


if __name__ == "__main__":
    raise SystemExit(main())
