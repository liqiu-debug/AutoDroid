"""One-off migration tool: testcase.steps -> testcasestep rows.

Usage:
  python -m backend.migrate_case_steps_to_standard
  python -m backend.migrate_case_steps_to_standard --force
"""
from __future__ import annotations

import argparse

from sqlmodel import Session, select

from backend.database import engine
from backend.models import TestCase, TestCaseStep
from backend.step_contract import build_standard_from_legacy_steps


def migrate(force: bool = False) -> None:
    migrated_cases = 0
    skipped_cases = 0
    created_steps = 0

    with Session(engine) as session:
        cases = session.exec(select(TestCase)).all()

        for case in cases:
            legacy_steps = list(case.steps or [])
            existing = session.exec(
                select(TestCaseStep).where(TestCaseStep.case_id == case.id)
            ).all()

            if existing and not force:
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
                        description=item.get("description"),
                    )
                )

            migrated_cases += 1
            created_steps += len(payload)

        session.commit()

    print(
        "migration finished:",
        {
            "migrated_cases": migrated_cases,
            "skipped_cases": skipped_cases,
            "created_steps": created_steps,
            "force": force,
        },
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Migrate legacy case steps to standard table")
    parser.add_argument(
        "--force",
        action="store_true",
        help="replace existing standard steps for each case",
    )
    args = parser.parse_args()
    migrate(force=args.force)


if __name__ == "__main__":
    main()
