"""步骤写入乐观锁（P4 功能1）：cases / scenarios steps 全量替换并发防护。

多端（桌面 + Web + App）并发保存步骤时互相覆盖的缺口收口。契约（前端
utils/versionToken.js 已按此实现，桌面保存链路同口径，三端对账以此为准）：

- 版本令牌取值：实体 payload 上有数值 ``version`` 用它（``str(version)``）；
  否则回退 ``updated_at`` 的 ISO 字符串（原样、不带引号）。
- 校验入口：``PUT /api/cases/{id}/steps`` 与 ``POST|PUT /api/scenarios/{id}/steps``
  接受三路令牌：
  - Header ``If-Match: <token>``（不带引号；兼容 ``"3"`` / ``W/"3"`` / ``*``）；
  - Query ``version=<int>``（仅数值令牌时前端发送）；
  - Query ``expected_updated_at=<updated_at ISO>``（实体有 updated_at 时发送）。
- 校验语义：任一令牌存在即进入校验，存在的令牌必须全部匹配（AND），任一
  不匹配 → 409；都缺失 = 跳过校验（兼容旧客户端/App）。
- 冲突响应：HTTP 409，``detail`` 固定文案 STEPS_VERSION_CONFLICT_DETAIL
  「保存冲突：内容已被其他端修改，请重新拉取后再保存」。
- 校验通过后写入并递增 ``version`` / 刷新 ``updated_at``；响应体（实体）携带
  新 ``version`` / ``updated_at`` 供前端回填下一次保存令牌（前端
  versionStateFrom 优先读响应体，其次 X-Version / X-Updated-At / ETag 头）。
- 写入原子性（P4 终验必修）：校验 + 写入 + 递增收敛为一条 CAS UPDATE
  （``write_with_version_lock``）——``UPDATE ... SET <payload>, version=version+1
  WHERE id=:id AND version=:expected``（ISO 令牌则 ``WHERE updated_at=:expected``），
  rowcount=0 → 409。禁止「先读后写」两步校验（真并发下丢失更新）。
- 非整数 / 失配令牌 → 409 伪冲突（平台无 400 路径）；平台同时支持 updated_at
  ISO 令牌（If-Match / ?expected_updated_at=），前端当前只发整数令牌是避免伪
  409 的选择。
"""
from datetime import datetime
from typing import Any, Dict, Optional

from fastapi import HTTPException
from sqlalchemy import update as sa_update

# 409 detail 固定文案（前端 VERSION_CONFLICT_MESSAGE 同文）：
# 「保存冲突：内容已被其他端修改，请重新拉取后再保存」
STEPS_VERSION_CONFLICT_DETAIL = "保存冲突：内容已被其他端修改，请重新拉取后再保存"

# 版本回传统一响应头（前端 extractVersionState 回退认 X-Version / X-Updated-At；
# X-Current-Version 为兼容早期接线保留，值与 X-Version 相同）。
VERSION_HEADER = "X-Current-Version"
VERSION_STATE_HEADER = "X-Version"
UPDATED_AT_HEADER = "X-Updated-At"


def _iso(value: Any) -> Optional[str]:
    """datetime → ISO 字符串（原样回传口径）；非 datetime 原样转字符串。"""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)


def _strip_token(raw: Optional[str]) -> str:
    """剥掉引号 / W/ 弱标记与首尾空白（前端发送不带引号，这里宽容接收）。"""
    token = (raw or "").strip() if isinstance(raw, str) else ""
    if token.startswith("W/"):
        token = token[2:].strip()
    if len(token) >= 2 and token.startswith('"') and token.endswith('"'):
        token = token[1:-1]
    return token


def _same_instant(raw: str, iso_value: Optional[str]) -> bool:
    """ISO 令牌匹配：字符串原样相等，或解析后为同一时刻（格式差异不误伤）。"""
    if iso_value is None:
        return False
    if raw == iso_value:
        return True
    try:
        left = datetime.fromisoformat(raw)
        right = datetime.fromisoformat(iso_value)
    except (TypeError, ValueError):
        return False
    return left == right


def current_version_token(entity: Any) -> str:
    """版本令牌：实体有数值 version 用它，否则回退 updated_at 的 ISO 字符串。"""
    version = getattr(entity, "version", None)
    if version is not None:
        return str(int(version))
    updated_at = getattr(entity, "updated_at", None)
    return _iso(updated_at) or ""


