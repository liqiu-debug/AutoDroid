"""
Flaky 用例识别 + 执行结果对比（纯查询计算，不建新表）。

一、场景级 Flaky 评分公式（0-100，越高越不稳定）：

    score = 100 * (0.6 * flip_rate + 0.4 * failure_balance)

    - flip_rate       = 翻转次数 / (样本数 - 1)
                        翻转次数：把窗口内已完结执行按时间排序后，
                        PASS <-> 非PASS（FAIL/WARNING/ERROR）的状态切换计数。
    - failure_balance = 1 - |fail_rate - 0.5| * 2
                        失败率越接近 50% 越"时好时坏"；全过或全挂时为 0。

统计口径：
    - 仅统计已完结执行（PASS/FAIL/WARNING/ERROR），ABORTED/RUNNING 不参与；
    - 样本数 < min_samples（默认 5）的场景不参与排名，避免新场景误报；
    - score <= 0（全过或持续失败且无翻转）的场景不进入榜单。

二、步骤级 Flaky：对进入榜单的场景，将其窗口内各次执行的 TestResult 按
    (step_order, step_name) 对齐，统计"时好时坏"（既有通过又有失败）的步骤，
    评分公式与场景级一致；SKIP 状态的步骤出现不计入样本。

三、执行对比（build_execution_compare）：仅允许同 scenario_id 的两次执行对比，
    步骤按 step_order 对齐；状态变化分类：
    regressed（基准过、本次挂）/ fixed（基准挂、本次过）/
    still-failing（两次都挂）/ unchanged（两次都过）/
    added（仅本次存在）/ removed（仅基准存在）。
"""
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

from sqlmodel import Session, col, select

from backend.models import TestExecution, TestResult

# 参与 flaky 统计的已完结状态（ABORTED 属于人为终止，不反映稳定性）
FLAKY_COMPLETED_STATUSES = ("PASS", "FAIL", "WARNING", "ERROR")
# 执行/步骤级"失败样"状态
FAIL_LIKE_STATUSES = {"FAIL", "WARNING", "ERROR"}

FLIP_WEIGHT = 0.6
FAIL_BALANCE_WEIGHT = 0.4
DEFAULT_MIN_SAMPLES = 5


class ExecutionCompareError(Exception):
    """执行对比参数错误（由 API 层转换为对应的 HTTP 状态码）。"""

    def __init__(self, status_code: int, detail: str):
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


def _normalize_status(status: Any) -> str:
    return str(status or "").strip().upper()


def _is_fail_like(status: Any) -> bool:
    return _normalize_status(status) in FAIL_LIKE_STATUSES


def _count_flips(pass_sequence: List[bool]) -> int:
    return sum(1 for prev, curr in zip(pass_sequence, pass_sequence[1:]) if prev != curr)


def _flaky_score(*, total: int, fail_count: int, flip_count: int) -> float:
    if total <= 1:
        return 0.0
    flip_rate = flip_count / (total - 1)
    fail_rate = fail_count / total
    failure_balance = max(0.0, 1.0 - abs(fail_rate - 0.5) * 2)
    return round(100 * (FLIP_WEIGHT * flip_rate + FAIL_BALANCE_WEIGHT * failure_balance), 1)


