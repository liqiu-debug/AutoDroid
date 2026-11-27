"""Structured composite locator helpers (by="composite").

Stores multi-attribute locators in canonical JSON inside ``selector`` while
preserving the 2-field ``{selector, by}`` contract across Pydantic schemas,
step DB rows, device-side scripts, and Desktop/mobile clients.
"""
from __future__ import annotations

import json
from typing import Any, Callable, Dict, Optional, Tuple

ALLOWED_COMPOSITE_KEYS = ("id", "text", "description", "class_name")

_KEY_ALIASES = {
    "id": "id",
    "resourceid": "id",
    "resource_id": "id",
    "resource-id": "id",
    "accessibilityid": "id",
    "accessibility_id": "id",
    "text": "text",
    "label": "text",
    "description": "description",
    "desc": "description",
    "content-desc": "description",
    "content_desc": "description",
    "contentdesc": "description",
    "name": "description",
    "class_name": "class_name",
    "classname": "class_name",
    "class": "class_name",
    "type": "class_name",
}


def _coerce_scalar_text(key: str, value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, (bool, dict, list, tuple, set)):
        raise ValueError(f"composite locator field {key!r} must be a string")
    return str(value).strip()


def normalize_composite_conditions(
    raw_dict: Any,
    *,
    allow_empty_values: bool = False,
) -> Dict[str, str]:
    """Validate and canonicalize a composite condition mapping."""
    if not isinstance(raw_dict, dict):
        raise ValueError("composite locator must be a JSON object")

    canonical_values: Dict[str, str] = {}
    for raw_key, raw_value in raw_dict.items():
        key_text = str(raw_key or "").strip().lower()
        canonical_key = _KEY_ALIASES.get(key_text)
        if not canonical_key:
            raise ValueError(f"composite locator contains unsupported key: {raw_key!r}")

        val_text = _coerce_scalar_text(canonical_key, raw_value)
        if not val_text and not allow_empty_values:
            continue

        if canonical_key in canonical_values and canonical_values[canonical_key] != val_text:
            raise ValueError(
                f"composite locator has conflicting values for {canonical_key!r}"
            )
        canonical_values[canonical_key] = val_text

    ordered = {
        key: canonical_values[key]
        for key in ALLOWED_COMPOSITE_KEYS
        if key in canonical_values and (allow_empty_values or canonical_values[key])
    }
    if not ordered:
        raise ValueError("composite locator requires at least one non-empty condition")
    return ordered


def parse_composite_selector(raw: Any) -> Dict[str, str]:
    """Parse a JSON composite selector string (or dict) into canonical conditions."""
    if isinstance(raw, dict):
        return normalize_composite_conditions(raw)

    text = "" if raw is None else str(raw).strip()
    if not text:
        raise ValueError("composite locator cannot be empty")
    try:
        parsed = json.loads(text)
    except Exception as exc:
        raise ValueError(f"composite locator must be valid JSON: {exc}") from exc

    if not isinstance(parsed, dict):
        raise ValueError("composite locator must be a JSON object")
    return normalize_composite_conditions(parsed)


def serialize_composite_selector(conditions: Dict[str, Any]) -> str:
    """Serialize canonical composite conditions into compact sorted JSON."""
    normalized = normalize_composite_conditions(conditions)
    return json.dumps(normalized, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def normalize_composite_selector(raw: Any) -> str:
    """Parse and re-serialize a composite selector into canonical JSON."""
    return serialize_composite_selector(parse_composite_selector(raw))


def render_composite_selector(raw: Any, render_fn: Callable[[str], str]) -> str:
    """Render template variables inside each composite value without breaking JSON."""
    try:
        conditions = parse_composite_selector(raw)
    except ValueError:
        return render_fn("" if raw is None else str(raw))

    rendered: Dict[str, str] = {}
    for key, value in conditions.items():
        rendered_value = render_fn(value)
        rendered[key] = "" if rendered_value is None else str(rendered_value).strip()
    return json.dumps(rendered, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def extract_semantic_fallback(raw: Any) -> Optional[Tuple[str, str]]:
    """Extract preferred semantic locator ('text' or 'description') for iOS inference."""
    try:
        conditions = parse_composite_selector(raw)
    except ValueError:
        return None

    if conditions.get("text"):
        return ("text", conditions["text"])
    if conditions.get("description"):
        return ("description", conditions["description"])
    return None


def _escape_predicate_literal(value: str) -> str:
    return str(value or "").replace("\\", "\\\\").replace("'", "\\'")


def build_ios_predicate_from_composite(
    conditions: Dict[str, str],
    *,
    text_contains: bool = False,
) -> str:
    """Compile composite conditions into an iOS WDA NSPredicate string."""
    normalized = normalize_composite_conditions(conditions)
    clauses = []

    if "id" in normalized:
        escaped = _escape_predicate_literal(normalized["id"])
        clauses.append(f"name == '{escaped}'")

    if "text" in normalized:
        escaped = _escape_predicate_literal(normalized["text"])
        if text_contains:
            clauses.append(
                f"(label CONTAINS[c] '{escaped}' OR name CONTAINS[c] '{escaped}' OR value CONTAINS[c] '{escaped}')"
            )
        else:
            clauses.append(
                f"(label == '{escaped}' OR name == '{escaped}' OR value == '{escaped}')"
            )

    if "description" in normalized:
        escaped = _escape_predicate_literal(normalized["description"])
        clauses.append(f"(name == '{escaped}' OR label == '{escaped}')")

    if "class_name" in normalized:
        escaped = _escape_predicate_literal(normalized["class_name"])
        clauses.append(f"type == '{escaped}'")

    if not clauses:
        raise ValueError("composite locator has no supported iOS conditions")
    return " AND ".join(clauses)
