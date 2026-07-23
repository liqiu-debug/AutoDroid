"""Read-only, manifest-based coverage acceptance for Haier Mall inspections."""

from __future__ import annotations

import html
import re
import sqlite3
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Optional, Sequence


MANIFEST_VERSION = "haier-mall-v1"
SUCCESSFUL_EDGE_STATUSES = frozenset({"PASS", "SUCCESS"})
HAIER_PACKAGE_NAMES = frozenset({"com.ehaier.zgq.shop.mall"})
ACCEPTABLE_RUN_STATUSES = frozenset({"PASS", "SUCCESS", "WARNING"})
SQLITE_MAX_INTEGER = (1 << 63) - 1


class CoverageAuditError(RuntimeError):
    """Raised when evidence cannot be loaded or the audit input is invalid."""


@dataclass(frozen=True)
class StateEvidence:
    id: int
    run_id: int
    branch_run_id: int
    page_subtype: str
    semantic_key: str = ""
    instance_anchor: str = ""
    activity: str = ""
    foreground_package: str = ""
    xml_path: str = ""
    xml_text: Optional[str] = None


@dataclass(frozen=True)
class TransitionEvidence:
    id: int
    run_id: int
    branch_run_id: int
    from_state_id: int
    to_state_id: Optional[int]
    action_type: str = ""
    action_key: str = ""
    action_role: str = ""
    execution_disposition: str = ""
    status: str = ""
    failure_type: str = ""
    risk_type: str = ""
    reason: str = ""


@dataclass(frozen=True)
class StateMatcher:
    subtypes: tuple[str, ...] = ()
    xml_patterns: tuple[str, ...] = ()
    xml_exclude_patterns: tuple[str, ...] = ()

    def matches(self, state: StateEvidence) -> bool:
        subtype = (state.page_subtype or "UNKNOWN").upper()
        if self.subtypes and subtype not in {item.upper() for item in self.subtypes}:
            return False
        if self.xml_patterns:
            if state.xml_text is None:
                return False
            if not all(re.search(pattern, state.xml_text, re.I) for pattern in self.xml_patterns):
                return False
        if state.xml_text is not None and any(
            re.search(pattern, state.xml_text, re.I)
            for pattern in self.xml_exclude_patterns
        ):
            return False
        return True


@dataclass(frozen=True)
class ManifestItem:
    key: str
    label: str
    weight: float
    required: bool
    kind: str
    matcher: Optional[StateMatcher] = None
    path: tuple[StateMatcher, ...] = ()
    edge_role_patterns: tuple[str, ...] = ()
    alternative_paths: tuple[tuple[StateMatcher, ...], ...] = ()
    alternative_edge_role_patterns: tuple[tuple[str, ...], ...] = ()
    alternative_path_labels: tuple[str, ...] = ()
    description: str = ""


@dataclass(frozen=True)
class ItemResult:
    key: str
    label: str
    weight: float
    required: bool
    covered: bool
    evidence_state_ids: tuple[int, ...] = ()
    evidence_transition_ids: tuple[int, ...] = ()
    detail: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "label": self.label,
            "weight": self.weight,
            "required": self.required,
            "covered": self.covered,
            "evidence_state_ids": list(self.evidence_state_ids),
            "evidence_transition_ids": list(self.evidence_transition_ids),
            "detail": self.detail,
        }


