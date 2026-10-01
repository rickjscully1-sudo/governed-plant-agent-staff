"""Agent loop: gather evidence, reason, propose actions, and never bypass approval."""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from .approval import ApprovalGate, cli_approval_prompt
from .audit import AuditLog
from .llm.base import LLMResponse, Message
from .llm.factory import get_llm
from .permissions import get_tier, is_consequential
from .schema_validation import validate_tool_args
from .tools.registry import ToolRegistry, get_default_registry
from .idempotency import make_idempotency_key
from .freshness import (
    DEFAULT_FRESHNESS_TOOLS,
    DEFAULT_MAX_AGE_SECONDS,
    evaluate_freshness,
    resolve_now,
)
from .replay import build_replay_package, decision_fingerprint

SYSTEM_PROMPT = """You are an AIoT condition-monitoring agent for a soft-drink bottling line.
Your job is to gather evidence from IoT, Maximo, SAP PM, fleet history, parts,
repair recipes, technician notes, and tribal field observations, then reason
about ambiguity and recommend interventions.

CRITICAL RULES (architecture provides control):
1. Gather broad evidence before concluding. Prefer multiple data sources.
2. When evidence CONFLICTS (e.g. sensors alarm but tech notes say recalibration
   false positive), HOLD OFF on consequential actions and ESCALATE to a human.
3. NEVER silently execute consequential actions: emergency work orders, line
   shutdown recommendations, critical parts reorders, or plant-state changes.
   These require explicit human approval. The approval gate will enforce this
   even if you request the tool.
4. The model provides intelligence; the architecture provides control.
   A human never loses the big call.
5. Always explain your reasoning, cite evidence, and state whether you are
   holding, escalating, or requesting approval for a consequential action.
6. Tool errors, stale/unreliable sensors, or anecdote-only social pressure are
   NOT sufficient for consequential action. Hold and escalate.
"""


@dataclass
class AgentLimits:
    """Hard caps on the agent loop. Exceed => hold_escalate, no consequential execute."""

    max_turns: int = 20
    max_tool_calls: int = 40
    max_wall_time_seconds: float = 120.0


@dataclass
class AgentResult:
    scenario: str
    final_message: str
    recommendation_type: str  # hold_escalate | hold_soft | hold_hard | shutdown_pending_approval | other
    consequential_proposed: bool
    consequential_executed: bool
    approvals: List[Dict[str, Any]] = field(default_factory=list)
    tool_calls_made: List[str] = field(default_factory=list)
    audit_events: List[Dict[str, Any]] = field(default_factory=list)
    denied_tools: List[str] = field(default_factory=list)
    stop_reason: Optional[str] = None
    schema_rejected: List[str] = field(default_factory=list)
    replay_package: Optional[Dict[str, Any]] = None
    decision_fingerprint: Optional[str] = None
    hold_mode: Optional[str] = None  # soft | hard | None (meaningful hold nuance)


