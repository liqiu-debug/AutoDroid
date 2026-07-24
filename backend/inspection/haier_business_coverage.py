"""Versioned business-journey coverage for the Haier Mall application.

The existing :mod:`haier_coverage` module is the frozen v1 historical audit.
This module owns the v2 runtime contract.  It deliberately measures a finite
set of business journeys instead of treating discovered page families as an
estimate of the whole application.
"""

from __future__ import annotations

import copy
import hashlib
import json
import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Iterable, Mapping, Optional, Sequence

from backend.inspection.haier_coverage import (
    HAIER_PACKAGE_NAMES,
    StateEvidence,
    StateMatcher,
    TransitionEvidence,
    _normalise_xml_text,
)


MANIFEST_ID = "haier-mall-v2"
MANIFEST_VERSION = 2
SEARCH_KEYWORD = "冰箱"
SEARCH_INPUT_RULE_ID = "haier_v2_search_keyword"
BRANCHES = ("guest", "authenticated")
ITEM_STATUSES = frozenset(
    {"COVERED", "MISSING", "INCONCLUSIVE", "NOT_IN_SCOPE"}
)
SUCCESSFUL_EDGE_STATUSES = frozenset({"PASS", "SUCCESS", "SELF_LOOP"})
VERIFIED_STATE_STATUSES = frozenset(
    {"REVERIFIED_ONCE", "STABLE", "VERIFIED_TWICE"}
)
TRANSPARENT_PATH_SUBTYPES = frozenset({"CHECKOUT_CONFIRMATION"})


def _matcher(
    *subtypes: str,
    xml_all: Sequence[str] = (),
    xml_exclude: Sequence[str] = (),
) -> dict[str, Any]:
    return {
        "subtypes": list(subtypes),
        "xml_all": list(xml_all),
        "xml_exclude": list(xml_exclude),
    }


def _journey(
    key: str,
    label: str,
    branches: Sequence[str],
    kind: str,
    stages: Sequence[Mapping[str, Any]],
    *,
    roles: Sequence[str] = (),
    required: bool = True,
    description: str = "",
    action_hints: Sequence[Mapping[str, Any]] = (),
) -> dict[str, Any]:
    return {
        "key": key,
        "label": label,
        "branches": list(branches),
        "required": required,
        "kind": kind,
        "stages": [dict(item) for item in stages],
        "edge_role_patterns": list(roles),
        "description": description,
        "action_hints": [dict(item) for item in action_hints],
    }


_PHYSICAL_DETAIL = _matcher(
    "PRODUCT_DETAIL",
    xml_all=(
        r"(?:Haier/海尔|Casarte/卡萨帝|Leader/统帅|海尔)",
        r"(?:商品参数|国家补贴|换新补贴|正品保障|一级能耗|风冷无霜)",
    ),
    xml_exclude=(
        r"(?:家生活服务|原厂服务|深度清洗|清洗服务|延保|服务套餐)",
    ),
)


HAIER_MALL_V2_MANIFEST: dict[str, Any] = {
    "id": MANIFEST_ID,
    "version": MANIFEST_VERSION,
    "package_names": sorted(HAIER_PACKAGE_NAMES),
    "scope_branches": list(BRANCHES),
    "search": {
        "keyword": SEARCH_KEYWORD,
        "input_rule_id": SEARCH_INPUT_RULE_ID,
    },
    "representative_sampling": {
        "products": 1,
        "stores": 1,
        "description": "同构商品和门店采用代表采样；核心旅程不得由页面族复用替代",
    },
    "journeys": [
        _journey(
            "home_five_tabs",
            "五底栏真实到达",
            BRANCHES,
            "bottom_tabs",
            [_matcher("HOME")],
            description="首页、分类、许愿池、购物车、我的均需真实到达",
            action_hints=[
                {
                    "source_subtypes": [
                        "HOME",
                        "CATALOG_CATEGORY",
                        "COMMUNITY_FEED",
                        "CART",
                        "PROFILE",
                    ],
                    "role_patterns": [r"NAV:.*"],
                }
            ],
        ),
        _journey(
            "category_search_product_flow",
            "分类到搜索结果及商品详情",
            BRANCHES,
            "fixed_search_path",
            [
                _matcher("CATALOG_CATEGORY"),
                _matcher("SEARCH"),
                _matcher("PRODUCT_LIST"),
                _PHYSICAL_DETAIL,
            ],
            roles=(
                r"COMMAND:SEARCH",
                r"INPUT",
                r"SEARCH_SUBMIT",
                r"ITEM_OPEN(?::.*)?",
            ),
            description=(
                "使用固定关键词“冰箱”形成真实搜索链路；兼容输入后自动进入结果页"
                "和显式提交两种模式"
            ),
            action_hints=[
                {
                    "source_subtypes": ["CATALOG_CATEGORY"],
                    "role_patterns": [r"COMMAND:SEARCH"],
                },
                {
                    "source_subtypes": ["SEARCH"],
                    "role_patterns": [r"INPUT", r"SEARCH_SUBMIT"],
                },
                {
                    "source_subtypes": ["PRODUCT_LIST"],
                    "role_patterns": [r"ITEM_OPEN(?::.*)?"],
                },
            ],
        ),
        _journey(
            "wish_pool_content",
            "许愿池内容",
            BRANCHES,
            "path",
            [
                _matcher("COMMUNITY_FEED", xml_all=(r"许愿池",)),
                _matcher(
                    "COMMUNITY_DETAIL",
                    xml_all=(
                        r"来说点什么",
                        r"(?:官方|精选|参与话题|获奖信息)",
                    ),
                ),
            ],
            roles=(r"ITEM_OPEN(?::.*)?",),
            action_hints=[
                {
                    "source_subtypes": ["COMMUNITY_FEED"],
                    "role_patterns": [r"ITEM_OPEN(?::.*)?"],
                }
            ],
        ),
        _journey(
            "service_detail_flow",
            "服务列表到详情",
            BRANCHES,
            "path",
            [
                _matcher(
                    "SERVICE_LIST",
                    xml_all=(r"(?:深度清洗|原厂服务|家电维修|家电安装|清洗服务)",),
                ),
                _matcher(
                    "SERVICE_DETAIL",
                    xml_all=(r"(?:家生活服务|原厂服务|深度清洗|清洗服务|延保|服务套餐)",),
                ),
            ],
            roles=(r"ITEM_OPEN(?::.*)?",),
            action_hints=[
                {
                    "source_subtypes": ["PROFILE", "SERVICE_LIST"],
                    "role_patterns": [r"COMMAND:.*", r"ITEM_OPEN(?::.*)?"],
                    "label_patterns": [
                        r"(?:清洗服务|深度清洗|家电维修|家电安装|原厂服务)"
                    ],
                }
            ],
        ),
        _journey(
            "store_detail_flow",
            "门店列表到详情",
            BRANCHES,
            "path",
            [_matcher("STORE_LIST"), _matcher("STORE_DETAIL")],
            roles=(r"STORE_OPEN",),
            action_hints=[
                {
                    "source_subtypes": ["STORE_LIST"],
                    "role_patterns": [r"STORE_OPEN"],
                }
            ],
        ),
        _journey(
            "physical_checkout_safety_flow",
            "商品详情到支付安全边界",
            ("authenticated",),
            "payment_safety_path",
            [
                _PHYSICAL_DETAIL,
                _matcher("PURCHASE_OPTIONS"),
                _matcher("CHECKOUT", xml_all=(r"提交订单|立即支付",)),
                _matcher("CASHIER", xml_all=(r"海尔收银台",)),
            ],
            roles=(
                r"(?:OPTION_SELECT|BUY_NOW|ADD_CART)",
                r"(?:BUY_NOW|CHECKOUT)",
                r"PLACE_ORDER",
            ),
            description=(
                "规格证据与购买主链必须锚定同一商品详情；收银台最终付款动作"
                "必须形成 BLOCKED/PAYMENT 证据"
            ),
            action_hints=[
                {
                    "source_subtypes": ["PRODUCT_DETAIL"],
                    "role_patterns": [r"OPTION_SELECT", r"BUY_NOW", r"ADD_CART"],
                },
                {
                    "source_subtypes": ["PRODUCT_DETAIL", "PURCHASE_OPTIONS"],
                    "role_patterns": [
                        r"BUY_NOW",
                        r"CHECKOUT",
                        r"ADD_CART",
                        r"DIALOG_CLOSE",
                    ],
                },
                {
                    "source_subtypes": ["CHECKOUT", "CHECKOUT_CONFIRMATION"],
                    "role_patterns": [r"PLACE_ORDER"],
                },
                {
                    "source_subtypes": ["CASHIER"],
                    "role_patterns": [r".*"],
                    "risk_types": ["PAYMENT"],
                },
            ],
        ),
        _journey(
            "product_orders_flow",
            "商品订单中心",
            ("authenticated",),
            "path",
            [
                _matcher("PROFILE"),
                _matcher("ORDER", "PRODUCT_LIST", xml_all=(r"待付款|全部订单",)),
            ],
            action_hints=[
                {
                    "source_subtypes": ["PROFILE"],
                    "role_patterns": [r"COMMAND:.*"],
                    "label_patterns": [
                        r"待付款|待发货|待收货|评价有礼|退款/售后|全部(?:订单)?"
                    ],
                }
            ],
        ),
        _journey(
            "settings_address_flow",
            "设置到收货地址",
            ("authenticated",),
            "path",
            [
                _matcher("PROFILE"),
                _matcher("SETTINGS", xml_all=(r"收货地址",)),
                _matcher("ADDRESS_LIST", xml_all=(r"收货地址",)),
            ],
            action_hints=[
                {
                    "source_subtypes": ["PROFILE"],
                    "role_patterns": [r"COMMAND:SETTINGS"],
                },
                {
                    "source_subtypes": ["SETTINGS"],
                    "role_patterns": [
                        r"COMMAND:ADDRESS",
                        r"ADDRESS_OPEN",
                        r"COMMAND:ROLE_CHECKOUT",
                    ],
                    "label_patterns": [r"收货地址|shipping\s+address"],
                },
            ],
        ),
        _journey(
            "member_benefits_flow",
            "会员权益",
            ("authenticated",),
            "path",
            [
                _matcher("PROFILE"),
                _matcher(
                    "MEMBER_BENEFITS",
                    xml_all=(
                        r"(?:Smart\s*life|我的权益)",
                        r"(?:积分|满减券|会员礼包|优惠券|卡包)",
                    ),
                ),
            ],
            action_hints=[
                {
                    "source_subtypes": ["PROFILE"],
                    "role_patterns": [r"COMMAND:.*"],
                    "label_patterns": [r"权益|积分|优惠券|卡包"],
                }
            ],
        ),
        _journey(
            "favorites_flow",
            "商品收藏",
            ("authenticated",),
            "path",
            [
                _matcher("PROFILE"),
                _matcher("FAVORITES", xml_all=(r"商品收藏|我的收藏",)),
            ],
            action_hints=[
                {
                    "source_subtypes": ["PROFILE"],
                    "role_patterns": [r"COMMAND:.*"],
                    "label_patterns": [r"收藏"],
                }
            ],
        ),
        _journey(
            "history_flow",
            "历史浏览",
            ("authenticated",),
            "path",
            [
                _matcher("PROFILE"),
                _matcher("BROWSING_HISTORY", xml_all=(r"历史浏览|浏览记录",)),
            ],
            action_hints=[
                {
                    "source_subtypes": ["PROFILE"],
                    "role_patterns": [r"COMMAND:.*"],
                    "label_patterns": [r"历史浏览|浏览记录"],
                }
            ],
        ),
        _journey(
            "profile_auth_gate",
            "个人中心登录门槛",
            ("guest",),
            "path",
            [_matcher("PROFILE"), _matcher("AUTH_GATE")],
            action_hints=[
                {
                    "source_subtypes": ["PROFILE"],
                    "role_patterns": [r"COMMAND:LOGIN", r"COMMAND:PROFILE"],
                }
            ],
        ),
        _journey(
            "purchase_auth_gate",
            "商品购买登录门槛",
            ("guest",),
            "path",
            [_PHYSICAL_DETAIL, _matcher("AUTH_GATE")],
            roles=(r"BUY_NOW",),
            action_hints=[
                {
                    "source_subtypes": ["PRODUCT_DETAIL"],
                    "role_patterns": [r"BUY_NOW"],
                }
            ],
        ),
        _journey(
            "address_edit_flow",
            "地址编辑",
            ("authenticated",),
            "path",
            [_matcher("ADDRESS_LIST"), _matcher("ADDRESS_FORM")],
            required=False,
        ),
        _journey(
            "store_appointment_flow",
            "门店预约",
            BRANCHES,
            "path",
            [_matcher("STORE_DETAIL"), _matcher("APPOINTMENT_LIST")],
            roles=(r"STORE_APPOINTMENT",),
            required=False,
        ),
        _journey(
            "consumables",
            "耗材专区",
            BRANCHES,
            "state",
            [_matcher("CONSUMABLE_LIST")],
            required=False,
        ),
        _journey(
            "service_orders",
            "服务订单",
            ("authenticated",),
            "state",
            [_matcher("ORDER", xml_all=(r"服务订单",))],
            required=False,
        ),
    ],
}


