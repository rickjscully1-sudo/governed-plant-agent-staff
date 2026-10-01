"""LLM backend: deterministic, rule-based mock (no model calls, no API key)."""

from .base import LLMResponse, Message, ToolCallRequest
from .factory import get_llm

__all__ = ["LLMResponse", "Message", "ToolCallRequest", "get_llm"]