def compute_flaky_report(
    session: Session,
    *,
    days: int = 30,
    limit: int = 20,
    min_samples: int = DEFAULT_MIN_SAMPLES,
    include_steps: bool = True,
    now: Optional[datetime] = None,
) -> Dict[str, Any]:
    """统计近 N 天各场景的 flaky 指标，返回 Top 列表（含可选的步骤级 Top）。"""
    now = now or datetime.now()
    window_start = now - timedelta(days=days)

    executions = session.exec(
        select(TestExecution)
        .where(TestExecution.start_time >= window_start)
        .where(col(TestExecution.status).in_(FLAKY_COMPLETED_STATUSES))
        .order_by(TestExecution.start_time, TestExecution.id)
    ).all()

    grouped: Dict[int, List[TestExecution]] = {}
    for execution in executions:
        grouped.setdefault(int(execution.scenario_id), []).append(execution)

    items: List[Dict[str, Any]] = []
    for scenario_id, rows in grouped.items():
        total = len(rows)
        if total < min_samples:
            continue
        pass_sequence = [not _is_fail_like(row.status) for row in rows]
        pass_count = sum(1 for passed in pass_sequence if passed)
        fail_count = total - pass_count
        flip_count = _count_flips(pass_sequence)
        score = _flaky_score(total=total, fail_count=fail_count, flip_count=flip_count)
        if score <= 0:
            # 全过或持续失败且无翻转，不属于 flaky
            continue
        last = rows[-1]
        items.append(
            {
                "scenario_id": scenario_id,
                "scenario_name": last.scenario_name or f"Scenario#{scenario_id}",
                "total": total,
                "pass_count": pass_count,
                "fail_count": fail_count,
                "pass_rate": round(pass_count / total * 100, 1),
                "flip_count": flip_count,
                "flip_rate": round(flip_count / (total - 1) * 100, 1) if total > 1 else 0.0,
                "score": score,
                "last_status": _normalize_status(last.status),
                "last_time": last.start_time,
            }
        )

    items.sort(key=lambda item: (item["score"], item["flip_count"], item["total"]), reverse=True)
    items = items[:limit]

    step_items: List[Dict[str, Any]] = []
    if include_steps and items:
        step_items = _compute_step_flaky(
            session,
            grouped=grouped,
            ranked_items=items,
            min_samples=min_samples,
            limit=limit,
        )

    return {
        "days": days,
        "min_samples": min_samples,
        "generated_at": now,
        "total_scenarios": len(grouped),
        "items": items,
        "step_items": step_items,
    }


def _compute_step_flaky(
    session: Session,
    *,
    grouped: Dict[int, List[TestExecution]],
    ranked_items: List[Dict[str, Any]],
    min_samples: int,
    limit: int,
) -> List[Dict[str, Any]]:
    """对进入榜单的场景做步骤级 flaky 统计（按 step_order + step_name 对齐）。"""
    scenario_names = {item["scenario_id"]: item["scenario_name"] for item in ranked_items}

    # execution_id -> (scenario_id, 时间序号)，用于给步骤出现排序
    exec_index: Dict[int, Tuple[int, int]] = {}
    exec_ids: List[int] = []
    for scenario_id in scenario_names:
        for seq, execution in enumerate(grouped.get(scenario_id, [])):
            if execution.id is None:
                continue
            exec_index[int(execution.id)] = (scenario_id, seq)
            exec_ids.append(int(execution.id))
    if not exec_ids:
        return []

    results = session.exec(
        select(TestResult).where(col(TestResult.execution_id).in_(exec_ids))
    ).all()

    buckets: Dict[Tuple[int, int, str], List[Tuple[int, str]]] = {}
    for result in results:
        meta = exec_index.get(int(result.execution_id))
        if not meta:
            continue
        status = _normalize_status(result.status)
        if status == "SKIP":
            continue
        scenario_id, seq = meta
        key = (scenario_id, int(result.step_order), str(result.step_name or ""))
        buckets.setdefault(key, []).append((seq, status))

    step_items: List[Dict[str, Any]] = []
    for (scenario_id, step_order, step_name), occurrences in buckets.items():
        occurrences.sort(key=lambda pair: pair[0])
        total = len(occurrences)
        if total < min_samples:
            continue
        pass_sequence = [status not in FAIL_LIKE_STATUSES for _, status in occurrences]
        pass_count = sum(1 for passed in pass_sequence if passed)
        fail_count = total - pass_count
        if pass_count == 0 or fail_count == 0:
            # 一直过或一直挂的步骤不属于"时好时坏"
            continue
        flip_count = _count_flips(pass_sequence)
        step_items.append(
            {
                "scenario_id": scenario_id,
                "scenario_name": scenario_names.get(scenario_id, f"Scenario#{scenario_id}"),
                "step_order": step_order,
                "step_name": step_name,
                "total": total,
                "pass_count": pass_count,
                "fail_count": fail_count,
                "flip_count": flip_count,
                "score": _flaky_score(total=total, fail_count=fail_count, flip_count=flip_count),
                "last_status": occurrences[-1][1],
            }
        )

    step_items.sort(key=lambda item: (item["score"], item["flip_count"], item["total"]), reverse=True)
    return step_items[:limit]


# --- 执行对比 ---


