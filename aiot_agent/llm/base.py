from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Protocol


@dataclass
class Message:
    role: str  # system | user | assistant | tool
    content: str
    tool_call_id: Optional[str] = None
    name: Optional[str] = None


@dataclass
class ToolCallRequest:
    id: str
    name: str
    arguments: Dict[str, Any]


@dataclass
class LLMResponse:
    content: str
    tool_calls: List[ToolCallRequest] = field(default_factory=list)
    stop_reason: str = "end"  # end | tool_use


class LLM(Protocol):
    def complete(
        self,
        messages: List[Message],
        tools: List[Dict[str, Any]],
        system: str,
    ) -> LLMResponse: ...
