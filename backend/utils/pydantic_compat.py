from __future__ import annotations

from typing import Any, Dict


def dump_model(value: Any, **kwargs: Any) -> Any:
    """Return a plain Python representation for Pydantic/SQLModel objects across v1/v2."""
    if isinstance(value, dict):
        return dict(value)
    if hasattr(value, "model_dump"):
        return value.model_dump(**kwargs)
    if hasattr(value, "dict"):
        return value.dict(**kwargs)
    return value


def dump_dict(value: Any, **kwargs: Any) -> Dict[str, Any]:
    data = dump_model(value, **kwargs)
    if isinstance(data, dict):
        return dict(data)
    raise TypeError(f"expected dict-like value, got {type(data)!r}")
