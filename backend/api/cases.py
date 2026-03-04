from typing import List, Optional
from fastapi import APIRouter, Depends, HTTPException, Query, BackgroundTasks
from sqlmodel import Session, select, col
from datetime import datetime

from backend.database import get_session
from backend.models import TestCase, User, CaseFolder
from backend.schemas import TestCaseCreate, TestCaseRead, TestCaseBase, PaginatedTestCaseRead
from backend.api import deps
from backend.runner import TestRunner
from typing import List

router = APIRouter()

@router.post("/", response_model=TestCaseRead)
def create_test_case(
    test_case: TestCaseCreate, 
    session: Session = Depends(get_session),
    current_user: User = Depends(deps.get_current_user)
):
    """Create a new test case"""
    db_case = TestCase(
        name=test_case.name,
        description=test_case.description,
        steps=test_case.steps,
        variables=test_case.variables,
        tags=test_case.tags,
        folder_id=test_case.folder_id,
        user_id=current_user.id,
        updater_id=current_user.id,
    )
    db_case.updated_at = db_case.created_at
    session.add(db_case)
    session.commit()
    session.refresh(db_case)
    return _enrich_case_read(db_case, session)

def _enrich_case_read(case: TestCase, session: Session, creator_info=None, updater_info=None) -> TestCaseRead:
    """为 TestCaseRead 补充 folder_name 和创建人信息"""
    case_read = TestCaseRead.from_orm(case)
    if creator_info:
        case_read.creator_name = creator_info[0] or creator_info[1] or "Unknown"
    if updater_info:
        case_read.updater_name = updater_info[0] or updater_info[1] or "Unknown"
    if case.folder_id:
        folder = session.get(CaseFolder, case.folder_id)
        if folder:
            case_read.folder_name = folder.name
    return case_read


@router.get("/", response_model=PaginatedTestCaseRead)
def list_test_cases(
    skip: int = 0,
    limit: int = 100,
    keyword: Optional[str] = None,
    tag: Optional[str] = None,
    folder_id: Optional[int] = Query(default=None, description="按目录 ID 过滤"),
    session: Session = Depends(get_session)
):
    """List test cases with pagination and filtering"""
    from sqlalchemy.orm import aliased
    from sqlalchemy import func
    Creator = aliased(User)
    Updater = aliased(User)
    
    query = session.query(TestCase, Creator.full_name, Creator.username, Updater.full_name, Updater.username)\
        .outerjoin(Creator, TestCase.user_id == Creator.id)\
        .outerjoin(Updater, TestCase.updater_id == Updater.id)

    if keyword:
        query = query.filter(TestCase.name.contains(keyword))
    if folder_id is not None:
        query = query.filter(TestCase.folder_id == folder_id)
        
    query = query.order_by(TestCase.created_at.desc())
    
    count_query = session.query(func.count(TestCase.id))
    if keyword:
        count_query = count_query.filter(TestCase.name.contains(keyword))
    if folder_id is not None:
        count_query = count_query.filter(TestCase.folder_id == folder_id)
    
    total = count_query.scalar()
    
    query = query.offset(skip).limit(limit)
    results = query.all()
    
    case_list = []
    for case, c_full, c_user, u_full, u_user in results:
        if tag and (not case.tags or tag not in case.tags):
            continue
        case_read = _enrich_case_read(
            case, session,
            creator_info=(c_full, c_user),
            updater_info=(u_full, u_user)
        )
        case_list.append(case_read)
        
    return PaginatedTestCaseRead(total=total, items=case_list)

@router.get("/{case_id}", response_model=TestCaseRead)
def get_test_case(case_id: int, session: Session = Depends(get_session)):
    """Get a single test case"""
    case = session.get(TestCase, case_id)
    if not case:
        raise HTTPException(status_code=404, detail="Case not found")
    return case

@router.put("/{case_id}", response_model=TestCaseRead)
def update_test_case(
    case_id: int, 
    test_case: TestCaseCreate, 
    session: Session = Depends(get_session),
    current_user: User = Depends(deps.get_current_user)
):
    """Update a test case"""
    db_case = session.get(TestCase, case_id)
    if not db_case:
        raise HTTPException(status_code=404, detail="Case not found")
    
    db_case.name = test_case.name
    db_case.description = test_case.description
    db_case.steps = test_case.steps
    db_case.variables = test_case.variables
    db_case.tags = test_case.tags
    db_case.folder_id = test_case.folder_id
    
    db_case.updater_id = current_user.id
    db_case.updated_at = datetime.now()
    
    session.add(db_case)
    session.commit()
    session.refresh(db_case)
    return _enrich_case_read(db_case, session)

@router.post("/{case_id}/duplicate", response_model=TestCaseRead)
def duplicate_test_case(
    case_id: int,
    session: Session = Depends(get_session),
    current_user: User = Depends(deps.get_current_user)
):
    """Clone a test case"""
    original_case = session.get(TestCase, case_id)
    if not original_case:
        raise HTTPException(status_code=404, detail="Case not found")
    
    new_case = TestCase(
        name=f"{original_case.name}_copy",
        description=original_case.description,
        steps=original_case.steps,
        variables=original_case.variables,
        tags=original_case.tags,
        folder_id=original_case.folder_id,
        user_id=current_user.id,
        created_at=datetime.now()
    )
    
    session.add(new_case)
    session.commit()
    session.refresh(new_case)
    return new_case

def _run_case_background(case_id: int, session_factory, env_id: Optional[int] = None, device_serial: Optional[str] = None):
    # We need a new session for the background thread
    with session_factory() as session:
        case = session.get(TestCase, case_id)
        if not case:
            return
            
        runner = TestRunner(device_serial=device_serial)
        try:
            runner.connect()
            
            # Prepare optional variables map
            variables_map = {}
            if env_id:
                from backend.models import GlobalVariable
                from sqlmodel import select
                global_vars = session.exec(select(GlobalVariable).where(GlobalVariable.env_id == env_id)).all()
                for gv in global_vars:
                    variables_map[gv.key] = gv.value
            
            result = runner.run_case(case, extra_variables=variables_map)
            
            # Update case status
            case.last_run_time = datetime.now()
            case.last_run_status = "Pass" if result.get("success") else "Fail"
            session.add(case)
            session.commit()
        except Exception as e:
            print(f"Error running case {case_id}: {e}")
            case.last_run_status = "Fail"
            session.add(case)
            session.commit()

@router.post("/{case_id}/run")
def run_test_case(
    case_id: int,
    background_tasks: BackgroundTasks,
    env_id: Optional[int] = None,
    device_serial: Optional[str] = None,
    session: Session = Depends(get_session)
):
    """Quick run a test case in background"""
    case = session.get(TestCase, case_id)
    if not case:
        raise HTTPException(status_code=404, detail="Case not found")
    
    # Pass the engine/factory to background task, not the dependency session
    from backend.database import engine
    from sqlmodel import Session as SQLSession # avoid conflict
    
    # Define a factory wrapper
    def session_factory():
        return SQLSession(engine)
        
    background_tasks.add_task(_run_case_background, case_id, session_factory, env_id, device_serial)
    
    return {"message": "Execution started", "case_id": case_id}

@router.delete("/{case_id}")
def delete_test_case(case_id: int, session: Session = Depends(get_session)):
    """Delete a test case"""
    case = session.get(TestCase, case_id)
    if not case:
        raise HTTPException(status_code=404, detail="Case not found")
    session.delete(case)
    session.commit()
    return {"message": "Case deleted", "id": case_id}
