"""Deterministic decision + audit replay for forensics.

Builds a stable fingerprint of the *control-plane* decision path (gates,
approvals, tool outcomes, final recommendation) so ops can:
  1. Compare two runs of the same scenario (fingerprint must match)
  2. Reconstruct the decision summary from an audit JSONL alone
  3. Detect tampering of decision-relevant audit fields

Wall-clock timestamps and hash-chain fields are excluded from the fingerprint
so mock-LLM demo runs remain deterministic across wall time. Hash-chain
integrity remains the job of ``verify_audit_integrity``.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Union

from .audit import AuditLog, load_audit_jsonl

# Control-plane / forensic events that define *what the architecture decided*.
CONTROL_EVENT_TYPES = frozenset(
    {
        "agent_start",
        "llm_step",
        "tool_proposed",
        "permission_check",
        "tool_schema_rejected",
        "tool_blocked",
        "tool_executed",
        "approval_pending",
        "approval_granted",
        "approval_denied",
        "approval_expired",
        "approval_default_deny",
        "idempotent_replay_denied",
        "circuit_open",
        "evidence_freshness",
        "freshness_sla_violation",
        "agent_stop_limit",
        "final_recommendation",
        "break_glass",
        "capability_token",
    }
)

# Fields that must not affect the forensic fingerprint (wall time / chain).
_EXCLUDED_FINGERPRINT_KEYS = frozenset(
    {
        "timestamp",
        "prev_hash",
        "event_hash",
        "decision_fingerprint",  # self-reference if re-hashed
        "approval_id",  # may be ephemeral
        "final_message",  # long prose; type + flags are enough
        "goal",  # start text can vary slightly
        "result_summary",  # truncated / may include wall ids
        "note",
        "detail",
        "evaluated_at",  # wall unless scenario-pinned; status fields remain
        "age_seconds",  # float drift; freshness_status is enough
        "newest_ts",
        "violations",  # nested duplicates of freshness; status is enough
    }
)

# Semantic keys kept when present (others dropped for stability).
_KEPT_KEYS = frozenset(
    {
        "event_type",
        "scenario",
        "decision",
        "recommendation_type",
        "tool",
        "tool_name",
        "tier",
        "step",
        "approved",
        "approval_role",
        "stop_reason",
        "consequential_proposed",
        "consequential_executed",
        "reason",
        "status",
        "freshness_ok",
        "freshness_status",
        "max_age_seconds",
        "idempotency_key",
        "consecutive_errors",
        "threshold",
        "last_tool",
        "args",
        "limits",
    }
)


def _canonical_json(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, default=str, separators=(",", ":"))


def _normalize_value(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: _normalize_value(value[k]) for k in sorted(value.keys())}
    if isinstance(value, list):
        return [_normalize_value(x) for x in value]
    if isinstance(value, float):
        # Stabilize floats for fingerprinting.
        return round(value, 3)
    return value


def canonicalize_control_event(event: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Return a fingerprint-stable subset of a control-plane audit event."""
    if not isinstance(event, dict):
        return None
    et = event.get("event_type")
    if et not in CONTROL_EVENT_TYPES:
        return None

    slim: Dict[str, Any] = {"event_type": et}
    for key, val in event.items():
        if key == "event_type":
            continue
        if key in _EXCLUDED_FINGERPRINT_KEYS:
            continue
        if key not in _KEPT_KEYS:
            continue
        # Drop bulky/noisy args detail for fingerprint; keep sorted key names only.
        if key == "args" and isinstance(val, dict):
            slim["arg_keys"] = sorted(str(k) for k in val.keys())
            continue
        if key == "limits" and isinstance(val, dict):
            slim["limits"] = _normalize_value(val)
            continue
        slim[key] = _normalize_value(val)

    # Prefer single tool field.
    tool = slim.get("tool") or slim.get("tool_name")
    if tool is not None:
        slim["tool"] = tool
        slim.pop("tool_name", None)
    return slim


def extract_decision_trace(
    source: Union[str, Path, List[Dict[str, Any]], AuditLog],
) -> List[Dict[str, Any]]:
    """Ordered canonical control-plane events from an audit source."""
    if isinstance(source, AuditLog):
        events = source.events
    elif isinstance(source, list):
        events = source
    else:
        events = load_audit_jsonl(source)

    trace: List[Dict[str, Any]] = []
    for event in events:
        canonical = canonicalize_control_event(event)
        if canonical is not None:
            trace.append(canonical)
    return trace


def decision_fingerprint(
    source: Union[str, Path, List[Dict[str, Any]], AuditLog],
) -> str:
    """sha256 of the canonical decision trace (hex digest)."""
    trace = extract_decision_trace(source)
    material = _canonical_json(trace)
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


@dataclass
class DecisionReplayPackage:
    """Forensic package: decision summary + fingerprint + tip hash."""

    schema_version: str = "1"
    scenario: str = ""
    recommendation_type: str = "other"
    consequential_proposed: bool = False
    consequential_executed: bool = False
    denied_tools: List[str] = field(default_factory=list)
    tool_calls_made: List[str] = field(default_factory=list)
    gates_fired: List[str] = field(default_factory=list)
    stop_reason: Optional[str] = None
    decision_fingerprint: str = ""
    audit_tip_hash: Optional[str] = None
    control_event_count: int = 0

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


_GATE_EVENT_TYPES = frozenset(
    {
        "tool_schema_rejected",
        "tool_blocked",
        "approval_denied",
        "approval_expired",
        "approval_default_deny",
        "idempotent_replay_denied",
        "circuit_open",
        "freshness_sla_violation",
        "evidence_freshness",
        "agent_stop_limit",
    }
)


