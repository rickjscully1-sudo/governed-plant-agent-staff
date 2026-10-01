"""Idempotency keys for consequential tool executions."""

from __future__ import annotations

import hashlib
import json
from typing import Any, Dict


def _normalize_args(args: Dict[str, Any]) -> Any:
    """Recursively sort dict keys for stable hashing."""
    if isinstance(args, dict):
        return {k: _normalize_args(args[k]) for k in sorted(args.keys())}
    if isinstance(args, list):
        return [_normalize_args(x) for x in args]
    return args


def make_idempotency_key(tool_name: str, args: Dict[str, Any], scenario: str) -> str:
    """Build a stable key: sha256(tool + normalized args + scenario)."""
    payload = {
        "tool": tool_name,
        "args": _normalize_args(args or {}),
        "scenario": scenario or "",
    }
    material = json.dumps(payload, sort_keys=True, default=str, separators=(",", ":"))
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


# Alias used by some call sites
make_idempotency_key = make_idempotency_key