def canonical_manifest_json(manifest: Mapping[str, Any]) -> str:
    return json.dumps(
        manifest,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def manifest_hash(manifest: Mapping[str, Any]) -> str:
    return hashlib.sha256(canonical_manifest_json(manifest).encode("utf-8")).hexdigest()


def freeze_manifest(
    package_name: str,
    selected_branches: Sequence[str],
) -> Optional[dict[str, Any]]:
    """Return a run-owned manifest snapshot for supported Haier packages."""
    if str(package_name or "") not in HAIER_PACKAGE_NAMES:
        return None
    snapshot = copy.deepcopy(HAIER_MALL_V2_MANIFEST)
    snapshot["selected_branches"] = [
        branch for branch in BRANCHES if branch in set(selected_branches or ())
    ]
    snapshot["hash"] = manifest_hash(snapshot)
    return snapshot


def haier_search_input_rule() -> dict[str, Any]:
    """Non-sensitive deterministic input used only inside the Haier package."""
    return {
        "id": SEARCH_INPUT_RULE_ID,
        "content_desc_regex": None,
        "text_regex": None,
        "class_regex": r"EditText",
        "ancestor_regex": None,
        "page_subtype_regex": r"^SEARCH$",
        "value_source": "literal",
        "value": SEARCH_KEYWORD,
        "variable_key": None,
        "allow_sensitive": False,
    }


def normalize_xml_evidence(raw_xml: str) -> Optional[str]:
    """Expose the v1 hardened XML text normalizer to runtime assessment."""
    return _normalise_xml_text(raw_xml)


@dataclass(frozen=True)
class BusinessCoverageItemResult:
    key: str
    label: str
    branch_key: str
    required: bool
    status: str
    evidence_state_ids: tuple[int, ...] = ()
    evidence_transition_ids: tuple[int, ...] = ()
    evidence_screenshots: tuple[str, ...] = ()
    deepest_stage: int = 0
    total_stages: int = 0
    reason_code: str = ""
    detail: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "label": self.label,
            "branch_key": self.branch_key,
            "required": self.required,
            "status": self.status,
            "evidence_state_ids": list(self.evidence_state_ids),
            "evidence_transition_ids": list(self.evidence_transition_ids),
            "evidence_screenshots": list(self.evidence_screenshots),
            "deepest_stage": self.deepest_stage,
            "total_stages": self.total_stages,
            "reason_code": self.reason_code,
            "detail": self.detail,
        }


def _compiled_matcher(payload: Mapping[str, Any], *, xml_required: bool = True) -> StateMatcher:
    xml_patterns = tuple(str(item) for item in payload.get("xml_all") or ())
    if not xml_required:
        xml_patterns = ()
    return StateMatcher(
        subtypes=tuple(str(item) for item in payload.get("subtypes") or ()),
        xml_patterns=xml_patterns,
        xml_exclude_patterns=tuple(
            str(item) for item in payload.get("xml_exclude") or ()
        ),
    )


def _is_real_edge(edge: TransitionEvidence) -> bool:
    return bool(
        edge.to_state_id is not None
        and str(edge.execution_disposition or "").upper() == "EXECUTED"
        and str(edge.status or "").upper() in SUCCESSFUL_EDGE_STATUSES
        and not str(getattr(edge, "sampling_disposition", "") or "").upper()
        in {"SAMPLED_OUT", "CONTRACT_REUSED", "NAVIGATION_REUSED"}
    )


def _path_prefix(
    matchers: Sequence[StateMatcher],
    states: Sequence[StateEvidence],
    transitions: Sequence[TransitionEvidence],
    role_patterns: Sequence[str] = (),
    *,
    fixed_search: bool = False,
    transparent_subtypes: Sequence[str] = (),
) -> tuple[tuple[int, ...], tuple[int, ...]]:
    """Return the deepest real, same-branch prefix of one journey."""
    if not matchers:
        return (), ()
    by_id = {state.id: state for state in states}
    adjacency: dict[int, list[TransitionEvidence]] = {}
    for edge in transitions:
        if _is_real_edge(edge):
            adjacency.setdefault(edge.from_state_id, []).append(edge)
    for edges in adjacency.values():
        edges.sort(key=lambda item: item.id)

    best: tuple[tuple[int, ...], tuple[int, ...]] = ((), ())
    transparent = {str(value).upper() for value in transparent_subtypes}

    def walk(
        state: StateEvidence,
        matcher_index: int,
        state_ids: tuple[int, ...],
        edge_ids: tuple[int, ...],
        visited_state_ids: frozenset[int],
    ) -> None:
        nonlocal best
        if len(state_ids) > len(best[0]):
            best = state_ids, edge_ids
        if matcher_index >= len(matchers) - 1:
            return
        role_pattern = (
            role_patterns[matcher_index]
            if matcher_index < len(role_patterns)
            else ""
        )
        for edge in adjacency.get(state.id, ()):
            if edge.branch_run_id != state.branch_run_id:
                continue
            if role_pattern and re.fullmatch(
                role_pattern, str(edge.action_role or ""), re.I
            ) is None:
                continue
            if fixed_search and matcher_index == 1:
                if (
                    str(getattr(edge, "input_rule_id", "") or "")
                    != SEARCH_INPUT_RULE_ID
                    or int(getattr(edge, "input_length", 0) or 0)
                    != len(SEARCH_KEYWORD)
                ):
                    continue
            target = by_id.get(int(edge.to_state_id or 0))
            if target is None or target.branch_run_id != state.branch_run_id:
                continue
            repeated_stage_state = bool(
                target.id == state.id
                and matchers[matcher_index + 1].matches(target)
            )
            if target.id in visited_state_ids and not repeated_stage_state:
                continue
            if str(target.page_subtype or "UNKNOWN").upper() in transparent:
                if target.id in visited_state_ids:
                    continue
                walk(
                    target,
                    matcher_index,
                    state_ids,
                    (*edge_ids, edge.id),
                    visited_state_ids | {target.id},
                )
                continue
            if not matchers[matcher_index + 1].matches(target):
                continue
            walk(
                target,
                matcher_index + 1,
                (*state_ids, target.id),
                (*edge_ids, edge.id),
                visited_state_ids | {target.id},
            )

    for state in sorted(states, key=lambda item: item.id):
        if matchers[0].matches(state):
            walk(state, 0, (state.id,), (), frozenset({state.id}))
    return best