@dataclass(frozen=True)
class CoverageReport:
    run_id: int
    database: str
    run_name: str
    package_name: str
    run_status: str
    threshold: float
    items: tuple[ItemResult, ...]
    state_count: int
    transition_count: int
    xml_loaded_count: int
    xml_missing_count: int
    manifest_version: str = MANIFEST_VERSION

    @property
    def total_weight(self) -> float:
        return sum(item.weight for item in self.items)

    @property
    def covered_weight(self) -> float:
        return sum(item.weight for item in self.items if item.covered)

    @property
    def weighted_coverage(self) -> float:
        total = self.total_weight
        return self.covered_weight / total if total else 0.0

    @property
    def required_passed(self) -> bool:
        return all(item.covered for item in self.items if item.required)

    @property
    def mandatory_core_passed(self) -> bool:
        return self.required_passed

    @property
    def threshold_passed(self) -> bool:
        return self.weighted_coverage >= self.threshold

    @property
    def run_status_acceptable(self) -> bool:
        return self.run_status.upper() in ACCEPTABLE_RUN_STATUSES

    @property
    def passed(self) -> bool:
        return (
            self.required_passed
            and self.threshold_passed
            and self.run_status_acceptable
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "manifest_version": self.manifest_version,
            "run_id": self.run_id,
            "database": self.database,
            "run": {
                "name": self.run_name,
                "package_name": self.package_name,
                "status": self.run_status,
            },
            "evidence": {
                "state_count": self.state_count,
                "transition_count": self.transition_count,
                "xml_loaded_count": self.xml_loaded_count,
                "xml_missing_count": self.xml_missing_count,
            },
            "summary": {
                "covered_weight": round(self.covered_weight, 4),
                "total_weight": round(self.total_weight, 4),
                "weighted_coverage": round(self.weighted_coverage, 6),
                "threshold": self.threshold,
                "threshold_passed": self.threshold_passed,
                "required_passed": self.required_passed,
                "mandatory_core_passed": self.mandatory_core_passed,
                "run_status_acceptable": self.run_status_acceptable,
                "passed": self.passed,
                "missing_required_keys": [
                    item.key
                    for item in self.items
                    if item.required and not item.covered
                ],
                "missing_mandatory_keys": [
                    item.key
                    for item in self.items
                    if item.required and not item.covered
                ],
            },
            "items": [item.to_dict() for item in self.items],
        }


HOME = StateMatcher(subtypes=("HOME",))
CATEGORY = StateMatcher(subtypes=("CATALOG_CATEGORY",))
SEARCH = StateMatcher(subtypes=("SEARCH",))
PROFILE = StateMatcher(subtypes=("PROFILE",))
SERVICE_LIST = StateMatcher(
    subtypes=("SERVICE_LIST",),
    xml_patterns=(r"(?:深度清洗|原厂服务|家电维修|家电安装|清洗服务)",),
)
SERVICE_DETAIL = StateMatcher(
    subtypes=("SERVICE_DETAIL", "PRODUCT_DETAIL"),
    # "上门服务" also appears in ordinary goods after-sales copy. Keep the
    # matcher aligned with the semantic classifier's service-specific cues.
    xml_patterns=(
        r"(?:家生活服务|原厂服务|深度清洗|清洗服务|延保|延长保修|服务套餐)",
    ),
)
PHYSICAL_PRODUCT_DETAIL = StateMatcher(
    subtypes=("PRODUCT_DETAIL",),
    xml_patterns=(
        r"(?:Haier/海尔|Casarte/卡萨帝|Leader/统帅|海尔)",
        r"(?:一级能耗|商品参数|国家补贴|换新补贴|正品保障|风冷无霜)",
    ),
    xml_exclude_patterns=(
        r"(?:家生活服务|原厂服务|深度清洗|清洗服务|延保|延长保修|服务套餐)",
    ),
)
PHYSICAL_INLINE_SPEC_DETAIL = StateMatcher(
    subtypes=PHYSICAL_PRODUCT_DETAIL.subtypes,
    xml_patterns=(*PHYSICAL_PRODUCT_DETAIL.xml_patterns, r"(?:已选|规格)"),
    xml_exclude_patterns=PHYSICAL_PRODUCT_DETAIL.xml_exclude_patterns,
)
CHECKOUT_STATE = StateMatcher(subtypes=("CHECKOUT",), xml_patterns=(r"提交订单",))
CASHIER_STATE = StateMatcher(subtypes=("CASHIER",), xml_patterns=(r"海尔收银台",))


def _state_item(
    key: str,
    label: str,
    weight: float,
    matcher: StateMatcher,
    *,
    required: bool = True,
) -> ManifestItem:
    return ManifestItem(key, label, weight, required, "state", matcher=matcher)


def _path_item(
    key: str,
    label: str,
    weight: float,
    path: tuple[StateMatcher, ...],
    *,
    roles: tuple[str, ...] = (),
    required: bool = True,
) -> ManifestItem:
    return ManifestItem(
        key,
        label,
        weight,
        required,
        "path",
        path=path,
        edge_role_patterns=roles,
    )


