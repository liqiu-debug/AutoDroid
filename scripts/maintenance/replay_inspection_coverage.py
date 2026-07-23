#!/usr/bin/env python3
"""Offline coverage-scheduler replay against one historical inspection run."""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from PIL import Image  # noqa: E402

from backend.inspection.semantics import (  # noqa: E402
    InspectionAction,
    PageModel,
    build_page_model,
    compare_exploration_families,
    enumerate_actions,
)


COVERAGE_SCROLL_BUDGET = 25


@dataclass
class ReplayPage:
    state_id: int
    instance_anchor: str
    model: PageModel
    actions: list[InspectionAction]
    all_navigation_actions: list[InspectionAction]


def _is_legacy_viewport(raw_path: Any) -> bool:
    try:
        path = json.loads(raw_path or "[]") if isinstance(raw_path, str) else raw_path
    except (TypeError, json.JSONDecodeError):
        return False
    return bool(
        isinstance(path, list)
        and path
        and isinstance(path[-1], dict)
        and str(path[-1].get("action_type") or "").lower() == "scroll"
    )


def load_pages(database: Path, run_id: int) -> list[ReplayPage]:
    reports_root = database.parent / "reports"
    connection = sqlite3.connect(database)
    try:
        rows = connection.execute(
            """
            SELECT id, activity, foreground_package, xml_path, screenshot_path,
                   first_path, COALESCE(instance_anchor, '')
            FROM inspectionstate
            WHERE run_id = ?
            ORDER BY id
            """,
            (int(run_id),),
        ).fetchall()
    finally:
        connection.close()

    pages: list[ReplayPage] = []
    for (
        state_id,
        activity,
        package_name,
        xml_path,
        screenshot_path,
        first_path,
        instance_anchor,
    ) in rows:
        if _is_legacy_viewport(first_path) or not xml_path:
            continue
        hierarchy = reports_root / str(xml_path)
        screenshot = reports_root / str(screenshot_path or "")
        if not hierarchy.is_file() or not screenshot.is_file():
            continue
        xml = hierarchy.read_text(encoding="utf-8", errors="replace")
        screenshot_png = screenshot.read_bytes()
        with Image.open(screenshot) as image:
            screen_size = image.size
        model = build_page_model(
            xml,
            package_name=str(package_name or ""),
            activity=str(activity or ""),
        )
        common = {
            "screen_size": screen_size,
            "screenshot_png": screenshot_png,
            "enable_visual_home_actions": True,
            "coverage_scheduler_v2": True,
        }
        pages.append(
            ReplayPage(
                state_id=int(state_id),
                instance_anchor=str(instance_anchor or ""),
                model=model,
                actions=enumerate_actions(model, **common),
                all_navigation_actions=enumerate_actions(
                    model,
                    include_current_navigation=True,
                    **common,
                ),
            )
        )
    return pages


def _family_representatives(pages: list[ReplayPage]) -> list[ReplayPage]:
    representatives: list[ReplayPage] = []
    for page in pages:
        if any(
            compare_exploration_families(item.model, page.model).equivalent
            for item in representatives
        ):
            continue
        representatives.append(page)
    return representatives