def _fixed_search_prefix(
    item: Mapping[str, Any],
    states: Sequence[StateEvidence],
    transitions: Sequence[TransitionEvidence],
) -> tuple[int, tuple[int, ...], tuple[int, ...]]:
    """Match one contiguous audited Haier search path in either submit mode."""
    stages = list(item.get("stages") or ())
    if len(stages) != 4:
        return 0, (), ()
    matchers = [_compiled_matcher(stage) for stage in stages]
    role_patterns = [str(value) for value in item.get("edge_role_patterns") or ()]
    role_patterns.extend(("",) * (4 - len(role_patterns)))
    by_id = {state.id: state for state in states}
    adjacency: dict[int, list[TransitionEvidence]] = {}
    for edge in transitions:
        if _is_real_edge(edge):
            adjacency.setdefault(edge.from_state_id, []).append(edge)
    for edges in adjacency.values():
        edges.sort(key=lambda row: row.id)

    best: tuple[int, tuple[int, ...], tuple[int, ...]] = (0, (), ())

    def remember(
        deepest_stage: int,
        state_ids: tuple[int, ...],
        edge_ids: tuple[int, ...],
    ) -> None:
        nonlocal best
        if (deepest_stage, len(edge_ids)) > (best[0], len(best[2])):
            best = deepest_stage, state_ids, edge_ids

    def role_matches(index: int, edge: TransitionEvidence) -> bool:
        pattern = role_patterns[index]
        return not pattern or re.fullmatch(
            pattern,
            str(edge.action_role or ""),
            re.I,
        ) is not None

    def target_for(
        edge: TransitionEvidence,
        source: StateEvidence,
    ) -> Optional[StateEvidence]:
        target = by_id.get(int(edge.to_state_id or 0))
        if target is None or target.branch_run_id != source.branch_run_id:
            return None
        return target

    def audited_input(edge: TransitionEvidence) -> bool:
        return bool(
            role_matches(1, edge)
            and str(getattr(edge, "input_rule_id", "") or "")
            == SEARCH_INPUT_RULE_ID
            and int(getattr(edge, "input_length", 0) or 0) == len(SEARCH_KEYWORD)
        )

    def follow_result(
        result: StateEvidence,
        state_ids: tuple[int, ...],
        edge_ids: tuple[int, ...],
    ) -> None:
        remember(3, state_ids, edge_ids)
        for item_edge in adjacency.get(result.id, ()):
            if not role_matches(3, item_edge):
                continue
            detail = target_for(item_edge, result)
            if detail is None or not matchers[3].matches(detail):
                continue
            remember(4, (*state_ids, detail.id), (*edge_ids, item_edge.id))

    for category in sorted(states, key=lambda row: row.id):
        if not matchers[0].matches(category):
            continue
        remember(1, (category.id,), ())
        for command_edge in adjacency.get(category.id, ()):
            if not role_matches(0, command_edge):
                continue
            search = target_for(command_edge, category)
            if search is None or not matchers[1].matches(search):
                continue
            command_states = (category.id, search.id)
            command_edges = (command_edge.id,)
            remember(2, command_states, command_edges)
            for input_edge in adjacency.get(search.id, ()):
                if not audited_input(input_edge):
                    continue
                input_target = target_for(input_edge, search)
                if input_target is None:
                    continue
                if matchers[2].matches(input_target):
                    follow_result(
                        input_target,
                        (*command_states, input_target.id),
                        (*command_edges, input_edge.id),
                    )
                    continue
                if not matchers[1].matches(input_target):
                    continue
                submitted_states = (*command_states, input_target.id)
                submitted_edges = (*command_edges, input_edge.id)
                remember(2, submitted_states, submitted_edges)
                for submit_edge in adjacency.get(input_target.id, ()):
                    if not role_matches(2, submit_edge):
                        continue
                    result = target_for(submit_edge, input_target)
                    if result is None or not matchers[2].matches(result):
                        continue
                    follow_result(
                        result,
                        (*submitted_states, result.id),
                        (*submitted_edges, submit_edge.id),
                    )
    return best


def _potential_blindness(
    stages: Sequence[Mapping[str, Any]],
    states: Sequence[StateEvidence],
    *,
    branch_status: str,
    stop_reason: str,
) -> tuple[str, str]:
    possible_subtypes = {
        str(subtype).upper()
        for stage in stages
        for subtype in stage.get("subtypes") or ()
    }
    if any(
        state.xml_text is None
        and (
            not possible_subtypes
            or str(state.page_subtype or "UNKNOWN").upper() in possible_subtypes
        )
        for state in states
    ):
        return "XML_MISSING", "候选页面 XML 不可读取，无法排除已到达但无法识别"
    if any(
        bool(getattr(state, "is_opaque", False))
        or str(state.page_subtype or "UNKNOWN").upper() in {"UNKNOWN", "OPAQUE"}
        for state in states
    ):
        return "UNKNOWN_OR_OPAQUE", "业务线包含未知或不透明页面，证据不足"
    combined = f"{branch_status} {stop_reason}".upper()
    if any(
        marker in combined
        for marker in ("BUDGET", "预算", "QUEUE", "FRONTIER", "WARNING", "ERROR", "ABORT")
    ):
        return "EXECUTION_INCOMPLETE", "任务在预算或执行边界停止，不能证明该旅程缺失"
    return "PATH_MISSING", "未形成该旅程自己的真实 Transition 链"


def _endpoint_is_verified(state: StateEvidence) -> bool:
    return bool(
        state.xml_text is not None
        and str(getattr(state, "stable_status", "") or "").upper()
        in VERIFIED_STATE_STATUSES
    )


def _path_xml_is_readable(
    state_ids: Sequence[int],
    states_by_id: Mapping[int, StateEvidence],
) -> bool:
    return bool(state_ids) and all(
        states_by_id[state_id].xml_text is not None for state_id in state_ids
    )


def _result_from_path(
    item: Mapping[str, Any],
    branch_key: str,
    states: Sequence[StateEvidence],
    transitions: Sequence[TransitionEvidence],
    *,
    branch_status: str,
    stop_reason: str,
) -> BusinessCoverageItemResult:
    stages = list(item.get("stages") or [])
    matchers = [_compiled_matcher(stage) for stage in stages]
    state_ids, edge_ids = _path_prefix(
        matchers,
        states,
        transitions,
        tuple(str(role) for role in item.get("edge_role_patterns") or ()),
        fixed_search=str(item.get("kind") or "") == "fixed_search_path",
        transparent_subtypes=(
            TRANSPARENT_PATH_SUBTYPES
            if str(item.get("kind") or "") == "payment_safety_path"
            else ()
        ),
    )
    by_id = {state.id: state for state in states}
    complete = len(state_ids) == len(stages)
    boundary_id: Optional[int] = None
    if complete and str(item.get("kind") or "") == "payment_safety_path":
        endpoint = by_id[state_ids[-1]]
        boundary = next(
            (
                edge
                for edge in sorted(transitions, key=lambda row: row.id)
                if edge.branch_run_id == endpoint.branch_run_id
                and edge.from_state_id == endpoint.id
                and edge.to_state_id is None
                and str(edge.status or "").upper() == "BLOCKED"
                and str(edge.risk_type or "").upper() == "PAYMENT"
                and str(edge.execution_disposition or "").upper()
                in {"EXECUTED", "SKIPPED"}
                and str(edge.failure_type or "").upper()
                in {"", "SAFETY_BLOCKED"}
            ),
            None,
        )
        complete = boundary is not None
        boundary_id = boundary.id if boundary is not None else None

    if complete:
        endpoint = by_id[state_ids[-1]]
        if not _path_xml_is_readable(state_ids, by_id):
            reason = "XML_MISSING"
            detail = "真实路径已到达，但路径中的 State XML 不可读取"
            status = "INCONCLUSIVE"
        elif not _endpoint_is_verified(endpoint):
            reason = (
                "XML_MISSING"
                if endpoint.xml_text is None
                else "ENDPOINT_NOT_REVERIFIED"
            )
            detail = (
                "终点 XML 不可读取"
                if endpoint.xml_text is None
                else "真实路径已到达，但终点未通过保留预算复验"
            )
            status = "INCONCLUSIVE"
        else:
            reason = ""
            detail = "真实成功 Transition、可读 XML 和终点复验均已满足"
            status = "COVERED"
    else:
        reason, detail = _potential_blindness(
            stages,
            states,
            branch_status=branch_status,
            stop_reason=stop_reason,
        )
        status = "INCONCLUSIVE" if reason != "PATH_MISSING" else "MISSING"
        if str(item.get("kind") or "") == "payment_safety_path" and len(state_ids) == len(stages):
            reason = "PAYMENT_BOUNDARY_MISSING"
            detail = "已到达收银台，但缺少明确的 BLOCKED/PAYMENT 安全边界"
            status = "MISSING"

    evidence_ids = (*edge_ids, *((boundary_id,) if boundary_id else ()))
    screenshots = tuple(
        str(getattr(by_id[state_id], "screenshot_path", "") or "")
        for state_id in state_ids
        if str(getattr(by_id[state_id], "screenshot_path", "") or "")
    )
    return BusinessCoverageItemResult(
        key=str(item.get("key") or ""),
        label=str(item.get("label") or ""),
        branch_key=branch_key,
        required=bool(item.get("required", True)),
        status=status,
        evidence_state_ids=state_ids,
        evidence_transition_ids=evidence_ids,
        evidence_screenshots=screenshots,
        deepest_stage=len(state_ids),
        total_stages=len(stages),
        reason_code=reason,
        detail=detail,
    )


