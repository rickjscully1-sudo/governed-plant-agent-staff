"""LLM backend selection for the public demo build.

This build is rule-based only: it always returns the deterministic mock
backend. There is no external model backend, no network call, and no API key
is read from the environment.
"""

from __future__ import annotations

from typing import Any, Optional

from .mock import MockLLM


def get_llm(force_mock: bool = True, scenario_hint: Optional[str] = None, **kwargs: Any):
    """Return the deterministic mock LLM. ``force_mock`` is kept for API compatibility."""
    return MockLLM(scenario_hint=scenario_hint)