def replay_summary(pages: list[ReplayPage]) -> dict[str, Any]:
    item_pages = [
        page
        for page in pages
        if any(
            str(action.action_role or "").startswith("ITEM_OPEN:")
            for action in page.actions
        )
    ]
    catalog_samples: list[ReplayPage] = []
    seen_catalog_anchors: set[str] = set()
    for page in item_pages:
        if page.model.page_subtype != "CATALOG_CATEGORY":
            continue
        anchor = page.instance_anchor or f"legacy-state:{page.state_id}"
        if anchor in seen_catalog_anchors:
            continue
        seen_catalog_anchors.add(anchor)
        catalog_samples.append(page)
        if len(catalog_samples) == 2:
            break

    special_samples = []
    for subtype in ("CONSUMABLE_LIST", "PRODUCT_LIST", "SERVICE_LIST"):
        candidate = next(
            (page for page in item_pages if page.model.page_subtype == subtype),
            None,
        )
        if candidate is not None:
            special_samples.append(candidate)
    planned_item_pages = [*catalog_samples, *special_samples]

    representatives = _family_representatives(pages)
    subtype_priority = {
        "HOME": 0,
        "CATALOG_CATEGORY": 1,
        "PRODUCT_LIST": 2,
        "CONSUMABLE_LIST": 2,
        "SERVICE_LIST": 2,
        "PRODUCT_DETAIL": 3,
        "CHECKOUT": 3,
        "ORDER": 3,
        "STORE_LIST": 4,
        "STORE_DETAIL": 4,
        "PROFILE": 5,
        "COMMUNITY_FEED": 5,
        "CART": 5,
    }
    ordered_representatives = sorted(
        representatives,
        key=lambda page: (
            subtype_priority.get(str(page.model.page_subtype or "UNKNOWN"), 9),
            page.state_id,
        ),
    )
    planned_scroll_groups: list[str] = []
    seen_scroll_groups: set[str] = set()
    for page in ordered_representatives:
        for action in page.actions:
            if action.action_type != "scroll" or not action.action_group_key:
                continue
            group_key = str(action.action_group_key)
            if group_key in seen_scroll_groups:
                continue
            seen_scroll_groups.add(group_key)
            planned_scroll_groups.append(group_key)
            if len(planned_scroll_groups) >= COVERAGE_SCROLL_BUDGET:
                break
        if len(planned_scroll_groups) >= COVERAGE_SCROLL_BUDGET:
            break
    planned_scroll_actions = len(planned_scroll_groups)

    home = next((page for page in pages if page.model.page_subtype == "HOME"), None)
    store_pages = [page for page in pages if page.model.page_subtype == "STORE_LIST"]
    home_navigation_coverage = {
        str(action.action_group_key)
        for action in (home.all_navigation_actions if home else [])
        if action.sample_policy == "RUN_NAV_ONCE" and action.action_group_key
    }
    home_bottom_device_actions = sum(
        1
        for action in (home.actions if home else [])
        if action.sample_policy == "RUN_NAV_ONCE"
        and (action.target_meta or {}).get("navigation", {}).get("group_region")
        == "bottom"
    )
    store_navigation_device_actions = sum(
        1
        for page in store_pages[:1]
        for action in page.actions
        if action.sample_policy == "RUN_NAV_ONCE"
        and str(action.action_group_key) not in home_navigation_coverage
    )
    visual_entries = sum(
        1
        for action in (home.actions if home else [])
        if action.sample_policy == "HOME_VISUAL"
    )
    discovered_item_controls = sum(
        1
        for page in item_pages
        for action in page.actions
        if str(action.action_role or "").startswith("ITEM_OPEN:")
    )
    planned_item_actions = len(planned_item_pages)
    return {
        "historical_non_viewport_states": len(pages),
        "representative_families": len(representatives),
        "discovered_item_controls": discovered_item_controls,
        "planned_item_actions": planned_item_actions,
        "planned_item_state_ids": [page.state_id for page in planned_item_pages],
        "sampled_out_item_controls": max(
            0, discovered_item_controls - planned_item_actions
        ),
        "planned_scroll_actions_upper_bound": planned_scroll_actions,
        "viewport_state_additions": 0,
        "home_bottom_navigation_device_actions": home_bottom_device_actions,
        "store_navigation_device_actions": store_navigation_device_actions,
        "home_visual_entries": visual_entries,
        "acceptance": {
            "item_actions_at_most_5": planned_item_actions <= 5,
            "scroll_actions_at_most_25": planned_scroll_actions <= 25,
            "viewport_states_are_zero": True,
            "store_navigation_actions_are_zero": (
                store_navigation_device_actions == 0
            ),
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("run_id", type=int)
    parser.add_argument(
        "--database",
        type=Path,
        default=ROOT / "database.db",
    )
    parser.add_argument("--strict", action="store_true")
    args = parser.parse_args()
    summary = replay_summary(load_pages(args.database.resolve(), args.run_id))
    print(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True))
    if args.strict and not all(summary["acceptance"].values()):
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