HAIER_COVERAGE_MANIFEST: tuple[ManifestItem, ...] = (
    ManifestItem(
        key="home_five_tabs",
        label="首页五底栏",
        weight=2.0,
        required=True,
        kind="bottom_tabs",
        description="首页及分类、许愿池、购物车、我的四个真实导航目的地",
    ),
    ManifestItem(
        key="category_search_flow",
        label="分类到搜索",
        weight=1.25,
        required=True,
        kind="path",
        path=(CATEGORY, SEARCH),
        description="分类页通过真实 transition 到搜索页",
    ),
    ManifestItem(
        key="physical_checkout_safety_flow",
        label="实物详情到收银台安全边界",
        weight=3.0,
        required=True,
        kind="payment_safety_path",
        path=(
            PHYSICAL_PRODUCT_DETAIL,
            StateMatcher(subtypes=("PURCHASE_OPTIONS",)),
            CHECKOUT_STATE,
            CASHIER_STATE,
        ),
        edge_role_patterns=(r"BUY_NOW", r"(?:BUY_NOW|CHECKOUT)", r"PLACE_ORDER"),
        alternative_paths=((PHYSICAL_INLINE_SPEC_DETAIL, CHECKOUT_STATE, CASHIER_STATE),),
        alternative_edge_role_patterns=((r"(?:BUY_NOW|CHECKOUT)", r"PLACE_ORDER"),),
        alternative_path_labels=("inline selected specification",),
        description="实物商品详情 -> 规格 -> CHECKOUT -> CASHIER，最终付款必须拦截",
    ),
    _state_item("service_list", "服务列表", 1.0, SERVICE_LIST),
    _path_item(
        "service_detail_flow",
        "服务列表到服务详情",
        1.5,
        (SERVICE_LIST, SERVICE_DETAIL),
        roles=(r"ITEM_OPEN(?::.*)?",),
    ),
    _path_item(
        "product_orders_flow",
        "商品订单中心",
        1.25,
        (
            PROFILE,
            StateMatcher(
                subtypes=("ORDER", "PRODUCT_LIST"),
                xml_patterns=(r"待付款", r"(?:待发货|待收货)"),
            ),
        ),
    ),
    _path_item(
        "settings_address_flow",
        "设置到收货地址",
        1.5,
        (
            PROFILE,
            StateMatcher(subtypes=("SETTINGS",), xml_patterns=(r"收货地址",)),
            StateMatcher(subtypes=("ADDRESS_LIST",), xml_patterns=(r"收货地址",)),
        ),
    ),
    _path_item(
        "store_detail_flow",
        "门店列表到门店详情",
        1.5,
        (
            StateMatcher(subtypes=("STORE_LIST",)),
            StateMatcher(subtypes=("STORE_DETAIL",)),
        ),
        roles=(r"STORE_OPEN",),
    ),
    _path_item(
        "member_benefits_flow",
        "会员权益",
        1.25,
        (
            PROFILE,
            StateMatcher(xml_patterns=(r"我的权益", r"(?:积分|优惠券|卡包)")),
        ),
    ),
    _path_item(
        "favorites_flow",
        "商品收藏",
        1.0,
        (PROFILE, StateMatcher(xml_patterns=(r"商品收藏", r"(?:管理|全部\()"))),
    ),
    _path_item(
        "history_flow",
        "历史浏览",
        1.0,
        (PROFILE, StateMatcher(xml_patterns=(r"历史浏览", r"商品浏览"))),
    ),
    _path_item(
        "wish_pool_content",
        "许愿池内容",
        1.25,
        (
            StateMatcher(
                subtypes=("COMMUNITY_FEED",),
                xml_patterns=(r"许愿池", r"(?:官方|精选|参与话题|获奖信息)"),
            ),
            StateMatcher(xml_patterns=(r"来说点什么", r"(?:官方|精选|参与话题)")),
        ),
        roles=(r"ITEM_OPEN(?::.*)?",),
    ),
    _state_item(
        "cart",
        "购物车内容",
        0.75,
        StateMatcher(subtypes=("CART",), xml_patterns=(r"购物车",)),
        required=False,
    ),
    _path_item(
        "search_result_flow",
        "搜索结果",
        0.75,
        (SEARCH, StateMatcher(subtypes=("CATALOG_CATEGORY", "PRODUCT_LIST"))),
        roles=(r"SEARCH_SUGGESTION",),
        required=False,
    ),
    _path_item(
        "address_edit_flow",
        "地址编辑",
        0.75,
        (
            StateMatcher(subtypes=("ADDRESS_LIST",)),
            StateMatcher(subtypes=("ADDRESS_FORM",)),
        ),
        required=False,
    ),
    _path_item(
        "store_appointment_flow",
        "门店预约",
        0.75,
        (
            StateMatcher(subtypes=("STORE_DETAIL",)),
            StateMatcher(subtypes=("APPOINTMENT_LIST",)),
        ),
        required=False,
    ),
    _state_item(
        "consumables",
        "耗材专区",
        0.5,
        StateMatcher(subtypes=("CONSUMABLE_LIST",)),
        required=False,
    ),
    _state_item(
        "service_orders",
        "服务订单",
        0.5,
        StateMatcher(subtypes=("ORDER",), xml_patterns=(r"服务订单",)),
        required=False,
    ),
)


