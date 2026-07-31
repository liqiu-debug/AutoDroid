"""Tests for the cross-run application map.

Two properties matter most.  The counters must be derived rather than
incremented, because a denominator that drifts when a run is re-folded is worth
no more than the self-referential one it replaces.  And priority must reflect
what earlier runs still owe, so consecutive runs complement each other instead
of resampling the same half of the app.
"""

import unittest
from datetime import datetime, timedelta

from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, select

from backend.inspection.app_map import (
    PRIORITY_COVERED,
    PRIORITY_FAILED_RETRY,
    PRIORITY_NEW_SURFACE,
    PRIORITY_STALE_ACTION,
    PRIORITY_UNCOVERED_ACTION,
    build_surface_coverage,
    load_app_map,
    normalize_action_slot,
    surface_priority,
    sync_app_map,
)
from backend.models import (
    InspectionAppAction,
    InspectionAppSurface,
    InspectionBranchRun,
    InspectionRun,
    InspectionState,
    InspectionTransition,
)

PACKAGE = "com.ehaier.zgq.shop.mall"

DYNAMIC_A = "COMMAND:" + "a" * 64
DYNAMIC_B = "COMMAND:" + "b" * 64


class NormalizeActionSlotTests(unittest.TestCase):
    def test_content_hashed_roles_collapse_to_one_dynamic_slot(self):
        """Per-record hashes must not each become an unpayable ledger slot."""
        self.assertEqual(
            normalize_action_slot(DYNAMIC_A, "k1"),
            normalize_action_slot(DYNAMIC_B, "k2"),
        )
        self.assertEqual(normalize_action_slot(DYNAMIC_A, "k1"), "COMMAND:DYNAMIC")
        self.assertEqual(
            normalize_action_slot("NAV:" + "c" * 64, ""), "NAV:DYNAMIC"
        )

    def test_named_roles_stay_distinct(self):
        self.assertEqual(normalize_action_slot("COMMAND:PAY", "k"), "COMMAND:PAY")
        self.assertEqual(normalize_action_slot("FAVORITE", "k"), "FAVORITE")
        self.assertEqual(
            normalize_action_slot("SCROLL:vertical:up", "k"), "SCROLL:vertical:up"
        )

    def test_empty_role_falls_back_to_the_key(self):
        self.assertEqual(normalize_action_slot("", "raw-key"), "raw-key")
        self.assertEqual(normalize_action_slot(None, None), "")


