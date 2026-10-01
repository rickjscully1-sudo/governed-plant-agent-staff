"""Structured JSONL audit log for tool calls, permissions, approvals, recommendations."""

from __future__ import annotations

import hashlib
import json
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Union


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def display_path(path: Optional[Union[str, Path]]) -> Optional[str]:
    """Short, safe name for an audit log path, for UI and API output.

    Absolute paths (for example per-visitor temp dirs in the hosted demo) are
    reduced to the file name so no server filesystem layout is exposed.
    Relative paths (CLI default ``logs/...``) are shown as given.
    """
    if path is None:
        return None
    p = Path(path)
    return p.name if p.is_absolute() else str(p)


@dataclass
class AuditEvent:
    event_type: str
    timestamp: str = field(default_factory=_utc_now)
    details: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {"event_type": self.event_type, "timestamp": self.timestamp, **self.details}


class AuditLog:
    """In-memory audit trail with optional JSONL dump."""

    GENESIS_HASH = "0" * 64

    def __init__(self, path: Optional[Path] = None) -> None:
        self._events: List[AuditEvent] = []
        self._lock = threading.Lock()
        self.path = path
        self._last_hash: str = self.GENESIS_HASH

    @staticmethod
    def _canonical_payload(payload: Dict[str, Any]) -> str:
        """Stable JSON for hashing (exclude hash fields)."""
        cleaned = {
            k: v
            for k, v in payload.items()
            if k not in ("prev_hash", "event_hash")
        }
        return json.dumps(cleaned, sort_keys=True, default=str, separators=(",", ":"))

    @classmethod
    def compute_event_hash(cls, prev_hash: str, payload: Dict[str, Any]) -> str:
        material = prev_hash + cls._canonical_payload(payload)
        return hashlib.sha256(material.encode("utf-8")).hexdigest()

    def record(self, event_type: str, **details: Any) -> AuditEvent:
        # Hash-chain: each event links to previous event_hash (genesis for first).
        base_details = dict(details)
        with self._lock:
            prev_hash = self._last_hash
            payload = {"event_type": event_type, "timestamp": _utc_now(), **base_details}
            event_hash = self.compute_event_hash(prev_hash, payload)
            base_details["prev_hash"] = prev_hash
            base_details["event_hash"] = event_hash
            event = AuditEvent(event_type=event_type, timestamp=payload["timestamp"], details=base_details)
            self._events.append(event)
            self._last_hash = event_hash
            if self.path is not None:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                with self.path.open("a", encoding="utf-8") as fh:
                    fh.write(json.dumps(event.to_dict(), default=str) + "\n")
        return event

    @property
    def events(self) -> List[Dict[str, Any]]:
        with self._lock:
            return [e.to_dict() for e in self._events]

    def dump(self, path: Optional[Path] = None) -> Path:
        target = path or self.path or Path("logs/audit.jsonl")
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("w", encoding="utf-8") as fh:
            for event in self.events:
                fh.write(json.dumps(event, default=str) + "\n")
        return target

    def clear(self) -> None:
        with self._lock:
            self._events.clear()

    def of_type(self, event_type: str) -> List[Dict[str, Any]]:
        return [e for e in self.events if e["event_type"] == event_type]

    def consequential_executed(self) -> bool:
        """True if any HIGH-risk tool actually ran (not merely proposed)."""
        return any(
            e["event_type"] == "tool_executed" and e.get("tier") == "high"
            for e in self.events
        )

    def pending_approvals(self) -> List[Dict[str, Any]]:
        pending = self.of_type("approval_pending")
        decided = {
            e.get("approval_id")
            for e in self.events
            if e["event_type"] in ("approval_granted", "approval_denied", "approval_expired")
        }
        return [p for p in pending if p.get("approval_id") not in decided]


# Events that must carry a tool identifier (tool or tool_name).
_TOOL_EVENTS = frozenset(
    {
        "tool_proposed",
        "tool_executed",
        "tool_blocked",
        "tool_schema_rejected",
        "permission_check",
        "approval_pending",
        "approval_granted",
        "approval_denied",
        "approval_expired",
        "approval_default_deny",
    }
)