def ensure_version_tokens_match(
    entity: Any,
    *,
    if_match: Optional[str] = None,
    version: Optional[int] = None,
    expected_updated_at: Optional[str] = None,
) -> Optional[int]:
    """校验写入令牌（AND 语义），返回校验基准 version（供递增）；全缺失返回 None。

    - 三路令牌全缺失 → 跳过校验（旧客户端兼容），返回 None；
    - 任一存在即进入校验，存在的令牌必须全部匹配，任一不匹配 → 409；
    - ``If-Match: *`` 视为无条件（该路不参与校验）；
    - If-Match 匹配：str(version)（实体有 version）或 updated_at ISO（回退令牌）；
    - ``version`` 查询参数匹配数值 version；
    - ``expected_updated_at`` 匹配 updated_at ISO（同一时刻）。

    注：直接函数调用（测试/内部）时 FastAPI 的 Header/Query 哨兵默认值会落到
    这里，按「未传」处理。
    """
    current_raw = getattr(entity, "version", None)
    try:
        current_version = int(current_raw) if current_raw is not None else None
    except (TypeError, ValueError):
        current_version = None
    updated_at_iso = _iso(getattr(entity, "updated_at", None))

    raw_if_match = if_match.strip() if isinstance(if_match, str) else ""
    if_match_token = _strip_token(if_match)
    has_if_match = bool(raw_if_match) and if_match_token != "*"
    has_version = isinstance(version, (int, str)) and not isinstance(version, bool) and str(version) != ""
    has_updated_at = isinstance(expected_updated_at, str) and bool(expected_updated_at.strip())

    if not (has_if_match or has_version or has_updated_at):
        return None  # 都缺失：跳过校验（兼容旧客户端/App）

    def _conflict() -> None:
        headers = {}
        if current_version is not None:
            headers[VERSION_HEADER] = str(current_version)
            headers[VERSION_STATE_HEADER] = str(current_version)
        raise HTTPException(
            status_code=409,
            detail=STEPS_VERSION_CONFLICT_DETAIL,
            headers=headers or None,
        )

    if has_if_match:
        token_matches = (
            (current_version is not None and if_match_token == str(current_version))
            or (updated_at_iso is not None and _same_instant(if_match_token, updated_at_iso))
        )
        if not token_matches:
            _conflict()

    if has_version:
        try:
            expected_int = int(str(version).strip())
        except (TypeError, ValueError):
            _conflict()
            return None  # unreachable：_conflict 必抛
        if current_version is None or expected_int != current_version:
            _conflict()

    if has_updated_at:
        if not _same_instant(expected_updated_at.strip(), updated_at_iso):
            _conflict()

    return current_version if current_version is not None else 1


def bump_version(current: Optional[int]) -> int:
    """步骤写入成功后自增版本（NULL 视为 1）。"""
    return int(current or 1) + 1


def _conflict_headers(current_version: Optional[int]) -> Optional[dict]:
    headers: Dict[str, str] = {}
    if current_version is not None:
        headers[VERSION_HEADER] = str(current_version)
        headers[VERSION_STATE_HEADER] = str(current_version)
    return headers or None


def raise_version_conflict(current_version: Optional[int]) -> None:
    """409 冲突（冻结文案），头带当前 version（前端回填令牌用）。"""
    raise HTTPException(
        status_code=409,
        detail=STEPS_VERSION_CONFLICT_DETAIL,
        headers=_conflict_headers(current_version),
    )


def _parse_version_guard(
    entity: Any,
    *,
    if_match: Optional[str],
    version: Optional[int],
    expected_updated_at: Optional[str],
) -> "VersionGuard":
    """解析三路令牌 → CAS WHERE 条件（AND 语义）；全缺失返回空 guard。

    与 ensure_version_tokens_match 同口径：非整数 / 非法或带时区 ISO / 既非
    整数又非 ISO 的 If-Match 令牌都永远无法命中 → 409 伪冲突（平台无 400
    路径）。If-Match 数值令牌必须是规范十进制（``str(int(token)) == token``）
    才走 version 条件，其余按 ISO 令牌解析。
    """
    guard = VersionGuard()

    raw_if_match = if_match.strip() if isinstance(if_match, str) else ""
    if_match_token = _strip_token(if_match)
    if raw_if_match and if_match_token != "*":
        is_canonical_int = (
            if_match_token.lstrip("-").isdigit() and if_match_token == str(int(if_match_token))
        )
        if is_canonical_int:
            guard.version = int(if_match_token)
            guard.has_token = True
        else:
            parsed = _parse_iso_token(if_match_token, entity)
            guard.updated_at = parsed
            guard.has_token = True

    if isinstance(version, (int, str)) and not isinstance(version, bool) and str(version) != "":
        try:
            expected_int = int(str(version).strip())
        except (TypeError, ValueError):
            raise_version_conflict(getattr(entity, "version", None))
            raise AssertionError("unreachable")  # raise_version_conflict 必抛
        if guard.version is not None and guard.version != expected_int:
            guard.version_mismatch = True
        guard.version = expected_int
        guard.has_token = True

    if isinstance(expected_updated_at, str) and expected_updated_at.strip():
        parsed = _parse_iso_token(expected_updated_at.strip(), entity)
        if guard.updated_at is not None and guard.updated_at != parsed:
            guard.updated_at_mismatch = True
        guard.updated_at = parsed
        guard.has_token = True

    return guard