def _execution_meta(execution: TestExecution) -> Dict[str, Any]:
    duration: Optional[float] = None
    if execution.start_time and execution.end_time:
        duration = (execution.end_time - execution.start_time).total_seconds()
    elif execution.duration and execution.duration > 0:
        duration = float(execution.duration)
    return {
        "id": execution.id,
        "status": _normalize_status(execution.status),
        "start_time": execution.start_time,
        "end_time": execution.end_time,
        "duration": duration,
        "device_serial": execution.device_serial,
        "device_info": execution.device_info,
        "platform": execution.platform,
        "executor_name": execution.executor_name,
        "batch_name": execution.batch_name,
    }


def _step_side(result: TestResult) -> Dict[str, Any]:
    display = result.report_display if isinstance(result.report_display, dict) else {}
    return {
        "id": result.id,
        "step_name": result.step_name,
        "status": _normalize_status(result.status),
        "duration": float(result.duration or 0.0),  # 毫秒
        "error_message": result.error_message,
        "error_code": str(display.get("error_code") or "").strip() or None,
        "suggestion": str(display.get("suggestion") or "").strip() or None,
        "display_text": display.get("display_text"),
    }


def _classify_change(base_side: Optional[Dict[str, Any]], target_side: Optional[Dict[str, Any]]) -> str:
    if base_side is None and target_side is not None:
        return "added"
    if target_side is None and base_side is not None:
        return "removed"
    if base_side is None or target_side is None:
        return "unchanged"
    base_failed = base_side["status"] in FAIL_LIKE_STATUSES
    target_failed = target_side["status"] in FAIL_LIKE_STATUSES
    if base_failed and target_failed:
        return "still-failing"
    if not base_failed and target_failed:
        return "regressed"
    if base_failed and not target_failed:
        return "fixed"
    return "unchanged"


def _load_steps_by_order(session: Session, execution_id: int) -> Dict[int, TestResult]:
    rows = session.exec(
        select(TestResult)
        .where(TestResult.execution_id == execution_id)
        .order_by(TestResult.step_order)
    ).all()
    return {int(row.step_order): row for row in rows}


def build_execution_compare(session: Session, base_id: int, target_id: int) -> Dict[str, Any]:
    """对比同一场景的两次执行：执行级元信息 + 步骤级逐行对齐 diff。"""
    base = session.get(TestExecution, int(base_id))
    if not base:
        raise ExecutionCompareError(404, f"基准执行记录 {base_id} 不存在")
    target = session.get(TestExecution, int(target_id))
    if not target:
        raise ExecutionCompareError(404, f"目标执行记录 {target_id} 不存在")
    if int(base.scenario_id) != int(target.scenario_id):
        raise ExecutionCompareError(400, "两次执行必须属于同一场景才能对比")

    base_steps = _load_steps_by_order(session, int(base_id))
    target_steps = _load_steps_by_order(session, int(target_id))

    summary = {
        "regressed": 0,
        "fixed": 0,
        "still_failing": 0,
        "unchanged": 0,
        "added": 0,
        "removed": 0,
    }
    steps: List[Dict[str, Any]] = []
    for order in sorted(set(base_steps) | set(target_steps)):
        base_row = base_steps.get(order)
        target_row = target_steps.get(order)
        base_side = _step_side(base_row) if base_row else None
        target_side = _step_side(target_row) if target_row else None
        change = _classify_change(base_side, target_side)
        summary[change.replace("-", "_")] += 1

        duration_delta: Optional[float] = None
        if base_side and target_side:
            duration_delta = round(target_side["duration"] - base_side["duration"], 2)

        name_changed = bool(
            base_side and target_side and base_side["step_name"] != target_side["step_name"]
        )
        steps.append(
            {
                "step_order": order,
                "step_name": (target_side or base_side or {}).get("step_name") or "",
                "change": change,
                "name_changed": name_changed,
                "duration_delta": duration_delta,
                "base": base_side,
                "target": target_side,
            }
        )

    base_meta = _execution_meta(base)
    target_meta = _execution_meta(target)
    duration_delta: Optional[float] = None
    if base_meta["duration"] is not None and target_meta["duration"] is not None:
        duration_delta = round(target_meta["duration"] - base_meta["duration"], 2)

    return {
        "scenario_id": int(target.scenario_id),
        "scenario_name": target.scenario_name or base.scenario_name,
        "base": base_meta,
        "target": target_meta,
        "duration_delta": duration_delta,
        "summary": summary,
        "steps": steps,
    }
