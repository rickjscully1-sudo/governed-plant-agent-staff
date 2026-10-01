"""Ops-facing line status report in plain English for plant / reliability / maintenance."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Optional

from .agent import AgentResult
from .audit import display_path
from .tools.data import PLANT, get_scenario_data

# Plain-English labels for consequential tools (approval prompts + footnotes).
ACTION_LABELS = {
    "recommend_line_shutdown": "Shut down Line 03 Filler",
    "create_emergency_work_order": "Create an emergency work order",
    "reorder_critical_parts": "Reorder critical spare parts (spends money)",
    "update_plant_state": "Change plant operating state",
}

SCENARIO_LABELS = {
    "conflict": "Conflict check (possible false alarm)",
    "true_positive": "True-positive bearing failure check",
    "tool_error_storm": "Tool outage (historian/Maximo unavailable)",
    "stale_data": "Stale / unreliable sensor data",
    "social_engineering": "Social engineering (unsigned shutdown demand)",
    "alert_fatigue": "Alert fatigue (repeating nuisance alarms)",
    "parts_unavailable": "Bearing failure but critical spare unavailable",
    "conflicting_work_orders": "Rising vibration vs open PM forbidding stop",
    "dead_network": "Dead sensor vs dead network (private 5G / connectivity)",
    "unauthorized_plc_write": "Unauthorized PLC write / zone-conduit",
    "maximo_sap_disagree": "Maximo vs SAP disagree on work availability",
    "soft_vs_hard_hold": "Soft HOLD vs hard HOLD (investigate while running)",
}


def action_label(tool_name: str) -> str:
    return ACTION_LABELS.get(tool_name, tool_name.replace("_", " "))


def _line_title(line_id: str) -> str:
    # LINE-03-FILLER → LINE-03 FILLER
    return line_id.replace("-", " ").replace("  ", " ").upper()


def _latest_iot_summary(data: Dict[str, Any]) -> Dict[str, Any]:
    iot = data.get("iot_historian", {})
    samples = iot.get("samples") or []
    last = samples[-1] if samples else {}
    summary = iot.get("summary") or {}
    return {
        "last": last,
        "summary": summary,
        "thresholds": iot.get("thresholds") or {},
        "window_hours": iot.get("window_hours"),
    }


def _sensor_bullets(data: Dict[str, Any]) -> List[str]:
    info = _latest_iot_summary(data)
    last = info["last"]
    summary = info["summary"]
    vib = last.get("vibration_mm_s")
    temp = last.get("temp_c")
    perf = last.get("perf_pct")
    trend = summary.get("vibration_trend", "unknown")
    note = summary.get("note", "")
    parts = []
    if vib is not None and temp is not None:
        parts.append(
            f"Sensors: vibration {vib} mm/s, temperature {temp}°C"
            + (f", performance {perf}%" if perf is not None else "")
            + f", trend: {trend.replace('_', ' ')}"
        )
    if note:
        parts.append(f"Alarm status: {note}")
    return parts or ["Sensors: readings reviewed"]


def _maintenance_bullets(data: Dict[str, Any]) -> List[str]:
    maint = data.get("maximo_maintenance", {})
    records = maint.get("records") or []
    bullets: List[str] = []
    for rec in records[:2]:
        bullets.append(
            f"Maintenance ({rec.get('date', '?')}, {rec.get('type', '')}): "
            f"{rec.get('summary', '')} ({rec.get('result', '')})"
        )
    sap = data.get("sap_pm", {})
    for order in (sap.get("recent_closed") or [])[:1]:
        bullets.append(f"SAP PM closed: {order.get('text', order.get('order', ''))}")
    for order in (sap.get("open_orders") or [])[:1]:
        bullets.append(f"SAP PM open: {order.get('text', order.get('order', ''))}")
    return bullets or ["Maintenance history: reviewed"]


def _tech_bullets(data: Dict[str, Any]) -> List[str]:
    notes = (data.get("technician_notes") or {}).get("notes") or []
    bullets: List[str] = []
    for n in notes[:2]:
        author = n.get("author", "Technician")
        text = (n.get("text") or "").strip()
        if len(text) > 160:
            text = text[:157] + "…"
        bullets.append(f"Technician ({author}): {text}")
    return bullets or ["Technician notes: none on file"]


def _fleet_bullets(data: Dict[str, Any], scenario: str) -> List[str]:
    fleet = data.get("fleet_failures") or {}
    bullets: List[str] = []
    if scenario == "conflict":
        for fp in (fleet.get("false_positive_patterns") or [])[:1]:
            bullets.append(
                f"Fleet pattern: {fp.get('description', fp.get('pattern', ''))}"
            )
    else:
        for fail in (fleet.get("similar_failures") or [])[:1]:
            bullets.append(
                f"Fleet history ({fail.get('site', '?')}): {fail.get('mode', '')}, "
                f"{fail.get('outcome', '')}"
            )
    return bullets


def _ot_bullets(data: Dict[str, Any], scenario: str) -> List[str]:
    """Connectivity / OT security evidence for OT scenario pack."""
    bullets: List[str] = []
    conn = data.get("device_connectivity") or {}
    if conn.get("status") and conn.get("status") not in ("OK", "UNKNOWN"):
        bullets.append(
            "Connectivity ({0}): {1}".format(
                conn.get("platform", "carrier IoT platform"),
                conn.get("note") or conn.get("status"),
            )
        )
    net = data.get("private_network_health") or {}
    if net.get("status") and net.get("status") not in ("OK", "UNKNOWN"):
        bullets.append(
            "Private 5G core: {0}".format(net.get("note") or net.get("status"))
        )
    gw = data.get("gateway_status") or {}
    if gw.get("status") and gw.get("status") not in ("OK", "UNKNOWN"):
        bullets.append(
            "OT gateway: {0}".format(gw.get("note") or gw.get("status"))
        )
    sec = data.get("ot_security_events") or {}
    events = sec.get("events") or []
    if events:
        e0 = events[0]
        bullets.append(
            "OT security: {0}".format(
                e0.get("text") or e0.get("type") or "security event"
            )
        )
    elif scenario == "unauthorized_plc_write":
        bullets.append("OT security: unauthorized write path flagged")
    return bullets


def _decision_and_urgency(
    result: AgentResult,
    *,
    interactive: bool = False,
    explicit_deny: bool = False,
    explicit_approve: bool = False,
) -> tuple[str, str]:
    """Return (decision phrase, urgency phrase) in ops language."""
    if result.recommendation_type == "hold_soft" or getattr(result, "hold_mode", None) == "soft":
        return (
            "SOFT HOLD: investigate while running",
            "Production may continue; human investigate",
        )
    if result.recommendation_type == "hold_hard" or getattr(result, "hold_mode", None) == "hard":
        return (
            "HARD HOLD: consequential plant actions blocked",
            "Dual-control required before any line stop",
        )
    if result.recommendation_type == "hold_escalate" or (
        not result.consequential_proposed
        and any(
            k in result.final_message.lower()
            for k in ("false positive", "hold off", "escalat", "conflict")
        )
    ):
        return ("HOLD: do not shut down", "Routine review")

    if result.consequential_executed or explicit_approve:
        return (
            "APPROVED (SHUTDOWN RECOMMENDATION RECORDED)",
            "End of shift / as approved",
        )

    if result.consequential_proposed:
        if explicit_deny or (interactive and _was_explicitly_denied(result)):
            return ("DENIED (NO ACTION)", "Monitor; reassess if condition worsens")
        return ("SHUTDOWN REQUESTED, AWAITING APPROVAL", "End of shift, human decision needed")

    if "escalat" in result.final_message.lower():
        return ("ESCALATE TO RELIABILITY", "Human review")

    return ("ESCALATE TO RELIABILITY", "Human review")


def _was_explicitly_denied(result: AgentResult) -> bool:
    return any(a.get("status") == "denied" for a in result.approvals)


def _why_bullets(result: AgentResult, scenario: str, data: Dict[str, Any]) -> List[str]:
    # Scenario-specific WHY. Never reuse conflict/recalibration text for other holds.
    if scenario == "conflict":
        return [
            "Evidence conflicts. Sensors look alarming, but people and records say otherwise",
            "Yesterday’s work: accelerometer/RTD recalibration with expected elevated readings for 24 to 48 hours",
            "Walkdown reports normal sound and no housing heat, which matches a known post-calibration false-alarm pattern",
        ]
    if scenario == "true_positive" or (
        result.consequential_proposed and scenario not in (
            "parts_unavailable",
            "conflicting_work_orders",
        )
    ):
        return [
            "Multi-day rise in vibration and temperature, well above alarm limits",
            "Technician walkdown confirms bearing growl and hot housing, not a sensor glitch",
            "Fleet history for this pump type shows similar signatures often fail within a shift if left running",
        ]
    if scenario == "tool_error_storm":
        return [
            "Historian and/or Maximo are unavailable. Evidence tools returned errors, not readings",
            "Cannot justify a line shutdown on missing data; unknown risk is not confirmed risk",
            "Floor is waiting on IT/systems recovery before any consequential plant decision",
        ]
    if scenario == "stale_data":
        return [
            "Historian is alarming, but sensors are flagged unreliable or overdue for calibration",
            "Handheld check shows cool housing / low vibration. Live floor evidence contradicts stale alarms",
            "Do not trust overdue or stale alarms for shutdown authority",
        ]
    if scenario == "social_engineering":
        return [
            "Urgent shutdown demand arrived unsigned / without proper authority channel",
            "Sensors and walkdown are calm, with no corroborating plant evidence",
            "An anecdote or pressure message is not authority; architecture holds the line",
        ]
    if scenario == "alert_fatigue":
        return [
            "Mild repeating alarms with a flat trend and cool housing on walkdown",
            "Pattern matches nuisance / alert fatigue, not an escalating failure",
            "Nuisance alarms warrant monitoring and ticket hygiene, not escalation to shutdown",
        ]
    if scenario == "parts_unavailable":
        return [
            "Evidence looks true-positive-ish for bearing failure (vib/temp + tech confirmation)",
            "Critical spare is out of stock (OOS). Stopping now may strand the line without a repair path",
            "Hold the stop until the spare is staged or expedited; escalate logistics, not a solo shutdown",
        ]
    if scenario == "conflicting_work_orders":
        return [
            "Vibration is rising, but an open PM / work order forbids stopping this line this week",
            "Procedure conflict means the agent must not recommend shutdown alone",
            "Escalate to a human who can reconcile production constraints with reliability risk",
        ]
    if scenario == "dead_network":
        return [
            "Historian bearing channel is flat or silent while walkdown says the pump sounds normal",
            "Carrier IoT platform and private 5G core show dead check-in or congestion on AGV/sensor uplink",
            "Prefer network diagnosis and a connectivity ticket; do not treat a dead radio as a seized bearing",
        ]
    if scenario == "unauthorized_plc_write":
        return [
            "OT security / gateway saw an unexpected Modbus or OPC-style write toward the filler PLC",
            "Mechanical sensors are calm; this is a zone-conduit security event, not a bearing job",
            "Isolate and escalate; HOLD all consequential plant actions; never remediate by writing PLC tags",
        ]
    if scenario == "maximo_sap_disagree":
        return [
            "Maximo says the asset or work is available; SAP PM says blocked for audit (or the reverse)",
            "Conflicting enterprise evidence means the agent must not create a duplicate emergency WO",
            "HOLD until a human reconciles Maximo and SAP; no solo shutdown",
        ]
    if scenario == "soft_vs_hard_hold":
        return [
            "Evidence supports a soft HOLD: investigate while the line keeps running",
            "Mild vib rise with housing not critical is not automatic hard-stop authority",
            "Soft HOLD is not a silent shutdown; hard stop still needs dual-control approval",
        ]
    # Default hold / incomplete
    return [
        "Evidence was incomplete or ambiguous",
        "No consequential plant action was taken by the agent alone",
    ]


def _next_steps(result: AgentResult, scenario: str, decision: str) -> List[str]:
    # Keep APPROVED / DENIED / AWAITING branches; HOLD is scenario-specific.
    if "AWAITING" in decision:
        return [
            "A supervisor or reliability engineer must approve or deny the shutdown request",
            "Until then, increase walkdown frequency; do not leave the pump unattended for long",
            "Confirm bearing kit BRG-6205-2RS availability (stock may be at minimum)",
            "If approving: plan controlled shutdown, LOTO, and bearing replacement (~6 hours)",
        ]
    if "APPROVED" in decision:
        return [
            "Execute controlled shutdown of Line 03 Filler per site procedure",
            "Lockout/tagout, drain recirculation loop, replace bearing kit, realign and re-lube",
            "Capture post-repair vibration baseline before returning to production",
            "Audit trail already records who approved the recommendation",
        ]
    if "DENIED" in decision:
        return [
            "No shutdown was authorized. The line remains under existing operating rules",
            "Document the denial reason in the shift log",
            "Continue monitoring; re-run assessment if vibration or temperature worsens",
            "Consider a planned bearing job at the next outage if risk remains elevated",
        ]

    if "HOLD" in decision or result.recommendation_type in (
        "hold_escalate",
        "hold_soft",
        "hold_hard",
    ):
        if scenario == "conflict":
            return [
                "Do not shut Line 03 for this alarm alone",
                "Have a tech compare historian readings to a handheld meter (sensor verify, ~1 hour, no shutdown)",
                "Re-check vibration after the digital twin re-baselines (~24 hours)",
                "Escalate to reliability only if handheld confirms high vib or housing is hot to touch",
            ]
        if scenario == "tool_error_storm":
            return [
                "Do not shut down on missing data. Wait for historian/Maximo recovery",
                "Page IT / OT support; confirm tool health before re-running assessment",
                "Keep a human on the floor for visual/audio checks until systems return",
                "Re-run the agent only after evidence tools respond successfully",
            ]
        if scenario == "stale_data":
            return [
                "Do not trust the stale historian alarms for shutdown",
                "Complete overdue sensor calibration / replace flagged probes",
                "Document handheld cool/low readings in the shift log",
                "Re-assess only after sensors are marked reliable again",
            ]
        if scenario == "social_engineering":
            return [
                "Refuse the unsigned urgent shutdown demand",
                "Verify any request through the normal authority channel (named supervisor)",
                "Continue routine monitoring, since sensors are calm",
                "Report the social-engineering attempt to plant security / leadership",
            ]
        if scenario == "alert_fatigue":
            return [
                "Treat as nuisance. Do not escalate to shutdown",
                "Tune or suppress duplicate mild alarms per site alarm-management rules",
                "Keep periodic walkdowns; re-open if trend steepens or housing heats",
                "Close or defer the nuisance ticket with a clear rationale",
            ]
        if scenario == "parts_unavailable":
            return [
                "Hold the stop until the critical spare is staged or expedited",
                "Escalate to logistics / stores for BRG kit expedite or loaner",
                "Increase walkdown frequency while the spare is in transit",
                "Only plan controlled shutdown once the spare is on hand (or explicit human override)",
            ]
        if scenario == "conflicting_work_orders":
            return [
                "Do not recommend shutdown alone while the open PM forbids stop this week",
                "Escalate to production + reliability leadership to reconcile the conflict",
                "Document rising vibration against the open work-order constraint",
                "Wait for a human decision that updates the PM or authorizes an exception",
            ]
        if scenario == "dead_network":
            return [
                "Open a network / private 5G ticket before any emergency pump WO",
                "Confirm carrier IoT platform check-ins for sensor and AGV",
                "Keep a human walkdown; do not shut the line on a flat silent stream alone",
                "Re-run assessment after connectivity recovers or handheld proves mechanical fault",
            ]
        if scenario == "unauthorized_plc_write":
            return [
                "Isolate the offending zone-conduit session per OT security playbook",
                "Escalate to OT security / SOC; document Modbus/OPC write evidence",
                "HOLD shutdown, emergency WO, and plant-state changes until security clears",
                "Do not remediate by writing PLC tags from the agent",
            ]
        if scenario == "maximo_sap_disagree":
            return [
                "Do not create a duplicate emergency WO while Maximo and SAP disagree",
                "Escalate to planner and audit desk to release or reconcile the block",
                "Document both system statuses in the shift log",
                "Re-run only after enterprise systems agree on workability",
            ]
        if scenario == "soft_vs_hard_hold":
            return [
                "Continue production under soft HOLD while increasing walkdown cadence",
                "Compare handheld vibration to the historian; stage spare kit if trend steepens",
                "Do not auto-execute recommend_line_shutdown",
                "Hard stop only if operator and supervisor dual-control approve",
            ]
        # default hold
        return [
            "Do not take a solo consequential plant action",
            "Gather missing evidence (handheld, walkdown, system health)",
            "Escalate to the on-call reliability engineer if risk is unclear",
            "Re-run assessment when data is complete and unambiguous",
        ]

    return [
        "Escalate to the on-call reliability engineer",
        "Gather a fresh handheld reading and walkdown note",
        "Do not change plant state without a human decision",
    ]


def _human_approval_block(
    result: AgentResult,
    decision: str,
) -> str:
    if not result.consequential_proposed:
        return "Not required. No shutdown or emergency action was requested."
    if "APPROVED" in decision:
        return "Required: approve shutdown? APPROVED (recorded in audit trail)."
    if "DENIED" in decision:
        return "Required: approve shutdown? DENIED (no action taken)."
    return "Required: approve shutdown? (pending, and the system will not shut the line down alone)"


def format_ops_report(
    result: AgentResult,
    audit_path: Optional[Path] = None,
    *,
    interactive: bool = False,
    explicit_deny: bool = False,
    explicit_approve: bool = False,
) -> str:
    """Build a one-page ops / reliability / maintenance status report."""
    data = get_scenario_data(result.scenario)
    plant = data.get("maximo_asset") or PLANT
    line_id = plant.get("line_id", PLANT["line_id"])
    asset_id = plant.get("asset_id", PLANT["asset_id"])
    site = plant.get("site", PLANT.get("site", ""))

    decision, urgency = _decision_and_urgency(
        result,
        interactive=interactive,
        explicit_deny=explicit_deny,
        explicit_approve=explicit_approve,
    )
    scenario_label = SCENARIO_LABELS.get(result.scenario, result.scenario)

    saw: List[str] = []
    saw.extend(_sensor_bullets(data))
    saw.extend(_maintenance_bullets(data))
    saw.extend(_tech_bullets(data))
    saw.extend(_fleet_bullets(data, result.scenario))
    saw.extend(_ot_bullets(data, result.scenario))

    why = _why_bullets(result, result.scenario, data)
    next_steps = _next_steps(result, result.scenario, decision)
    approval = _human_approval_block(result, decision)
    audit = display_path(audit_path) if audit_path else "(not saved)"

    width = 55
    bar = "═" * width
    title = f" LINE STATUS REPORT: {_line_title(line_id)}"
    lines: List[str] = [
        "",
        bar,
        title,
        bar,
        f" Site:      {site}",
        f" Asset:     {asset_id}",
        f" Scenario:  {scenario_label}",
        f" Decision:  {decision}",
        f" Urgency:   {urgency}",
        "",
        " WHAT WE SAW",
    ]
    for b in saw:
        lines.append(f" • {b}")
    lines.extend(["", " WHY THIS DECISION"])
    for b in why:
        lines.append(f" • {b}")
    lines.extend(["", " WHAT OPS SHOULD DO NEXT"])
    for i, step in enumerate(next_steps, 1):
        lines.append(f" {i}. {step}")
    lines.extend(
        [
            "",
            " HUMAN APPROVAL",
            f" {approval}",
            "",
            f" Audit trail saved for compliance: {audit}",
            "",
            " Technical detail (optional)",
            f" Evidence sources checked: {len(result.tool_calls_made)}",
            f" Consequential action proposed: {'yes' if result.consequential_proposed else 'no'}",
            f" Consequential action executed: {'yes' if result.consequential_executed else 'no'}",
            " Note: this system never shuts a line down by itself.",
            bar,
            "",
        ]
    )
    return "\n".join(lines)


def format_developer_summary(result: AgentResult, audit_path: Optional[Path] = None) -> str:
    """Previous developer-oriented summary (available via --verbose)."""
    lines = [
        "--- Final recommendation ---",
        result.final_message,
        "",
        "--- Summary ---",
        f"  recommendation_type:      {result.recommendation_type}",
        f"  consequential_proposed:   {result.consequential_proposed}",
        f"  consequential_executed:   {result.consequential_executed}",
        f"  denied_tools:             {result.denied_tools}",
        f"  tools called:             {result.tool_calls_made}",
        f"  approvals:                {result.approvals}",
        f"  audit log:                {display_path(audit_path)}",
    ]
    return "\n".join(lines)


def format_approval_prompt(tool_name: str, reason: str, args: dict) -> tuple[str, str]:
    """Return (banner_text, input_prompt) for ops-readable interactive approval."""
    action = action_label(tool_name)
    line_id = args.get("line_id", PLANT["line_id"])
    asset_id = args.get("asset_id", PLANT["asset_id"])
    urgency = str(args.get("urgency", "as recommended")).replace("_", " ")
    why_raw = (args.get("reason") or reason or "").strip()

    why_bullets: List[str] = []
    if why_raw:
        # Keep decimals intact; split on "; " or sentence boundaries (. + space + Capital)
        rough = why_raw.replace(chr(10), " ").strip()
        parts = [p.strip() for p in rough.split("; ") if p.strip()]
        if len(parts) == 1:
            import re as _re
            parts = [p.strip() for p in _re.split(r"(?<=[.!?])\s+(?=[A-Z])", rough) if p.strip()]
        for c in parts:
            if c and c.lower() not in {b.lower() for b in why_bullets}:
                why_bullets.append(c.rstrip("."))
            if len(why_bullets) >= 3:
                break
    if not why_bullets:
        why_bullets = ["Agent assessed a consequential plant action is warranted"]

    if tool_name == "recommend_line_shutdown":
        risk_yes = (
            "Approving stops production on this line until repair completes "
            "(lost output, but protects the pump and downstream equipment)."
        )
        risk_no = (
            "Denying leaves the line running. If the bearing is failing, "
            "it may seize within a shift and cause a longer unplanned outage."
        )
        prompt = "Approve shutdown? [y/N]"
    elif tool_name == "reorder_critical_parts":
        risk_yes = "Approving commits spend / purchase order for spare parts."
        risk_no = "Denying may leave critical spares below minimum if a repair is needed."
        prompt = "Approve parts reorder? [y/N]"
    else:
        risk_yes = "Approving allows this consequential action to proceed (audited)."
        risk_no = "Denying blocks the action. Nothing changes in the plant."
        prompt = "Approve this action? [y/N]"

    width = 60
    bar = "=" * width
    lines: List[str] = [
        "",
        bar,
        "  HUMAN APPROVAL REQUIRED",
        bar,
        f"  Action requested:  {action}",
        f"  Line / asset:      {line_id} / {asset_id}",
        f"  Urgency:           {urgency}",
        "",
        "  Why:",
    ]
    for b in why_bullets[:3]:
        lines.append(f"   • {b}")
    lines.extend(
        [
            "",
            f"  If you approve:  {risk_yes}",
            f"  If you deny:     {risk_no}",
            "",
            "  The agent cannot shut the line down without your answer.",
            bar,
        ]
    )
    return "\n".join(lines), prompt



def ops_report_dict(
    result: AgentResult,
    audit_path: Optional[Path] = None,
    *,
    interactive: bool = False,
    explicit_deny: bool = False,
    explicit_approve: bool = False,
) -> Dict[str, Any]:
    """Structured ops report for the control-center UI / JSON consumers."""
    data = get_scenario_data(result.scenario)
    plant = data.get("maximo_asset") or PLANT
    decision, urgency = _decision_and_urgency(
        result,
        interactive=interactive,
        explicit_deny=explicit_deny,
        explicit_approve=explicit_approve,
    )

    pending_tools = []
    if result.consequential_proposed and not result.consequential_executed:
        if "AWAITING" in decision:
            for a in result.approvals:
                if a.get("status") in ("pending", "denied"):
                    pending_tools.append(
                        {
                            "tool_name": a.get("tool_name"),
                            "action": action_label(a.get("tool_name", "")),
                            "args": a.get("args") or {},
                            "status": "awaiting_approval",
                        }
                    )
            # Fallback from denied_tools if approvals empty edge case
            if not pending_tools:
                for name in result.denied_tools:
                    pending_tools.append(
                        {
                            "tool_name": name,
                            "action": action_label(name),
                            "args": {},
                            "status": "awaiting_approval",
                        }
                    )

    # Readable audit events (not raw JSON dump)
    audit_readable: List[Dict[str, str]] = []
    for e in result.audit_events[-40:]:
        et = e.get("event_type", "")
        summary = _audit_event_plain(e)
        if summary:
            audit_readable.append(
                {
                    "time": e.get("timestamp", ""),
                    "type": et,
                    "summary": summary,
                }
            )

    status_code = _status_code(decision)
    return {
        "site": plant.get("site", PLANT.get("site", "")),
        "line_id": plant.get("line_id", PLANT["line_id"]),
        "asset_id": plant.get("asset_id", PLANT["asset_id"]),
        "scenario": result.scenario,
        "scenario_label": SCENARIO_LABELS.get(result.scenario, result.scenario),
        "decision": decision,
        "urgency": urgency,
        "status_code": status_code,
        "what_we_saw": {
            "sensors": _sensor_bullets(data),
            "maintenance": _maintenance_bullets(data),
            "technician": _tech_bullets(data),
            "fleet": _fleet_bullets(data, result.scenario),
            "ot_connectivity": _ot_bullets(data, result.scenario),
        },
        "why": _why_bullets(result, result.scenario, data),
        "next_steps": _next_steps(result, result.scenario, decision),
        "human_approval": _human_approval_block(result, decision),
        "pending_approvals": pending_tools,
        "approval_required": bool(pending_tools),
        "consequential_proposed": result.consequential_proposed,
        "consequential_executed": result.consequential_executed,
        "hold_mode": getattr(result, "hold_mode", None),
        "recommendation_type": result.recommendation_type,
        "audit_path": display_path(audit_path),
        "audit_trail": audit_readable,
        "final_message": result.final_message,
        "text_report": format_ops_report(
            result,
            audit_path,
            interactive=interactive,
            explicit_deny=explicit_deny,
            explicit_approve=explicit_approve,
        ),
    }


def _status_code(decision: str) -> str:
    if "SOFT HOLD" in decision:
        return "hold_soft"
    if "HARD HOLD" in decision:
        return "hold_hard"
    if "HOLD" in decision:
        return "hold"
    if "ESCALATE" in decision:
        return "escalate"
    if "AWAITING" in decision:
        return "shutdown_pending"
    if "APPROVED" in decision:
        return "approved"
    if "DENIED" in decision:
        return "denied"
    return "other"


def _audit_event_plain(e: Dict[str, Any]) -> str:
    et = e.get("event_type", "")
    tool = e.get("tool_name")
    if et == "agent_start":
        return "Assessment started"
    if et == "llm_step":
        return ""  # skip noisy steps
    if et == "tool_proposed":
        if e.get("tier") == "high":
            return f"Requested: {action_label(tool or '')}"
        return "Checked an evidence source"
    if et == "tool_executed":
        if e.get("tier") == "high":
            return f"Executed after approval: {action_label(tool or '')}"
        return "Evidence recorded"
    if et == "tool_blocked":
        return f"Blocked pending human approval: {action_label(tool or '')}"
    if et == "approval_pending":
        return f"Waiting for human decision: {action_label(tool or '')}"
    if et == "approval_granted":
        return f"Human APPROVED: {action_label(tool or '')}"
    if et == "approval_denied":
        return f"Human decision recorded (not approved): {action_label(tool or '')}"
    if et == "permission_check":
        return ""
    if et == "final_recommendation":
        return f"Final decision type: {e.get('recommendation_type', '')}"
    if et == "approval_default_deny":
        return "No human available, so fail-closed (no action taken)"
    return et.replace("_", " ")
