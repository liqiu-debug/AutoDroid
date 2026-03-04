from pydantic import BaseModel
class Step(BaseModel):
    id: str = None
s = Step()
print(s.id)