def _result_from_fixed_search_graph(
    item: Mapping[str, Any],
    branch_key: str,
    states: Sequence[StateEvidence],
    transitions: Sequence[TransitionEvidence],
    *,
    branch_status: str,
    stop_reason: str,
) -> BusinessCoverageItemResult:
    """Require one real fixed-keyword result list and its own product detail."""
    stages = list(item.get("stages") or ())
    deepest_stage, state_ids, edge_ids = _fixed_search_prefix(
        item,
        states,
        transitions,
    )
    by_id = {state.id: state for state in states}
    complete = deepest_stage == len(stages) and bool(state_ids)
    if complete:
        endpoint = by_id[state_ids[-1]]
        if not _path_xml_is_readable(state_ids, by_id):
            reason = "XML_MISSING"
            detail = "固定词搜索路径中的 State XML 不可读取"
            status = "INCONCLUSIVE"
        elif not _endpoint_is_verified(endpoint):
            reason = (
                "XML_MISSING"
                if endpoint.xml_text is None
                else "ENDPOINT_NOT_REVERIFIED"
            )
            detail = (
                "商品详情终点 XML 不可读取"
                if endpoint.xml_text is None
                else "固定词搜索路径已到达商品详情，但终点未通过保留预算复验"
            )
            status = "INCONCLUSIVE"
        else:
            reason = ""
            detail = (
                "固定关键词输入、独立结果列表、真实商品点击和终点复验均已满足"
            )
            status = "COVERED"
    else:
        reason, detail = _potential_blindness(
            stages,
            states,
            branch_status=branch_status,
            stop_reason=stop_reason,
        )
        status = "INCONCLUSIVE" if reason != "PATH_MISSING" else "MISSING"

    return BusinessCoverageItemResult(
        key=str(item.get("key") or ""),
        label=str(item.get("label") or ""),
        branch_key=branch_key,
        required=bool(item.get("required", True)),
        status=status,
        evidence_state_ids=state_ids,
        evidence_transition_ids=edge_ids,
        evidence_screenshots=tuple(
            str(getattr(by_id[state_id], "screenshot_path", "") or "")
            for state_id in state_ids
            if str(getattr(by_id[state_id], "screenshot_path", "") or "")
        ),
        deepest_stage=deepest_stage,
        total_stages=len(stages),
        reason_code=reason,
        detail=detail,
    )


def _result_from_payment_graph(
    item: Mapping[str, Any],
    branch_key: str,
    states: Sequence[StateEvidence],
    transitions: Sequence[TransitionEvidence],
    *,
    branch_status: str,
    stop_reason: str,
) -> BusinessCoverageItemResult:
    """Evaluate Haier's spec side branch and checkout chain from one product."""
    stages = list(item.get("stages") or [])
    if len(stages) != 4:
        return _result_from_path(
            item,
            branch_key,
            states,
            transitions,
            branch_status=branch_status,
            stop_reason=stop_reason,
        )
    matchers = [_compiled_matcher(stage) for stage in stages]
    role_patterns = [str(value) for value in item.get("edge_role_patterns") or ()]
    by_id = {state.id: state for state in states}
    real_edges = sorted((edge for edge in transitions if _is_real_edge(edge)), key=lambda row: row.id)
    adjacency: dict[int, list[TransitionEvidence]] = {}
    for edge in real_edges:
        adjacency.setdefault(edge.from_state_id, []).append(edge)

    def role_matches(index: int, edge: TransitionEvidence) -> bool:
        pattern = role_patterns[index] if index < len(role_patterns) else ""
        return not pattern or re.fullmatch(
            pattern,
            str(edge.action_role or ""),
            re.I,
        ) is not None

    def target_for(edge: TransitionEvidence) -> Optional[StateEvidence]:
        target = by_id.get(int(edge.to_state_id or 0))
        if target is None:
            return None
        source = by_id.get(edge.from_state_id)
        if source is None or target.branch_run_id != source.branch_run_id:
            return None
        return target

    def cashier_path(
        state_id: int,
        visited: frozenset[int],
    ) -> Optional[tuple[int, tuple[int, ...]]]:
        for edge in adjacency.get(state_id, ()):
            if not role_matches(2, edge):
                continue
            target = target_for(edge)
            if target is None or target.id in visited:
                continue
            if matchers[3].matches(target):
                return target.id, (edge.id,)
            if str(target.page_subtype or "UNKNOWN").upper() not in TRANSPARENT_PATH_SUBTYPES:
                continue
            continuation = cashier_path(target.id, visited | {target.id})
            if continuation is not None:
                cashier_id, edge_ids = continuation
                return cashier_id, (edge.id, *edge_ids)
        return None

    def payment_boundary(
        state_id: int,
        *,
        after_transition_id: int,
    ) -> Optional[TransitionEvidence]:
        state = by_id[state_id]
        return next(
            (
                edge
                for edge in sorted(transitions, key=lambda row: row.id)
                if edge.branch_run_id == state.branch_run_id
                and edge.from_state_id == state_id
                and edge.id > after_transition_id
                and edge.to_state_id is None
                and str(edge.status or "").upper() == "BLOCKED"
                and str(edge.risk_type or "").upper() == "PAYMENT"
                and str(edge.execution_disposition or "").upper()
                in {"EXECUTED", "SKIPPED"}
                and str(edge.failure_type or "").upper()
                in {"", "SAFETY_BLOCKED"}
            ),
            None,
        )

    best_states: tuple[int, ...] = ()
    best_edges: tuple[int, ...] = ()
    complete_graphs: list[
        tuple[tuple[int, ...], tuple[int, ...], Optional[TransitionEvidence]]
    ] = []

    def remember(state_ids: tuple[int, ...], edge_ids: tuple[int, ...]) -> None:
        nonlocal best_states, best_edges
        if len(state_ids) > len(best_states) or (
            len(state_ids) == len(best_states)
            and edge_ids
            and (not best_edges or edge_ids < best_edges)
        ):
            best_states, best_edges = state_ids, edge_ids

    for detail in sorted(states, key=lambda row: row.id):
        if not matchers[0].matches(detail):
            continue
        remember((detail.id,), ())
        spec_edges = [
            edge
            for edge in adjacency.get(detail.id, ())
            if role_matches(0, edge)
            and (target := target_for(edge)) is not None
            and matchers[1].matches(target)
        ]
        for spec_edge in spec_edges:
            options = target_for(spec_edge)
            if options is None:
                continue
            remember((detail.id, options.id), (spec_edge.id,))
            return_edge = next(
                (
                    edge
                    for edge in adjacency.get(options.id, ())
                    if int(edge.to_state_id or 0) == detail.id
                ),
                None,
            )
            purchase_sources = [
                (options.id, ()),
                (
                    detail.id,
                    ((return_edge.id,) if return_edge is not None else ()),
                ),
            ]
            for source_id, bridge_edges in purchase_sources:
                for checkout_edge in adjacency.get(source_id, ()):
                    if not role_matches(1, checkout_edge):
                        continue
                    checkout = target_for(checkout_edge)
                    if checkout is None or not matchers[2].matches(checkout):
                        continue
                    state_ids = (detail.id, options.id, checkout.id)
                    edge_ids = (spec_edge.id, *bridge_edges, checkout_edge.id)
                    remember(state_ids, edge_ids)
                    cashier = cashier_path(checkout.id, frozenset({checkout.id}))
                    if cashier is None:
                        continue
                    cashier_id, cashier_edges = cashier
                    full_states = (*state_ids, cashier_id)
                    full_edges = (*edge_ids, *cashier_edges)
                    remember(full_states, full_edges)
                    complete_graphs.append(
                        (
                            full_states,
                            full_edges,
                            payment_boundary(
                                cashier_id,
                                after_transition_id=cashier_edges[-1],
                            ),
                        )
                    )

    selected_graph = next(
        (candidate for candidate in complete_graphs if candidate[2] is not None),
        complete_graphs[0] if complete_graphs else None,
    )
    boundary_id: Optional[int] = None
    if selected_graph is not None:
        best_states, best_edges, boundary = selected_graph
        boundary_id = boundary.id if boundary is not None else None
        endpoint = by_id[best_states[-1]]
        if boundary is None:
            status = "MISSING"
            reason = "PAYMENT_BOUNDARY_MISSING"
            detail_text = "已到达收银台，但缺少明确的 BLOCKED/PAYMENT 安全边界"
        elif not _path_xml_is_readable(best_states, by_id):
            status = "INCONCLUSIVE"
            reason = "XML_MISSING"
            detail_text = "支付路径中的 State XML 不可读取"
        elif not _endpoint_is_verified(endpoint):
            status = "INCONCLUSIVE"
            reason = (
                "XML_MISSING"
                if endpoint.xml_text is None
                else "ENDPOINT_NOT_REVERIFIED"
            )
            detail_text = (
                "终点 XML 不可读取"
                if endpoint.xml_text is None
                else "真实证据图已到达，但收银台未通过保留预算复验"
            )
        else:
            status = "COVERED"
            reason = ""
            detail_text = (
                "同一商品详情的规格分支、购买主链、可读 XML、收银台复验和"
                "支付拦截均已满足"
            )
    else:
        reason, detail_text = _potential_blindness(
            stages,
            states,
            branch_status=branch_status,
            stop_reason=stop_reason,
        )
        status = "INCONCLUSIVE" if reason != "PATH_MISSING" else "MISSING"

    evidence_ids = (*best_edges, *((boundary_id,) if boundary_id else ()))
    return BusinessCoverageItemResult(
        key=str(item.get("key") or ""),
        label=str(item.get("label") or ""),
        branch_key=branch_key,
        required=bool(item.get("required", True)),
        status=status,
        evidence_state_ids=best_states,
        evidence_transition_ids=evidence_ids,
        evidence_screenshots=tuple(
            str(getattr(by_id[state_id], "screenshot_path", "") or "")
            for state_id in best_states
            if str(getattr(by_id[state_id], "screenshot_path", "") or "")
        ),
        deepest_stage=len(best_states),
        total_stages=len(stages),
        reason_code=reason,
        detail=detail_text,
    )