def _normalise_xml_text(raw_xml: str) -> Optional[str]:
    values: list[str] = []
    try:
        root = ET.fromstring(raw_xml)
    except ET.ParseError:
        return None
    for node in root.iter():
        for name in ("text", "content-desc", "resource-id"):
            value = node.attrib.get(name)
            if value:
                values.append(value)
    return html.unescape("\n".join(values)).replace("\u200b", "")


def _resolve_xml_path(
    database: Path,
    stored_path: str,
    run_id: int,
) -> Optional[Path]:
    if not stored_path:
        return None
    path = Path(stored_path).expanduser()
    candidates = (path,) if path.is_absolute() else (
        database.parent / "reports" / path,
        database.parent / path,
    )
    run_root = (database.parent / "reports" / "inspection" / str(run_id)).resolve()
    for candidate in candidates:
        resolved = candidate.resolve()
        try:
            resolved.relative_to(run_root)
        except ValueError:
            continue
        if resolved.is_file():
            return resolved
    return None


def _read_xml_evidence(
    database: Path,
    stored_path: str,
    run_id: int,
) -> Optional[str]:
    path = _resolve_xml_path(database, stored_path, run_id)
    if path is None:
        return None
    try:
        return _normalise_xml_text(path.read_text(encoding="utf-8", errors="replace"))
    except OSError:
        return None


def _read_only_connection(database: Path) -> sqlite3.Connection:
    resolved = database.expanduser().resolve()
    if not resolved.is_file():
        raise CoverageAuditError(f"database does not exist: {resolved}")
    wal_path = Path(f"{resolved}-wal")
    shm_path = Path(f"{resolved}-shm")
    if wal_path.is_file() and wal_path.stat().st_size > 0 and not shm_path.is_file():
        raise CoverageAuditError(
            "refusing read-only WAL audit without an existing -shm sidecar; "
            "SQLite would create one"
        )
    try:
        connection = sqlite3.connect(f"{resolved.as_uri()}?mode=ro", uri=True)
    except sqlite3.Error as exc:
        raise CoverageAuditError(f"cannot open database read-only: {exc}") from exc
    connection.row_factory = sqlite3.Row
    return connection