def reconstruct_decision(
    source: Union[str, Path, List[Dict[str, Any]], AuditLog],
) -> Dict[str, Any]:
    """Rebuild decision summary from audit alone (no LLM re-run)."""
    if isinstance(source, AuditLog):
        events = source.events
    elif isinstance(source, list):
        events = source
    else:
        events = load_audit_jsonl(source)

    scenario = ""
    recommendation_type = "other"
    consequential_proposed = False
    consequential_executed = False
    stop_reason = None
    denied_tools: List[str] = []
    tool_calls_made: List[str] = []
    gates_fired: List[str] = []

    for event in events:
        if not isinstance(event, dict):
            continue
        et = event.get("event_type")
        if event.get("scenario"):
            scenario = str(event.get("scenario"))
        if et == "tool_proposed":
            consequential_proposed = consequential_proposed or bool(
                event.get("consequential")
            )
            # Some trails mark tier high on proposed.
            if str(event.get("tier", "")).lower() == "high":
                consequential_proposed = True
        if et == "tool_executed":
            tool = event.get("tool") or event.get("tool_name")
            if tool:
                tool_calls_made.append(str(tool))
            if str(event.get("tier", "")).lower() == "high":
                consequential_executed = True
        if et in (
            "tool_blocked",
            "idempotent_replay_denied",
            "freshness_sla_violation",
            "tool_schema_rejected",
        ):
            tool = event.get("tool") or event.get("tool_name")
            if tool:
                denied_tools.append(str(tool))
        if et in _GATE_EVENT_TYPES:
            # For evidence_freshness only count fail-closed outcomes.
            if et == "evidence_freshness" and event.get("freshness_ok") is True:
                pass
            else:
                gates_fired.append(str(et))
        if et == "agent_stop_limit":
            stop_reason = event.get("stop_reason") or stop_reason
        if et == "final_recommendation":
            recommendation_type = str(
                event.get("recommendation_type") or event.get("decision") or "other"
            )
            consequential_proposed = bool(
                event.get("consequential_proposed", consequential_proposed)
            )
            consequential_executed = bool(
                event.get("consequential_executed", consequential_executed)
            )
            if event.get("stop_reason"):
                stop_reason = event.get("stop_reason")
            scenario = str(event.get("scenario") or scenario)

    # Deduplicate denied tools preserving order.
    seen = set()
    denied_unique: List[str] = []
    for t in denied_tools:
        if t not in seen:
            seen.add(t)
            denied_unique.append(t)

    return {
        "scenario": scenario,
        "recommendation_type": recommendation_type,
        "consequential_proposed": consequential_proposed,
        "consequential_executed": consequential_executed,
        "denied_tools": denied_unique,
        "tool_calls_made": tool_calls_made,
        "gates_fired": gates_fired,
        "stop_reason": stop_reason,
    }


def build_replay_package(
    source: Union[str, Path, List[Dict[str, Any]], AuditLog],
    *,
    denied_tools: Optional[List[str]] = None,
    tool_calls_made: Optional[List[str]] = None,
) -> DecisionReplayPackage:
    """Build a forensic replay package from audit events (and optional AgentResult hints)."""
    if isinstance(source, AuditLog):
        events = source.events
    elif isinstance(source, list):
        events = source
    else:
        events = load_audit_jsonl(source)

    reconstructed = reconstruct_decision(events)
    if denied_tools is not None:
        reconstructed["denied_tools"] = list(denied_tools)
    if tool_calls_made is not None:
        reconstructed["tool_calls_made"] = list(tool_calls_made)

    trace = extract_decision_trace(events)
    fp = decision_fingerprint(events)

    tip = None
    for event in reversed(events):
        if isinstance(event, dict) and event.get("event_hash"):
            tip = event.get("event_hash")
            break

    return DecisionReplayPackage(
        scenario=reconstructed["scenario"],
        recommendation_type=reconstructed["recommendation_type"],
        consequential_proposed=reconstructed["consequential_proposed"],
        consequential_executed=reconstructed["consequential_executed"],
        denied_tools=list(reconstructed["denied_tools"]),
        tool_calls_made=list(reconstructed["tool_calls_made"]),
        gates_fired=list(reconstructed["gates_fired"]),
        stop_reason=reconstructed["stop_reason"],
        decision_fingerprint=fp,
        audit_tip_hash=tip,
        control_event_count=len(trace),
    )


def verify_replay_package(
    package: Union[DecisionReplayPackage, Dict[str, Any]],
    source: Union[str, Path, List[Dict[str, Any]], AuditLog],
) -> Tuple[bool, List[str]]:
    """Verify package fingerprint and decision fields still match the audit trail."""
    if isinstance(package, DecisionReplayPackage):
        pkg = package.to_dict()
    else:
        pkg = dict(package)

    problems: List[str] = []
    rebuilt = build_replay_package(source)

    if pkg.get("decision_fingerprint") != rebuilt.decision_fingerprint:
        problems.append(
            "decision_fingerprint mismatch (control-plane audit changed or tampered)"
        )

    for field_name in (
        "scenario",
        "recommendation_type",
        "consequential_proposed",
        "consequential_executed",
    ):
        if pkg.get(field_name) != getattr(rebuilt, field_name):
            problems.append(
                "{0} mismatch: package={1!r} audit={2!r}".format(
                    field_name, pkg.get(field_name), getattr(rebuilt, field_name)
                )
            )

    return (len(problems) == 0), problems


def dump_replay_package(
    package: Union[DecisionReplayPackage, Dict[str, Any]],
    path: Union[str, Path],
) -> Path:
    """Write replay package JSON for ops / forensics handoff."""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(package, DecisionReplayPackage):
        payload = package.to_dict()
    else:
        payload = dict(package)
    target.write_text(
        json.dumps(payload, indent=2, sort_keys=True, default=str) + "\n",
        encoding="utf-8",
    )
    return target
