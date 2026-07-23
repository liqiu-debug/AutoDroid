#!/usr/bin/env python3
"""Audit a Haier Mall inspection run against the explicit coverage manifest."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Sequence

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.inspection.haier_coverage import (  # noqa: E402
    CoverageAuditError,
    audit_haier_coverage,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Read-only Haier Mall inspection coverage acceptance",
    )
    parser.add_argument("run_id", type=int, help="InspectionRun id")
    parser.add_argument(
        "--database",
        type=Path,
        default=ROOT / "database.db",
        help="SQLite database path (default: project database.db)",
    )
    parser.add_argument(
        "--threshold",
        type=float,
        default=0.85,
        help="Weighted coverage threshold, from 0 to 1 (default: 0.85)",
    )
    parser.add_argument(
        "--strict",
        action="store_true",
        help="Exit non-zero for a non-terminal run, low coverage, or failed required item",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        report = audit_haier_coverage(
            args.database,
            args.run_id,
            threshold=args.threshold,
        )
    except CoverageAuditError as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False, indent=2))
        return 2
    print(json.dumps(report.to_dict(), ensure_ascii=False, indent=2))
    return 1 if args.strict and not report.passed else 0


if __name__ == "__main__":
    raise SystemExit(main())
