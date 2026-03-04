from typing import List, Optional
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlmodel import Session, select

from backend.database import get_session
from backend.models import CaseFolder, TestCase
from backend.schemas import CaseFolderCreate, CaseFolderUpdate, CaseFolderRead

router = APIRouter()


def _make_case_node(c: TestCase) -> dict:
    return {
        "id": f"case-{c.id}",
        "case_id": c.id,
        "name": c.name,
        "type": "case",
        "is_leaf": True,
        "children": []
    }


def _build_tree(folders: List[CaseFolder], cases: List[TestCase]) -> dict:
    """O(N) 哈希映射组装树形结构，目录下挂载用例子节点，返回 {tree, all_cases}"""
    all_case_nodes = []
    case_map: dict[int, list] = {}
    for c in cases:
        node = _make_case_node(c)
        all_case_nodes.append(node)
        if c.folder_id is not None:
            case_map.setdefault(c.folder_id, []).append(node)

    node_map = {}
    for f in folders:
        node_map[f.id] = {
            "id": f"folder-{f.id}",
            "folder_id": f.id,
            "name": f.name,
            "parent_id": f.parent_id,
            "type": "folder",
            "children": case_map.get(f.id, [])
        }

    roots = []
    for f in folders:
        node = node_map[f.id]
        if f.parent_id and f.parent_id in node_map:
            node_map[f.parent_id]["children"].insert(0, node)
        else:
            roots.append(node)

    return {"tree": roots, "all_cases": all_case_nodes}


@router.get("/tree")
def get_folder_tree(session: Session = Depends(get_session)):
    """获取目录树，包含每个目录下的用例节点"""
    folders = session.exec(select(CaseFolder).order_by(CaseFolder.id)).all()
    cases = session.exec(select(TestCase).order_by(TestCase.id)).all()
    return _build_tree(folders, cases)


class MoveCaseBody(BaseModel):
    folder_id: Optional[int] = None


@router.patch("/move-case/{case_id}")
def move_case(case_id: int, body: MoveCaseBody, session: Session = Depends(get_session)):
    """将用例移动到指定目录"""
    case = session.get(TestCase, case_id)
    if not case:
        raise HTTPException(status_code=404, detail="用例不存在")
    if body.folder_id is not None:
        folder = session.get(CaseFolder, body.folder_id)
        if not folder:
            raise HTTPException(status_code=400, detail="目标目录不存在")
    case.folder_id = body.folder_id
    session.add(case)
    session.commit()
    return {"message": "用例已移动", "case_id": case_id, "folder_id": body.folder_id}


@router.post("/", response_model=CaseFolderRead)
def create_folder(
    folder: CaseFolderCreate,
    session: Session = Depends(get_session)
):
    """创建目录"""
    if folder.parent_id:
        parent = session.get(CaseFolder, folder.parent_id)
        if not parent:
            raise HTTPException(status_code=400, detail="父目录不存在")

    db_folder = CaseFolder(name=folder.name, parent_id=folder.parent_id)
    session.add(db_folder)
    session.commit()
    session.refresh(db_folder)
    return db_folder


@router.put("/{folder_id}", response_model=CaseFolderRead)
def rename_folder(
    folder_id: int,
    data: CaseFolderUpdate,
    session: Session = Depends(get_session)
):
    """重命名目录"""
    folder = session.get(CaseFolder, folder_id)
    if not folder:
        raise HTTPException(status_code=404, detail="目录不存在")
    folder.name = data.name
    session.add(folder)
    session.commit()
    session.refresh(folder)
    return folder


@router.delete("/{folder_id}")
def delete_folder(folder_id: int, session: Session = Depends(get_session)):
    """删除目录（有子目录或关联用例时拒绝）"""
    folder = session.get(CaseFolder, folder_id)
    if not folder:
        raise HTTPException(status_code=404, detail="目录不存在")

    children = session.exec(
        select(CaseFolder).where(CaseFolder.parent_id == folder_id)
    ).first()
    if children:
        raise HTTPException(status_code=400, detail="该目录下存在子目录，无法删除")

    linked_cases = session.exec(
        select(TestCase).where(TestCase.folder_id == folder_id)
    ).first()
    if linked_cases:
        raise HTTPException(status_code=400, detail="该目录下存在关联用例，无法删除")

    session.delete(folder)
    session.commit()
    return {"message": "目录已删除", "id": folder_id}