def _parse_iso_token(token: str, entity: Any) -> datetime:
    """ISO 令牌 → datetime；非法 / 带时区 → 409 伪冲突（旧口径 aware vs naive 永不相等）。"""
    try:
        parsed = datetime.fromisoformat(token)
    except (TypeError, ValueError):
        raise_version_conflict(getattr(entity, "version", None))
        raise AssertionError("unreachable")  # raise_version_conflict 必抛
    if parsed.tzinfo is not None:
        raise_version_conflict(getattr(entity, "version", None))
        raise AssertionError("unreachable")
    return parsed


class VersionGuard:
    """解析后的 CAS WHERE 条件（AND 语义）。*_mismatch 表示两路令牌互相矛盾。"""

    __slots__ = ("version", "updated_at", "has_token", "version_mismatch", "updated_at_mismatch")

    def __init__(self) -> None:
        self.version: Optional[int] = None
        self.updated_at: Optional[datetime] = None
        self.has_token = False
        self.version_mismatch = False
        self.updated_at_mismatch = False


def write_with_version_lock(
    session: Any,
    entity: Any,
    *,
    values: Dict[str, Any],
    if_match: Optional[str] = None,
    version: Optional[int] = None,
    expected_updated_at: Optional[str] = None,
    missing_detail: str = "资源不存在",
) -> None:
    """校验 + 写入 + 版本递增一条原子 CAS UPDATE（P4 终验必修缺陷 1）。

    - 三路令牌解析为 WHERE 条件并入 UPDATE（AND）：version 令牌 →
      ``WHERE version=:expected``；ISO updated_at 令牌 → ``WHERE updated_at=:expected``；
      都缺失 = 跳过校验（旧客户端兼容）但仍递增 version；
    - payload（steps 等）与 ``version=version+1`` 在同一条 UPDATE / 同一事务，
      消除 check-then-write 丢失更新；
    - rowcount=0：令牌条件失配 → 409（冻结文案，头带当前 version）；行消失 → 404；
    - 令牌结构性非法（非整数 version / 非法或带时区 ISO / 非数字非 ISO 的
      If-Match）→ 409 伪冲突（非整数/失配令牌 → 409，平台无 400 路径）。
    """
    from sqlalchemy import func, select

    model = type(entity)
    entity_id = entity.id
    guard = _parse_version_guard(
        entity,
        if_match=if_match,
        version=version,
        expected_updated_at=expected_updated_at,
    )

    if guard.version_mismatch or guard.updated_at_mismatch:
        # 两路令牌互相矛盾（如 If-Match=3 + ?version=2）：不可能同时命中 → 409
        raise_version_conflict(getattr(entity, "version", None))
        return  # unreachable

    stmt = (
        sa_update(model)
        .where(model.id == entity_id)
        .values(**values, version=func.coalesce(model.version, 1) + 1)
    )
    if guard.version is not None:
        stmt = stmt.where(model.version == guard.version)
    if guard.updated_at is not None:
        stmt = stmt.where(model.updated_at == guard.updated_at)

    result = session.execute(stmt, execution_options={"synchronize_session": False})
    if getattr(result, "rowcount", 0) == 0:
        # 失配或行消失：读当前 version 填 409 头（列级 select，绕过 ORM 缓存）
        current = session.execute(
            select(model.version).where(model.id == entity_id)
        ).scalar()
        if current is None and session.execute(
            select(model.id).where(model.id == entity_id)
        ).scalar() is None:
            raise HTTPException(status_code=404, detail=missing_detail)
        try:
            current_version = int(current) if current is not None else None
        except (TypeError, ValueError):
            current_version = None
        raise_version_conflict(current_version)

    session.refresh(entity)


def set_version_header(response, version: int, updated_at: Any = None) -> None:
    """把当前版本状态写进响应头（response 为 None 时跳过：直接函数调用的测试路径）。"""
    if response is None:
        return
    response.headers[VERSION_HEADER] = str(version)
    response.headers[VERSION_STATE_HEADER] = str(version)
    if updated_at is not None:
        response.headers[UPDATED_AT_HEADER] = _iso(updated_at) or ""


def version_state_payload(version: int, updated_at: Any) -> dict:
    """响应体内的新令牌字段（前端 versionStateFrom 认领顶层 version / updated_at）。"""
    return {"version": int(version), "updated_at": _iso(updated_at)}
