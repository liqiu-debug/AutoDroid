"""One-off migration tool: testcase.steps -> testcasestep rows.

The same backfill also runs automatically (idempotently) on app startup via
`backend.database.create_db_and_tables`. This CLI stays for manual runs and
for `--force` rebuilds.

Usage:
  python -m backend.migrate_case_steps_to_standard
  python -m backend.migrate_case_steps_to_standard --force
"""
from __future__ import annotations

import argparse

from sqlmodel import Session

from backend.database import backfill_case_steps_to_standard, engine


def migrate(force: bool = False) -> dict:
    with Session(engine) as session:
        summary = backfill_case_steps_to_standard(session, force=force)

    print("migration finished:", summary)
    return summary


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
