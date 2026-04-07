"""Autofill iOS-ready standard steps from Android-centric cases.

Usage:
  python -m backend.autofill_ios_case_steps
  python -m backend.autofill_ios_case_steps --apply
  python -m backend.autofill_ios_case_steps --apply --case-id 2 --case-id 9
"""
from __future__ import annotations

import argparse
import json
from typing import Any, Dict, List, Optional

from sqlmodel import Session, select

from backend.cross_platform_execution import load_app_key_mapping
from backend.database import engine
from backend.ios_step_autofill import autofill_step_for_ios
from backend.models import TestCase, TestCaseStep
from backend.step_contract import build_standard_from_legacy_steps


def _row_to_step_payload(row: TestCaseStep) -> Dict[str, Any]:
    return {
        "order": row.order,
        "action": row.action,
        "args": row.args or {},
        "value": row.value,
        "execute_on": row.execute_on or ["android", "ios"],
        "platform_overrides": row.platform_overrides or {},
        "timeout": row.timeout,
        "error_strategy": row.error_strategy,
        "description": row.description,
    }


def _replace_case_rows(
    session: Session,
    case_id: int,
    rows: List[TestCaseStep],
    payloads: List[Dict[str, Any]],
) -> None:
    if rows:
        for row, item in zip(rows, payloads):
            row.order = int(item.get("order", row.order or 0))
            row.action = str(item.get("action") or row.action)
            row.args = item.get("args") or {}
            row.value = item.get("value")
            row.execute_on = item.get("execute_on") or ["android", "ios"]
            row.platform_overrides = item.get("platform_overrides") or {}
            row.timeout = int(item.get("timeout", row.timeout or 10))
            row.error_strategy = str(item.get("error_strategy") or row.error_strategy or "ABORT")
            row.description = item.get("description")
            session.add(row)
        return

    for item in payloads:
        session.add(
            TestCaseStep(
                case_id=case_id,
                order=int(item.get("order", 0)),
                action=str(item.get("action") or "click"),
                args=item.get("args") or {},
                value=item.get("value"),
                execute_on=item.get("execute_on") or ["android", "ios"],
                platform_overrides=item.get("platform_overrides") or {},
                timeout=int(item.get("timeout", 10)),
                error_strategy=str(item.get("error_strategy") or "ABORT"),
                description=item.get("description"),
            )
        )


def autofill(
    *,
    apply: bool = False,
    case_ids: Optional[List[int]] = None,
    sync_legacy: bool = True,
) -> Dict[str, Any]:
    report: Dict[str, Any] = {
        "mode": "apply" if apply else "dry_run",
        "summary": {
            "cases_scanned": 0,
            "cases_changed": 0,
            "cases_synced_from_legacy": 0,
            "steps_scanned": 0,
            "steps_changed": 0,
            "steps_with_blockers": 0,
        },
        "cases": [],
    }

    with Session(engine) as session:
        app_mapping = load_app_key_mapping(session)

        statement = select(TestCase).order_by(TestCase.id)
        if case_ids:
            statement = statement.where(TestCase.id.in_(case_ids))  # type: ignore[attr-defined]
        cases = session.exec(statement).all()

        for case in cases:
            report["summary"]["cases_scanned"] += 1

            rows = session.exec(
                select(TestCaseStep)
                .where(TestCaseStep.case_id == case.id)
                .order_by(TestCaseStep.order, TestCaseStep.id)
            ).all()

            source = "standard" if rows else "legacy"
            if rows:
                original_payloads = [_row_to_step_payload(row) for row in rows]
            elif sync_legacy and case.steps:
                original_payloads = build_standard_from_legacy_steps(case.steps or [], case_id=case.id)
                report["summary"]["cases_synced_from_legacy"] += 1
            else:
                original_payloads = []

            updated_payloads: List[Dict[str, Any]] = []
            case_step_reports: List[Dict[str, Any]] = []
            case_changed = False

            for index, item in enumerate(original_payloads, start=1):
                report["summary"]["steps_scanned"] += 1
                updated, meta = autofill_step_for_ios(item, app_mapping=app_mapping)
                updated_payloads.append(updated)

                if meta.get("changed"):
                    report["summary"]["steps_changed"] += 1
                    case_changed = True
                if meta.get("blockers"):
                    report["summary"]["steps_with_blockers"] += 1

                if meta.get("changed") or meta.get("blockers"):
                    case_step_reports.append(
                        {
                            "order": index,
                            "action": meta.get("action"),
                            "changes": meta.get("changes") or [],
                            "blockers": meta.get("blockers") or [],
                        }
                    )

            if case_changed:
                report["summary"]["cases_changed"] += 1

            if apply and (case_changed or (not rows and bool(updated_payloads))):
                if rows and len(rows) != len(updated_payloads):
                    raise RuntimeError(
                        f"case_id={case.id} steps length mismatch: rows={len(rows)} payloads={len(updated_payloads)}"
                    )
                _replace_case_rows(session, case.id, rows, updated_payloads)

            report["cases"].append(
                {
                    "case_id": case.id,
                    "case_name": case.name,
                    "source": source,
                    "step_count": len(original_payloads),
                    "changed": case_changed,
                    "step_reports": case_step_reports,
                }
            )

        if apply:
            session.commit()
        else:
            session.rollback()

    return report


def main() -> None:
    parser = argparse.ArgumentParser(description="Autofill iOS-ready case steps from Android data")
    parser.add_argument(
        "--apply",
        action="store_true",
        help="write changes into database (default is dry-run)",
    )
    parser.add_argument(
        "--case-id",
        action="append",
        type=int,
        dest="case_ids",
        help="optional case id, can be repeated",
    )
    parser.add_argument(
        "--no-sync-legacy",
        action="store_true",
        help="do not create standard rows for cases without testcasestep rows",
    )
    parser.add_argument(
        "--report",
        type=str,
        default="",
        help="optional report output file path",
    )
    args = parser.parse_args()

    report = autofill(
        apply=bool(args.apply),
        case_ids=args.case_ids,
        sync_legacy=not bool(args.no_sync_legacy),
    )
    text = json.dumps(report, ensure_ascii=False, indent=2)
    print(text)

    if args.report:
        with open(args.report, "w", encoding="utf-8") as fp:
            fp.write(text + "\n")


if __name__ == "__main__":
    main()
