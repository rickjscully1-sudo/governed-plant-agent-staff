"""Human approval gate for consequential (HIGH-risk) tool calls.

Sacred constraint: fail-closed HITL. Consequential tools never auto-execute.
Approvals are single-use (consumed on grant/deny resolve) and may expire.
Dual-control tools (e.g. recommend_line_shutdown) require two distinct
role approvals (operator + supervisor) when require_dual_control is True.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional

from .audit import AuditLog
from .permissions import PermissionTier, get_tier, is_consequential


ApprovalCallback = Callable[[str, str, dict], bool]
"""(tool_name, reason, args) -> approved?"""


@dataclass
class PendingApproval:
    approval_id: str
    tool_name: str
    args: dict
    reason: str
    status: str = "pending"  # pending | granted | denied | expired
    created_at: float = field(default_factory=time.time)
    timeout_seconds: Optional[float] = None


@dataclass
class ApprovalGate:
    """Pause on HIGH-risk tools; execute only after explicit approve/deny.

    Interactive mode: ``interactive_callback`` prompts a human (CLI).
    Programmatic mode: pre-seed decisions via ``set_decision`` / ``auto_deny``.

    Decisions are **one-shot** by default: after a consequential tool is
    resolved once, a second attempt needs a fresh approval (fail-closed).
    Pending approvals that exceed ``approval_timeout_seconds`` expire denied.
    """

    audit: AuditLog
    interactive_callback: Optional[ApprovalCallback] = None
    auto_deny: bool = False  # useful for evals that assert no silent auto-approve
    approval_timeout_seconds: Optional[float] = 300.0
    consume_decisions: bool = True
    require_dual_control: bool = True
    dual_control_tools: Optional[frozenset] = None  # default: recommend_line_shutdown
    dual_control_roles: tuple = ("operator", "supervisor")
    _decisions: Dict[str, bool] = field(default_factory=dict)
    _pending: Dict[str, PendingApproval] = field(default_factory=dict)
    _history: List[PendingApproval] = field(default_factory=list)

    def _dual_tools(self) -> frozenset:
        if self.dual_control_tools is not None:
            return frozenset(self.dual_control_tools)
        return frozenset({"recommend_line_shutdown"})

    def requires_dual_control(self, tool_name: str) -> bool:
        return bool(self.require_dual_control) and tool_name in self._dual_tools()

    def set_decision(
        self,
        tool_name: str,
        approved: bool,
        role: Optional[str] = None,
    ) -> None:
        """Pre-register approve/deny for a tool (optional role for dual-control)."""
        if role:
            self._decisions["{0}:{1}".format(tool_name, role)] = approved
        else:
            self._decisions[tool_name] = approved

    def clear_decisions(self) -> None:
        self._decisions.clear()

    def request_approval(
        self,
        tool_name: str,
        args: dict,
        reason: str = "",
        timeout_seconds: Optional[float] = None,
    ) -> PendingApproval:
        if not is_consequential(tool_name):
            raise ValueError("{0} is not a consequential tool".format(tool_name))

        approval_id = str(uuid.uuid4())
        timeout = (
            timeout_seconds
            if timeout_seconds is not None
            else self.approval_timeout_seconds
        )
        pending = PendingApproval(
            approval_id=approval_id,
            tool_name=tool_name,
            args=dict(args),
            reason=reason or "Agent proposes consequential action: {0}".format(tool_name),
            timeout_seconds=timeout,
        )
        self._pending[approval_id] = pending
        self._history.append(pending)

        self.audit.record(
            "approval_pending",
            approval_id=approval_id,
            tool_name=tool_name,
            tool=tool_name,
            args=args,
            reason=pending.reason,
            tier=PermissionTier.HIGH.value,
            timeout_seconds=timeout,
            decision="pending",
        )
        return pending

    def _is_expired(self, pending: PendingApproval) -> bool:
        timeout = pending.timeout_seconds
        if timeout is None:
            return False
        return (time.time() - pending.created_at) > float(timeout)

    def resolve(self, pending: PendingApproval) -> bool:
        """Resolve a pending approval using callback, pre-seeded decision, or auto_deny.

        Fail-closed on timeout / missing decision. One-shot: consumes pre-seeded
        decisions when ``consume_decisions`` is True.
        """
        tool_name = pending.tool_name

        # Fail-closed: expired pending approvals are denied and audited.
        if self._is_expired(pending):
            pending.status = "expired"
            self.audit.record(
                "approval_expired",
                approval_id=pending.approval_id,
                tool_name=tool_name,
                tool=tool_name,
                args=pending.args,
                approved=False,
                decision="denied",
                stop_reason="approval_timeout",
                tier=PermissionTier.HIGH.value,
                age_seconds=round(time.time() - pending.created_at, 3),
                timeout_seconds=pending.timeout_seconds,
            )
            self.audit.record(
                "approval_denied",
                approval_id=pending.approval_id,
                tool_name=tool_name,
                tool=tool_name,
                args=pending.args,
                approved=False,
                decision="denied",
                reason="approval_timeout",
                tier=PermissionTier.HIGH.value,
            )
            self._pending.pop(pending.approval_id, None)
            return False

        # Dual-control HIGH tools: require TWO distinct role approvals.
        if self.requires_dual_control(tool_name):
            approved = self._resolve_dual_control(pending)
            self._pending.pop(pending.approval_id, None)
            return approved

        # 1) Pre-seeded programmatic decision (keyed by tool name), one-shot
        if tool_name in self._decisions:
            if self.consume_decisions:
                approved = bool(self._decisions.pop(tool_name))
            else:
                approved = bool(self._decisions[tool_name])
        # 2) Interactive human callback
        elif self.interactive_callback is not None:
            approved = bool(
                self.interactive_callback(tool_name, pending.reason, pending.args)
            )
        # 3) Fail closed. Never silently auto-approve
        else:
            approved = False
            if not self.auto_deny:
                self.audit.record(
                    "approval_default_deny",
                    approval_id=pending.approval_id,
                    tool_name=tool_name,
                    tool=tool_name,
                    decision="denied",
                    note="No approval callback or pre-seeded decision; denying (fail closed).",
                )

        pending.status = "granted" if approved else "denied"
        event = "approval_granted" if approved else "approval_denied"
        self.audit.record(
            event,
            approval_id=pending.approval_id,
            tool_name=tool_name,
            tool=tool_name,
            args=pending.args,
            approved=approved,
            decision="allowed" if approved else "denied",
            tier=PermissionTier.HIGH.value,
        )
        self._pending.pop(pending.approval_id, None)
        return approved

    def _pop_decision(self, key: str) -> Optional[bool]:
        if key not in self._decisions:
            return None
        if self.consume_decisions:
            return bool(self._decisions.pop(key))
        return bool(self._decisions[key])

    def _resolve_dual_control(self, pending: PendingApproval) -> bool:
        """Require two distinct role approvals; audit each; fail-closed if incomplete."""
        tool_name = pending.tool_name
        roles = tuple(self.dual_control_roles) or ("operator", "supervisor")
        role_results = {}

        for role in roles:
            key = "{0}:{1}".format(tool_name, role)
            seeded = self._pop_decision(key)
            if seeded is not None:
                role_approved = seeded
            elif self.interactive_callback is not None:
                role_reason = "{0} [{1} approval required]".format(pending.reason, role)
                role_approved = bool(
                    self.interactive_callback(tool_name, role_reason, pending.args)
                )
            else:
                role_approved = False

            role_results[role] = role_approved
            event = "approval_granted" if role_approved else "approval_denied"
            self.audit.record(
                event,
                approval_id=pending.approval_id,
                tool_name=tool_name,
                tool=tool_name,
                args=pending.args,
                approved=role_approved,
                decision="allowed" if role_approved else "denied",
                tier=PermissionTier.HIGH.value,
                approval_role=role,
                dual_control=True,
            )

        # Also consume a bare tool-level decision if present (does not satisfy dual).
        bare = self._pop_decision(tool_name)
        if bare is not None and not all(role_results.values()):
            self.audit.record(
                "approval_denied",
                approval_id=pending.approval_id,
                tool_name=tool_name,
                tool=tool_name,
                args=pending.args,
                approved=False,
                decision="denied",
                reason="dual_control_incomplete",
                note=(
                    "Single approval is insufficient for dual-control tool; "
                    "need distinct {0} approvals.".format("+".join(roles))
                ),
                tier=PermissionTier.HIGH.value,
                dual_control=True,
                role_results=role_results,
            )

        approved = all(bool(v) for v in role_results.values()) and len(role_results) >= 2
        # Distinct roles: ensure we actually evaluated 2+ roles
        if len(set(roles)) < 2:
            approved = False

        pending.status = "granted" if approved else "denied"
        if not approved:
            self.audit.record(
                "approval_denied",
                approval_id=pending.approval_id,
                tool_name=tool_name,
                tool=tool_name,
                args=pending.args,
                approved=False,
                decision="denied",
                reason="dual_control_required",
                role_results=role_results,
                tier=PermissionTier.HIGH.value,
                dual_control=True,
            )
        else:
            self.audit.record(
                "approval_granted",
                approval_id=pending.approval_id,
                tool_name=tool_name,
                tool=tool_name,
                args=pending.args,
                approved=True,
                decision="allowed",
                reason="dual_control_satisfied",
                role_results=role_results,
                tier=PermissionTier.HIGH.value,
                dual_control=True,
            )
        return approved

    def check_and_maybe_approve(
        self,
        tool_name: str,
        args: dict,
        reason: str = "",
        timeout_seconds: Optional[float] = None,
    ) -> bool:
        """Return True if the tool may execute now.

        LOW-risk: always True (after audit).
        HIGH-risk: request approval and resolve; True only if granted and not expired.
        """
        tier = get_tier(tool_name)
        self.audit.record(
            "permission_check",
            tool_name=tool_name,
            tool=tool_name,
            args=args,
            tier=tier.value,
            consequential=is_consequential(tool_name),
            decision="check",
        )

        if not is_consequential(tool_name):
            return True

        pending = self.request_approval(
            tool_name, args, reason=reason, timeout_seconds=timeout_seconds
        )
        return self.resolve(pending)

    @property
    def history(self) -> List[PendingApproval]:
        return list(self._history)


def cli_approval_prompt(tool_name: str, reason: str, args: dict) -> bool:
    """Interactive CLI prompt for human approval, in ops-readable language."""
    from .ops_report import format_approval_prompt

    banner, prompt = format_approval_prompt(tool_name, reason, args)
    print(banner)
    while True:
        answer = input("{0} ".format(prompt)).strip().lower()
        if answer in ("y", "yes"):
            return True
        if answer in ("n", "no", ""):
            return False
        print("Please enter y or n.")