class AppMapTestBase(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine(
            "sqlite://",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        SQLModel.metadata.create_all(self.engine)

    def _run(self, session, run_id):
        run = InspectionRun(
            id=run_id,
            name=f"run-{run_id}",
            package_name=PACKAGE,
            device_serial="serial",
        )
        branch = InspectionBranchRun(
            id=run_id,
            run_id=run_id,
            branch_key="authenticated",
            branch_name="已登录",
        )
        session.add(run)
        session.add(branch)
        session.flush()
        return run

    def _state(self, session, *, state_id, run_id, surface_key, subtype="ORDER"):
        state = InspectionState(
            id=state_id,
            run_id=run_id,
            branch_run_id=run_id,
            branch_key="authenticated",
            cluster_key=f"cluster-{state_id}",
            state_key=f"state-{state_id}",
            surface_key=surface_key,
            surface_fingerprint_version=1,
            page_subtype=subtype,
            expansion_status="EXPANDED",
            screenshot_path=f"reports/{state_id}.png",
        )
        session.add(state)
        session.flush()
        return state

    def _transition(
        self, session, *, run_id, from_state_id, role_key, status="PASS", seq=1
    ):
        edge = InspectionTransition(
            run_id=run_id,
            branch_run_id=run_id,
            from_state_id=from_state_id,
            sequence=seq,
            action_type="click",
            action_key=f"{role_key}-{seq}",
            action_role_key=role_key,
            action_role=role_key,
            status=status,
        )
        session.add(edge)
        session.flush()
        return edge


class SyncIdempotencyTests(AppMapTestBase):
    def _seed_run(self, session, run_id, *, surfaces):
        self._run(session, run_id)
        for index, (surface_key, roles) in enumerate(surfaces.items()):
            state_id = run_id * 100 + index
            self._state(session, state_id=state_id, run_id=run_id, surface_key=surface_key)
            for seq, (role_key, status) in enumerate(roles.items()):
                self._transition(
                    session,
                    run_id=run_id,
                    from_state_id=state_id,
                    role_key=role_key,
                    status=status,
                    seq=seq,
                )
        session.commit()

    def test_resyncing_a_run_does_not_inflate_counters(self):
        with Session(self.engine) as session:
            self._seed_run(
                session,
                1,
                surfaces={"surface-a": {"OPEN": "PASS", "FILTER": "PASS"}},
            )
            sync_app_map(session, 1, package_name=PACKAGE)
            first = self._counters(session)
            for _ in range(3):
                sync_app_map(session, 1, package_name=PACKAGE)
            self.assertEqual(self._counters(session), first)

    def test_a_second_run_accumulates_instead_of_replacing(self):
        with Session(self.engine) as session:
            self._seed_run(session, 1, surfaces={"surface-a": {"OPEN": "PASS"}})
            sync_app_map(session, 1, package_name=PACKAGE)
            self._seed_run(
                session,
                2,
                surfaces={"surface-a": {"OPEN": "PASS"}, "surface-b": {"PAY": "PASS"}},
            )
            sync_app_map(session, 2, package_name=PACKAGE)

            surfaces = session.exec(select(InspectionAppSurface)).all()
            self.assertEqual({row.surface_key for row in surfaces}, {"surface-a", "surface-b"})
            by_key = {row.surface_key: row for row in surfaces}
            self.assertEqual(by_key["surface-a"].seen_run_count, 2)
            self.assertEqual(by_key["surface-b"].seen_run_count, 1)
            self.assertEqual(by_key["surface-a"].first_seen_run_id, 1)

    def test_blocked_actions_do_not_count_as_covered(self):
        """A safety block is not evidence that the action works."""
        with Session(self.engine) as session:
            self._seed_run(session, 1, surfaces={"surface-a": {"PAY": "BLOCKED"}})
            sync_app_map(session, 1, package_name=PACKAGE)
            action = session.exec(select(InspectionAppAction)).one()
            self.assertEqual(action.coverage_count, 0)
            self.assertEqual(action.last_status, "BLOCKED")

    def test_one_success_survives_a_later_failure_in_the_same_run(self):
        with Session(self.engine) as session:
            self._run(session, 1)
            self._state(session, state_id=10, run_id=1, surface_key="surface-a")
            self._transition(
                session, run_id=1, from_state_id=10, role_key="OPEN", status="PASS", seq=1
            )
            self._transition(
                session,
                run_id=1,
                from_state_id=10,
                role_key="OPEN",
                status="LOCATOR_NOT_FOUND",
                seq=2,
            )
            session.commit()
            sync_app_map(session, 1, package_name=PACKAGE)
            action = session.exec(select(InspectionAppAction)).one()
            self.assertEqual(action.last_status, "PASS")
            self.assertEqual(action.coverage_count, 1)

    def test_dynamic_commands_share_one_slot_across_instances(self):
        """Two products' hashed commands are the same page function, not two
        pieces of coverage debt."""
        with Session(self.engine) as session:
            self._run(session, 1)
            self._state(session, state_id=10, run_id=1, surface_key="surface-a")
            self._state(session, state_id=11, run_id=1, surface_key="surface-a")
            self._transition(
                session, run_id=1, from_state_id=10, role_key=DYNAMIC_A, seq=1
            )
            self._transition(
                session, run_id=1, from_state_id=11, role_key=DYNAMIC_B, seq=2
            )
            session.commit()
            sync_app_map(session, 1, package_name=PACKAGE)
            action = session.exec(select(InspectionAppAction)).one()
            self.assertEqual(action.action_role_key, "COMMAND:DYNAMIC")
            self.assertEqual(action.coverage_count, 1)
            self.assertEqual(action.last_status, "PASS")

    def test_stale_hash_keyed_rows_are_purged_on_sync(self):
        """Ledger rows from before normalization would otherwise stay 'never
        covered' forever and keep manufacturing phantom frontier demand."""
        with Session(self.engine) as session:
            self._run(session, 1)
            self._state(session, state_id=10, run_id=1, surface_key="surface-a")
            self._transition(
                session, run_id=1, from_state_id=10, role_key="OPEN", seq=1
            )
            session.commit()
            sync_app_map(session, 1, package_name=PACKAGE)
            surface = session.exec(select(InspectionAppSurface)).one()
            session.add(
                InspectionAppAction(
                    surface_id=surface.id,
                    package_name=PACKAGE,
                    action_role_key="e" * 64,
                    action_role=DYNAMIC_A,
                )
            )
            session.commit()
            sync_app_map(session, 1, package_name=PACKAGE)
            keys = {
                row.action_role_key
                for row in session.exec(select(InspectionAppAction)).all()
            }
            self.assertEqual(keys, {"OPEN"})

    def _counters(self, session):
        surfaces = session.exec(select(InspectionAppSurface)).all()
        actions = session.exec(select(InspectionAppAction)).all()
        return (
            len(surfaces),
            sum(row.seen_run_count for row in surfaces),
            len(actions),
            sum(row.coverage_count for row in actions),
            sum(row.failed_run_count for row in actions),
        )


class SurfacePriorityTests(AppMapTestBase):
    def _view(self, session):
        return load_app_map(session, PACKAGE)

    def test_an_empty_map_stays_silent(self):
        """Otherwise every surface reads as new and the engine's tiers flatten."""
        with Session(self.engine) as session:
            view = self._view(session)
            self.assertEqual(surface_priority(view, "anything", ["OPEN"]), (None, None))

    def test_an_unseen_surface_goes_to_the_front(self):
        with Session(self.engine) as session:
            session.add(
                InspectionAppSurface(
                    package_name=PACKAGE, surface_key="known", page_subtype="ORDER"
                )
            )
            session.commit()
            view = self._view(session)
            priority, reason = surface_priority(view, "brand-new", ["OPEN"])
            self.assertEqual(priority, PRIORITY_NEW_SURFACE)
            self.assertEqual(reason, "APP_MAP_NEW_SURFACE")

    def test_tiers_follow_what_the_map_still_owes(self):
        now = datetime(2026, 7, 30, 12, 0, 0)
        with Session(self.engine) as session:
            surface = InspectionAppSurface(
                package_name=PACKAGE, surface_key="known", page_subtype="ORDER"
            )
            session.add(surface)
            session.flush()
            session.add_all(
                [
                    InspectionAppAction(
                        surface_id=surface.id,
                        package_name=PACKAGE,
                        action_role_key="FRESH",
                        coverage_count=1,
                        last_status="PASS",
                        last_covered_at=now - timedelta(hours=2),
                    ),
                    InspectionAppAction(
                        surface_id=surface.id,
                        package_name=PACKAGE,
                        action_role_key="STALE",
                        coverage_count=1,
                        last_status="PASS",
                        last_covered_at=now - timedelta(days=9),
                    ),
                    InspectionAppAction(
                        surface_id=surface.id,
                        package_name=PACKAGE,
                        action_role_key="BROKEN",
                        coverage_count=1,
                        last_status="LOCATOR_NOT_FOUND",
                        last_covered_at=now - timedelta(hours=3),
                    ),
                ]
            )
            session.commit()
            view = self._view(session)

            self.assertEqual(
                surface_priority(view, "known", ["NEVER_SEEN"], now=now)[0],
                PRIORITY_UNCOVERED_ACTION,
            )
            self.assertEqual(
                surface_priority(view, "known", ["STALE"], now=now)[0],
                PRIORITY_STALE_ACTION,
            )
            self.assertEqual(
                surface_priority(view, "known", ["BROKEN"], now=now)[0],
                PRIORITY_FAILED_RETRY,
            )
            self.assertEqual(
                surface_priority(view, "known", ["FRESH"], now=now)[0],
                PRIORITY_COVERED,
            )

    def test_a_retired_surface_is_left_to_the_engine(self):
        with Session(self.engine) as session:
            session.add(
                InspectionAppSurface(
                    package_name=PACKAGE,
                    surface_key="gone",
                    page_subtype="ORDER",
                    is_retired=True,
                )
            )
            session.commit()
            view = self._view(session)
            self.assertEqual(surface_priority(view, "gone", ["OPEN"]), (None, None))

    def test_a_different_rule_version_is_not_mixed_in(self):
        with Session(self.engine) as session:
            session.add(
                InspectionAppSurface(
                    package_name=PACKAGE,
                    surface_key="v2-only",
                    page_subtype="ORDER",
                    surface_fingerprint_version=2,
                )
            )
            session.commit()
            view = load_app_map(session, PACKAGE, fingerprint_version=1)
            self.assertEqual(view.surfaces, {})


class SurfaceCoverageReportTests(AppMapTestBase):
    def test_report_has_a_real_denominator_and_names_the_gap(self):
        with Session(self.engine) as session:
            self._run(session, 1)
            self._state(session, state_id=10, run_id=1, surface_key="covered")
            self._transition(session, run_id=1, from_state_id=10, role_key="OPEN")
            session.commit()
            sync_app_map(session, 1, package_name=PACKAGE)

            # A surface an earlier run found but this run never touched.
            session.add(
                InspectionAppSurface(
                    package_name=PACKAGE,
                    surface_key="untouched",
                    page_subtype="CASHIER",
                    first_seen_run_id=0,
                )
            )
            session.commit()

            report = build_surface_coverage(session, 1, package_name=PACKAGE)
            self.assertTrue(report["available"])
            self.assertEqual(report["package_known_surfaces"], 2)
            self.assertEqual(report["run_visited_surfaces"], 1)
            self.assertEqual(report["cumulative_verdict"], "INCOMPLETE")
            gaps = {item["surface_key"] for item in report["never_covered_surfaces"]}
            self.assertIn("untouched", gaps)

    def test_report_reports_unavailable_rather_than_a_fake_hundred_percent(self):
        with Session(self.engine) as session:
            self._run(session, 1)
            session.commit()
            report = build_surface_coverage(session, 1, package_name=PACKAGE)
            self.assertFalse(report["available"])
            self.assertEqual(report["reason"], "APP_MAP_EMPTY")


if __name__ == "__main__":
    unittest.main()
