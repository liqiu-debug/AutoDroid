from typing import List
from datetime import datetime
from fastapi import APIRouter, Depends, HTTPException
from sqlmodel import Session, select

from backend.database import get_session
from backend.models import Environment, GlobalVariable
from backend.schemas import (
    EnvironmentCreate, EnvironmentRead,
    GlobalVariableCreate, GlobalVariableRead, GlobalVariableUpdate,
)

router = APIRouter()


# ==================== Environment CRUD ====================

@router.get("/", response_model=List[EnvironmentRead])
def list_environments(session: Session = Depends(get_session)):
    envs = session.exec(select(Environment).order_by(Environment.id)).all()
    return envs


@router.post("/", response_model=EnvironmentRead)
def create_environment(payload: EnvironmentCreate, session: Session = Depends(get_session)):
    env = Environment(name=payload.name, description=payload.description)
    session.add(env)
    session.commit()
    session.refresh(env)
    return env


@router.put("/{env_id}", response_model=EnvironmentRead)
def update_environment(env_id: int, payload: EnvironmentCreate, session: Session = Depends(get_session)):
    env = session.get(Environment, env_id)
    if not env:
        raise HTTPException(status_code=404, detail="环境不存在")
    env.name = payload.name
    env.description = payload.description
    session.add(env)
    session.commit()
    session.refresh(env)
    return env


@router.delete("/{env_id}")
def delete_environment(env_id: int, session: Session = Depends(get_session)):
    env = session.get(Environment, env_id)
    if not env:
        raise HTTPException(status_code=404, detail="环境不存在")
    # 级联删除该环境下所有变量
    variables = session.exec(select(GlobalVariable).where(GlobalVariable.env_id == env_id)).all()
    for v in variables:
        session.delete(v)
    session.delete(env)
    session.commit()
    return {"message": "环境已删除", "id": env_id}


# ==================== Variable CRUD ====================

@router.get("/{env_id}/variables", response_model=List[GlobalVariableRead])
def list_variables(env_id: int, session: Session = Depends(get_session)):
    env = session.get(Environment, env_id)
    if not env:
        raise HTTPException(status_code=404, detail="环境不存在")
    variables = session.exec(
        select(GlobalVariable).where(GlobalVariable.env_id == env_id).order_by(GlobalVariable.id)
    ).all()
    return variables


@router.post("/{env_id}/variables", response_model=GlobalVariableRead)
def create_variable(env_id: int, payload: GlobalVariableCreate, session: Session = Depends(get_session)):
    env = session.get(Environment, env_id)
    if not env:
        raise HTTPException(status_code=404, detail="环境不存在")
    existing = session.exec(
        select(GlobalVariable).where(
            GlobalVariable.env_id == env_id,
            GlobalVariable.key == payload.key
        )
    ).first()
    if existing:
        raise HTTPException(status_code=400, detail=f"变量 Key '{payload.key}' 在该环境下已存在")
    var = GlobalVariable(
        env_id=env_id,
        key=payload.key,
        value=payload.value,
        is_secret=payload.is_secret,
        description=payload.description,
    )
    session.add(var)
    session.commit()
    session.refresh(var)
    return var


@router.put("/variables/{var_id}", response_model=GlobalVariableRead)
def update_variable(var_id: int, payload: GlobalVariableUpdate, session: Session = Depends(get_session)):
    var = session.get(GlobalVariable, var_id)
    if not var:
        raise HTTPException(status_code=404, detail="变量不存在")
    if payload.key is not None:
        dup = session.exec(
            select(GlobalVariable).where(
                GlobalVariable.env_id == var.env_id,
                GlobalVariable.key == payload.key,
                GlobalVariable.id != var_id,
            )
        ).first()
        if dup:
            raise HTTPException(status_code=400, detail=f"变量 Key '{payload.key}' 在该环境下已存在")
        var.key = payload.key
    if payload.value is not None:
        var.value = payload.value
    if payload.is_secret is not None:
        var.is_secret = payload.is_secret
    if payload.description is not None:
        var.description = payload.description
    var.updated_at = datetime.now()
    session.add(var)
    session.commit()
    session.refresh(var)
    return var


@router.delete("/variables/{var_id}")
def delete_variable(var_id: int, session: Session = Depends(get_session)):
    var = session.get(GlobalVariable, var_id)
    if not var:
        raise HTTPException(status_code=404, detail="变量不存在")
    session.delete(var)
    session.commit()
    return {"message": "变量已删除", "id": var_id}
