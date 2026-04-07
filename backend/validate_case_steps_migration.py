"""Validate testcase.steps -> testcasestep migration quality.

Usage:
  python -m backend.validate_case_steps_migration
  python -m backend.validate_case_steps_migration --sample-limit 30
  python -m backend.validate_case_steps_migration --strict
"""
from __future__ import annotations

import argparse
import json
from collections import Counter
from typing import Any, Dict, List, Tuple

from sqlmodel import Session, select

from backend.database import engine
from backend.models import TestCase, TestCaseStep
from backend.step_contract import normalize_action
from backend.utils.pydantic_compat import dump_model


def _to_dict(step: Any) -> Dict[str, Any]:
    data = dump_model(step)
    return data if isinstance(data, dict) else {}


def _normalize_action_safe(action: Any) -> str:
    try:
        return normalize_action(action)
    except Exception:
        return f"INVALID:{action}"


def _legacy_actions(legacy_steps: List[Any]) -> List[str]:
    result: List[str] = []
    for step in legacy_steps or []:
        result.append(_normalize_action_safe(_to_dict(step).get("action")))
    return result


def _standard_actions(standard_steps: List[TestCaseStep]) -> List[str]:
    result: List[str] = []
    for step in standard_steps or []:
        result.append(_normalize_action_safe(step.action))
    return result


def validate(sample_limit: int = 20) -> Tuple[Dict[str, Any], List[Dict[str, Any]]]:
    legacy_counter: Counter = Counter()
    standard_counter: Counter = Counter()
    mismatch_samples: List[Dict[str, Any]] = []

    total_cases = 0
    total_legacy_steps = 0
    total_standard_steps = 0
    count_mismatch_cases = 0
    action_mismatch_cases = 0

    with Session(engine) as session:
        cases = session.exec(select(TestCase)).all()
        total_cases = len(cases)

        for case in cases:
            legacy_steps = list(case.steps or [])
            standard_steps = session.exec(
                select(TestCaseStep)
                .where(TestCaseStep.case_id == case.id)
                .order_by(TestCaseStep.order, TestCaseStep.id)
            ).all()

            legacy_actions = _legacy_actions(legacy_steps)
            standard_actions = _standard_actions(standard_steps)

            legacy_counter.update(legacy_actions)
            standard_counter.update(standard_actions)

            total_legacy_steps += len(legacy_steps)
            total_standard_steps += len(standard_steps)

            count_mismatch = len(legacy_steps) != len(standard_steps)
            action_mismatch = legacy_actions != standard_actions
            if count_mismatch:
                count_mismatch_cases += 1
            if action_mismatch:
                action_mismatch_cases += 1

            if (count_mismatch or action_mismatch) and len(mismatch_samples) < sample_limit:
                mismatch_samples.append(
                    {
                        "case_id": case.id,
                        "case_name": case.name,
                        "legacy_count": len(legacy_steps),
                        "standard_count": len(standard_steps),
                        "legacy_actions": legacy_actions,
                        "standard_actions": standard_actions,
                    }
                )

    summary = {
        "total_cases": total_cases,
        "total_legacy_steps": total_legacy_steps,
        "total_standard_steps": total_standard_steps,
        "count_mismatch_cases": count_mismatch_cases,
        "action_mismatch_cases": action_mismatch_cases,
        "legacy_action_distribution": dict(legacy_counter.most_common()),
        "standard_action_distribution": dict(standard_counter.most_common()),
        "invalid_legacy_actions": sum(
            count for action, count in legacy_counter.items() if action.startswith("INVALID:")
        ),
        "invalid_standard_actions": sum(
            count for action, count in standard_counter.items() if action.startswith("INVALID:")
        ),
    }
    return summary, mismatch_samples


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Validate testcase.steps and testcasestep migration consistency"
    )
    parser.add_argument(
        "--sample-limit",
        type=int,
        default=20,
        help="max mismatch samples to print",
    )
    parser.add_argument(
        "--strict",
        action="store_true",
        help="exit with code 1 if mismatch exists",
    )
    args = parser.parse_args()

    summary, mismatch_samples = validate(sample_limit=max(args.sample_limit, 1))
    print("validation summary:")
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    print("mismatch samples:")
    print(json.dumps(mismatch_samples, ensure_ascii=False, indent=2))

    if args.strict:
        if summary["count_mismatch_cases"] > 0 or summary["action_mismatch_cases"] > 0:
            raise SystemExit(1)


if __name__ == "__main__":
    main()
