from typing import List, Optional
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlmodel import Session, select

from backend.database import get_session
from backend.models import CaseFolder, ScenarioFolder, TestCase, TestScenario
from backend.schemas import CaseFolderCreate, CaseFolderUpdate, CaseFolderRead

router = APIRouter()
scenario_router = APIRouter()


def _make_leaf_node(item_id: int, name: str, leaf_type: str) -> dict:
    return {
        "id": f"{leaf_type}-{item_id}",
        f"{leaf_type}_id": item_id,
        "name": name,
        "type": leaf_type,
        "is_leaf": True,
        "children": []
    }


def _build_tree(folders: List, items: List, leaf_type: str) -> tuple[list, list]:
    """O(N) 哈希映射组装树形结构，目录下挂载叶子节点，返回 (tree, all_leaf_nodes)"""
    all_leaf_nodes = []
    leaf_map: dict[int, list] = {}
    for item in items:
        node = _make_leaf_node(item.id, item.name, leaf_type)
        all_leaf_nodes.append(node)
        if item.folder_id is not None:
            leaf_map.setdefault(item.folder_id, []).append(node)

    node_map = {}
    for f in folders:
        node_map[f.id] = {
            "id": f"folder-{f.id}",
            "folder_id": f.id,
            "name": f.name,
            "parent_id": f.parent_id,
            "type": "folder",
            "children": leaf_map.get(f.id, [])
        }

    roots = []
    for f in folders:
        node = node_map[f.id]
        if f.parent_id and f.parent_id in node_map:
            node_map[f.parent_id]["children"].insert(0, node)
        else:
            roots.append(node)

    return roots, all_leaf_nodes


class MoveItemBody(BaseModel):
    folder_id: Optional[int] = None


def _create_folder(session: Session, folder_model, payload: CaseFolderCreate):
    if payload.parent_id:
        parent = session.get(folder_model, payload.parent_id)
        if not parent:
            raise HTTPException(status_code=400, detail="父目录不存在")

    db_folder = folder_model(name=payload.name, parent_id=payload.parent_id)
    session.add(db_folder)
    session.commit()
    session.refresh(db_folder)
    return db_folder


def _rename_folder(session: Session, folder_model, folder_id: int, data: CaseFolderUpdate):
    folder = session.get(folder_model, folder_id)
    if not folder:
        raise HTTPException(status_code=404, detail="目录不存在")
    folder.name = data.name
    session.add(folder)
    session.commit()
    session.refresh(folder)
    return folder


def _delete_folder(session: Session, folder_model, item_model, folder_id: int, item_label: str):
    folder = session.get(folder_model, folder_id)
    if not folder:
        raise HTTPException(status_code=404, detail="目录不存在")

    children = session.exec(
        select(folder_model).where(folder_model.parent_id == folder_id)
    ).first()
    if children:
        raise HTTPException(status_code=400, detail="该目录下存在子目录，无法删除")

    linked_items = session.exec(
        select(item_model).where(item_model.folder_id == folder_id)
    ).first()
    if linked_items:
        raise HTTPException(status_code=400, detail=f"该目录下存在关联{item_label}，无法删除")

    session.delete(folder)
    session.commit()
    return {"message": "目录已删除", "id": folder_id}


def _move_item(session: Session, folder_model, item_model, item_id: int, folder_id: Optional[int], item_label: str):
    item = session.get(item_model, item_id)
    if not item:
        raise HTTPException(status_code=404, detail=f"{item_label}不存在")
    if folder_id is not None:
        folder = session.get(folder_model, folder_id)
        if not folder:
            raise HTTPException(status_code=400, detail="目标目录不存在")
    item.folder_id = folder_id
    session.add(item)
    session.commit()
    return {"message": f"{item_label}已移动", "folder_id": folder_id}


# ==================== 用例目录 ====================

@router.get("/tree")
def get_folder_tree(session: Session = Depends(get_session)):
    """获取用例目录树，包含每个目录下的用例节点"""
    folders = session.exec(select(CaseFolder).order_by(CaseFolder.id)).all()
    cases = session.exec(select(TestCase).order_by(TestCase.id)).all()
    tree, all_cases = _build_tree(folders, cases, leaf_type="case")
    return {"tree": tree, "all_cases": all_cases}


@router.patch("/move-case/{case_id}")
def move_case(case_id: int, body: MoveItemBody, session: Session = Depends(get_session)):
    """将用例移动到指定目录"""
    result = _move_item(session, CaseFolder, TestCase, case_id, body.folder_id, "用例")
    return {**result, "case_id": case_id}


@router.post("/", response_model=CaseFolderRead)
def create_folder(
    folder: CaseFolderCreate,
    session: Session = Depends(get_session)
):
    """创建用例目录"""
    return _create_folder(session, CaseFolder, folder)


@router.put("/{folder_id}", response_model=CaseFolderRead)
def rename_folder(
    folder_id: int,
    data: CaseFolderUpdate,
    session: Session = Depends(get_session)
):
    """重命名用例目录"""
    return _rename_folder(session, CaseFolder, folder_id, data)


@router.delete("/{folder_id}")
def delete_folder(folder_id: int, session: Session = Depends(get_session)):
    """删除用例目录（有子目录或关联用例时拒绝）"""
    return _delete_folder(session, CaseFolder, TestCase, folder_id, "用例")


# ==================== 场景目录 ====================

@scenario_router.get("/tree")
def get_scenario_folder_tree(session: Session = Depends(get_session)):
    """获取场景目录树，包含每个目录下的场景节点"""
    folders = session.exec(select(ScenarioFolder).order_by(ScenarioFolder.id)).all()
    scenarios = session.exec(select(TestScenario).order_by(TestScenario.id)).all()
    tree, all_scenarios = _build_tree(folders, scenarios, leaf_type="scenario")
    return {"tree": tree, "all_scenarios": all_scenarios}


@scenario_router.patch("/move-scenario/{scenario_id}")
def move_scenario(scenario_id: int, body: MoveItemBody, session: Session = Depends(get_session)):
    """将场景移动到指定目录"""
    result = _move_item(session, ScenarioFolder, TestScenario, scenario_id, body.folder_id, "场景")
    return {**result, "scenario_id": scenario_id}


@scenario_router.post("/", response_model=CaseFolderRead)
def create_scenario_folder(
    folder: CaseFolderCreate,
    session: Session = Depends(get_session)
):
    """创建场景目录"""
    return _create_folder(session, ScenarioFolder, folder)


@scenario_router.put("/{folder_id}", response_model=CaseFolderRead)
def rename_scenario_folder(
    folder_id: int,
    data: CaseFolderUpdate,
    session: Session = Depends(get_session)
):
    """重命名场景目录"""
    return _rename_folder(session, ScenarioFolder, folder_id, data)


@scenario_router.delete("/{folder_id}")
def delete_scenario_folder(folder_id: int, session: Session = Depends(get_session)):
    """删除场景目录（有子目录或关联场景时拒绝）"""
    return _delete_folder(session, ScenarioFolder, TestScenario, folder_id, "场景")