def load_run_evidence(
    database: Path | str,
    run_id: int,
) -> tuple[dict[str, str], list[StateEvidence], list[TransitionEvidence]]:
    """Load one run without importing the application's mutating DB engine."""

    if not 1 <= int(run_id) <= SQLITE_MAX_INTEGER:
        raise CoverageAuditError("run_id must be a positive SQLite 64-bit integer")
    database_path = Path(database).expanduser().resolve()
    connection = _read_only_connection(database_path)
    try:
        connection.execute("BEGIN")
        run = connection.execute(
            "SELECT id, name, package_name, status FROM inspectionrun WHERE id = ?",
            (int(run_id),),
        ).fetchone()
        if run is None:
            raise CoverageAuditError(f"inspection run {run_id} does not exist")

        state_rows = connection.execute(
            """
            SELECT id, run_id, branch_run_id, page_subtype, semantic_key,
                   instance_anchor, activity, foreground_package, xml_path
            FROM inspectionstate
            WHERE run_id = ?
            ORDER BY id
            """,
            (int(run_id),),
        ).fetchall()
        transition_rows = connection.execute(
            """
            SELECT id, run_id, branch_run_id, from_state_id, to_state_id,
                   action_type, action_key, action_role, execution_disposition,
                   status, failure_type, risk_type, reason
            FROM inspectiontransition
            WHERE run_id = ?
            ORDER BY id
            """,
            (int(run_id),),
        ).fetchall()
    except sqlite3.Error as exc:
        raise CoverageAuditError(f"invalid inspection database schema: {exc}") from exc
    finally:
        connection.close()

    def text(row: sqlite3.Row, name: str) -> str:
        return str(row[name] or "")

    states = [
        StateEvidence(
            id=int(row["id"]),
            run_id=int(row["run_id"]),
            branch_run_id=int(row["branch_run_id"]),
            page_subtype=text(row, "page_subtype"),
            semantic_key=text(row, "semantic_key"),
            instance_anchor=text(row, "instance_anchor"),
            activity=text(row, "activity"),
            foreground_package=text(row, "foreground_package"),
            xml_path=text(row, "xml_path"),
            xml_text=_read_xml_evidence(
                database_path,
                text(row, "xml_path"),
                int(run_id),
            ),
        )
        for row in state_rows
    ]
    transitions = [
        TransitionEvidence(
            id=int(row["id"]),
            run_id=int(row["run_id"]),
            branch_run_id=int(row["branch_run_id"]),
            from_state_id=int(row["from_state_id"]),
            to_state_id=(int(row["to_state_id"]) if row["to_state_id"] is not None else None),
            action_type=text(row, "action_type"),
            action_key=text(row, "action_key"),
            action_role=text(row, "action_role"),
            execution_disposition=text(row, "execution_disposition"),
            status=text(row, "status"),
            failure_type=text(row, "failure_type"),
            risk_type=text(row, "risk_type"),
            reason=text(row, "reason"),
        )
        for row in transition_rows
    ]
    metadata = {
        "name": text(run, "name"),
        "package_name": text(run, "package_name"),
        "status": text(run, "status"),
    }
    return metadata, states, transitions


def _is_real_edge(transition: TransitionEvidence) -> bool:
    return bool(
        transition.to_state_id is not None
        and transition.execution_disposition.upper() == "EXECUTED"
        and transition.status.upper() in SUCCESSFUL_EDGE_STATUSES
    )


def _path_evidence(
    matchers: Sequence[StateMatcher],
    states: Sequence[StateEvidence],
    transitions: Sequence[TransitionEvidence],
    edge_role_patterns: Sequence[str] = (),
) -> Optional[tuple[tuple[int, ...], tuple[int, ...]]]:
    if not matchers:
        return None
    by_id = {state.id: state for state in states}
    adjacency: dict[int, list[TransitionEvidence]] = {}
    for transition in transitions:
        if _is_real_edge(transition):
            adjacency.setdefault(transition.from_state_id, []).append(transition)
    for edges in adjacency.values():
        edges.sort(key=lambda edge: edge.id)

    def walk(
        state: StateEvidence,
        matcher_index: int,
        state_ids: tuple[int, ...],
        transition_ids: tuple[int, ...],
    ) -> Optional[tuple[tuple[int, ...], tuple[int, ...]]]:
        if matcher_index == len(matchers) - 1:
            return state_ids, transition_ids
        role_pattern = (
            edge_role_patterns[matcher_index]
            if matcher_index < len(edge_role_patterns)
            else ""
        )
        for edge in adjacency.get(state.id, ()):
            if edge.branch_run_id != state.branch_run_id:
                continue
            if role_pattern and not re.fullmatch(role_pattern, edge.action_role or "", re.I):
                continue
            target = by_id.get(int(edge.to_state_id or 0))
            if target is None or target.branch_run_id != state.branch_run_id:
                continue
            if not matchers[matcher_index + 1].matches(target):
                continue
            found = walk(
                target,
                matcher_index + 1,
                (*state_ids, target.id),
                (*transition_ids, edge.id),
            )
            if found is not None:
                return found
        return None

    for state in sorted(states, key=lambda item: item.id):
        if matchers[0].matches(state):
            found = walk(state, 0, (state.id,), ())
            if found is not None:
                return found
    return None