def _result_from_state(
    item: Mapping[str, Any],
    branch_key: str,
    states: Sequence[StateEvidence],
    *,
    branch_status: str,
    stop_reason: str,
) -> BusinessCoverageItemResult:
    stages = list(item.get("stages") or [])
    matcher = _compiled_matcher(stages[0])
    matches = [state for state in sorted(states, key=lambda row: row.id) if matcher.matches(state)]
    verified = next((state for state in matches if _endpoint_is_verified(state)), None)
    if verified is not None:
        status, reason, detail = "COVERED", "", "页面证据可读且终点已复验"
        evidence = (verified,)
    elif matches:
        status, reason, detail = (
            "INCONCLUSIVE",
            "ENDPOINT_NOT_REVERIFIED",
            "页面已到达，但终点未通过保留预算复验",
        )
        evidence = (matches[0],)
    else:
        reason, detail = _potential_blindness(
            stages,
            states,
            branch_status=branch_status,
            stop_reason=stop_reason,
        )
        status = "INCONCLUSIVE" if reason != "PATH_MISSING" else "MISSING"
        evidence = ()
    return BusinessCoverageItemResult(
        key=str(item.get("key") or ""),
        label=str(item.get("label") or ""),
        branch_key=branch_key,
        required=bool(item.get("required", True)),
        status=status,
        evidence_state_ids=tuple(state.id for state in evidence),
        evidence_screenshots=tuple(
            str(getattr(state, "screenshot_path", "") or "")
            for state in evidence
            if str(getattr(state, "screenshot_path", "") or "")
        ),
        deepest_stage=1 if evidence else 0,
        total_stages=1,
        reason_code=reason,
        detail=detail,
    )


def _bottom_tabs_result(
    item: Mapping[str, Any],
    branch_key: str,
    states: Sequence[StateEvidence],
    transitions: Sequence[TransitionEvidence],
    *,
    branch_status: str,
    stop_reason: str,
) -> BusinessCoverageItemResult:
    by_id = {state.id: state for state in states}
    expected = {
        "HOME",
        "CATALOG_CATEGORY",
        "COMMUNITY_FEED",
        "CART",
        "PROFILE",
    }
    best_states: dict[str, StateEvidence] = {}
    best_edges: dict[str, TransitionEvidence] = {}
    for home in sorted(states, key=lambda row: row.id):
        if (
            str(home.page_subtype or "").upper() != "HOME"
            or home.xml_text is None
            or not all(
                re.search(label, home.xml_text, re.I)
                for label in (r"首页", r"分类", r"许愿池", r"购物车", r"我的")
            )
        ):
            continue
        targets = {"HOME": home}
        target_edges: dict[str, TransitionEvidence] = {}
        for edge in sorted(transitions, key=lambda row: row.id):
            if (
                not _is_real_edge(edge)
                or edge.from_state_id != home.id
                or not str(edge.action_role or "").upper().startswith("NAV:")
            ):
                continue
            target = by_id.get(int(edge.to_state_id or 0))
            if target is None or target.branch_run_id != home.branch_run_id:
                continue
            subtype = str(target.page_subtype or "UNKNOWN").upper()
            if subtype in expected and subtype not in targets:
                targets[subtype] = target
                target_edges[subtype] = edge
        if len(targets) > len(best_states):
            best_states, best_edges = targets, target_edges
        if set(targets) == expected:
            break
    complete = set(best_states) == expected
    endpoints_verified = complete and all(
        _endpoint_is_verified(state)
        for subtype, state in best_states.items()
        if subtype != "HOME"
    )
    if endpoints_verified:
        status, reason, detail = (
            "COVERED",
            "",
            "首页及四个底栏目的地均由真实 Transition 到达并完成复验",
        )
    elif complete:
        status, reason, detail = (
            "INCONCLUSIVE",
            "ENDPOINT_NOT_REVERIFIED",
            "五个底栏页面均已到达，但至少一个目的地未完成终点复验",
        )
    else:
        reason, detail = _potential_blindness(
            list(item.get("stages") or []),
            states,
            branch_status=branch_status,
            stop_reason=stop_reason,
        )
        status = "INCONCLUSIVE" if reason != "PATH_MISSING" else "MISSING"
        missing = sorted(expected - set(best_states))
        detail = f"缺少底栏真实目的地: {', '.join(missing)}；{detail}"
    ordered = [
        best_states[subtype]
        for subtype in ("HOME", "CATALOG_CATEGORY", "COMMUNITY_FEED", "CART", "PROFILE")
        if subtype in best_states
    ]
    return BusinessCoverageItemResult(
        key=str(item.get("key") or ""),
        label=str(item.get("label") or ""),
        branch_key=branch_key,
        required=bool(item.get("required", True)),
        status=status,
        evidence_state_ids=tuple(state.id for state in ordered),
        evidence_transition_ids=tuple(
            best_edges[subtype].id
            for subtype in ("CATALOG_CATEGORY", "COMMUNITY_FEED", "CART", "PROFILE")
            if subtype in best_edges
        ),
        evidence_screenshots=tuple(
            str(getattr(state, "screenshot_path", "") or "")
            for state in ordered
            if str(getattr(state, "screenshot_path", "") or "")
        ),
        deepest_stage=len(best_states),
        total_stages=5,
        reason_code=reason,
        detail=detail,
    )


def _scope_verdict(items: Sequence[BusinessCoverageItemResult]) -> str:
    required = [item for item in items if item.required and item.status != "NOT_IN_SCOPE"]
    if required and all(item.status == "COVERED" for item in required):
        return "COMPLETE"
    covered = sum(item.status == "COVERED" for item in required)
    if covered:
        return "PARTIAL"
    if any(item.status == "INCONCLUSIVE" for item in required):
        return "INCONCLUSIVE"
    return "INCOMPLETE"


