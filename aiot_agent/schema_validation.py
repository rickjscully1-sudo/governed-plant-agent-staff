"""Lightweight JSON-schema checks for tool arguments (stdlib only, Py3.9+)."""

from __future__ import annotations

from typing import Any, Dict, List, Tuple


def _type_ok(value: Any, expected: str) -> bool:
    if expected == "string":
        return isinstance(value, str)
    if expected == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if expected == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if expected == "boolean":
        return isinstance(value, bool)
    if expected == "object":
        return isinstance(value, dict)
    if expected == "array":
        return isinstance(value, list)
    if expected == "null":
        return value is None
    return True


def validate_tool_args(schema: Dict[str, Any], args: Any) -> Tuple[bool, List[str]]:
    """Validate ``args`` against a minimal JSON Schema subset.

    Supports: type=object, properties.{type,enum,default}, required[].
    Returns (ok, error_messages).
    """
    errors: List[str] = []
    if not isinstance(args, dict):
        return False, ["tool arguments must be a JSON object"]

    if not schema:
        return True, []

    required = schema.get("required") or []
    for key in required:
        if key not in args:
            errors.append("missing required field: {0}".format(key))

    properties = schema.get("properties") or {}
    for key, value in args.items():
        if key not in properties:
            # Extra fields allowed (forward-compatible); not an error.
            continue
        prop = properties[key] or {}
        expected_type = prop.get("type")
        if expected_type and not _type_ok(value, expected_type):
            errors.append(
                "field '{0}' expected type {1}, got {2}".format(
                    key, expected_type, type(value).__name__
                )
            )
        enum_vals = prop.get("enum")
        if enum_vals is not None and value not in enum_vals:
            errors.append(
                "field '{0}' value {1!r} not in enum {2}".format(key, value, enum_vals)
            )

    return (len(errors) == 0), errors
