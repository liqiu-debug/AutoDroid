"""Backfill ``surface_key`` and the cross-run app map from archived captures.

A run can only be measured against a denominator that already exists.  Without
this, the first runs after the app map ships would compare themselves to a map
they had just created themselves, so cumulative coverage would read 100% while
measuring nothing.

Historical runs kept their ``hierarchy.xml`` under ``reports/inspection/``, so
the surface identity can be recomputed offline and folded in, giving the very
first new run a real denominator.

    python scripts/maintenance/backfill_app_map.py --dry-run
    python scripts/maintenance/backfill_app_map.py
"""

from __future__ import annotations

import argparse
import sys
from collections import defaultdict
from pathlib import Path
from typing import Dict, List, Optional, Sequence

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from sqlmodel import Session, select  # noqa: E402

from backend.database import create_db_and_tables, engine  # noqa: E402
from backend.inspection.app_map import sync_app_map  # noqa: E402
from backend.inspection.semantics import (  # noqa: E402
    SURFACE_FINGERPRINT_VERSION,
    build_page_model,
)
from backend.models import InspectionRun, InspectionState  # noqa: E402


def _archived_xml(state: InspectionState) -> Optional[Path]:
    candidate = (
        PROJECT_ROOT
        / "reports"
        / "inspection"
        / str(state.run_id)
        / str(state.branch_key or "")
        / str(state.id)
        / "hierarchy.xml"
    )
    return candidate if candidate.is_file() else None


def _recompute_surface_keys(
    session: Session,
    run: InspectionRun,
    *,
    dry_run: bool,
    force: bool,
) -> Dict[str, int]:
    states = session.exec(
        select(InspectionState).where(InspectionState.run_id == run.id)
    ).all()
    stats = {"total": len(states), "written": 0, "missing_xml": 0, "failed": 0, "skipped": 0}
    for state in states:
        if state.surface_key and not force:
            stats["skipped"] += 1
            continue
        xml_path = _archived_xml(state)
        if xml_path is None:
            stats["missing_xml"] += 1
            continue
        try:
            page = build_page_model(
                xml_path.read_text(encoding="utf-8", errors="replace"),
                package_name=str(run.package_name or ""),
                activity=str(state.activity or ""),
            )
        except Exception as exc:  # noqa: BLE001 - diagnostic tool
            stats["failed"] += 1
            print(f"    ! state {state.id}: {type(exc).__name__}: {exc}")
            continue
        if not page.surface_key:
            stats["failed"] += 1
            continue
        if not dry_run:
            state.surface_key = page.surface_key
            state.surface_fingerprint_version = SURFACE_FINGERPRINT_VERSION
            session.add(state)
        stats["written"] += 1
    if not dry_run:
        session.commit()
    return stats


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--runs",
        default="",
        help="comma separated run ids; default is every finished run with archived XML",
    )
    parser.add_argument("--package", default="", help="restrict to one package")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="report what would change without writing",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="recompute surface_key even where one is already stored",
    )
    args = parser.parse_args()

    # The app-map tables and the surface_key columns may not exist yet on a
    # database that has not been through a backend start since the migration.
    create_db_and_tables()

    wanted: Sequence[int] = [
        int(value) for value in args.runs.split(",") if value.strip()
    ]

    with Session(engine) as session:
        statement = select(InspectionRun).order_by(InspectionRun.id)
        if args.package:
            statement = statement.where(InspectionRun.package_name == args.package)
        runs: List[InspectionRun] = list(session.exec(statement).all())
    if wanted:
        runs = [run for run in runs if int(run.id or 0) in set(wanted)]
    if not runs:
        print("没有匹配的 run。")
        return 2

    mode = "DRY RUN — 不写入" if args.dry_run else "写入模式"
    print(f"{mode}；surface_fingerprint_version = {SURFACE_FINGERPRINT_VERSION}")

    totals: Dict[str, int] = defaultdict(int)
    by_package: Dict[str, List[int]] = defaultdict(list)
    for run in runs:
        with Session(engine) as session:
            fresh = session.get(InspectionRun, run.id)
            if fresh is None:
                continue
            run_id = int(fresh.id or 0)
            run_package = str(fresh.package_name or "")
            print(f"\nrun {run_id} ({run_package}) — {fresh.name}")
            stats = _recompute_surface_keys(
                session, fresh, dry_run=args.dry_run, force=args.force
            )
        print(
            f"  states={stats['total']} 回算={stats['written']} "
            f"已有跳过={stats['skipped']} 缺XML={stats['missing_xml']} 失败={stats['failed']}"
        )
        for key, value in stats.items():
            totals[key] += value
        # Sync even when every surface_key was already stored (states written by
        # a live run): the ledger itself may still need rebuilding, e.g. after a
        # slot-normalization change.
        if stats["written"] or stats["skipped"]:
            by_package[run_package].append(run_id)

    print(
        f"\n合计: states={totals['total']} 回算={totals['written']} "
        f"已有跳过={totals['skipped']} 缺XML={totals['missing_xml']} 失败={totals['failed']}"
    )

    if args.dry_run:
        print("\nDRY RUN 结束，未写入 app map。")
        return 0

    print("\n=== 折叠进 app map ===")
    for package_name, run_ids in by_package.items():
        for run_id in run_ids:
            with Session(engine) as session:
                result = sync_app_map(session, run_id, package_name=package_name)
            print(f"  run {run_id}: {result.as_dict()}")

    from backend.models import InspectionAppAction, InspectionAppSurface

    with Session(engine) as session:
        for package_name in by_package:
            surfaces = session.exec(
                select(InspectionAppSurface).where(
                    InspectionAppSurface.package_name == package_name,
                    InspectionAppSurface.surface_fingerprint_version
                    == SURFACE_FINGERPRINT_VERSION,
                )
            ).all()
            actions = session.exec(
                select(InspectionAppAction).where(
                    InspectionAppAction.package_name == package_name
                )
            ).all()
            covered = [row for row in actions if row.coverage_count]
            print(
                f"\n{package_name}: {len(surfaces)} 个面 · {len(actions)} 个动作槽位 "
                f"（已覆盖 {len(covered)}）"
            )
            by_subtype: Dict[str, int] = defaultdict(int)
            for row in surfaces:
                by_subtype[str(row.page_subtype or "UNKNOWN")] += 1
            for subtype, count in sorted(by_subtype.items(), key=lambda kv: -kv[1]):
                print(f"    {subtype:<20} {count}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