def evaluate_haier_business_coverage(
    *,
    states: Sequence[StateEvidence],
    transitions: Sequence[TransitionEvidence],
    selected_branches: Sequence[str],
    branch_run_keys: Optional[Mapping[int, str]] = None,
    branch_statuses: Optional[Mapping[str, str]] = None,
    branch_stop_reasons: Optional[Mapping[str, str]] = None,
    run_stop_reason: str = "",
    manifest: Optional[Mapping[str, Any]] = None,
    contract_conflict_count: int = 0,
    evaluated_at: Optional[datetime] = None,
) -> dict[str, Any]:
    """Evaluate the frozen v2 manifest against auditable graph evidence."""
    frozen = copy.deepcopy(dict(manifest or HAIER_MALL_V2_MANIFEST))
    expected_hash = str(frozen.pop("hash", "") or "")
    actual_hash = manifest_hash(frozen)
    hash_valid = not expected_hash or expected_hash == actual_hash
    frozen["hash"] = expected_hash or actual_hash
    selected = [branch for branch in BRANCHES if branch in set(selected_branches or ())]
    keys = {int(key): str(value) for key, value in (branch_run_keys or {}).items()}
    statuses = {str(key): str(value) for key, value in (branch_statuses or {}).items()}
    stop_reasons = {
        str(key): str(value) for key, value in (branch_stop_reasons or {}).items()
    }

    def branch_for_state(state: StateEvidence) -> str:
        return str(getattr(state, "branch_key", "") or keys.get(state.branch_run_id, ""))

    results: list[BusinessCoverageItemResult] = []
    for branch_key in BRANCHES:
        branch_states = [state for state in states if branch_for_state(state) == branch_key]
        branch_ids = {state.id for state in branch_states}
        branch_transitions = [
            edge
            for edge in transitions
            if edge.from_state_id in branch_ids
            and (edge.to_state_id is None or edge.to_state_id in branch_ids)
        ]
        for item in frozen.get("journeys") or []:
            scoped_branches = set(item.get("branches") or ())
            if branch_key not in selected or branch_key not in scoped_branches:
                results.append(
                    BusinessCoverageItemResult(
                        key=str(item.get("key") or ""),
                        label=str(item.get("label") or ""),
                        branch_key=branch_key,
                        required=bool(item.get("required", True)),
                        status="NOT_IN_SCOPE",
                        total_stages=(
                            5
                            if item.get("kind") == "bottom_tabs"
                            else len(item.get("stages") or ())
                        ),
                        reason_code="BRANCH_NOT_SELECTED",
                        detail="本次 Run 未选择该业务线或旅程不适用于该业务线",
                    )
                )
                continue
            common = {
                "branch_status": statuses.get(branch_key, ""),
                "stop_reason": stop_reasons.get(branch_key, run_stop_reason),
            }
            kind = str(item.get("kind") or "path")
            if kind == "bottom_tabs":
                result = _bottom_tabs_result(
                    item,
                    branch_key,
                    branch_states,
                    branch_transitions,
                    **common,
                )
            elif kind == "state":
                result = _result_from_state(
                    item,
                    branch_key,
                    branch_states,
                    **common,
                )
            elif kind == "payment_safety_path":
                result = _result_from_payment_graph(
                    item,
                    branch_key,
                    branch_states,
                    branch_transitions,
                    **common,
                )
            elif kind == "fixed_search_path":
                if len(item.get("stages") or ()) == 4:
                    result = _result_from_fixed_search_graph(
                        item,
                        branch_key,
                        branch_states,
                        branch_transitions,
                        **common,
                    )
                else:
                    result = _result_from_path(
                        item,
                        branch_key,
                        branch_states,
                        branch_transitions,
                        **common,
                    )
            else:
                result = _result_from_path(
                    item,
                    branch_key,
                    branch_states,
                    branch_transitions,
                    **common,
                )
            if result.status not in ITEM_STATUSES:
                raise ValueError(f"invalid coverage item status: {result.status}")
            results.append(result)

    selected_results = [
        item
        for item in results
        if item.branch_key in selected and item.status != "NOT_IN_SCOPE"
    ]
    selected_verdict = _scope_verdict(selected_results)
    if not hash_valid:
        selected_verdict = "INCONCLUSIVE"
    all_required = [
        item
        for item in results
        if item.required
        and item.branch_key in BRANCHES
        and item.status != "NOT_IN_SCOPE"
    ]
    both_selected = set(selected) == set(BRANCHES)
    full_complete = bool(
        hash_valid
        and both_selected
        and all_required
        and all(item.status == "COVERED" for item in all_required)
    )
    covered_required = sum(
        item.required and item.status == "COVERED" for item in selected_results
    )
    total_required = sum(item.required for item in selected_results)
    full_verdict = "INCOMPLETE" if not hash_valid or not both_selected else (
        "COMPLETE"
        if full_complete
        else "PARTIAL"
        if covered_required
        else "INCOMPLETE"
    )

    xml_missing = sum(state.xml_text is None for state in states)
    unknown = sum(
        str(state.page_subtype or "UNKNOWN").upper() == "UNKNOWN" for state in states
    )
    opaque = sum(
        bool(getattr(state, "is_opaque", False))
        or str(state.page_subtype or "").upper() == "OPAQUE"
        for state in states
    )
    required_reverify_failures = sum(
        item.required
        and item.status == "INCONCLUSIVE"
        and item.reason_code == "ENDPOINT_NOT_REVERIFIED"
        for item in selected_results
    )
    optional_reverify_failures = sum(
        not item.required
        and item.status == "INCONCLUSIVE"
        and item.reason_code == "ENDPOINT_NOT_REVERIFIED"
        for item in selected_results
    )
    blind_spots: list[dict[str, Any]] = []
    if not hash_valid:
        blind_spots.append(
            {
                "type": "MANIFEST_HASH_MISMATCH",
                "severity": "HIGH",
                "message": "冻结覆盖清单与创建时哈希不一致，结论不可采信",
            }
        )
    for missing_branch in sorted(set(BRANCHES) - set(selected)):
        blind_spots.append(
            {
                "type": "UNRUN_BRANCH",
                "branch_key": missing_branch,
                "severity": "HIGH",
                "message": f"未运行 {missing_branch} 业务线",
            }
        )
    for kind, count, severity, message in (
        ("XML_MISSING", xml_missing, "HIGH", "存在 XML 缺失或不可读取的页面"),
        ("UNKNOWN_PAGE", unknown, "MEDIUM", "存在无法识别的页面"),
        ("OPAQUE_PAGE", opaque, "HIGH", "存在不透明页面"),
        (
            "CONTRACT_CONFLICT",
            int(contract_conflict_count),
            "HIGH",
            "存在覆盖 Contract 冲突",
        ),
        (
            "REVERIFY_FAILED",
            required_reverify_failures,
            "HIGH",
            "存在终点复验失败的必达旅程",
        ),
        (
            "OPTIONAL_REVERIFY_FAILED",
            optional_reverify_failures,
            "MEDIUM",
            "存在终点复验失败的可选旅程",
        ),
    ):
        if count:
            blind_spots.append(
                {
                    "type": kind,
                    "count": count,
                    "severity": severity,
                    "message": message,
                }
            )
    if any(
        marker in f"{run_stop_reason} {' '.join(stop_reasons.values())}".upper()
        for marker in ("预算", "BUDGET", "QUEUE", "FRONTIER")
    ):
        blind_spots.append(
            {
                "type": "BUDGET_STOP",
                "severity": "HIGH",
                "message": "任务因预算或探索前沿边界停止",
            }
        )

    verified_endpoints = len(
        {
            item.evidence_state_ids[-1]
            for item in selected_results
            if item.status == "COVERED" and item.evidence_state_ids
        }
    )
    evidence_quality = (
        "LOW"
        if not hash_valid
        else "HIGH"
        if not blind_spots and selected_verdict == "COMPLETE"
        else "MEDIUM"
        if verified_endpoints and xml_missing == 0 and opaque == 0
        else "LOW"
    )
    branch_payload = []
    for branch_key in BRANCHES:
        branch_items = [item for item in results if item.branch_key == branch_key]
        in_scope = branch_key in selected
        branch_payload.append(
            {
                "branch_key": branch_key,
                "selected": in_scope,
                "verdict": _scope_verdict(branch_items) if in_scope else "NOT_IN_SCOPE",
                "covered_required": sum(
                    item.required and item.status == "COVERED" for item in branch_items
                ),
                "total_required": sum(
                    item.required and item.status != "NOT_IN_SCOPE" for item in branch_items
                ),
                "items": [item.to_dict() for item in branch_items],
            }
        )
    return {
        "schema_version": 2,
        "assessment_origin": "RUNTIME_V2",
        "manifest": {
            "id": str(frozen.get("id") or MANIFEST_ID),
            "version": int(frozen.get("version") or MANIFEST_VERSION),
            "hash": str(frozen.get("hash") or actual_hash),
            "hash_valid": hash_valid,
        },
        "selected_branches": selected,
        "selected_scope_verdict": selected_verdict,
        "full_app_verdict": full_verdict,
        "coverage_verdict": full_verdict,
        "summary": {
            "covered_required": covered_required,
            "total_required": total_required,
            "required_ratio": (
                round(covered_required / total_required, 4) if total_required else 0.0
            ),
            "covered_optional": sum(
                not item.required and item.status == "COVERED" for item in selected_results
            ),
            "total_optional": sum(not item.required for item in selected_results),
            "scope_branches_selected": len(selected),
            "scope_branches_covered": sum(
                row["verdict"] == "COMPLETE" for row in branch_payload
            ),
            "scope_branches_total": len(BRANCHES),
            "evidence_quality": evidence_quality,
            "verified_endpoint_count": verified_endpoints,
        },
        "blind_spots": blind_spots,
        "branches": branch_payload,
        "evaluated_at": (evaluated_at or datetime.now()).isoformat(),
    }


@dataclass
class CoverageGoalProgress:
    key: str
    label: str
    deepest_stage: int
    total_stages: int
    covered: bool
    next_source_subtypes: tuple[str, ...] = ()
    next_role_patterns: tuple[str, ...] = ()


