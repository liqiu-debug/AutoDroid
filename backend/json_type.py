from typing import Any, List, Type

from sqlalchemy.types import JSON, TypeDecorator

try:
    from pydantic import TypeAdapter
except ImportError:  # pragma: no cover - pydantic v1
    TypeAdapter = None

try:
    from pydantic import parse_obj_as
except ImportError:  # pragma: no cover - pydantic v2 always exports TypeAdapter
    parse_obj_as = None


class PydanticListType(TypeDecorator):
    """
    SQLAlchemy TypeDecorator to store a list of Pydantic models or primitive values
    as a JSON column.
    """

    impl = JSON
    cache_ok = True

    def __init__(self, pydantic_model: Type[Any], *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.pydantic_model = pydantic_model

    @staticmethod
    def _dump_item(item: Any) -> Any:
        if hasattr(item, "model_dump"):
            return item.model_dump()
        if hasattr(item, "dict"):
            return item.dict()
        return item

    def process_bind_param(self, value: Any, dialect):
        """Convert Python list items to JSON-serializable values for storage."""
        if value is None:
            return None
        if not isinstance(value, list):
            return value
        return [self._dump_item(item) for item in value]

    def process_result_value(self, value: Any, dialect):
        """Convert JSON list values back to typed Python objects."""
        if value is None:
            return None

        item_list_type = List[self.pydantic_model]
        if TypeAdapter is not None:
            return TypeAdapter(item_list_type).validate_python(value)
        if parse_obj_as is not None:  # pragma: no cover - pydantic v1 fallback
            return parse_obj_as(item_list_type, value)
        return value
