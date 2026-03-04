from sqlmodel import Session, select
from backend.database import engine
from backend.models import TestCase, GlobalVariable
from backend.runner import TestRunner
from backend.schemas import Step

with Session(engine) as session:
    # Build fake case
    case = TestCase(name="Test Vars", steps=[])
    
    # Add step with password
    step = Step(action="input", selector="test_id", selector_type="resourceId", value="{{ PASSWORD }}", description="Test replacement")
    case.steps.append(step)
    
    # Get global vars for env_id=1
    gvs = session.exec(select(GlobalVariable).where(GlobalVariable.env_id == 1)).all()
    env_vars = {g.key: g.value for g in gvs}
    
    runner = TestRunner()
    
    # Mock connect to avoid real device execution
    class MockDevice:
        def __getattr__(self, name):
            if name == "info": return {"manufacturer": "test"}
            def _mock(*args, **kwargs):
                return self
            return _mock
            
        def __call__(self, *args, **kwargs):
            return self
            
    runner.d = MockDevice()
    
    print("Testing execute_step:")
    res = runner.execute_step(step, env_vars)
    print("Result:", res)
