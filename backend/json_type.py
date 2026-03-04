from typing import Type, List, Any
from sqlalchemy.types import TypeDecorator, JSON
from pydantic import BaseModel, parse_obj_as

class PydanticListType(TypeDecorator):
    """
    SQLAlchemy TypeDecorator to store a list of Pydantic models as a JSON column.
    """
    impl = JSON
    cache_ok = True

    def __init__(self, pydantic_model: Type[BaseModel], *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.pydantic_model = pydantic_model

    def process_bind_param(self, value: Any, dialect):
        """Converting the list of Pydantic models to a list of dicts for storage."""
        if value is None:
            return None
        # Verify it's a list
        if not isinstance(value, list):
            return value # Should probably raise error or let SQLAlchemy handle it
        
        # Convert each item to dict if it's a Pydantic model
        return [item.dict() if hasattr(item, 'dict') else item for item in value]

    def process_result_value(self, value: Any, dialect):
        """Converting the JSON list of dicts back to a list of Pydantic models."""
        if value is None:
            return None
        
        # Use Pydantic's parse_obj_as to valid/convert list
        # Note: In Pydantic v2 use TypeAdapter, but we are likely on v1 compatible mode or v2.
        # Let's check installed version: pydantic 2.x
        # For Pydantic 2, .dict() is deprecated (use model_dump), and parse_obj_as is deprecated (use TypeAdapter).
        # But let's try to support both or stick to v2 as per requirements.txt
        
        try:
            from pydantic import TypeAdapter
            adapter = TypeAdapter(List[self.pydantic_model])
            return adapter.validate_python(value)
        except ImportError:
            # Fallback for Pydantic v1
            return parse_obj_as(List[self.pydantic_model], value)
