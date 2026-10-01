"""Approval board for the Agent Staff layer (SIMULATION, mock plant data).

Holds drafted and production-affecting requests until the named humans decide.

* DRAFT: one named approver.
* PRODUCTION: two different named humans (dual control). One approval shows
  "1 of 2" and nothing executes.
* A request held for stale or conflicting data cannot be approved.
* Any denial ends the request. Nothing is executed.
* On the final approval the core ``ApprovalGate`` re-checks the decision
  (defense in depth) before the mock tool runs, and an idempotency key stops
  a second fire.
* Every request records the control policy version and fingerprint it was
  raised under. If the policy is no longer signed and active, or its
  fingerprint changed since the request, no decision counts and nothing runs.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Dict, List, Optional, Tuple

from ..approval import ApprovalGate
from ..idempotency import make_idempotency_key
from .control_policy import POLICY_BLOCKED_MESSAGE
from .policy import AGENTS, TOOL_PLAIN_NAMES, human_label

if TYPE_CHECKING:  # pragma: no cover
    from .session import Chain, StaffSession

DEFAULT_APPROVAL_TIMEOUT_SECONDS = 12 * 3600.0  # one shift


@dataclass
class StaffApproval:
    approval_id: str
    tool: str
    args: Dict[str, Any]
    requested_by: str
    tier: str
    required_roles: Tuple[str, ...]
    summary: str
    chain_id: Optional[str] = None
    decisions: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    status: str = "pending"  # pending | held | executed | denied | expired
    hold_reason: Optional[str] = None
    result: Optional[Dict[str, Any]] = None
    created_at: float = field(default_factory=time.time)
    timeout_seconds: Optional[float] = DEFAULT_APPROVAL_TIMEOUT_SECONDS
    policy_id: str = ""
    policy_version: str = ""
    policy_fingerprint: str = ""

    @property
    def dual_control(self) -> bool:
        return len(set(self.required_roles)) >= 2

    def approvals_count(self) -> int:
        return sum(1 for d in self.decisions.values() if d.get("approved"))

    def progress_label(self) -> str:
        return "{0} of {1} approvals".format(self.approvals_count(), len(self.required_roles))

    def to_dict(self) -> Dict[str, Any]:
        required = []
        for role in self.required_roles:
            d = self.decisions.get(role)
            required.append(
                {
                    "role": role,
                    "label": human_label(role),
                    "decision": None if d is None else ("approved" if d.get("approved") else "denied"),
                    "at": None if d is None else d.get("at"),
                }
            )
        return {
            "approval_id": self.approval_id,
            "tool": self.tool,
            "tool_label": TOOL_PLAIN_NAMES.get(self.tool, self.tool),
            "tier": self.tier,
            "requested_by": self.requested_by,
            "requested_by_name": AGENTS[self.requested_by].name if self.requested_by in AGENTS else self.requested_by,
            "summary": self.summary,
            "args": self.args,
            "required": required,
            "dual_control": self.dual_control,
            "approvals": self.approvals_count(),
            "needed": len(self.required_roles),
            "progress": self.progress_label(),
            "status": self.status,
            "hold_reason": self.hold_reason,
            "result": self.result,
            "can_decide": self.status == "pending",
            "policy_id": self.policy_id,
            "policy_version": self.policy_version,
            "policy_label": "Policy v{0}".format(self.policy_version) if self.policy_version else "Policy unknown",
            "policy_fingerprint": self.policy_fingerprint,
        }


class ApprovalBoard:
    def __init__(self, session: "StaffSession", timeout_seconds: Optional[float] = None) -> None:
        self.session = session
        self.timeout_seconds = (
            DEFAULT_APPROVAL_TIMEOUT_SECONDS if timeout_seconds is None else timeout_seconds
        )
        self._items: Dict[str, StaffApproval] = {}
        self._order: List[str] = []
        self._executed_keys: set = set()
        self._counter = 0

    # ------------------------------------------------------------------
    def request(
        self,
        agent_id: str,
        tool: str,
        args: Dict[str, Any],
        tier: str,
        required_roles: Tuple[str, ...],
        summary: str,
        chain: Optional["Chain"] = None,
    ) -> StaffApproval:
        self._counter += 1
        approval_id = "APR-{0:03d}".format(self._counter)
        policy = self.session.policy
        item = StaffApproval(
            approval_id=approval_id,
            tool=tool,
            args=dict(args or {}),
            requested_by=agent_id,
            tier=tier,
            required_roles=tuple(required_roles),
            summary=summary or TOOL_PLAIN_NAMES.get(tool, tool),
            chain_id=chain.chain_id if chain else None,
            timeout_seconds=self.timeout_seconds,
            policy_id=policy.policy_id,
            policy_version=policy.version,
            policy_fingerprint=policy.fingerprint_short,
        )
        self._items[approval_id] = item
        self._order.append(approval_id)
        if chain is not None:
            chain.approval_ids.append(approval_id)
        self.session.log(
            "approval_pending",
            agent=agent_id,
            tool=tool,
            tool_name=tool,
            approval_id=approval_id,
            staff_tier=tier,
            tier="high",
            required_roles=list(required_roles),
            dual_control=item.dual_control,
            summary=item.summary,
            args=item.args,
            decision="pending",
            progress=item.progress_label(),
        )
        return item

    def get(self, approval_id: str) -> Optional[StaffApproval]:
        return self._items.get(approval_id)

    def all(self) -> List[StaffApproval]:
        return [self._items[i] for i in self._order]

    def open_items(self) -> List[StaffApproval]:
        return [a for a in self.all() if a.status in ("pending", "held")]

    def pending_for(self, role: str) -> List[StaffApproval]:
        return [
            a
            for a in self.all()
            if a.status == "pending" and role in a.required_roles and role not in a.decisions
        ]

    # ------------------------------------------------------------------
    def hold(self, approval_id: str, reason: str) -> None:
        item = self._items.get(approval_id)
        if item is None or item.status != "pending":
            return
        item.status = "held"
        item.hold_reason = reason
        self.session.log(
            "approval_held",
            agent="coordinator",
            tool=item.tool,
            tool_name=item.tool,
            approval_id=approval_id,
            decision="held_fail_closed",
            reason=reason,
        )

    # ------------------------------------------------------------------
    def decide(self, approval_id: str, role: str, approved: bool) -> Dict[str, Any]:
        """Record one human's decision. Execute only when every required role approved."""
        item = self._items.get(approval_id)
        if item is None:
            self.session.log(
                "approval_rejected",
                tool="unknown",
                approval_id=approval_id,
                approval_role=role,
                decision="denied",
                reason="unknown_approval",
            )
            return {"ok": False, "message": "No such approval: {0}".format(approval_id)}

        who = human_label(role)
        if role not in self.session.policy.human_roles:
            return self._reject(item, role, "unknown_role", "{0} is not a known plant role.".format(role))

        if item.status != "pending":
            why = {
                "held": "It is held because the data is stale or conflicting. Reconcile first.",
                "executed": "It has already been executed.",
                "denied": "It was already denied.",
                "expired": "It expired and needs a fresh request.",
            }.get(item.status, "It is not pending.")
            return self._reject(item, role, "not_pending_" + item.status,
                                "{0} cannot act on {1}. {2}".format(who, approval_id, why))

        policy_problem = self._policy_problem(item)
        if policy_problem:
            return self._reject(item, role, "policy_not_signed",
                                "{0} cannot act on {1}. {2}".format(who, approval_id, policy_problem))

        if self._expired(item):
            item.status = "expired"
            self.session.log(
                "approval_expired",
                tool=item.tool,
                tool_name=item.tool,
                approval_id=approval_id,
                decision="denied",
                stop_reason="approval_timeout",
            )
            self.session.after_decision()
            return {"ok": False, "message": "{0} expired (fail closed).".format(approval_id),
                    "approval": item.to_dict()}

        if role not in item.required_roles:
            return self._reject(
                item, role, "role_not_authorized",
                "{0} is not a named approver for {1}. Needed: {2}.".format(
                    who, approval_id, ", ".join(human_label(r) for r in item.required_roles)
                ),
            )

        if role in item.decisions:
            return self._reject(item, role, "duplicate_decision",
                                "{0} already decided on {1}. The same person cannot count twice.".format(
                                    who, approval_id))

        item.decisions[role] = {
            "approved": bool(approved),
            "at": datetime.now(timezone.utc).isoformat(),
            "by": who,
        }
        self.session.log(
            "approval_granted" if approved else "approval_denied",
            agent=item.requested_by,
            tool=item.tool,
            tool_name=item.tool,
            approval_id=approval_id,
            approval_role=role,
            approver=who,
            approved=bool(approved),
            decision="approved" if approved else "denied",
            dual_control=item.dual_control,
            progress=item.progress_label(),
        )

        if not approved:
            item.status = "denied"
            self.session.log(
                "tool_blocked",
                agent=item.requested_by,
                tool=item.tool,
                tool_name=item.tool,
                approval_id=approval_id,
                decision="denied",
                reason="denied_by_human",
                note="{0} denied. Nothing was executed.".format(who),
            )
            self.session.after_decision()
            return {"ok": True, "message": "{0} denied {1}. Nothing was executed.".format(who, approval_id),
                    "approval": item.to_dict()}

        if item.approvals_count() < len(item.required_roles):
            waiting = [human_label(r) for r in item.required_roles if r not in item.decisions]
            self.session.log(
                "approval_waiting",
                agent=item.requested_by,
                tool=item.tool,
                tool_name=item.tool,
                approval_id=approval_id,
                decision="not_executed",
                reason="dual_control_incomplete",
                progress=item.progress_label(),
                waiting_on=waiting,
            )
            self.session.after_decision()
            return {
                "ok": True,
                "message": "{0}: {1}. Not executed. Still waiting on {2}.".format(
                    approval_id, item.progress_label(), ", ".join(waiting)
                ),
                "approval": item.to_dict(),
            }

        message = self._execute(item)
        self.session.after_decision()
        return {"ok": True, "message": message, "approval": item.to_dict()}

    # ------------------------------------------------------------------
    def _reject(self, item: StaffApproval, role: str, reason: str, message: str) -> Dict[str, Any]:
        self.session.log(
            "approval_rejected",
            agent=item.requested_by,
            tool=item.tool,
            tool_name=item.tool,
            approval_id=item.approval_id,
            approval_role=role,
            decision="denied",
            reason=reason,
            note=message,
        )
        return {"ok": False, "message": message, "approval": item.to_dict()}

    def _policy_problem(self, item: StaffApproval) -> Optional[str]:
        """Why the current policy does not allow this request to proceed, if any."""
        policy = self.session.policy
        if not policy.is_active:
            return POLICY_BLOCKED_MESSAGE
        if item.policy_fingerprint and item.policy_fingerprint != policy.fingerprint_short:
            return "The control policy changed since this request was raised. Raise it again."
        return None

    def _expired(self, item: StaffApproval) -> bool:
        if item.timeout_seconds is None:
            return False
        return (time.time() - item.created_at) > float(item.timeout_seconds)

    def _execute(self, item: StaffApproval) -> str:
        session = self.session
        chain = session.chains.get(item.chain_id) if item.chain_id else None
        if chain is not None and chain.fail_closed:
            self.hold(item.approval_id, chain.reason or "Chain stopped (fail closed).")
            return "{0} not executed: chain stopped for stale or conflicting data.".format(item.approval_id)

        policy_problem = self._policy_problem(item)
        if policy_problem:
            item.status = "denied"
            session.log("tool_blocked", agent=item.requested_by, tool=item.tool, tool_name=item.tool,
                        approval_id=item.approval_id, decision="denied", reason="policy_not_signed",
                        note=policy_problem)
            return "{0} not executed: {1}".format(item.approval_id, policy_problem)

        # Defense in depth: the core ApprovalGate re-checks the human decisions.
        roles = tuple(item.required_roles)
        gate = ApprovalGate(
            audit=session.audit,
            auto_deny=True,
            approval_timeout_seconds=None,
            require_dual_control=len(set(roles)) >= 2,
            dual_control_tools=frozenset({item.tool}),
            dual_control_roles=roles if len(set(roles)) >= 2 else ("operator", "supervisor"),
        )
        if len(set(roles)) >= 2:
            for role in roles:
                gate.set_decision(item.tool, bool(item.decisions.get(role, {}).get("approved")), role=role)
        else:
            gate.set_decision(item.tool, bool(item.decisions.get(roles[0], {}).get("approved")))
        allowed = gate.check_and_maybe_approve(item.tool, item.args, reason=item.summary)
        if not allowed:
            item.status = "denied"
            session.log("tool_blocked", agent=item.requested_by, tool=item.tool, tool_name=item.tool,
                        approval_id=item.approval_id, decision="denied", reason="core_gate_denied")
            return "{0} blocked by the core approval gate.".format(item.approval_id)

        key = make_idempotency_key(item.tool, item.args, session.scenario)
        if key in self._executed_keys:
            item.status = "denied"
            session.log("idempotent_replay_denied", agent=item.requested_by, tool=item.tool,
                        tool_name=item.tool, approval_id=item.approval_id, decision="denied",
                        idempotency_key=key)
            return "{0} already executed once; second fire blocked.".format(item.approval_id)

        result = session.registry.execute(item.tool, item.args)
        self._executed_keys.add(key)
        item.status = "executed"
        item.result = result
        session.log(
            "tool_executed",
            agent=item.requested_by,
            tool=item.tool,
            tool_name=item.tool,
            approval_id=item.approval_id,
            tier="high",
            staff_tier=item.tier,
            decision="executed",
            approved_by=[human_label(r) for r in roles],
            idempotency_key=key,
            result_summary=result.get("message") if isinstance(result, dict) else str(result),
        )
        return "{0} approved by {1}. Executed (mock): {2}".format(
            item.approval_id,
            " and ".join(human_label(r) for r in roles),
            result.get("message", "") if isinstance(result, dict) else result,
        )