def _state_result(item: ManifestItem, states: Sequence[StateEvidence]) -> ItemResult:
    assert item.matcher is not None
    matches = tuple(state.id for state in states if item.matcher.matches(state))
    return ItemResult(
        key=item.key,
        label=item.label,
        weight=item.weight,
        required=item.required,
        covered=bool(matches),
        evidence_state_ids=matches,
        detail="state evidence matched" if matches else "no matching state/XML evidence",
    )


def _path_result(
    item: ManifestItem,
    states: Sequence[StateEvidence],
    transitions: Sequence[TransitionEvidence],
) -> ItemResult:
    evidence = _path_evidence(item.path, states, transitions, item.edge_role_patterns)
    return ItemResult(
        key=item.key,
        label=item.label,
        weight=item.weight,
        required=item.required,
        covered=evidence is not None,
        evidence_state_ids=evidence[0] if evidence else (),
        evidence_transition_ids=evidence[1] if evidence else (),
        detail=(
            "real transition chain matched"
            if evidence
            else "matching states do not form the required real transition chain"
        ),
    )


def _bottom_tabs_result(
    item: ManifestItem,
    states: Sequence[StateEvidence],
    transitions: Sequence[TransitionEvidence],
) -> ItemResult:
    home_matcher = StateMatcher(
        subtypes=("HOME",),
        xml_patterns=(r"首页", r"分类", r"许愿池", r"购物车", r"我的"),
    )
    target_subtypes = ("CATALOG_CATEGORY", "COMMUNITY_FEED", "CART", "PROFILE")
    by_id = {state.id: state for state in states}
    adjacency: dict[int, list[TransitionEvidence]] = {}
    for transition in transitions:
        if _is_real_edge(transition):
            adjacency.setdefault(transition.from_state_id, []).append(transition)

    for home_state in sorted(states, key=lambda state: state.id):
        if not home_matcher.matches(home_state):
            continue
        targets: dict[str, tuple[StateEvidence, TransitionEvidence]] = {}
        for edge in sorted(adjacency.get(home_state.id, ()), key=lambda row: row.id):
            if edge.branch_run_id != home_state.branch_run_id:
                continue
            target = by_id.get(int(edge.to_state_id or 0))
            if target is None or target.branch_run_id != home_state.branch_run_id:
                continue
            subtype = target.page_subtype.upper()
            if subtype in target_subtypes and subtype not in targets:
                targets[subtype] = (target, edge)
        if all(subtype in targets for subtype in target_subtypes):
            return ItemResult(
                key=item.key,
                label=item.label,
                weight=item.weight,
                required=item.required,
                covered=True,
                evidence_state_ids=(
                    home_state.id,
                    *(targets[subtype][0].id for subtype in target_subtypes),
                ),
                evidence_transition_ids=tuple(
                    targets[subtype][1].id for subtype in target_subtypes
                ),
                detail="home and four bottom-tab destinations have real transitions",
            )
    return ItemResult(
        key=item.key,
        label=item.label,
        weight=item.weight,
        required=item.required,
        covered=False,
        detail="no HOME has XML labels plus real transitions to all four destinations",
    )


