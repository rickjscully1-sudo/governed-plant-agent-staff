"""Tool broker: the single door every agent tool call goes through.

SIMULATION, mock plant data. Enforces, in this order:

1. PLC writes are always blocked (every agent, no approval can unlock it).
2. The tool must be on the calling agent's allowlist (job description).
3. Tools not in the staff catalog fail closed.
4. If the signed control policy is not active (unsigned, or edited after
   sign-off) or the scenario is not on the policy's approved list, no drafts or
   production requests are accepted. Reads still run.
5. If the event chain has been stopped for stale or conflicting data, no new
   drafts or production requests are accepted.
6. READ tools run; DRAFT and PRODUCTION tools become approval requests with
   named human approvers. Nothing gated executes here.

Every rule is read from the session's ``ControlPolicy`` (single source of
truth). Every step is written to the hash-chained audit log, stamped with the
policy version and fingerprint it ran under.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Dict, List, Optional

from .control_policy import POLICY_BLOCKED_MESSAGE
from .policy import AGENTS, TOOL_PLAIN_NAMES, StaffTier, human_label

if TYPE_CHECKING:  # pragma: no cover
    from .session import Chain, StaffSession

# Defense in depth: even if the PLC rule were removed from the policy file,
# these stay blocked. The policy can add PLC tools to the block, never remove.
PLC_WRITE_TOOLS = frozenset({"plc_write_tag"})

REASON_TEXT = {
    "plc_write_always_blocked": "PLC writes are always blocked for every agent.",
    "out_of_lane": "Tool is not in this agent's job description.",
    "unknown_tool_fail_closed": "Tool is not in the staff catalog, so it fails closed.",
    "fail_closed_stale_or_conflict": "Chain is stopped for stale or conflicting data. No new requests.",
    "policy_not_signed": POLICY_BLOCKED_MESSAGE + " Reads still run.",
    "scenario_not_in_policy": "This scenario is not on the signed control policy's approved list.",
}


class ToolBroker:
    def __init__(self, session: "StaffSession") -> None:
        self.session = session

    def call(
        self,
        agent_id: str,
        tool: str,
        args: Optional[Dict[str, Any]] = None,
        *,
        chain: Optional["Chain"] = None,
        summary: str = "",
        injected: bool = False,
        why: str = "",
    ) -> Dict[str, Any]:
        s = self.session
        policy = s.policy
        args = dict(args or {})
        tier = policy.tool_tier(tool, args)
        agent_name = AGENTS[agent_id].name if agent_id in AGENTS else agent_id
        s.log(
            "tool_proposed",
            agent=agent_id,
            tool=tool,
            tool_name=tool,
            staff_tier=tier.value,
            args=args,
            decision="proposed",
            injected=injected,
            chain=chain.chain_id if chain else None,
        )

        reasons: List[str] = []
        if (
            tool in PLC_WRITE_TOOLS
            or (tool.startswith("plc_") and "write" in tool)
            or policy.is_plc_write(tool)
        ):
            reasons.append("plc_write_always_blocked")
        if not policy.is_allowed(agent_id, tool):
            reasons.append("out_of_lane")
        if tool not in policy.tool_tiers and "plc_write_always_blocked" not in reasons:
            reasons.append("unknown_tool_fail_closed")
        if not reasons and tier in (StaffTier.DRAFT, StaffTier.PRODUCTION):
            # Fail closed on the policy itself: re-verified on every gated call.
            if not policy.is_active:
                reasons.append("policy_not_signed")
            elif s.scenario not in policy.scenario_ids:
                reasons.append("scenario_not_in_policy")
        if (
            not reasons
            and chain is not None
            and chain.fail_closed
            and tier in (StaffTier.DRAFT, StaffTier.PRODUCTION)
        ):
            reasons.append("fail_closed_stale_or_conflict")

        if reasons:
            message = "{0} tried to '{1}'. Denied. {2}".format(
                agent_name,
                TOOL_PLAIN_NAMES.get(tool, tool),
                " ".join(REASON_TEXT[r] for r in reasons),
            )
            record = {
                "agent": agent_id,
                "agent_name": agent_name,
                "tool": tool,
                "tool_label": TOOL_PLAIN_NAMES.get(tool, tool),
                "reasons": reasons,
                "message": message,
                "injected": injected,
                "why": why,
                "args": args,
            }
            s.blocked.append(record)
            s.log(
                "tool_blocked",
                agent=agent_id,
                tool=tool,
                tool_name=tool,
                staff_tier=tier.value,
                tier="high",
                args=args,
                decision="denied",
                reason=reasons[0],
                reasons=reasons,
                injected=injected,
                note=message,
            )
            return {"status": "DENIED", "tool": tool, "reasons": reasons, "message": message}

        if tier == StaffTier.READ:
            s.log(
                "permission_check",
                agent=agent_id,
                tool=tool,
                tool_name=tool,
                staff_tier="read",
                tier="low",
                decision="auto_allowed",
            )
            try:
                result = s.registry.execute(tool, args)
            except Exception as exc:  # noqa: BLE001 - fail closed on mock tool errors
                result = {"tool": tool, "status": "ERROR", "error": str(exc)}
            s.log(
                "tool_executed",
                agent=agent_id,
                tool=tool,
                tool_name=tool,
                staff_tier="read",
                tier="low",
                decision="executed",
            )
            return result

        roles = policy.required_approvers(tool, args)
        item = s.board.request(
            agent_id,
            tool,
            args,
            tier=tier.value,
            required_roles=roles,
            summary=summary,
            chain=chain,
        )
        return {
            "status": "PENDING_APPROVAL",
            "tool": tool,
            "approval_id": item.approval_id,
            "required": [human_label(r) for r in roles],
            "message": "Drafted only. Waiting on {0}.".format(
                " and ".join(human_label(r) for r in roles)
            ),
        }