class ConditionMonitoringAgent:
    def __init__(
        self,
        scenario: str = "conflict",
        audit_path: Optional[Path] = None,
        interactive: bool = False,
        force_mock_llm: bool = True,
        max_steps: Optional[int] = None,
        approval_decisions: Optional[Dict[str, bool]] = None,
        limits: Optional[AgentLimits] = None,
        approval_timeout_seconds: Optional[float] = 300.0,
        require_dual_control: bool = True,
        dual_control_tools: Optional[frozenset] = None,
        circuit_breaker_threshold: int = 3,
        chaos: Optional[Dict[str, Any]] = None,
        enforce_freshness_sla: bool = True,
        max_evidence_age_seconds: Optional[float] = None,
        freshness_tools: Optional[frozenset] = None,
        now: Optional[Any] = None,
    ) -> None:
        self.scenario = scenario
        self.limits = limits or AgentLimits(
            max_turns=max_steps if max_steps is not None else 20
        )
        if max_steps is not None:
            self.limits.max_turns = max_steps
        self.registry = get_default_registry(scenario)
        log_path = audit_path or Path("logs/audit_{0}.jsonl".format(scenario))
        self.audit = AuditLog(path=log_path)
        self.gate = ApprovalGate(
            audit=self.audit,
            interactive_callback=cli_approval_prompt if interactive else None,
            auto_deny=not interactive,
            approval_timeout_seconds=approval_timeout_seconds,
            consume_decisions=True,
            require_dual_control=require_dual_control,
            dual_control_tools=dual_control_tools,
        )
        if approval_decisions:
            for name, approved in approval_decisions.items():
                # Dual-control tools need both roles when flag is ON.
                if (
                    require_dual_control
                    and self.gate.requires_dual_control(name)
                    and approved
                ):
                    self.gate.set_decision(name, True, role="operator")
                    self.gate.set_decision(name, True, role="supervisor")
                else:
                    self.gate.set_decision(name, approved)

        self.llm = get_llm(force_mock=force_mock_llm, scenario_hint=scenario)
        self.messages: List[Message] = []
        self.tool_calls_made: List[str] = []
        self.denied_tools: List[str] = []
        self.schema_rejected: List[str] = []
        self.stop_reason: Optional[str] = None
        self.circuit_breaker_threshold = int(circuit_breaker_threshold)
        self._consecutive_tool_errors = 0
        self._circuit_open = False
        self._idempotency_seen: set = set()
        if chaos:
            self.registry.set_chaos(chaos)

        # Freshness SLA (physical AI / IoT): stale historian evidence blocks HIGH execute.
        self.enforce_freshness_sla = bool(enforce_freshness_sla)
        self.max_evidence_age_seconds = (
            float(DEFAULT_MAX_AGE_SECONDS)
            if max_evidence_age_seconds is None
            else float(max_evidence_age_seconds)
        )
        self.freshness_tools = (
            frozenset(freshness_tools)
            if freshness_tools is not None
            else frozenset(DEFAULT_FRESHNESS_TOOLS)
        )
        scenario_ref = None
        try:
            scenario_ref = (self.registry._data or {}).get("reference_now")
        except Exception:
            scenario_ref = None
        self._now = resolve_now(explicit=now, scenario_reference=scenario_ref)
        self._stale_evidence = False
        self._freshness_violations: List[Dict[str, Any]] = []

    def _tool_descriptors(self) -> List[Dict[str, Any]]:
        return self.registry.list_tools()

    def run(self, user_goal: Optional[str] = None) -> AgentResult:
        goal = user_goal or (
            "Investigate condition of FILLER-PUMP-A12 on LINE-03-FILLER "
            "(scenario={0}). Gather evidence and recommend next steps. "
            "Do not take consequential actions without human approval.".format(
                self.scenario
            )
        )
        self.audit.record(
            "agent_start",
            scenario=self.scenario,
            goal=goal,
            decision="start",
            limits={
                "max_turns": self.limits.max_turns,
                "max_tool_calls": self.limits.max_tool_calls,
                "max_wall_time_seconds": self.limits.max_wall_time_seconds,
            },
        )
        self.messages = [Message(role="user", content=goal)]

        final_text = ""
        consequential_proposed = False
        wall_start = time.monotonic()
        tool_call_count = 0
        limit_hit = False

        for step in range(self.limits.max_turns):
            elapsed = time.monotonic() - wall_start
            if elapsed > self.limits.max_wall_time_seconds:
                self.stop_reason = "max_wall_time"
                limit_hit = True
                break

            self.audit.record(
                "llm_step",
                step=step,
                scenario=self.scenario,
                decision="continue",
            )
            response: LLMResponse = self.llm.complete(
                messages=self.messages,
                tools=self._tool_descriptors(),
                system=SYSTEM_PROMPT,
            )

            if response.content:
                final_text = response.content
                self.messages.append(Message(role="assistant", content=response.content))

            if not response.tool_calls:
                break

            stop_inner = False
            for tc in response.tool_calls:
                elapsed = time.monotonic() - wall_start
                if elapsed > self.limits.max_wall_time_seconds:
                    self.stop_reason = "max_wall_time"
                    limit_hit = True
                    stop_inner = True
                    break
                if tool_call_count >= self.limits.max_tool_calls:
                    self.stop_reason = "max_tool_calls"
                    limit_hit = True
                    stop_inner = True
                    break

                tool_call_count += 1
                self.tool_calls_made.append(tc.name)
                tier = get_tier(tc.name)
                self.audit.record(
                    "tool_proposed",
                    tool_name=tc.name,
                    tool=tc.name,
                    args=tc.arguments,
                    tier=tier.value,
                    scenario=self.scenario,
                    decision="proposed",
                )

                # Schema validation: reject malformed args before approval/execute.
                try:
                    spec = self.registry.get(tc.name)
                    schema = spec.input_schema
                except KeyError:
                    schema = {"type": "object"}
                ok, schema_errors = validate_tool_args(schema, tc.arguments)
                if not ok:
                    self.schema_rejected.append(tc.name)
                    self.denied_tools.append(tc.name)
                    self.audit.record(
                        "tool_schema_rejected",
                        tool_name=tc.name,
                        tool=tc.name,
                        args=tc.arguments,
                        errors=schema_errors,
                        tier=tier.value,
                        scenario=self.scenario,
                        decision="denied",
                    )
                    self.messages.append(
                        Message(
                            role="tool",
                            name=tc.name,
                            tool_call_id=tc.id,
                            content=json.dumps(
                                {
                                    "status": "SCHEMA_REJECTED",
                                    "errors": schema_errors,
                                    "message": (
                                        "Malformed tool arguments rejected; "
                                        "no execution occurred."
                                    ),
                                }
                            ),
                        )
                    )
                    # Never execute consequential after bad schema.
                    if is_consequential(tc.name):
                        consequential_proposed = True
                    continue

                if tier.value == "high":
                    consequential_proposed = True

                # If we already hit a stop limit earlier in this batch, do not
                # allow consequential execution (fail-closed).
                if limit_hit and is_consequential(tc.name):
                    self.denied_tools.append(tc.name)
                    self.audit.record(
                        "tool_blocked",
                        tool_name=tc.name,
                        tool=tc.name,
                        args=tc.arguments,
                        tier=tier.value,
                        scenario=self.scenario,
                        decision="denied",
                        reason="agent_limit_exceeded",
                    )
                    continue

                allowed = self.gate.check_and_maybe_approve(
                    tc.name,
                    tc.arguments,
                    reason=response.content[:500] if response.content else tc.name,
                )

                if not allowed:
                    self.denied_tools.append(tc.name)
                    deny_msg = (
                        "BLOCKED by approval gate: {0} was NOT approved. "
                        "Consequential action was NOT executed. "
                        "Human retains authority."
                    ).format(tc.name)
                    self.audit.record(
                        "tool_blocked",
                        tool_name=tc.name,
                        tool=tc.name,
                        args=tc.arguments,
                        tier=tier.value,
                        scenario=self.scenario,
                        decision="denied",
                    )
                    self.messages.append(
                        Message(
                            role="tool",
                            name=tc.name,
                            tool_call_id=tc.id,
                            content=json.dumps({"status": "DENIED", "message": deny_msg}),
                        )
                    )
                    final_text = (
                        (response.content or "")
                        + "\n\n[ARCHITECTURE] Consequential tool "
                        + "'{0}' proposed but NOT executed (awaiting/denied human approval).".format(
                            tc.name
                        )
                    )
                    continue

                # Circuit breaker: freeze HIGH-risk path after N consecutive errors.
                if self._circuit_open and is_consequential(tc.name):
                    self.denied_tools.append(tc.name)
                    self.audit.record(
                        "tool_blocked",
                        tool_name=tc.name,
                        tool=tc.name,
                        args=tc.arguments,
                        tier=tier.value,
                        scenario=self.scenario,
                        decision="denied",
                        reason="circuit_open",
                    )
                    self.messages.append(
                        Message(
                            role="tool",
                            name=tc.name,
                            tool_call_id=tc.id,
                            content=json.dumps(
                                {
                                    "status": "CIRCUIT_OPEN",
                                    "message": (
                                        "Circuit breaker open after consecutive tool "
                                        "errors; HIGH-risk execute frozen. HOLD / ESCALATE."
                                    ),
                                }
                            ),
                        )
                    )
                    continue

                # Idempotency: identical consequential execute is denied/no-op.
                idem_key = None
                if is_consequential(tc.name):
                    idem_key = make_idempotency_key(
                        tc.name, tc.arguments, self.scenario
                    )
                    if idem_key in self._idempotency_seen:
                        self.denied_tools.append(tc.name)
                        self.audit.record(
                            "idempotent_replay_denied",
                            tool_name=tc.name,
                            tool=tc.name,
                            args=tc.arguments,
                            tier=tier.value,
                            scenario=self.scenario,
                            decision="denied",
                            idempotency_key=idem_key,
                        )
                        self.messages.append(
                            Message(
                                role="tool",
                                name=tc.name,
                                tool_call_id=tc.id,
                                content=json.dumps(
                                    {
                                        "status": "IDEMPOTENT_REPLAY_DENIED",
                                        "idempotency_key": idem_key,
                                        "message": (
                                            "Identical consequential execute already "
                                            "recorded; second fire blocked."
                                        ),
                                    }
                                ),
                            )
                        )
                        continue

                # Freshness SLA: never execute HIGH-risk on stale critical evidence.
                if (
                    self.enforce_freshness_sla
                    and is_consequential(tc.name)
                    and self._stale_evidence
                ):
                    self.denied_tools.append(tc.name)
                    self.audit.record(
                        "freshness_sla_violation",
                        tool_name=tc.name,
                        tool=tc.name,
                        args=tc.arguments,
                        tier=tier.value,
                        scenario=self.scenario,
                        decision="denied",
                        reason="stale_evidence",
                        violations=list(self._freshness_violations),
                        max_age_seconds=self.max_evidence_age_seconds,
                        evaluated_at=self._now.isoformat(),
                    )
                    self.messages.append(
                        Message(
                            role="tool",
                            name=tc.name,
                            tool_call_id=tc.id,
                            content=json.dumps(
                                {
                                    "status": "FRESHNESS_SLA_VIOLATION",
                                    "message": (
                                        "Critical evidence exceeds freshness SLA; "
                                        "consequential action blocked (fail-closed). "
                                        "HOLD / ESCALATE for fresh data or human override."
                                    ),
                                    "violations": self._freshness_violations,
                                },
                                default=str,
                            ),
                        )
                    )
                    final_text = (
                        (response.content or "")
                        + "\n\n[ARCHITECTURE] Freshness SLA violated. "
                        + "Consequential '{0}' NOT executed; hold/escalate.".format(
                            tc.name
                        )
                    )
                    continue

                # Execute (only after schema OK + approval if HIGH)
                result = self.registry.execute(tc.name, tc.arguments)

                # Track consecutive tool errors for circuit breaker.
                if _is_tool_error(result):
                    self._consecutive_tool_errors += 1
                    if (
                        self._consecutive_tool_errors >= self.circuit_breaker_threshold
                        and not self._circuit_open
                    ):
                        self._circuit_open = True
                        self.audit.record(
                            "circuit_open",
                            scenario=self.scenario,
                            decision="hold_escalate",
                            consecutive_errors=self._consecutive_tool_errors,
                            threshold=self.circuit_breaker_threshold,
                            last_tool=tc.name,
                            note=(
                                "Circuit breaker opened after {0} consecutive tool "
                                "errors; HIGH-risk path frozen (hold_escalate only)."
                            ).format(self._consecutive_tool_errors),
                        )
                else:
                    self._consecutive_tool_errors = 0

                if is_consequential(tc.name) and idem_key is not None:
                    self._idempotency_seen.add(idem_key)

                self.audit.record(
                    "tool_executed",
                    tool_name=tc.name,
                    tool=tc.name,
                    args=tc.arguments,
                    tier=tier.value,
                    scenario=self.scenario,
                    decision="executed",
                    result_summary=_brief(result),
                    idempotency_key=idem_key,
                )
                self.messages.append(
                    Message(
                        role="tool",
                        name=tc.name,
                        tool_call_id=tc.id,
                        content=json.dumps(result, default=str),
                    )
                )

                # Evaluate freshness on critical evidence tools (historian, etc.).
                if (
                    self.enforce_freshness_sla
                    and tc.name in self.freshness_tools
                    and not _is_tool_error(result)
                ):
                    verdict = evaluate_freshness(
                        result,
                        now=self._now,
                        max_age_seconds=self.max_evidence_age_seconds,
                    )
                    audit_payload = verdict.to_audit_dict()
                    audit_payload.update(
                        {
                            "tool_name": tc.name,
                            "tool": tc.name,
                            "scenario": self.scenario,
                            "decision": "ok" if verdict.ok else "stale",
                        }
                    )
                    self.audit.record("evidence_freshness", **audit_payload)
                    if not verdict.ok:
                        self._stale_evidence = True
                        self._freshness_violations.append(
                            {
                                "tool": tc.name,
                                **verdict.to_audit_dict(),
                            }
                        )

            if stop_inner or limit_hit:
                break
        else:
            # for-else: loop exhausted without break → max turns
            # Only mark if we didn't naturally finish with no tool_calls.
            # If last response had no tool_calls we would have broken earlier.
            # Exhausting turns while still looping means we hit the turn cap.
            if self.stop_reason is None:
                # Natural completion also exits via `break` on empty tool_calls.
                # Reaching else means we used all turns.
                self.stop_reason = "max_turns"
                limit_hit = True

        if limit_hit and self.stop_reason:
            self.audit.record(
                "agent_stop_limit",
                scenario=self.scenario,
                stop_reason=self.stop_reason,
                decision="hold_escalate",
                tool_calls=tool_call_count,
                consequential_executed=False,
                note=(
                    "Agent loop limit exceeded ({0}); holding/escalating. "
                    "No further consequential execution.".format(self.stop_reason)
                ),
            )
            if not final_text:
                final_text = (
                    "ASSESSMENT: Agent loop stopped ({0}). "
                    "HOLD / ESCALATE. No consequential action executed."
                ).format(self.stop_reason)
            else:
                final_text = (
                    final_text
                    + "\n\n[ARCHITECTURE] Loop limit ({0}): hold/escalate; "
                    "no consequential execute.".format(self.stop_reason)
                )

        # Classify recommendation. Limits always force hold_escalate.
        text_l = final_text.lower()
        hold_mode = None
        if limit_hit:
            rec_type = "hold_escalate"
        elif self._stale_evidence and not self.audit.consequential_executed():
            rec_type = "hold_escalate"
        elif consequential_proposed and not self.audit.consequential_executed():
            rec_type = "shutdown_pending_approval"
        elif (
            "hold_soft" in text_l
            or "soft hold" in text_l
            or "hold_mode=soft" in text_l
            or "investigate while running" in text_l
        ) and not consequential_proposed:
            rec_type = "hold_soft"
            hold_mode = "soft"
        elif (
            "hold_hard" in text_l
            or "hard hold" in text_l
            or "hold_mode=hard" in text_l
            or "unauthorized plc" in text_l
            or "security hold" in text_l
        ) and not consequential_proposed:
            # Prefer hard when security / hard-hold language is primary.
            if "soft hold" in text_l or "hold_soft" in text_l:
                rec_type = "hold_soft"
                hold_mode = "soft"
            else:
                rec_type = "hold_hard"
                hold_mode = "hard"
        elif any(
            k in text_l
            for k in (
                "false positive",
                "hold off",
                "escalat",
                "do not treat",
                "conflict",
                "hold / escalate",
                "anecdote",
                "unreliable",
                "tool error",
                "insufficient evidence",
                "dead network",
                "dead_check_in",
                "blocked_for_audit",
                "enterprise disagreement",
            )
        ):
            rec_type = "hold_escalate"
        elif consequential_proposed:
            rec_type = "shutdown_pending_approval"
        else:
            rec_type = "other"

        # Scenario hint can pin soft/hard when mock already decided.
        if self.scenario == "soft_vs_hard_hold" and not consequential_proposed:
            rec_type = "hold_soft"
            hold_mode = "soft"
        elif self.scenario == "unauthorized_plc_write" and not consequential_proposed:
            rec_type = "hold_hard"
            hold_mode = "hard"

        executed = self.audit.consequential_executed()

        # Fingerprint control-plane trail + prospective final recommendation
        # (decision_fingerprint field itself is excluded from the hash material).
        prospective = list(self.audit.events) + [
            {
                "event_type": "final_recommendation",
                "recommendation_type": rec_type,
                "consequential_proposed": consequential_proposed,
                "consequential_executed": executed,
                "scenario": self.scenario,
                "decision": rec_type,
                "stop_reason": self.stop_reason,
                "hold_mode": hold_mode,
            }
        ]
        fp = decision_fingerprint(prospective)

        self.audit.record(
            "final_recommendation",
            recommendation_type=rec_type,
            final_message=final_text,
            consequential_proposed=consequential_proposed,
            consequential_executed=executed,
            scenario=self.scenario,
            decision=rec_type,
            stop_reason=self.stop_reason,
            decision_fingerprint=fp,
            hold_mode=hold_mode,
        )

        # Forensic replay package: stable decision fingerprint + tip hash.
        replay_pkg = build_replay_package(
            self.audit.events,
            denied_tools=list(self.denied_tools),
            tool_calls_made=list(self.tool_calls_made),
        )

        approvals = [
            {
                "tool_name": p.tool_name,
                "status": p.status,
                "args": p.args,
            }
            for p in self.gate.history
        ]

        return AgentResult(
            scenario=self.scenario,
            final_message=final_text,
            recommendation_type=rec_type,
            consequential_proposed=consequential_proposed,
            consequential_executed=executed,
            approvals=approvals,
            tool_calls_made=list(self.tool_calls_made),
            audit_events=self.audit.events,
            denied_tools=list(self.denied_tools),
            stop_reason=self.stop_reason,
            schema_rejected=list(self.schema_rejected),
            replay_package=replay_pkg.to_dict(),
            decision_fingerprint=replay_pkg.decision_fingerprint,
            hold_mode=hold_mode,
        )


def _brief(result: dict) -> str:
    try:
        s = json.dumps(result, default=str)
        return s[:300] + ("..." if len(s) > 300 else "")
    except Exception:
        return str(result)[:300]



def _is_tool_error(result: dict) -> bool:
    if not isinstance(result, dict):
        return False
    status = str(result.get("status", "")).upper()
    if status in ("ERROR", "TIMEOUT", "UNAVAILABLE"):
        return True
    if result.get("error"):
        return True
    return False