def _payment_safety_result(
    item: ManifestItem,
    states: Sequence[StateEvidence],
    transitions: Sequence[TransitionEvidence],
) -> ItemResult:
    path = _path_evidence(item.path, states, transitions, item.edge_role_patterns)
    specification_mode = "primary manifest path"
    for index, alternative in enumerate(item.alternative_paths):
        if path is not None:
            break
        roles = (
            item.alternative_edge_role_patterns[index]
            if index < len(item.alternative_edge_role_patterns)
            else ()
        )
        path = _path_evidence(alternative, states, transitions, roles)
        specification_mode = (
            item.alternative_path_labels[index]
            if index < len(item.alternative_path_labels)
            else f"manifest alternative {index + 1}"
        )
    if path is None:
        return ItemResult(
            key=item.key,
            label=item.label,
            weight=item.weight,
            required=item.required,
            covered=False,
            detail="physical detail/specification/checkout/cashier transition chain missing",
        )
    cashier_id = path[0][-1]
    cashier_branch_id = next(
        state.branch_run_id for state in states if state.id == cashier_id
    )
    boundary = next(
        (
            transition
            for transition in sorted(transitions, key=lambda row: row.id)
            if transition.from_state_id == cashier_id
            and transition.branch_run_id == cashier_branch_id
            and transition.to_state_id is None
            and transition.execution_disposition.upper() in {"EXECUTED", "SKIPPED"}
            and transition.status.upper() == "BLOCKED"
            and transition.risk_type.upper() == "PAYMENT"
            and (
                transition.failure_type.upper() == "SAFETY_BLOCKED"
                or (
                    transition.execution_disposition.upper() == "EXECUTED"
                    and not transition.failure_type
                )
            )
        ),
        None,
    )
    if boundary is None:
        return ItemResult(
            key=item.key,
            label=item.label,
            weight=item.weight,
            required=item.required,
            covered=False,
            evidence_state_ids=path[0],
            evidence_transition_ids=path[1],
            detail="cashier reached, but no executed PAYMENT/BLOCKED boundary evidence",
        )
    return ItemResult(
        key=item.key,
        label=item.label,
        weight=item.weight,
        required=item.required,
        covered=True,
        evidence_state_ids=path[0],
        evidence_transition_ids=(*path[1], boundary.id),
        detail=(
            f"real purchase chain ({specification_mode}) reached cashier and "
            "final payment was blocked"
        ),
    )


def evaluate_manifest(
    states: Sequence[StateEvidence],
    transitions: Sequence[TransitionEvidence],
    manifest: Sequence[ManifestItem] = HAIER_COVERAGE_MANIFEST,
) -> tuple[ItemResult, ...]:
    """Evaluate every manifest item; missing evidence always remains denominator."""

    results: list[ItemResult] = []
    for item in manifest:
        if item.weight <= 0:
            raise CoverageAuditError(f"manifest item {item.key!r} has non-positive weight")
        if item.kind == "state":
            results.append(_state_result(item, states))
        elif item.kind == "path":
            results.append(_path_result(item, states, transitions))
        elif item.kind == "bottom_tabs":
            results.append(_bottom_tabs_result(item, states, transitions))
        elif item.kind == "payment_safety_path":
            results.append(_payment_safety_result(item, states, transitions))
        else:
            raise CoverageAuditError(f"unknown manifest item kind: {item.kind}")
    return tuple(results)


def audit_haier_coverage(
    database: Path | str,
    run_id: int,
    *,
    threshold: float = 0.85,
    manifest: Sequence[ManifestItem] = HAIER_COVERAGE_MANIFEST,
) -> CoverageReport:
    if not 0.0 <= threshold <= 1.0:
        raise CoverageAuditError("threshold must be between 0 and 1")
    metadata, states, transitions = load_run_evidence(database, run_id)
    if metadata["package_name"] not in HAIER_PACKAGE_NAMES:
        raise CoverageAuditError(
            "run package is not Haier Mall: "
            f"{metadata['package_name'] or '<empty>'}"
        )
    items = evaluate_manifest(states, transitions, manifest)
    return CoverageReport(
        run_id=int(run_id),
        database=str(Path(database).expanduser().resolve()),
        run_name=metadata["name"],
        package_name=metadata["package_name"],
        run_status=metadata["status"],
        threshold=float(threshold),
        items=items,
        state_count=len(states),
        transition_count=len(transitions),
        xml_loaded_count=sum(state.xml_text is not None for state in states),
        xml_missing_count=sum(bool(state.xml_path) and state.xml_text is None for state in states),
    )


def weighted_coverage(items: Iterable[ItemResult]) -> float:
    """Small pure helper used by callers that evaluate a custom manifest."""

    rows = tuple(items)
    total = sum(item.weight for item in rows)
    return sum(item.weight for item in rows if item.covered) / total if total else 0.0