# Events that must carry an explicit decision field when present in policy trail.
_DECISION_EVENTS = frozenset(
    {
        "approval_pending",
        "approval_granted",
        "approval_denied",
        "approval_expired",
        "permission_check",
        "final_recommendation",
        "agent_stop_limit",
    }
)


def load_audit_jsonl(path: Union[str, Path]) -> List[Dict[str, Any]]:
    """Load JSONL audit file into a list of event dicts."""
    target = Path(path)
    events: List[Dict[str, Any]] = []
    if not target.exists():
        return events
    with target.open("r", encoding="utf-8") as fh:
        for line_no, line in enumerate(fh, start=1):
            text = line.strip()
            if not text:
                continue
            try:
                events.append(json.loads(text))
            except json.JSONDecodeError as exc:
                raise ValueError(
                    "Invalid JSON on line {0} of {1}: {2}".format(line_no, target, exc)
                )
    return events


def verify_audit_integrity(
    source: Union[str, Path, List[Dict[str, Any]], AuditLog],
    expected_scenario: Optional[str] = None,
) -> Tuple[bool, List[str]]:
    """Verify required audit fields exist (timestamp, tool, decision, scenario).

    Returns (ok, list_of_problems). Safe for tests and post-run checks.
    """
    if isinstance(source, AuditLog):
        events = source.events
    elif isinstance(source, list):
        events = source
    else:
        events = load_audit_jsonl(source)

    problems: List[str] = []
    if not events:
        problems.append("audit log is empty")
        return False, problems

    scenario_seen = False
    for idx, event in enumerate(events):
        if not isinstance(event, dict):
            problems.append("event[{0}] is not an object".format(idx))
            continue
        if "timestamp" not in event:
            problems.append("event[{0}] missing timestamp".format(idx))
        if "event_type" not in event:
            problems.append("event[{0}] missing event_type".format(idx))

        et = event.get("event_type")
        if et == "agent_start" and event.get("scenario"):
            scenario_seen = True
        if event.get("scenario"):
            scenario_seen = True
            if expected_scenario and event.get("scenario") != expected_scenario:
                problems.append(
                    "event[{0}] scenario {1!r} != expected {2!r}".format(
                        idx, event.get("scenario"), expected_scenario
                    )
                )

        if et in _TOOL_EVENTS:
            tool = event.get("tool") or event.get("tool_name")
            if not tool:
                problems.append(
                    "event[{0}] type={1} missing tool/tool_name".format(idx, et)
                )

        if et in _DECISION_EVENTS:
            # Accept decision, recommendation_type, or approved bool as decision signal.
            has_decision = (
                "decision" in event
                or "recommendation_type" in event
                or "approved" in event
                or "stop_reason" in event
            )
            if not has_decision:
                problems.append(
                    "event[{0}] type={1} missing decision/recommendation_type".format(
                        idx, et
                    )
                )

    if not scenario_seen:
        problems.append("no scenario field found in audit trail")

    # Hash-chain integrity (when prev_hash/event_hash present).
    chain_problems = _verify_hash_chain(events)
    problems.extend(chain_problems)

    return (len(problems) == 0), problems


def _verify_hash_chain(events: List[Dict[str, Any]]) -> List[str]:
    """Detect broken prev_hash/event_hash chains (mutated middle event fails)."""
    problems: List[str] = []
    if not events:
        return problems
    # Only enforce when the log looks hash-chained.
    if not any("event_hash" in e for e in events if isinstance(e, dict)):
        return problems

    expected_prev = AuditLog.GENESIS_HASH
    for idx, event in enumerate(events):
        if not isinstance(event, dict):
            continue
        if "prev_hash" not in event or "event_hash" not in event:
            problems.append(
                "event[{0}] missing prev_hash/event_hash in chained log".format(idx)
            )
            continue
        if event.get("prev_hash") != expected_prev:
            problems.append(
                "event[{0}] prev_hash mismatch (chain broken)".format(idx)
            )
        recomputed = AuditLog.compute_event_hash(event["prev_hash"], event)
        if recomputed != event.get("event_hash"):
            problems.append(
                "event[{0}] event_hash mismatch (content tampered)".format(idx)
            )
        expected_prev = event.get("event_hash") or expected_prev
    return problems
