"""Agent Staff policy: who each agent is, what it may touch, and who approves.

SIMULATION ONLY. Mock plant data for a beverage bottling plant.
Illustrative, not a live plant deployment.

The rules themselves (tool lanes, tiers, approvers, spend threshold, PLC block,
HOLD rules, scenarios) live in ONE signed document, ``control_policy.json``,
owned by the Agent Control Architect and signed by plant leadership. See
``control_policy.py``. The constants below are derived from that document at
import time so older callers keep working; nothing here duplicates a rule.

Control rules (enforced in code by ``broker.ToolBroker`` and
``approvals.ApprovalBoard``, read from the session's ``ControlPolicy``):

* Each agent may only call tools on its own allowlist (its job description).
  Anything else is denied and written to the audit log.
* READ tools run automatically.
* DRAFT tools need one named human approver before anything opens.
* PRODUCTION tools (line hold, setpoint change, spend above the threshold)
  need two different humans (dual control).
* PLC writes are always blocked, for every agent, with or without approval.
* Stale or conflicting data stops the chain and escalates (fail closed).
* If the policy is unsigned or was edited after sign-off, agents may read only.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from .control_policy import (
    POLICY_BLOCKED_MESSAGE,
    ControlPolicy,
    StaffTier,
    get_active_policy,
)

ACTIVE_POLICY: ControlPolicy = get_active_policy()

SIMULATION_LABEL = (
    "Simulation / mock plant data. Illustrative, not a live plant deployment."
)
PLANT_LABEL = "Beverage bottling plant (simulated), Line 3"

# Spend above this (USD, mock) is production-affecting and needs dual control.
# Read from the signed control policy (single source of truth).
SPEND_DUAL_CONTROL_THRESHOLD_USD = ACTIVE_POLICY.spend_threshold_usd

TIER_RULES: Dict[str, str] = ACTIVE_POLICY.tier_rules()


# --------------------------------------------------------------------------
# Humans (fictional names, for the demo only). Named in the control policy.
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class HumanRole:
    role_id: str
    title: str
    name: str  # fictional
    decides: str


def _humans_from(policy: ControlPolicy) -> Dict[str, HumanRole]:
    return {
        h["role_id"]: HumanRole(h["role_id"], h.get("title", h["role_id"]), h.get("name", "?"), h.get("decides", ""))
        for h in policy.humans
    }


HUMANS: Dict[str, HumanRole] = _humans_from(ACTIVE_POLICY)


def human_label(role_id: str) -> str:
    h = HUMANS.get(role_id)
    if not h:
        return role_id
    return "{0} ({1})".format(h.title, h.name)


# --------------------------------------------------------------------------
# Tool catalog: tier per tool (from the policy) and plain names (UI text)
# --------------------------------------------------------------------------

TOOL_TIERS: Dict[str, StaffTier] = {t: StaffTier(v) for t, v in ACTIVE_POLICY.tool_tiers.items()}

TOOL_PLAIN_NAMES: Dict[str, str] = {
    "iot_historian_query": "Read historian trends",
    "get_plc_tag_snapshot": "Read PLC tags (read-only)",
    "get_ot_security_events": "Read OT security events",
    "maximo_asset_lookup": "Read Maximo-style asset record",
    "maximo_maintenance_history": "Read Maximo-style work history",
    "maximo_storeroom_lookup": "Read Maximo-style storeroom spares",
    "repair_recipe_lookup": "Read plant repair procedures",
    "oem_manual_search": "Search OEM manual / plant knowledge (mock RAG)",
    "technician_notes": "Read technician notes",
    "sap_inventory_lead_time": "Read SAP-style inventory and lead times",
    "quality_lab_results": "Read fill level, CO2 and lab results vs spec",
    "plant_event_feed": "Read plant event feed",
    "shift_event_log": "Read shift event log",
    "staff_status_read": "Read open items, approvals and blocks",
    "draft_work_order": "Draft a work order",
    "draft_purchase_request": "Draft a purchase request",
    "request_line_hold": "Request a production hold",
    "change_setpoint": "Change a process setpoint",
    "plc_write_tag": "Write to a PLC tag",
    "create_emergency_work_order": "Create emergency work order (core agent tool)",
    "recommend_line_shutdown": "Recommend line shutdown (core agent tool)",
}

# Who must approve each gated tool (from the policy).
TOOL_APPROVERS: Dict[str, Tuple[str, ...]] = ACTIVE_POLICY.tool_approvers
HIGH_SPEND_APPROVERS: Tuple[str, ...] = ACTIVE_POLICY.high_spend_approvers


def tool_tier(tool_name: str, args: Optional[dict] = None, policy: Optional[ControlPolicy] = None) -> StaffTier:
    """Tier for a tool call under ``policy`` (default: the signed active policy).
    Unknown tools fail closed as BLOCKED."""
    return (policy or ACTIVE_POLICY).tool_tier(tool_name, args)


def required_approvers(
    tool_name: str, args: Optional[dict] = None, policy: Optional[ControlPolicy] = None
) -> Tuple[str, ...]:
    """Named human roles that must ALL approve before the tool executes."""
    return (policy or ACTIVE_POLICY).required_approvers(tool_name, args)


# --------------------------------------------------------------------------
# Agents (job descriptions). Lanes and approvers come from the policy.
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class AgentRole:
    agent_id: str
    name: str
    job: str
    allowed_tools: Tuple[str, ...]
    approver: str  # role_id of the named human it works for / who signs off
    briefs: Tuple[str, ...]  # human roles it briefs
    never: str
    notes: List[str] = field(default_factory=list)


_AGENT_TEXT: Dict[str, Dict[str, object]] = {
    "coordinator": {
        "name": "Coordinator (chief of staff)",
        "job": (
            "Routes plant events to the right agent, spots conflicts and stale data, "
            "stops and escalates when evidence disagrees, and writes a brief for each human."
        ),
        "briefs": ("shift_lead", "maintenance_supervisor", "buyer", "qa_lead", "plant_manager"),
        "never": "Has no write authority of its own. Cannot draft, hold, buy, or write to a PLC.",
    },
    "line_watch": {
        "name": "Line Watch",
        "job": "Watches historian and PLC tag data for drift, stale sensors, and early fault signs.",
        "briefs": ("shift_lead",),
        "never": "Never writes to a PLC. Any PLC write attempt is blocked and logged.",
    },
    "maintenance_planner": {
        "name": "Maintenance Planner",
        "job": (
            "Reads Maximo-style asset and work order history plus OEM manual snippets, "
            "then drafts work orders."
        ),
        "briefs": ("maintenance_supervisor",),
        "never": "Drafts only. Nothing opens until the Maintenance Supervisor approves.",
    },
    "parts_supply": {
        "name": "Parts & Supply",
        "job": "Reads SAP-style inventory and lead times, flags part gaps, drafts purchase requests.",
        "briefs": ("buyer",),
        "never": (
            "Drafts only. Buyer approves. Spend above ${0:,.0f} also needs the Plant Manager.".format(
                SPEND_DUAL_CONTROL_THRESHOLD_USD
            )
        ),
    },
    "quality": {
        "name": "Quality",
        "job": "Reads fill level, CO2 and lab results against spec. Recommends hold or release.",
        "briefs": ("qa_lead", "shift_lead"),
        "never": "Cannot hold product alone. A hold needs QA Lead AND Shift Lead (dual control).",
    },
    "shift_handoff": {
        "name": "Shift Handoff",
        "job": "Builds the end-of-shift brief: what happened, what is open, what is risky.",
        "briefs": ("shift_lead",),
        "never": "Read-only. Cannot draft, approve, or change anything.",
    },
}

AGENT_ORDER: Tuple[str, ...] = (
    "coordinator",
    "line_watch",
    "maintenance_planner",
    "parts_supply",
    "quality",
    "shift_handoff",
)


def _agents_from(policy: ControlPolicy) -> Dict[str, AgentRole]:
    lanes = policy.agent_lanes
    out: Dict[str, AgentRole] = {}
    for aid in AGENT_ORDER:
        text = _AGENT_TEXT[aid]
        lane = lanes.get(aid) or {"allowed_tools": (), "approver": "shift_lead"}  # missing lane: no tools
        out[aid] = AgentRole(
            agent_id=aid,
            name=str(text["name"]),
            job=str(text["job"]),
            allowed_tools=tuple(lane["allowed_tools"]),
            approver=lane["approver"] or "shift_lead",
            briefs=tuple(text["briefs"]),  # type: ignore[arg-type]
            never=str(text["never"]),
        )
    return out


AGENTS: Dict[str, AgentRole] = _agents_from(ACTIVE_POLICY)


def is_allowed(agent_id: str, tool_name: str, policy: Optional[ControlPolicy] = None) -> bool:
    return (policy or ACTIVE_POLICY).is_allowed(agent_id, tool_name)


def policy_panel(policy: Optional[ControlPolicy] = None) -> Dict[str, object]:
    """Control Policy panel data: policy summary plus named lanes and approvers."""
    p = policy or ACTIVE_POLICY
    data = p.summary()
    humans = _humans_from(p)

    def label(role_id: str) -> str:
        h = humans.get(role_id)
        return "{0} ({1})".format(h.title, h.name) if h else role_id

    lanes = []
    policy_lanes = p.agent_lanes
    for aid in AGENT_ORDER:
        lane = policy_lanes.get(aid) or {"allowed_tools": (), "approver": ""}
        lanes.append(
            {
                "agent_id": aid,
                "name": _AGENT_TEXT[aid]["name"],
                "approver": label(lane["approver"]) if lane["approver"] else "None",
                "tools": [
                    {
                        "tool": t,
                        "label": TOOL_PLAIN_NAMES.get(t, t),
                        "tier": p.tool_tier(t).value,
                        "approvers": [label(r) for r in p.required_approvers(t)],
                    }
                    for t in lane["allowed_tools"]
                ],
            }
        )
    data["lanes"] = lanes
    data["blocked_message"] = POLICY_BLOCKED_MESSAGE
    return data


def org_chart(policy: Optional[ControlPolicy] = None) -> Dict[str, object]:
    """Plain-English org chart for the control center and docs."""
    p = policy or ACTIVE_POLICY
    agents = []
    for aid in AGENT_ORDER:
        a = AGENTS[aid]
        tools = []
        for t in p.agent_lanes.get(aid, {}).get("allowed_tools", ()):
            tier = p.tool_tier(t)
            appr = [human_label(r) for r in p.required_approvers(t)]
            tools.append(
                {
                    "tool": t,
                    "label": TOOL_PLAIN_NAMES.get(t, t),
                    "tier": tier.value,
                    "approvers": appr,
                }
            )
        agents.append(
            {
                "agent_id": a.agent_id,
                "name": a.name,
                "job": a.job,
                "tools": tools,
                "approver": human_label(a.approver),
                "briefs": [human_label(r) for r in a.briefs],
                "never": a.never,
            }
        )
    humans = [
        {"role_id": h.role_id, "title": h.title, "name": h.name, "decides": h.decides}
        for h in HUMANS.values()
    ]
    return {
        "simulation_label": SIMULATION_LABEL,
        "plant": PLANT_LABEL,
        "agents": agents,
        "humans": humans,
        "tiers": [{"tier": k, "rule": v} for k, v in p.tier_rules().items()],
        "spend_threshold_usd": p.spend_threshold_usd,
        "policy": policy_panel(p),
        "rules": [
            "Each agent can only use the tools in its job description. Anything else is denied and logged.",
            "Read = automatic. Draft = one named approver. Production-affecting = two different humans.",
            "PLC writes are always blocked, for every agent.",
            "Stale or conflicting data stops the chain and escalates. Nothing is executed.",
            "Every recommendation, approval, denial and blocked attempt is in the audit log.",
            "The rules come from one signed control policy. Unsigned or edited policy means reads only.",
        ],
    }
