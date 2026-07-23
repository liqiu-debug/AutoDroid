import json
import os
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

from fastapi import BackgroundTasks, HTTPException
from pydantic import ValidationError
from sqlmodel import Session, SQLModel, create_engine, select

from backend.api.inspections import (
    _graph_action_records,
    _run_list_summary,
    _state_frontier_values,
    create_profile,
    create_run,
    delete_run,
    get_run,
    get_run_graph,
    get_state_action_map,
    list_run_families,
    list_replay_paths,
    list_state_observations,
    list_profiles,
    update_representative_observation,
    update_regression_selection,
)
from backend.feature_flags import (
    FLAG_INSPECTION_EXPLORATION_FAMILY_CONVERGENCE,
    FLAG_MODEL_INSPECTION,
)
from backend.inspection.engine import resolve_inspection_asset
from backend.inspection.runtime import discard_abort_event, get_abort_event
from backend.models import (
    AssetReference,
    CompatibilityRun,
    Device,
    InspectionBranchRun,
    InspectionExplorationFamily,
    InspectionFamilyActionCoverage,
    InspectionFault,
    InspectionObservation,
    InspectionPageTemplate,
    InspectionProfile,
    InspectionRun,
    InspectionState,
    InspectionTransition,
    StoredAsset,
    SystemSetting,
    TestCase,
    User,
)
from backend.schemas import (
    InspectionBudgets,
    InspectionProfileCreate,
    InspectionRunCreate,
    InspectionRepresentativeUpdate,
    InspectionSelectionUpdate,
)


class InspectionApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.capacity_patch = patch(
            "backend.artifact_store.ensure_asset_capacity_for_new_run",
            return_value={"can_start": True},
        )
        self.capacity_patch.start()
        self.engine = create_engine(
            "sqlite://",
            connect_args={"check_same_thread": False},
        )
        SQLModel.metadata.create_all(self.engine)
        self.session = Session(self.engine)
        self.user = User(username="inspection-user", hashed_password="x")
        self.case = TestCase(name="inspection entry", steps=[], variables=[])
        self.device = Device(
            serial="inspection-android-1",
            platform="android",
            status="IDLE",
        )
        self.session.add(self.user)
        self.session.add(self.case)
        self.session.add(self.device)
        self.session.commit()
        self.session.refresh(self.user)
        self.session.refresh(self.case)

    def tearDown(self) -> None:
        for row in self.session.exec(select(InspectionRun)).all():
            if row.id is not None:
                discard_abort_event(row.id)
        self.session.close()
        self.capacity_patch.stop()

    def test_graph_action_records_use_latest_outcome_for_retried_action(self):
        transitions = [
            InspectionTransition(
                run_id=1,
                branch_run_id=1,
                from_state_id=10,
                sequence=1,
                action_type="click",
                action_key="open-product",
                status="LOCATOR_NOT_FOUND",
                failure_type="LOCATOR_NOT_FOUND",
                execution_disposition="FAILED",
            ),
            InspectionTransition(
                run_id=1,
                branch_run_id=1,
                from_state_id=10,
                sequence=2,
                action_type="click",
                action_key="open-product",
                status="PASS",
                execution_disposition="EXECUTED",
            ),
        ]

        records = _graph_action_records([], transitions)

        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["status"], "PASS")
        self.assertEqual(records[0]["failure_type"], "")
        self.assertEqual(records[0]["execution_disposition"], "EXECUTED")
        self.assertTrue(records[0]["invoked"])

    def test_coverage_scroll_budget_is_configurable_and_bounded(self):
        budgets = InspectionBudgets(max_coverage_scroll_actions=80)

        self.assertEqual(budgets.max_coverage_scroll_actions, 80)
        with self.assertRaises(ValidationError):
            InspectionBudgets(max_coverage_scroll_actions=1001)

    def test_run_list_summary_exposes_family_coverage_and_safe_replay(self):
        states = [
            InspectionState(
                run_id=1,
                branch_run_id=1,
                branch_key="authenticated",
                cluster_key="home",
                state_key="home",
                semantic_key="home",
                instance_anchor="home",
                identity_version=2,
                exploration_family_id=10,
                exploration_mode="FULL",
                expansion_status="EXPANDED",
                observation_count=2,
                stable_status="STABLE",
            ),
            InspectionState(
                run_id=1,
                branch_run_id=1,
                branch_key="authenticated",
                cluster_key="category-a",
                state_key="category-a",
                semantic_key="category-a",
                instance_anchor="category-a",
                identity_version=2,
                exploration_family_id=10,
                exploration_mode="DELTA_ONLY",
                expansion_status="EXPANDED",
                observation_count=1,
            ),
            InspectionState(
                run_id=1,
                branch_run_id=1,
                branch_key="authenticated",
                cluster_key="product",
                state_key="product",
                semantic_key="product",
                instance_anchor="product",
                identity_version=2,
                exploration_family_id=20,
                exploration_mode="FULL",
                expansion_status="QUEUED",
                observation_count=1,
            ),
        ]

        summary = _run_list_summary(states)

        self.assertEqual(
            summary["family_coverage"],
            {"total": 2, "representatives_expanded": 1, "ratio": 0.5},
        )
        self.assertEqual(summary["replay_eligible_count"], 3)
        self.assertEqual(summary["verified_path_count"], 1)
        self.assertEqual(summary["observed_replay_paths"], 2)
        self.assertEqual(summary["replay_paths"]["total"], 3)
        self.assertEqual(summary["replay_paths"]["full_path"], 3)

    def test_run_list_summary_distinguishes_unavailable_from_explicit_zero(self):
        legacy = _run_list_summary([], run_status="WARNING")
        self.assertFalse(legacy["summary_available"])
        self.assertEqual(
            legacy["summary_unavailable_reason"], "IDENTITY_V2_REQUIRED"
        )
        self.assertNotIn("replay_eligible_count", legacy)
        self.assertNotIn("replay_paths", legacy)

        no_evidence = InspectionState(
            run_id=1,
            branch_run_id=1,
            branch_key="authenticated",
            cluster_key="empty",
            state_key="empty",
            identity_version=2,
        )
        summary = _run_list_summary([no_evidence], run_status="WARNING")
        self.assertTrue(summary["summary_available"])
        self.assertEqual(summary["replay_eligible_count"], 0)
        self.assertEqual(summary["replay_paths"]["total"], 0)

        state = InspectionState(
            run_id=1,
            branch_run_id=1,
            branch_key="authenticated",
            cluster_key="home",
            state_key="home",
            identity_version=2,
            observation_count=1,
        )
        aborted = _run_list_summary([state], run_status="ABORTED")
        self.assertFalse(aborted["replay_source_eligible"])
        self.assertEqual(aborted["replay_paths"]["total"], 0)

    def test_run_creation_rejects_critical_asset_watermark(self):
        from backend.artifact_store import AssetCapacityExceeded

        self._enable()
        profile = self._create_profile()
        with (
            patch(
                "backend.artifact_store.ensure_asset_capacity_for_new_run",
                side_effect=AssetCapacityExceeded({"can_start": False, "used_percent": 95.1}),
            ),
            self.assertRaises(HTTPException) as context,
        ):
            create_run(
                InspectionRunCreate(
                    profile_id=profile.id,
                    device_serial=self.device.serial,
                    branches=["guest"],
                ),
                BackgroundTasks(),
                session=self.session,
                current_user=self.user,
            )
        self.assertEqual(context.exception.status_code, 507)

    def _enable(self) -> None:
        self.session.add(SystemSetting(key=FLAG_MODEL_INSPECTION, value="true"))
        self.session.commit()

    def test_aborted_frontier_status_wins_over_completion_timestamp(self):
        state = InspectionState(
            run_id=1,
            branch_run_id=1,
            branch_key="guest",
            cluster_key="aborted",
            state_key="aborted",
            expansion_status="ABORTED",
            expansion_completed_at=datetime.now(),
        )

        values = _state_frontier_values(state, run_status="ABORTED")

        self.assertEqual(values["expansion_status"], "ABORTED")

    def test_aborted_run_exposes_observations_but_not_replay_source(self):
        self._enable()
        profile = self._create_profile()
        created = create_run(
            InspectionRunCreate(
                profile_id=profile.id,
                device_serial=self.device.serial,
                branches=["guest"],
            ),
            BackgroundTasks(),
            session=self.session,
            current_user=self.user,
        )
        run = self.session.get(InspectionRun, created.id)
        branch = self.session.exec(
            select(InspectionBranchRun).where(
                InspectionBranchRun.run_id == created.id
            )
        ).one()
        state = InspectionState(
            run_id=created.id,
            branch_run_id=branch.id,
            branch_key="guest",
            cluster_key="home",
            state_key="home",
            semantic_key="home",
            identity_version=2,
            observation_count=1,
            expansion_status="ABORTED",
        )
        run.status = "ABORTED"
        branch.status = "ABORTED"
        self.session.add_all([run, branch, state])
        self.session.commit()

        graph = get_run_graph(
            created.id,
            session=self.session,
            current_user=self.user,
        )

        self.assertFalse(graph["replay_source_eligible"])
        self.assertEqual(graph["nodes"][0]["reachability_evidence"], "OBSERVED_ONCE")
        self.assertEqual(graph["nodes"][0]["replay_scope"], "NONE")
        self.assertEqual(graph["summary"]["replay_paths"]["total"], 0)
        with self.assertRaises(HTTPException) as context:
            list_replay_paths(
                created.id,
                branch_key="guest",
                session=self.session,
                current_user=self.user,
            )
        self.assertEqual(context.exception.status_code, 409)

    def _profile_payload(self) -> InspectionProfileCreate:
        branch = {
            "name": "业务线",
            "prepare_case_id": self.case.id,
            "entry_case_id": self.case.id,
            "ready_assertion": {
                "selector": "首页",
                "by": "description",
                "timeout": 5,
            },
        }
        return InspectionProfileCreate(
            name="无 ID 巡检",
            package_name="com.example.inspection",
            branches={
                "guest": {**branch, "name": "未登录"},
                "authenticated": {**branch, "name": "已登录"},
            },
            budgets={"duration_seconds": 300, "max_actions": 20},
        )

    def _create_profile(self):
        return create_profile(
            self._profile_payload(),
            session=self.session,
            current_user=self.user,
        )

    def test_feature_flag_defaults_to_hidden(self):
        with self.assertRaises(HTTPException) as context:
            list_profiles(session=self.session, current_user=self.user)
        self.assertEqual(context.exception.status_code, 404)

    def test_profile_schema_rejects_invalid_or_duplicate_rules(self):
        data = self._profile_payload().model_dump()
        data["input_rules"] = [
            {
                "id": "login",
                "content_desc_regex": "[",
                "value_source": "literal",
                "value": "demo",
            }
        ]
        with self.assertRaises(ValidationError):
            InspectionProfileCreate(**data)

        data = self._profile_payload().model_dump()
        data["safety_rules"] = [
            {"id": "same", "pattern": "first"},
            {"id": "same", "pattern": "second"},
        ]
        with self.assertRaises(ValidationError):
            InspectionProfileCreate(**data)

    def test_profile_and_run_capture_immutable_two_branch_snapshot(self):
        self._enable()
        profile = self._create_profile()
        tasks = BackgroundTasks()
        result = create_run(
            InspectionRunCreate(
                profile_id=profile.id,
                device_serial="inspection-android-1",
                branches=["guest", "authenticated"],
            ),
            tasks,
            session=self.session,
            current_user=self.user,
        )

        self.assertEqual(result.status, "PENDING")
        self.assertEqual(result.selected_branches, ["guest", "authenticated"])
        self.assertEqual(len(result.branches), 2)
        self.assertEqual(len(tasks.tasks), 1)
        stored = self.session.get(InspectionRun, result.id)
        self.assertEqual(
            set(stored.profile_snapshot["branches"]),
            {"guest", "authenticated"},
        )
        self.assertNotIn("user_id", stored.profile_snapshot)
        self.assertNotIn("id", stored.profile_snapshot)
        self.assertEqual(stored.profile_snapshot["graph_hierarchy_version"], 2)
        self.assertEqual(stored.profile_snapshot["graph_schema_version"], 8)
        self.assertIn("effective_features", stored.profile_snapshot)

    def test_run_duration_override_is_bounded_and_only_changes_run_snapshot(self):
        self._enable()
        profile = self._create_profile()
        result = create_run(
            InspectionRunCreate(
                profile_id=profile.id,
                device_serial=self.device.serial,
                branches=["guest"],
                duration_seconds=3600,
            ),
            BackgroundTasks(),
            session=self.session,
            current_user=self.user,
        )

        stored = self.session.get(InspectionRun, result.id)
        self.assertEqual(stored.profile_snapshot["budgets"]["duration_seconds"], 3600)
        self.assertEqual(
            stored.profile_snapshot["run_overrides"],
            {"duration_seconds": 3600},
        )
        stored_profile = self.session.get(InspectionProfile, profile.id)
        self.assertEqual(stored_profile.budgets["duration_seconds"], 300)
        with self.assertRaises(ValidationError):
            InspectionRunCreate(
                profile_id=profile.id,
                device_serial=self.device.serial,
                branches=["guest"],
                duration_seconds=3601,
            )

    def test_run_creation_rejects_non_idle_device(self):
        self._enable()
        profile = self._create_profile()
        self.device.status = "BUSY"
        self.device.lease_task_id = "fastbot:2"
        self.session.add(self.device)
        self.session.commit()

        with self.assertRaises(HTTPException) as context:
            create_run(
                InspectionRunCreate(
                    profile_id=profile.id,
                    device_serial=self.device.serial,
                    branches=["guest"],
                ),
                BackgroundTasks(),
                session=self.session,
                current_user=self.user,
            )
        self.assertEqual(context.exception.status_code, 409)

    def test_run_creation_rejects_pending_legacy_fastbot_lock(self):
        self._enable()
        profile = self._create_profile()
        with (
            patch(
                "backend.api.inspections.legacy_fastbot_device_locked",
                return_value=True,
            ),
            self.assertRaises(HTTPException) as context,
        ):
            create_run(
                InspectionRunCreate(
                    profile_id=profile.id,
                    device_serial=self.device.serial,
                    branches=["guest"],
                ),
                BackgroundTasks(),
                session=self.session,
                current_user=self.user,
            )
        self.assertEqual(context.exception.status_code, 409)

    def test_graph_and_regression_selection_only_accept_stable_semantic_state(self):
        self._enable()
        profile = self._create_profile()
        created = create_run(
            InspectionRunCreate(
                profile_id=profile.id,
                device_serial=self.device.serial,
                branches=["guest"],
            ),
            BackgroundTasks(),
            session=self.session,
            current_user=self.user,
        )
        branch = self.session.exec(select(InspectionBranchRun).where(InspectionBranchRun.run_id == created.id)).one()
        stable = InspectionState(
            run_id=created.id,
            branch_run_id=branch.id,
            branch_key="guest",
            cluster_key="cluster-stable",
            state_key="state-stable",
            activity=".Home",
            stable_status="STABLE",
            locator_quality="DESCRIPTION",
            screenshot_path=(f"inspection/{created.id}/guest/stable/screenshot.png"),
            xml_path=f"inspection/{created.id}/guest/stable/hierarchy.xml",
        )
        coordinate = InspectionState(
            run_id=created.id,
            branch_run_id=branch.id,
            branch_key="guest",
            cluster_key="cluster-coordinate",
            state_key="state-coordinate",
            activity=".Canvas",
            stable_status="STABLE",
            locator_quality="COORDINATE_ONLY",
        )
        self.session.add(stable)
        self.session.add(coordinate)
        self.session.flush()
        self.session.add(
            InspectionTransition(
                run_id=created.id,
                branch_run_id=branch.id,
                from_state_id=stable.id,
                to_state_id=coordinate.id,
                sequence=1,
                action_type="click",
                action_key="open-canvas",
                locator_candidates=[{"selector": "下一页", "by": "description"}],
                status="PASS",
            )
        )
        self.session.commit()

        selected = update_regression_selection(
            created.id,
            InspectionSelectionUpdate(state_ids=[stable.id]),
            session=self.session,
            current_user=self.user,
        )
        self.assertEqual(selected["state_ids"], [stable.id])
        self.assertTrue(self.session.get(InspectionState, stable.id).selected_for_regression)

        with self.assertRaises(HTTPException) as context:
            update_regression_selection(
                created.id,
                InspectionSelectionUpdate(state_ids=[coordinate.id]),
                session=self.session,
                current_user=self.user,
            )
        self.assertEqual(context.exception.status_code, 400)

        graph = get_run_graph(
            created.id,
            session=self.session,
            current_user=self.user,
        )
        self.assertEqual(len(graph["nodes"]), 2)
        self.assertEqual(len(graph["links"]), 1)
        stable_node = next(item for item in graph["nodes"] if item["state_id"] == stable.id)
        self.assertIn(f"/runs/{created.id}/assets", stable_node["thumbnail_url"])
        self.assertEqual(graph["stats"]["locator_methods"]["description"], 1)
        self.assertEqual(graph["schema_version"], 8)

    def test_graph_v3_and_observation_pagination_preserve_cycle_evidence(self):
        self._enable()
        profile = self._create_profile()
        created = create_run(
            InspectionRunCreate(
                profile_id=profile.id,
                device_serial=self.device.serial,
                branches=["guest"],
            ),
            BackgroundTasks(),
            session=self.session,
            current_user=self.user,
        )
        branch = self.session.exec(select(InspectionBranchRun).where(InspectionBranchRun.run_id == created.id)).one()
        template = InspectionPageTemplate(
            package_name="com.example.inspection",
            activity=".Home",
            activity_family="Home",
            page_role="HOME",
            fingerprint_version=2,
            template_key="template-home",
            structure_signature=["structure"],
            action_signature=["action"],
            anchor_signature=["anchor"],
        )
        self.session.add(template)
        self.session.flush()
        state = InspectionState(
            run_id=created.id,
            branch_run_id=branch.id,
            branch_key="guest",
            cluster_key="legacy-cluster",
            state_key="legacy-state",
            template_id=template.id,
            semantic_key="semantic-home",
            identity_version=2,
            observation_count=2,
            stable_status="STABLE",
            locator_quality="DESCRIPTION",
        )
        self.session.add(state)
        self.session.flush()
        first = InspectionObservation(
            run_id=created.id,
            branch_run_id=branch.id,
            state_id=state.id,
            template_id=template.id,
            sequence=1,
            capture_kind="DISCOVERY",
            exact_cluster_key="legacy-cluster",
            exact_replay_key="replay-1",
            exact_state_key="legacy-state-1",
            screenshot_phash="01",
            retention_class="HOT",
            asset_status="CLEANED",
            metadata_only=True,
        )
        second = InspectionObservation(
            run_id=created.id,
            branch_run_id=branch.id,
            state_id=state.id,
            template_id=template.id,
            sequence=2,
            capture_kind="CYCLE",
            exact_cluster_key="legacy-cluster",
            exact_replay_key="replay-2",
            exact_state_key="legacy-state-2",
            screenshot_phash="02",
            retention_class="WARM",
            asset_status="CLEANED",
            metadata_only=True,
            is_representative=True,
            match_confidence=0.98,
            original_width=1080,
            original_height=2412,
        )
        self.session.add(first)
        self.session.add(second)
        self.session.flush()
        state.representative_observation_id = second.id
        branch.root_state_id = state.id
        self.session.add(state)
        self.session.add(branch)
        transition = InspectionTransition(
            run_id=created.id,
            branch_run_id=branch.id,
            from_state_id=state.id,
            to_state_id=state.id,
            sequence=1,
            action_type="click",
            action_key="loop",
            status="PASS",
            topology_type="SELF_LOOP",
            source_observation_id=first.id,
            target_observation_id=second.id,
            traversal_count=4,
        )
        self.session.add(transition)
        run = self.session.get(InspectionRun, created.id)
        run.status = "WARNING"
        branch.status = "WARNING"
        self.session.add_all([run, branch])
        self.session.commit()

        page = list_state_observations(
            created.id,
            state.id,
            page=1,
            page_size=1,
            session=self.session,
            current_user=self.user,
        )
        self.assertEqual(page["total"], 2)
        self.assertEqual(page["items"][0].id, second.id)

        graph = get_run_graph(
            created.id,
            session=self.session,
            current_user=self.user,
        )
        self.assertEqual(graph["schema_version"], 8)
        self.assertEqual(graph["nodes"][0]["page_role"], "HOME")
        self.assertEqual(graph["nodes"][0]["representative_observation_id"], second.id)
        self.assertEqual(graph["nodes"][0]["display_label"], "P001")
        self.assertEqual(graph["nodes"][0]["page_title"], "首页")
        self.assertEqual(graph["nodes"][0]["image_width"], 1080)
        self.assertAlmostEqual(
            graph["nodes"][0]["image_aspect_ratio"],
            1080 / 2412,
            places=6,
        )
        self.assertEqual(
            graph["nodes"][0]["reachability_evidence"],
            "VERIFIED_TWICE",
        )
        self.assertEqual(graph["nodes"][0]["replay_eligibility"], "FULL")
        self.assertEqual(graph["nodes"][0]["replay_scope"], "FULL_PATH")
        self.assertFalse(graph["paths_included"])
        self.assertNotIn("first_path", graph["nodes"][0])
        graph_with_paths = get_run_graph(
            created.id,
            include_paths=True,
            session=self.session,
            current_user=self.user,
        )
        self.assertTrue(graph_with_paths["paths_included"])
        self.assertEqual(graph_with_paths["nodes"][0]["first_path"], [])
        self.assertEqual(graph["summary"]["reached_pages"], 1)
        self.assertEqual(graph["summary"]["replay_paths"]["verified_twice"], 1)
        self.assertEqual(graph["links"][0]["topology_type"], "SELF_LOOP")
        self.assertEqual(graph["links"][0]["traversal_count"], 4)
        self.assertEqual(graph["cycles"][0]["state_ids"], [state.id])

        selected = update_regression_selection(
            created.id,
            InspectionSelectionUpdate(observation_ids=[second.id]),
            session=self.session,
            current_user=self.user,
        )
        self.assertEqual(selected["state_ids"], [state.id])
        self.assertEqual(selected["observation_ids"], [second.id])

        representative = update_representative_observation(
            created.id,
            state.id,
            InspectionRepresentativeUpdate(observation_id=first.id),
            session=self.session,
            current_user=self.user,
        )
        self.assertEqual(representative.id, first.id)
        self.session.refresh(state)
        self.session.refresh(first)
        self.session.refresh(second)
        self.assertEqual(state.representative_observation_id, first.id)
        self.assertTrue(first.is_representative)
        self.assertFalse(second.is_representative)

    def test_replay_paths_api_is_paginated_and_keeps_branch_context(self):
        self._enable()
        profile = self._create_profile()
        created = create_run(
            InspectionRunCreate(
                profile_id=profile.id,
                device_serial=self.device.serial,
                branches=["guest"],
            ),
            BackgroundTasks(),
            session=self.session,
            current_user=self.user,
        )
        run = self.session.get(InspectionRun, created.id)
        branch = self.session.exec(
            select(InspectionBranchRun).where(
                InspectionBranchRun.run_id == created.id
            )
        ).one()
        run.status = "WARNING"
        branch.status = "WARNING"
        source_state = InspectionState(
            run_id=created.id,
            branch_run_id=branch.id,
            branch_key="guest",
            cluster_key="home",
            state_key="home",
            semantic_key="home",
            identity_version=2,
            observation_count=1,
        )
        self.session.add_all([run, branch, source_state])
        self.session.commit()
        plan = {
            "chains": [
                {
                    "chain_id": "chain-safe",
                    "path_key": "path-safe",
                    "prefix_path_key": "path-safe",
                    "name": "首页",
                    "endpoint_state_id": 1,
                    "evidence_level": "OBSERVED_ONCE",
                    "replay_eligibility": "SAFE_PREFIX",
                    "terminal_boundaries": [
                        {
                            "terminal_outcome": "SAFETY_BLOCKED",
                            "boundary_type": "SAFETY_BLOCKED",
                        }
                    ],
                    "first_path": [],
                    "checkpoints": [],
                    "depth": 0,
                }
            ]
        }
        with patch(
            "backend.api.inspections.build_replay_plan",
            return_value=plan,
        ):
            response = list_replay_paths(
                created.id,
                branch_key="guest",
                page=1,
                page_size=1,
                session=self.session,
                current_user=self.user,
            )
        self.assertEqual(response.total, 1)
        self.assertEqual(response.items[0].branch_key, "guest")
        self.assertEqual(response.items[0].replay_eligibility, "SAFE_PREFIX")
        self.assertEqual(
            response.items[0].replay_scope,
            "PREFIX_TO_SAFETY_BOUNDARY",
        )
        self.assertEqual(response.summary["safe_prefix"], 1)

    def test_run_graph_and_replay_plan_share_v3_scope_counts(self):
        self._enable()
        profile = self._create_profile()
        created = create_run(
            InspectionRunCreate(
                profile_id=profile.id,
                device_serial=self.device.serial,
                branches=["guest"],
            ),
            BackgroundTasks(),
            session=self.session,
            current_user=self.user,
        )
        run = self.session.get(InspectionRun, created.id)
        branch = self.session.exec(
            select(InspectionBranchRun).where(
                InspectionBranchRun.run_id == created.id
            )
        ).one()
        run.status = "WARNING"
        branch.status = "WARNING"
        self.session.add_all([run, branch])

        def add_state(name, *, path, stable_status="UNVERIFIED"):
            template = InspectionPageTemplate(
                package_name="com.example.inspection",
                activity=".MainActivity",
                activity_family="main",
                page_role="HOME" if name == "home" else "LIST",
                template_key=f"template-{name}",
                structure_signature=[f"structure-{name}"],
                action_signature=[f"action-{name}"],
                anchor_signature=[f"anchor-{name}"],
            )
            self.session.add(template)
            self.session.flush()
            state = InspectionState(
                run_id=created.id,
                branch_run_id=branch.id,
                branch_key="guest",
                cluster_key=f"cluster-{name}",
                state_key=f"state-{name}",
                semantic_key=name,
                identity_version=2,
                instance_anchor=f"instance-{name}",
                template_id=template.id,
                foreground_package="com.example.inspection",
                page_subtype="HOME" if name == "home" else "PRODUCT_LIST",
                observation_count=1,
                expansion_status="EXPANDED",
                stable_status=stable_status,
                first_path=path,
            )
            self.session.add(state)
            self.session.flush()
            observation = InspectionObservation(
                run_id=created.id,
                branch_run_id=branch.id,
                state_id=state.id,
                template_id=template.id,
                sequence=int(state.id),
                exact_cluster_key=state.cluster_key,
                exact_replay_key=name,
                exact_state_key=state.state_key,
                is_representative=True,
            )
            self.session.add(observation)
            self.session.flush()
            state.representative_observation_id = observation.id
            self.session.add(state)
            return state

        def step(source, target, action_key, *, risk_type=None):
            return {
                "action_type": "click",
                "action_key": action_key,
                "action_role": f"NAV:{target.upper()}",
                "action_role_key": f"role-{action_key}",
                "action_group_key": f"group-{action_key}",
                "locator_candidates": [
                    {"by": "description", "selector": action_key}
                ],
                "target_meta": {},
                "coordinate_only": False,
                "replayable": True,
                "risk_type": risk_type,
                "status": "PASS",
                "execution_disposition": "EXECUTED",
                "expected_source_semantic_key": source,
                "expected_target_semantic_key": target,
                "expected_source_signature": {
                    "package": "com.example.inspection",
                    "role": "HOME",
                    "instance_anchor": f"instance-{source}",
                },
                "expected_target_signature": {
                    "package": "com.example.inspection",
                    "role": "LIST",
                    "instance_anchor": f"instance-{target}",
                },
            }

        home = add_state("home", path=[])
        allowed_payment_step = step(
            "home", "checkout", "enter-checkout", risk_type="PAYMENT"
        )
        checkout = add_state("checkout", path=[allowed_payment_step])
        unstable_step = step("home", "unstable", "open-unstable")
        unstable = add_state(
            "unstable",
            path=[unstable_step],
            stable_status="UNSTABLE",
        )
        for target, transition_step in (
            (checkout, allowed_payment_step),
            (unstable, unstable_step),
        ):
            transition = InspectionTransition(
                run_id=created.id,
                branch_run_id=branch.id,
                from_state_id=home.id,
                to_state_id=target.id,
                action_type="click",
                action_key=transition_step["action_key"],
                action_role=transition_step["action_role"],
                action_role_key=transition_step["action_role_key"],
                action_group_key=transition_step["action_group_key"],
                locator_candidates=transition_step["locator_candidates"],
                status="PASS",
                risk_type=transition_step.get("risk_type"),
                execution_disposition="EXECUTED",
            )
            self.session.add(transition)
            self.session.flush()
            target.parent_state_id = home.id
            target.incoming_transition_id = transition.id
            self.session.add(target)
        blocked = InspectionTransition(
            run_id=created.id,
            branch_run_id=branch.id,
            from_state_id=checkout.id,
            action_type="click",
            action_key="submit-payment",
            action_role="COMMAND:PAY",
            locator_candidates=[{"by": "text", "selector": "立即支付"}],
            status="BLOCKED",
            risk_type="PAYMENT",
            replayable=False,
            execution_disposition="SKIPPED",
        )
        self.session.add(blocked)
        branch.root_state_id = home.id
        self.session.add(branch)
        self.session.commit()

        detail = get_run(
            created.id,
            session=self.session,
            current_user=self.user,
        )
        graph = get_run_graph(
            created.id,
            session=self.session,
            current_user=self.user,
        )
        paths = list_replay_paths(
            created.id,
            branch_key="guest",
            page=1,
            page_size=20,
            session=self.session,
            current_user=self.user,
        )

        expected = {
            "full_path": 1,
            "safe_prefix": 1,
            "diagnostic_only": 1,
        }
        for summary in (
            detail.summary["replay_paths"],
            graph["summary"]["replay_paths"],
            paths.summary,
        ):
            self.assertEqual(
                {key: summary[key] for key in expected},
                expected,
            )
            self.assertEqual(summary["replayable_count"], 2)
            self.assertEqual(summary["candidate_count"], 3)
        allowed_link = next(
            item
            for item in graph["links"]
            if item["action_key"] == "enter-checkout"
        )
        self.assertEqual(allowed_link["terminal_outcome"], "NONE")
        self.assertEqual(
            next(
                item
                for item in graph["nodes"]
                if item["state_id"] == checkout.id
            )["replay_scope"],
            "PREFIX_TO_SAFETY_BOUNDARY",
        )

    def test_graph_exposes_relation_metadata_and_hierarchy_roles(self):
        self._enable()
        profile = self._create_profile()
        created = create_run(
            InspectionRunCreate(
                profile_id=profile.id,
                device_serial=self.device.serial,
                branches=["guest"],
            ),
            BackgroundTasks(),
            session=self.session,
            current_user=self.user,
        )
        branch = self.session.exec(select(InspectionBranchRun).where(InspectionBranchRun.run_id == created.id)).one()
        root = InspectionState(
            run_id=created.id,
            branch_run_id=branch.id,
            branch_key="guest",
            cluster_key="root",
            state_key="root",
        )
        peer = InspectionState(
            run_id=created.id,
            branch_run_id=branch.id,
            branch_key="guest",
            cluster_key="peer",
            state_key="peer",
        )
        page = InspectionState(
            run_id=created.id,
            branch_run_id=branch.id,
            branch_key="guest",
            cluster_key="page",
            state_key="page",
        )
        viewport = InspectionState(
            run_id=created.id,
            branch_run_id=branch.id,
            branch_key="guest",
            cluster_key="viewport",
            state_key="viewport",
        )
        legacy_viewport = InspectionState(
            run_id=created.id,
            branch_run_id=branch.id,
            branch_key="guest",
            cluster_key="legacy-viewport",
            state_key="legacy-viewport",
            stable_status="VIEWPORT",
        )
        orphan = InspectionState(
            run_id=created.id,
            branch_run_id=branch.id,
            branch_key="guest",
            cluster_key="orphan",
            state_key="orphan",
        )
        self.session.add_all([root, peer, page, viewport, legacy_viewport, orphan])
        self.session.flush()
        peer.parent_state_id = None
        page.parent_state_id = peer.id
        viewport.parent_state_id = page.id
        legacy_viewport.parent_state_id = page.id
        transitions = [
            InspectionTransition(
                run_id=created.id,
                branch_run_id=branch.id,
                from_state_id=root.id,
                to_state_id=peer.id,
                sequence=1,
                action_type="click",
                action_key="tab-peer",
                status="PASS",
                relation_type="PEER",
                relation_confidence=0.93,
            ),
            InspectionTransition(
                run_id=created.id,
                branch_run_id=branch.id,
                from_state_id=peer.id,
                to_state_id=page.id,
                sequence=2,
                action_type="click",
                action_key="open-page",
                status="PASS",
                relation_type="CHILD",
                relation_confidence=0.99,
            ),
            InspectionTransition(
                run_id=created.id,
                branch_run_id=branch.id,
                from_state_id=page.id,
                to_state_id=viewport.id,
                sequence=3,
                action_type="scroll",
                action_key="scroll-page",
                status="PASS",
                relation_type="VIEWPORT",
                relation_confidence=1.0,
            ),
            InspectionTransition(
                run_id=created.id,
                branch_run_id=branch.id,
                from_state_id=page.id,
                to_state_id=legacy_viewport.id,
                sequence=4,
                action_type="scroll",
                action_key="legacy-scroll",
                status="PASS",
            ),
        ]
        self.session.add_all(transitions)
        self.session.flush()
        peer.incoming_transition_id = transitions[0].id
        page.incoming_transition_id = transitions[1].id
        viewport.incoming_transition_id = transitions[2].id
        legacy_viewport.incoming_transition_id = transitions[3].id
        branch.root_state_id = root.id
        self.session.add(branch)
        self.session.commit()

        graph = get_run_graph(
            created.id,
            session=self.session,
            current_user=self.user,
        )

        self.assertEqual(graph["hierarchy_version"], 2)
        roles = {item["state_id"]: item["hierarchy_role"] for item in graph["nodes"]}
        self.assertEqual(roles[root.id], "BRANCH_ROOT")
        self.assertEqual(roles[peer.id], "PEER")
        self.assertEqual(roles[page.id], "PAGE")
        self.assertEqual(roles[viewport.id], "VIEWPORT")
        self.assertEqual(roles[legacy_viewport.id], "VIEWPORT")
        self.assertEqual(roles[orphan.id], "ORPHAN")
        links = {item["action_key"]: item for item in graph["links"]}
        self.assertEqual(links["tab-peer"]["relation_type"], "PEER")
        self.assertEqual(links["tab-peer"]["relation_confidence"], 0.93)
        self.assertIsNone(links["legacy-scroll"]["relation_type"])
        self.assertIsNone(links["legacy-scroll"]["relation_confidence"])

    def test_graph_v4_and_families_expose_frontier_and_legacy_classification(self):
        self._enable()
        self.session.add(
            SystemSetting(
                key=FLAG_INSPECTION_EXPLORATION_FAMILY_CONVERGENCE,
                value="true",
            )
        )
        self.session.commit()
        profile = self._create_profile()
        created = create_run(
            InspectionRunCreate(
                profile_id=profile.id,
                device_serial=self.device.serial,
                branches=["guest"],
            ),
            BackgroundTasks(),
            session=self.session,
            current_user=self.user,
        )
        run = self.session.get(InspectionRun, created.id)
        run.status = "PASS"
        run.current_stage = "验证稳定路径"
        branch = self.session.exec(select(InspectionBranchRun).where(InspectionBranchRun.run_id == created.id)).one()
        state = InspectionState(
            run_id=created.id,
            branch_run_id=branch.id,
            branch_key="guest",
            cluster_key="legacy-home",
            state_key="legacy-home-v1",
            instance_anchor="home-instance",
            queued_at=datetime.now(),
        )
        self.session.add_all([run, state])
        self.session.flush()
        family = InspectionExplorationFamily(
            run_id=created.id,
            branch_run_id=branch.id,
            family_key="home-family",
            fingerprint_version=1,
            page_role="HOME",
            representative_state_id=state.id,
            signature={"role": "HOME"},
            member_count=1,
        )
        self.session.add(family)
        self.session.flush()
        state.exploration_family_id = family.id
        state.family_match_confidence = 0.98
        state.family_match_evidence = {"match_type": "EXACT"}
        self.session.add(state)
        transition = InspectionTransition(
            run_id=created.id,
            branch_run_id=branch.id,
            from_state_id=state.id,
            sequence=1,
            action_type="click",
            action_key="coordinate-action",
            status="LOCATOR_DRIFT",
            reason="页面像素已变化，拒绝使用采集时保存的坐标",
            coordinate_only=True,
        )
        self.session.add(transition)
        self.session.flush()
        self.session.add(
            InspectionFamilyActionCoverage(
                family_id=family.id,
                action_role_key="open-home",
                action_role="NAVIGATION",
                status="PENDING",
                source_state_id=state.id,
                source_transition_id=transition.id,
            )
        )
        rollout = self.session.exec(
            select(SystemSetting).where(SystemSetting.key == FLAG_INSPECTION_EXPLORATION_FAMILY_CONVERGENCE)
        ).one()
        rollout.value = "false"
        self.session.add(rollout)
        self.session.commit()

        with tempfile.TemporaryDirectory() as temp_root:
            action_dir = Path(temp_root) / "reports" / "inspection" / str(created.id) / "guest" / str(state.id)
            action_dir.mkdir(parents=True)
            (action_dir / "actions.json").write_text(
                json.dumps(
                    {
                        "actions": [
                            {
                                "action_key": "legacy-not-reached-1",
                                "status": "NOT_REACHED",
                            },
                            {
                                "action_key": "legacy-not-reached-2",
                                "status": "NOT_REACHED",
                            },
                            {
                                "action_key": "legacy-pass",
                                "status": "PASS",
                                "invoked": True,
                                "execution_disposition": "PENDING",
                            },
                            {
                                "action_key": "payment-blocked",
                                "status": "BLOCKED",
                                "risk_type": "PAYMENT",
                                "execution_disposition": "PENDING",
                            },
                            {
                                "action_key": "cancelled-action",
                                "status": "CANCELLED",
                                "execution_disposition": "NOT_REACHED",
                            },
                            {
                                "action_key": "unsafe-coordinate",
                                "status": "COORDINATE_UNSAFE",
                                "failure_type": "COORDINATE_UNSAFE",
                                "execution_disposition": "SKIPPED",
                                "coordinate_only": True,
                            },
                        ]
                    }
                ),
                encoding="utf-8",
            )
            with patch(
                "backend.inspection.engine.project_path",
                side_effect=lambda *parts: Path(temp_root).joinpath(*parts),
            ):
                graph = get_run_graph(
                    created.id,
                    session=self.session,
                    current_user=self.user,
                )
                families = list_run_families(
                    created.id,
                    session=self.session,
                    current_user=self.user,
                )
                detail = get_run(
                    created.id,
                    session=self.session,
                    current_user=self.user,
                )
                action_map = get_state_action_map(
                    created.id,
                    state.id,
                    session=self.session,
                    current_user=self.user,
                )

        self.assertEqual(graph["schema_version"], 8)
        self.assertEqual(graph["phase"], "验证稳定路径")
        self.assertEqual(
            graph["frontier"],
            {"queued": 0, "deferred": 1, "pending": 2},
        )
        node = graph["nodes"][0]
        self.assertEqual(node["instance_anchor"], "home-instance")
        self.assertEqual(node["exploration_family_id"], family.id)
        self.assertEqual(node["expansion_status"], "DEFERRED")
        self.assertEqual(node["pending_action_count"], 2)
        link = graph["links"][0]
        self.assertEqual(link["status"], "LOCATOR_DRIFT")
        self.assertEqual(link["execution_disposition"], "SKIPPED")
        self.assertEqual(link["failure_type"], "COORDINATE_STALE")
        self.assertEqual(link["topology_type"], "TERMINAL")
        self.assertEqual(graph["stats"]["topology"]["TERMINAL"], 1)
        self.assertEqual(graph["stats"]["coordinate_stale"], 1)
        self.assertEqual(graph["stats"]["coordinate_unsafe"], 1)
        self.assertEqual(graph["stats"]["blocked"], 1)
        self.assertEqual(graph["stats"]["risks"], {"PAYMENT": 1})
        self.assertEqual(graph["stats"]["cancelled"], 1)
        self.assertEqual(graph["stats"]["not_reached"], 2)
        self.assertEqual(graph["stats"]["terminal_unexecuted"], 3)
        self.assertEqual(graph["stats"]["actual_device_actions"], 1)
        self.assertEqual(
            next(item for item in action_map["actions"] if item["action_key"] == "legacy-pass")[
                "execution_disposition"
            ],
            "EXECUTED",
        )
        self.assertTrue(graph["effective_features"][FLAG_INSPECTION_EXPLORATION_FAMILY_CONVERGENCE])
        self.assertEqual(detail.frontier, graph["frontier"])
        self.assertEqual(detail.branches[0].frontier, graph["frontier"])
        self.assertEqual(families.schema_version, 8)
        self.assertTrue(families.effective_features[FLAG_INSPECTION_EXPLORATION_FAMILY_CONVERGENCE])
        self.assertEqual(families.frontier, graph["frontier"])
        self.assertEqual(len(families.items), 1)
        self.assertEqual(families.items[0].frontier["pending"], 2)
        self.assertEqual(
            families.items[0].action_coverage[0].action_role_key,
            "open-home",
        )

    def test_historical_graph_defaults_to_v1_without_hierarchy_roles(self):
        self._enable()
        profile = self._create_profile()
        created = create_run(
            InspectionRunCreate(
                profile_id=profile.id,
                device_serial=self.device.serial,
                branches=["guest"],
            ),
            BackgroundTasks(),
            session=self.session,
            current_user=self.user,
        )
        run = self.session.get(InspectionRun, created.id)
        historical_snapshot = dict(run.profile_snapshot or {})
        historical_snapshot.pop("graph_hierarchy_version", None)
        historical_snapshot.pop("graph_schema_version", None)
        historical_snapshot.pop("effective_features", None)
        historical_snapshot[FLAG_INSPECTION_EXPLORATION_FAMILY_CONVERGENCE] = "false"
        run.profile_snapshot = historical_snapshot
        self.session.add(
            SystemSetting(
                key=FLAG_INSPECTION_EXPLORATION_FAMILY_CONVERGENCE,
                value="true",
            )
        )
        branch = self.session.exec(select(InspectionBranchRun).where(InspectionBranchRun.run_id == created.id)).one()
        root = InspectionState(
            run_id=created.id,
            branch_run_id=branch.id,
            branch_key="guest",
            cluster_key="root",
            state_key="root",
        )
        peer = InspectionState(
            run_id=created.id,
            branch_run_id=branch.id,
            branch_key="guest",
            cluster_key="peer",
            state_key="peer",
        )
        self.session.add_all([root, peer])
        self.session.flush()
        transition = InspectionTransition(
            run_id=created.id,
            branch_run_id=branch.id,
            from_state_id=root.id,
            to_state_id=peer.id,
            sequence=1,
            action_type="click",
            action_key="tab-peer",
            status="PASS",
            relation_type="PEER",
            relation_confidence=0.97,
        )
        self.session.add(transition)
        self.session.flush()
        peer.parent_state_id = root.id
        peer.incoming_transition_id = transition.id
        branch.root_state_id = root.id
        self.session.add_all([run, branch, peer])
        self.session.commit()

        graph = get_run_graph(
            created.id,
            session=self.session,
            current_user=self.user,
        )

        self.assertEqual(graph["schema_version"], 8)
        self.assertEqual(graph["hierarchy_version"], 1)
        self.assertFalse(graph["effective_features"][FLAG_INSPECTION_EXPLORATION_FAMILY_CONVERGENCE])
        self.assertTrue(all(item["hierarchy_role"] is None for item in graph["nodes"]))
        self.assertEqual(graph["links"][0]["relation_type"], "PEER")

    def test_identity_v1_report_marks_summary_unavailable(self):
        self._enable()
        profile = self._create_profile()
        created = create_run(
            InspectionRunCreate(
                profile_id=profile.id,
                device_serial=self.device.serial,
                branches=["guest"],
            ),
            BackgroundTasks(),
            session=self.session,
            current_user=self.user,
        )
        run = self.session.get(InspectionRun, created.id)
        branch = self.session.exec(
            select(InspectionBranchRun).where(
                InspectionBranchRun.run_id == created.id
            )
        ).one()
        run.status = "WARNING"
        branch.status = "WARNING"
        state = InspectionState(
            run_id=created.id,
            branch_run_id=branch.id,
            branch_key="guest",
            cluster_key="legacy-home",
            state_key="legacy-home",
            identity_version=1,
            observation_count=1,
            first_path=[{"action_key": "legacy"}],
        )
        self.session.add_all([run, branch, state])
        self.session.commit()

        detail = get_run(
            created.id,
            session=self.session,
            current_user=self.user,
        )
        graph = get_run_graph(
            created.id,
            session=self.session,
            current_user=self.user,
        )

        self.assertFalse(detail.summary_available)
        self.assertEqual(
            detail.summary_unavailable_reason, "IDENTITY_V2_REQUIRED"
        )
        self.assertNotIn("replay_paths", detail.summary)
        self.assertFalse(detail.replay_source_eligible)
        self.assertIn("旧版", detail.replay_source_reason)
        self.assertFalse(graph["summary_available"])
        self.assertFalse(graph["summary"]["replay_paths"]["summary_available"])
        self.assertNotIn("total", graph["summary"]["replay_paths"])
        self.assertNotIn("first_path", graph["nodes"][0])
        with self.assertRaises(HTTPException) as context:
            list_replay_paths(
                created.id,
                branch_key="guest",
                session=self.session,
                current_user=self.user,
            )
        self.assertEqual(context.exception.status_code, 409)

    def test_delete_run_removes_records_artifacts_and_detaches_compatibility(self):
        self._enable()
        profile = self._create_profile()
        created = create_run(
            InspectionRunCreate(
                profile_id=profile.id,
                device_serial=self.device.serial,
                branches=["guest"],
            ),
            BackgroundTasks(),
            session=self.session,
            current_user=self.user,
        )
        branch = self.session.exec(select(InspectionBranchRun).where(InspectionBranchRun.run_id == created.id)).one()
        state = InspectionState(
            run_id=created.id,
            branch_run_id=branch.id,
            branch_key="guest",
            cluster_key="home",
            state_key="home-1",
            stable_status="STABLE",
        )
        self.session.add(state)
        self.session.flush()
        transition = InspectionTransition(
            run_id=created.id,
            branch_run_id=branch.id,
            from_state_id=state.id,
            to_state_id=state.id,
            action_type="click",
            action_key="noop",
            status="PASS",
        )
        self.session.add(transition)
        self.session.flush()
        fault = InspectionFault(
            run_id=created.id,
            branch_run_id=branch.id,
            state_id=state.id,
            transition_id=transition.id,
            fault_type="CRASH",
            signature="demo-crash",
            full_log_path=f"inspection/{created.id}/faults/1/device.log",
        )
        self.session.add(fault)
        self.session.flush()
        self.session.add_all(
            [
                StoredAsset(
                    id="inspection-state-asset",
                    logical_sha256="inspection-state-logical",
                    blob_sha256="inspection-state-blob",
                    media_type="application/xml",
                    storage_key="inspection/state.xml",
                    byte_size=1,
                ),
                StoredAsset(
                    id="inspection-fault-asset",
                    logical_sha256="inspection-fault-logical",
                    blob_sha256="inspection-fault-blob",
                    media_type="text/plain",
                    storage_key="inspection/fault.txt",
                    byte_size=1,
                ),
            ]
        )
        self.session.flush()
        self.session.add_all(
            [
                AssetReference(
                    asset_id="inspection-state-asset",
                    owner_type="inspection_state",
                    owner_id=int(state.id),
                    role="xml",
                    retention_class="PINNED",
                ),
                AssetReference(
                    asset_id="inspection-fault-asset",
                    owner_type="inspection_fault",
                    owner_id=int(fault.id),
                    role="full_log",
                    retention_class="PINNED",
                ),
            ]
        )
        self.session.add(
            CompatibilityRun(
                name="copied inspection baseline",
                source_type="inspection",
                inspection_run_id=created.id,
                inspection_state_ids=[state.id],
                new_package_id=1,
                status="PASS",
            )
        )
        run = self.session.get(InspectionRun, created.id)
        run.status = "PASS"
        run.current_stage = "完成"
        self.session.add(run)
        self.session.commit()

        report = get_run(
            created.id,
            session=self.session,
            current_user=self.user,
        )
        self.assertEqual(
            report.faults[0].full_log_asset_id,
            "inspection-fault-asset",
        )

        with tempfile.TemporaryDirectory() as temp_root:
            report_dir = Path(temp_root) / "reports" / "inspection" / str(created.id)
            report_dir.mkdir(parents=True)
            (report_dir / "state.xml").write_text("<hierarchy />", encoding="utf-8")
            with patch(
                "backend.api.inspections.project_path",
                side_effect=lambda *parts: Path(temp_root).joinpath(*parts),
            ):
                response = delete_run(
                    created.id,
                    session=self.session,
                    current_user=self.user,
                )

            self.assertFalse(report_dir.exists())

        self.assertTrue(response["success"])
        self.assertTrue(response["artifacts_deleted"])
        self.assertEqual(response["deleted_branches"], 1)
        self.assertEqual(response["deleted_states"], 1)
        self.assertEqual(response["deleted_transitions"], 1)
        self.assertEqual(response["deleted_faults"], 1)
        self.assertEqual(response["detached_compatibility_runs"], 1)
        state_reference = self.session.exec(
            select(AssetReference).where(
                AssetReference.owner_type == "inspection_state",
                AssetReference.owner_id == state.id,
            )
        ).one()
        fault_reference = self.session.exec(
            select(AssetReference).where(
                AssetReference.owner_type == "inspection_fault",
                AssetReference.owner_id == fault.id,
            )
        ).one()
        for reference in (state_reference, fault_reference):
            self.assertIsNotNone(reference.released_at)
            self.assertIsNotNone(reference.grace_until)
        self.assertIsNone(self.session.get(InspectionRun, created.id))
        self.assertEqual(
            self.session.exec(select(InspectionBranchRun).where(InspectionBranchRun.run_id == created.id)).all(),
            [],
        )
        self.assertEqual(
            self.session.exec(select(InspectionState).where(InspectionState.run_id == created.id)).all(),
            [],
        )
        self.assertEqual(
            self.session.exec(select(InspectionTransition).where(InspectionTransition.run_id == created.id)).all(),
            [],
        )
        self.assertEqual(
            self.session.exec(select(InspectionFault).where(InspectionFault.run_id == created.id)).all(),
            [],
        )
        compatibility = self.session.exec(select(CompatibilityRun)).one()
        self.assertIsNone(compatibility.inspection_run_id)
        self.assertEqual(compatibility.inspection_state_ids, [state.id])
        self.assertIsNone(get_abort_event(created.id))

    def test_delete_active_run_is_rejected_and_keeps_artifacts(self):
        self._enable()
        profile = self._create_profile()
        created = create_run(
            InspectionRunCreate(
                profile_id=profile.id,
                device_serial=self.device.serial,
                branches=["guest"],
            ),
            BackgroundTasks(),
            session=self.session,
            current_user=self.user,
        )

        with tempfile.TemporaryDirectory() as temp_root:
            report_dir = Path(temp_root) / "reports" / "inspection" / str(created.id)
            report_dir.mkdir(parents=True)
            with (
                patch(
                    "backend.api.inspections.project_path",
                    side_effect=lambda *parts: Path(temp_root).joinpath(*parts),
                ),
                self.assertRaises(HTTPException) as context,
            ):
                delete_run(
                    created.id,
                    session=self.session,
                    current_user=self.user,
                )

            self.assertEqual(context.exception.status_code, 400)
            self.assertTrue(report_dir.exists())

        self.assertIsNotNone(self.session.get(InspectionRun, created.id))
        branches = self.session.exec(select(InspectionBranchRun).where(InspectionBranchRun.run_id == created.id)).all()
        self.assertEqual(len(branches), 1)

    def test_delete_stale_cancelled_run_waits_for_owned_lease_then_succeeds(self):
        self._enable()
        profile = self._create_profile()
        created = create_run(
            InspectionRunCreate(
                profile_id=profile.id,
                device_serial=self.device.serial,
                branches=["guest"],
            ),
            BackgroundTasks(),
            session=self.session,
            current_user=self.user,
        )
        run = self.session.get(InspectionRun, created.id)
        run.status = "ABORTED"
        run.current_stage = "取消中"
        run.stop_reason = "用户取消"
        self.device.status = "BUSY"
        self.device.lease_kind = "inspection"
        self.device.lease_task_id = f"inspection:{created.id}"
        self.session.add_all([run, self.device])
        self.session.commit()

        with tempfile.TemporaryDirectory() as temp_root:
            report_dir = Path(temp_root) / "reports" / "inspection" / str(created.id)
            report_dir.mkdir(parents=True)
            with patch(
                "backend.api.inspections.project_path",
                side_effect=lambda *parts: Path(temp_root).joinpath(*parts),
            ):
                with self.assertRaises(HTTPException) as context:
                    delete_run(
                        created.id,
                        session=self.session,
                        current_user=self.user,
                    )
                self.assertEqual(context.exception.status_code, 400)
                self.assertTrue(report_dir.exists())

                self.device.status = "IDLE"
                self.device.lease_kind = None
                self.device.lease_task_id = None
                self.session.add(self.device)
                self.session.commit()
                response = delete_run(
                    created.id,
                    session=self.session,
                    current_user=self.user,
                )

        self.assertTrue(response["success"])
        self.assertTrue(response["artifacts_deleted"])
        self.assertIsNone(self.session.get(InspectionRun, created.id))
        self.assertIsNone(get_abort_event(created.id))

    def test_asset_resolver_rejects_traversal_and_symlink_escape(self):
        with tempfile.TemporaryDirectory() as temp_root:
            reports = Path(temp_root) / "reports"
            run_root = reports / "inspection" / "91"
            run_root.mkdir(parents=True)
            outside = Path(temp_root) / "secret.xml"
            outside.write_text("secret", encoding="utf-8")
            (run_root / "safe.xml").write_text("safe", encoding="utf-8")

            def fake_project_path(*parts):
                return Path(temp_root).joinpath(*parts)

            with patch(
                "backend.inspection.engine.project_path",
                side_effect=fake_project_path,
            ):
                safe = resolve_inspection_asset(
                    "inspection/91/safe.xml",
                    run_id=91,
                )
                self.assertEqual(safe.read_text(encoding="utf-8"), "safe")
                with self.assertRaises(ValueError):
                    resolve_inspection_asset("../secret.xml", run_id=91)
                if hasattr(os, "symlink"):
                    link = run_root / "escape.xml"
                    link.symlink_to(outside)
                    with self.assertRaises(ValueError):
                        resolve_inspection_asset(
                            "inspection/91/escape.xml",
                            run_id=91,
                        )
                    outside_run = Path(temp_root) / "outside-run"
                    outside_run.mkdir()
                    (outside_run / "secret.xml").write_text(
                        "secret",
                        encoding="utf-8",
                    )
                    run_link = reports / "inspection" / "92"
                    run_link.symlink_to(outside_run, target_is_directory=True)
                    with self.assertRaises(ValueError):
                        resolve_inspection_asset(
                            "inspection/92/secret.xml",
                            run_id=92,
                        )


if __name__ == "__main__":
    unittest.main()
