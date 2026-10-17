"""App（AutoDroid Runner）直跑支持：脚本下发端点。

App 以 JWT 登录后调用这里获取"可直接执行"的脚本包（JSON 形式）：
- 与 zip 导出共用同一份生成器（backend/autojs_export/packager.py），
  内容 = manifest + runtime.js + main.js + cases/*.js + assets(base64)；
- App 将各文件原样写入运行目录，经运行引擎执行 main.js；
- 执行结果回传 POST /device-runs/report（run_id 幂等；notify 控制飞书通知）。
"""
from __future__ import annotations

import logging
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from sqlmodel import Session

from backend.api import deps
from backend.autojs_export import packager
from backend.database import get_session
from backend.models import TestCase, TestScenario, User

logger = logging.getLogger(__name__)

router = APIRouter()


def _precheck_or_400(bundle: dict) -> dict:
    precheck = bundle.get("precheck") or {}
    if not precheck.get("ok"):
        raise HTTPException(
            status_code=400,
            detail={
                "message": "脚本预检未通过，请先在平台修复导出问题",
                "issues": precheck.get("issues") or [],
            },
        )
    return bundle


@router.get("/cases/{case_id}/script")
def get_case_script_bundle(
    case_id: int,
    env_id: Optional[int] = None,
    session: Session = Depends(get_session),
    current_user: User = Depends(deps.get_current_user),
):
    """下发单个用例的可执行脚本包（App 直跑）。"""
    case = session.get(TestCase, case_id)
    if not case:
        raise HTTPException(status_code=404, detail="用例不存在")
    bundle = packager.build_case_bundle(session, case, env_id=env_id)
    return _precheck_or_400(bundle)


@router.get("/scenarios/{scenario_id}/script")
def get_scenario_script_bundle(
    scenario_id: int,
    env_id: Optional[int] = None,
    session: Session = Depends(get_session),
    current_user: User = Depends(deps.get_current_user),
):
    """下发场景的可执行脚本包（App 直跑）。"""
    scenario = session.get(TestScenario, scenario_id)
    if not scenario:
        raise HTTPException(status_code=404, detail="场景不存在")
    bundle = packager.build_scenario_bundle(session, scenario, env_id=env_id)
    return _precheck_or_400(bundle)