@dataclass
class CoverageGoalTracker:
    """Small runtime scheduler view over unresolved required journeys."""

    branch_key: str
    manifest: Mapping[str, Any] = field(
        default_factory=lambda: copy.deepcopy(HAIER_MALL_V2_MANIFEST)
    )
    _progress: dict[str, CoverageGoalProgress] = field(default_factory=dict, init=False)
    _state_subtypes: dict[int, str] = field(default_factory=dict, init=False)
    _path_frontiers: dict[str, dict[int, set[int]]] = field(
        default_factory=dict,
        init=False,
    )
    _bottom_destinations: set[str] = field(default_factory=set, init=False)
    _fixed_search_entries: dict[str, set[int]] = field(default_factory=dict, init=False)
    _fixed_search_ready: dict[str, set[int]] = field(default_factory=dict, init=False)
    _fixed_search_results: dict[str, set[int]] = field(default_factory=dict, init=False)
    _payment_details: dict[str, set[int]] = field(default_factory=dict, init=False)
    _payment_spec_details: dict[str, set[int]] = field(default_factory=dict, init=False)
    _payment_option_origins: dict[str, dict[int, int]] = field(
        default_factory=dict,
        init=False,
    )
    _payment_checkout_origins: dict[str, dict[int, int]] = field(
        default_factory=dict,
        init=False,
    )
    _payment_pending_checkouts: dict[str, dict[int, set[int]]] = field(
        default_factory=dict,
        init=False,
    )
    _payment_transparent_origins: dict[str, dict[int, int]] = field(
        default_factory=dict,
        init=False,
    )
    _payment_cashier_origins: dict[str, dict[int, int]] = field(
        default_factory=dict,
        init=False,
    )

    def __post_init__(self) -> None:
        for item in self.manifest.get("journeys") or []:
            if not item.get("required", True) or self.branch_key not in set(
                item.get("branches") or ()
            ):
                continue
            total = (
                5
                if item.get("kind") == "bottom_tabs"
                else len(item.get("stages") or ())
            )
            self._progress[str(item.get("key") or "")] = CoverageGoalProgress(
                key=str(item.get("key") or ""),
                label=str(item.get("label") or ""),
                deepest_stage=0,
                total_stages=total,
                covered=False,
            )

    @staticmethod
    def _stage_accepts(stage: Mapping[str, Any], subtype: str) -> bool:
        accepted = {str(value).upper() for value in stage.get("subtypes") or ()}
        return bool(accepted and str(subtype or "UNKNOWN").upper() in accepted)

    def observe_state(self, state_id: int, page_subtype: str) -> None:
        subtype = str(page_subtype or "UNKNOWN").upper()
        self._state_subtypes[int(state_id)] = subtype
        for item in self.manifest.get("journeys") or []:
            key = str(item.get("key") or "")
            progress = self._progress.get(key)
            if progress is None or progress.covered:
                continue
            stages = list(item.get("stages") or [])
            if item.get("kind") == "state" and stages and self._stage_accepts(
                stages[0], subtype
            ):
                progress.deepest_stage = 1
                progress.covered = True
            elif (
                item.get("kind") == "payment_safety_path"
                and stages
                and self._stage_accepts(stages[0], subtype)
            ):
                self._payment_details.setdefault(key, set()).add(int(state_id))
                progress.deepest_stage = max(progress.deepest_stage, 1)
            elif stages and self._stage_accepts(stages[0], subtype):
                self._path_frontiers.setdefault(key, {}).setdefault(0, set()).add(
                    int(state_id)
                )
                progress.deepest_stage = max(progress.deepest_stage, 1)

    def observe_transition(
        self,
        *,
        from_state_id: int,
        to_state_id: Optional[int],
        action_role: str,
        status: str,
        execution_disposition: str,
        risk_type: str = "",
        input_rule_id: str = "",
        input_length: Optional[int] = None,
    ) -> None:
        source = self._state_subtypes.get(int(from_state_id), "UNKNOWN")
        target = (
            self._state_subtypes.get(int(to_state_id), "UNKNOWN")
            if to_state_id is not None
            else ""
        )
        normalized_status = str(status or "").upper()
        disposition = str(execution_disposition or "").upper()
        role = str(action_role or "")
        real_edge = bool(
            to_state_id is not None
            and normalized_status in SUCCESSFUL_EDGE_STATUSES
            and disposition == "EXECUTED"
        )
        for item in self.manifest.get("journeys") or []:
            key = str(item.get("key") or "")
            progress = self._progress.get(key)
            if progress is None or progress.covered:
                continue
            kind = str(item.get("kind") or "path")
            if kind == "bottom_tabs":
                if source == "HOME" and real_edge and target in {
                    "CATALOG_CATEGORY",
                    "COMMUNITY_FEED",
                    "CART",
                    "PROFILE",
                }:
                    self._bottom_destinations.add(target)
                    progress.deepest_stage = 1 + len(self._bottom_destinations)
                    progress.covered = progress.deepest_stage >= 5
                continue
            stages = list(item.get("stages") or [])
            if not stages:
                continue
            if kind == "payment_safety_path" and len(stages) == 4:
                role_patterns = [
                    str(value) for value in item.get("edge_role_patterns") or ()
                ]
                role_patterns.extend(("",) * (3 - len(role_patterns)))

                def payment_role_matches(index: int) -> bool:
                    pattern = role_patterns[index]
                    return not pattern or re.fullmatch(pattern, role, re.I) is not None

                details = self._payment_details.setdefault(key, set())
                spec_details = self._payment_spec_details.setdefault(key, set())
                option_origins = self._payment_option_origins.setdefault(key, {})
                checkout_origins = self._payment_checkout_origins.setdefault(key, {})
                pending_checkouts = self._payment_pending_checkouts.setdefault(key, {})
                transparent_origins = self._payment_transparent_origins.setdefault(
                    key,
                    {},
                )
                cashier_origins = self._payment_cashier_origins.setdefault(key, {})
                source_id = int(from_state_id)
                target_id = int(to_state_id or 0)
                if (
                    normalized_status == "BLOCKED"
                    and source_id in cashier_origins
                    and str(risk_type or "").upper() == "PAYMENT"
                    and disposition in {"EXECUTED", "SKIPPED"}
                ):
                    progress.deepest_stage = len(stages)
                    progress.covered = True
                    continue
                if not real_edge:
                    continue
                if (
                    source_id in details
                    and self._stage_accepts(stages[1], target)
                    and payment_role_matches(0)
                ):
                    option_origins[target_id] = source_id
                    spec_details.add(source_id)
                    for checkout_id in pending_checkouts.get(source_id, set()):
                        checkout_origins[checkout_id] = source_id
                    progress.deepest_stage = max(
                        progress.deepest_stage,
                        3 if pending_checkouts.get(source_id) else 2,
                    )
                    continue
                option_origin = option_origins.get(source_id)
                if option_origin is not None:
                    if (
                        target_id == option_origin
                        and self._stage_accepts(stages[0], target)
                    ):
                        details.add(target_id)
                        spec_details.add(target_id)
                        for checkout_id in pending_checkouts.get(target_id, set()):
                            checkout_origins[checkout_id] = target_id
                        progress.deepest_stage = max(
                            progress.deepest_stage,
                            3 if pending_checkouts.get(target_id) else 2,
                        )
                        continue
                    if (
                        self._stage_accepts(stages[2], target)
                        and payment_role_matches(1)
                    ):
                        checkout_origins[target_id] = option_origin
                        progress.deepest_stage = max(progress.deepest_stage, 3)
                        continue
                if (
                    source_id in details
                    and self._stage_accepts(stages[2], target)
                    and payment_role_matches(1)
                ):
                    if source_id in spec_details:
                        checkout_origins[target_id] = source_id
                        progress.deepest_stage = max(progress.deepest_stage, 3)
                    else:
                        pending_checkouts.setdefault(source_id, set()).add(target_id)
                    continue
                purchase_origin = checkout_origins.get(source_id)
                if purchase_origin is None:
                    purchase_origin = transparent_origins.get(source_id)
                if purchase_origin is not None and payment_role_matches(2):
                    if str(target or "UNKNOWN").upper() in TRANSPARENT_PATH_SUBTYPES:
                        transparent_origins[target_id] = purchase_origin
                        progress.deepest_stage = max(progress.deepest_stage, 3)
                    elif self._stage_accepts(stages[3], target):
                        cashier_origins[target_id] = purchase_origin
                        progress.deepest_stage = len(stages)
                    continue
                continue
            if kind == "fixed_search_path" and len(stages) == 4:
                role_patterns = [
                    str(value) for value in item.get("edge_role_patterns") or ()
                ]
                role_patterns.extend(("",) * (4 - len(role_patterns)))

                def role_matches(index: int) -> bool:
                    pattern = role_patterns[index]
                    return not pattern or re.fullmatch(pattern, role, re.I) is not None

                search_entries = self._fixed_search_entries.setdefault(key, set())
                ready_searches = self._fixed_search_ready.setdefault(key, set())
                result_states = self._fixed_search_results.setdefault(key, set())
                if (
                    real_edge
                    and self._stage_accepts(stages[0], source)
                    and self._stage_accepts(stages[1], target)
                    and role_matches(0)
                ):
                    search_entries.add(int(to_state_id or 0))
                    progress.deepest_stage = max(progress.deepest_stage, 2)
                elif (
                    real_edge
                    and int(from_state_id) in search_entries
                    and role_matches(1)
                    and str(input_rule_id or "") == SEARCH_INPUT_RULE_ID
                    and int(input_length or 0) == len(SEARCH_KEYWORD)
                ):
                    if self._stage_accepts(stages[2], target):
                        result_states.add(int(to_state_id or 0))
                        progress.deepest_stage = max(progress.deepest_stage, 3)
                    elif self._stage_accepts(stages[1], target):
                        search_entries.add(int(to_state_id or 0))
                        ready_searches.add(int(to_state_id or 0))
                        progress.deepest_stage = max(progress.deepest_stage, 2)
                elif (
                    real_edge
                    and int(from_state_id) in ready_searches
                    and self._stage_accepts(stages[2], target)
                    and role_matches(2)
                ):
                    result_states.add(int(to_state_id or 0))
                    progress.deepest_stage = max(progress.deepest_stage, 3)
                elif (
                    real_edge
                    and int(from_state_id) in result_states
                    and self._stage_accepts(stages[3], target)
                    and role_matches(3)
                ):
                    progress.deepest_stage = len(stages)
                    progress.covered = True
                continue
            if not real_edge:
                continue
            frontiers = self._path_frontiers.setdefault(key, {})
            if self._stage_accepts(stages[0], source):
                frontiers.setdefault(0, set()).add(int(from_state_id))
            role_patterns = list(item.get("edge_role_patterns") or [])
            # Extend only a prefix whose concrete endpoint is this edge's
            # source. Descending order prevents one self-loop from satisfying
            # more than one journey edge in a single observation.
            for edge_index in range(len(stages) - 2, -1, -1):
                if int(from_state_id) not in frontiers.get(edge_index, set()):
                    continue
                role_pattern = (
                    role_patterns[edge_index]
                    if edge_index < len(role_patterns)
                    else ""
                )
                if role_pattern and re.fullmatch(
                    str(role_pattern), role, re.I
                ) is None:
                    continue
                if not self._stage_accepts(stages[edge_index + 1], target):
                    continue
                frontiers.setdefault(edge_index + 1, set()).add(
                    int(to_state_id or 0)
                )
                progress.deepest_stage = max(
                    progress.deepest_stage,
                    edge_index + 2,
                )
                if edge_index + 2 >= len(stages):
                    progress.covered = True
                break

    def state_is_required_candidate(self, page_subtype: str) -> bool:
        subtype = str(page_subtype or "UNKNOWN").upper()
        if subtype in TRANSPARENT_PATH_SUBTYPES and any(
            item.get("kind") == "payment_safety_path"
            and (known := self._progress.get(str(item.get("key") or ""))) is not None
            and not known.covered
            and known.deepest_stage >= 3
            for item in self.manifest.get("journeys") or []
            if item.get("required", True)
            and self.branch_key in set(item.get("branches") or ())
        ):
            return True
        return any(
            self._stage_accepts(stage, subtype)
            for item in self.manifest.get("journeys") or []
            if item.get("required", True)
            and self.branch_key in set(item.get("branches") or ())
            for stage in item.get("stages") or ()
            if stage.get("subtypes")
        )

    def refresh(
        self,
        states: Sequence[StateEvidence],
        transitions: Sequence[TransitionEvidence],
    ) -> tuple[CoverageGoalProgress, ...]:
        branch_states = [
            state
            for state in states
            if not getattr(state, "branch_key", "")
            or str(getattr(state, "branch_key", "")) == self.branch_key
        ]
        branch_ids = {state.id for state in branch_states}
        branch_edges = [
            edge
            for edge in transitions
            if edge.from_state_id in branch_ids
            and (edge.to_state_id is None or edge.to_state_id in branch_ids)
        ]
        progress: dict[str, CoverageGoalProgress] = {}
        for item in self.manifest.get("journeys") or []:
            if not item.get("required", True) or self.branch_key not in set(
                item.get("branches") or ()
            ):
                continue
            if item.get("kind") == "bottom_tabs":
                result = _bottom_tabs_result(
                    item,
                    self.branch_key,
                    branch_states,
                    branch_edges,
                    branch_status="RUNNING",
                    stop_reason="",
                )
            elif item.get("kind") == "state":
                result = _result_from_state(
                    item,
                    self.branch_key,
                    branch_states,
                    branch_status="RUNNING",
                    stop_reason="",
                )
            elif item.get("kind") == "payment_safety_path":
                result = _result_from_payment_graph(
                    item,
                    self.branch_key,
                    branch_states,
                    branch_edges,
                    branch_status="RUNNING",
                    stop_reason="",
                )
            elif item.get("kind") == "fixed_search_path":
                if len(item.get("stages") or ()) == 4:
                    result = _result_from_fixed_search_graph(
                        item,
                        self.branch_key,
                        branch_states,
                        branch_edges,
                        branch_status="RUNNING",
                        stop_reason="",
                    )
                else:
                    result = _result_from_path(
                        item,
                        self.branch_key,
                        branch_states,
                        branch_edges,
                        branch_status="RUNNING",
                        stop_reason="",
                    )
            else:
                result = _result_from_path(
                    item,
                    self.branch_key,
                    branch_states,
                    branch_edges,
                    branch_status="RUNNING",
                    stop_reason="",
                )
            hints = list(item.get("action_hints") or [])
            hint_index = min(max(0, result.deepest_stage - 1), max(0, len(hints) - 1))
            hint = hints[hint_index] if hints else {}
            progress[result.key] = CoverageGoalProgress(
                key=result.key,
                label=result.label,
                deepest_stage=result.deepest_stage,
                total_stages=result.total_stages,
                covered=result.status == "COVERED",
                next_source_subtypes=tuple(
                    str(value).upper() for value in hint.get("source_subtypes") or ()
                ),
                next_role_patterns=tuple(
                    str(value) for value in hint.get("role_patterns") or ()
                ),
            )
        self._progress = progress
        return tuple(progress.values())

    @property
    def unresolved(self) -> tuple[CoverageGoalProgress, ...]:
        return tuple(item for item in self._progress.values() if not item.covered)

    def action_is_required(
        self,
        page_subtype: str,
        action_role: str,
        *,
        label: str = "",
        risk_type: str = "",
    ) -> bool:
        subtype = str(page_subtype or "UNKNOWN").upper()
        role = str(action_role or "")
        for item in self.manifest.get("journeys") or []:
            if not item.get("required", True) or self.branch_key not in set(
                item.get("branches") or ()
            ):
                continue
            known = self._progress.get(str(item.get("key") or ""))
            if known is not None and known.covered:
                continue
            hints = list(item.get("action_hints") or [])
            if known is not None and hints and item.get("kind") != "bottom_tabs":
                hint_index = min(
                    max(0, known.deepest_stage - 1),
                    len(hints) - 1,
                )
                hints = [hints[hint_index]]
            for hint in hints:
                if subtype not in {
                    str(value).upper() for value in hint.get("source_subtypes") or ()
                }:
                    continue
                if hint.get("risk_types") and str(risk_type or "").upper() not in {
                    str(value).upper() for value in hint.get("risk_types") or ()
                }:
                    continue
                if not any(
                    re.fullmatch(str(pattern), role, re.I)
                    for pattern in hint.get("role_patterns") or (r".*",)
                ):
                    continue
                if hint.get("label_patterns") and not any(
                    re.search(str(pattern), str(label or ""), re.I)
                    for pattern in hint.get("label_patterns") or ()
                ):
                    continue
                return True
        return False

    def prioritize_actions(
        self,
        page_subtype: str,
        actions: Sequence[Any],
    ) -> list[Any]:
        subtype = str(page_subtype or "UNKNOWN").upper()

        def required_rank(action_role: str) -> int:
            bottom_tabs = next(
                (
                    self._progress.get(str(item.get("key") or ""))
                    for item in self.manifest.get("journeys") or []
                    if item.get("kind") == "bottom_tabs"
                    and item.get("required", True)
                    and self.branch_key in set(item.get("branches") or ())
                ),
                None,
            )
            if (
                bottom_tabs is not None
                and not bottom_tabs.covered
                and subtype
                in {
                    "HOME",
                    "CATALOG_CATEGORY",
                    "COMMUNITY_FEED",
                    "CART",
                    "PROFILE",
                }
            ):
                return 0 if str(action_role or "").upper().startswith("NAV:") else 1
            if subtype != "PRODUCT_DETAIL":
                return 0
            payment = next(
                (
                    self._progress.get(str(item.get("key") or ""))
                    for item in self.manifest.get("journeys") or []
                    if item.get("kind") == "payment_safety_path"
                    and item.get("required", True)
                    and self.branch_key in set(item.get("branches") or ())
                ),
                None,
            )
            if payment is None or payment.covered:
                return 0
            normalized_role = str(action_role or "").upper()
            if payment.deepest_stage <= 1:
                return {
                    "OPTION_SELECT": 0,
                    "ADD_CART": 1,
                    "BUY_NOW": 2,
                }.get(normalized_role, 3)
            if payment.deepest_stage == 2:
                return {
                    "BUY_NOW": 0,
                    "CHECKOUT": 0,
                    "DIALOG_CLOSE": 1,
                    "ADD_CART": 2,
                    "OPTION_SELECT": 3,
                }.get(normalized_role, 4)
            return 0

        return sorted(
            actions,
            key=lambda action: (
                0
                if self.action_is_required(
                    page_subtype,
                    str(getattr(action, "action_role", "") or ""),
                    label=" ".join(
                        str((getattr(action, "target_meta", {}) or {}).get(key) or "")
                        for key in ("content_desc", "text")
                    ),
                    risk_type=str(getattr(action, "risk_type", "") or ""),
                )
                else 1,
                required_rank(str(getattr(action, "action_role", "") or "")),
            ),
        )

    def frontier_priority(self, page_subtype: str, actions: Sequence[Any]) -> Optional[int]:
        return (
            10
            if any(
                self.action_is_required(
                    page_subtype,
                    str(getattr(action, "action_role", "") or ""),
                    label=" ".join(
                        str((getattr(action, "target_meta", {}) or {}).get(key) or "")
                        for key in ("content_desc", "text")
                    ),
                    risk_type=str(getattr(action, "risk_type", "") or ""),
                )
                for action in actions
            )
            else None
        )


def coverage_state_assignments(
    assessment: Mapping[str, Any],
    all_state_ids: Iterable[int],
) -> dict[int, str]:
    """Map frozen item evidence back to the State.coverage_status contract."""
    assignments = {int(state_id): "EXPLORED" for state_id in all_state_ids}
    priority = {
        "EXPLORED": 0,
        "INCOMPLETE": 1,
        "OPTIONAL_EVIDENCE": 2,
        "REQUIRED_EVIDENCE": 3,
    }

    def promote(state_id: int, value: str) -> None:
        current = assignments.get(state_id, "EXPLORED")
        if priority[value] > priority.get(current, 0):
            assignments[state_id] = value

    for branch in assessment.get("branches") or []:
        for item in branch.get("items") or []:
            evidence_ids = [int(value) for value in item.get("evidence_state_ids") or ()]
            if not evidence_ids:
                continue
            if item.get("status") == "COVERED":
                value = "REQUIRED_EVIDENCE" if item.get("required") else "OPTIONAL_EVIDENCE"
                for state_id in evidence_ids:
                    promote(state_id, value)
            elif item.get("required"):
                for state_id in evidence_ids:
                    promote(state_id, "INCOMPLETE")
    return assignments
