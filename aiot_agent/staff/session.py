"""Staff session: one run of a multi-agent scenario, plus the Coordinator.

SIMULATION / mock plant data. Illustrative, not a live plant deployment.

Reuses the core building blocks: ``AuditLog`` (hash-chained JSONL),
``ToolRegistry`` (mock tools), ``ApprovalGate`` (final re-check inside the
approval board), ``evaluate_freshness`` (stale data), idempotency keys.

Every session runs under one ``ControlPolicy`` (default: the signed policy in
``control_policy.json``). Every audit entry is stamped with that policy's id,
version and fingerprint, including the core ApprovalGate re-check events.
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from ..audit import AuditLog, display_path, verify_audit_integrity
from ..freshness import resolve_now
from ..tools.registry import ToolRegistry, ToolSpec
from .agents import Contribution, LineWatch, MaintenancePlanner, PartsSupply, Quality, ShiftHandoff
from .approvals import ApprovalBoard
from .broker import ToolBroker
from .data import get_staff_scenario
from .control_policy import POLICY_BLOCKED_MESSAGE, ControlPolicy, get_active_policy
from .policy import AGENTS, HUMANS, PLANT_LABEL, SIMULATION_LABEL, human_label, policy_panel

MAINTENANCE_EVENTS = ("vibration_drift", "sensor_alarm", "routine_check")
QUALITY_EVENTS = ("quality_deviation",)


@dataclass
class Chain:
    """One plant event and everything the staff does about it."""

    chain_id: str
    event: Dict[str, Any]
    fail_closed: bool = False
    reason: Optional[str] = None
    approval_ids: List[str] = field(default_factory=list)


class PolicyStampedAuditLog(AuditLog):
    """Hash-chained audit log that records the policy each event ran under."""

    def __init__(self, path: Optional[Path], policy: ControlPolicy) -> None:
        super().__init__(path=path)
        self._policy = policy

    def record(self, event_type: str, **details: Any):  # type: ignore[override]
        details.setdefault("policy_id", self._policy.policy_id)
        details.setdefault("policy_version", self._policy.version)
        details.setdefault("policy_fingerprint", self._policy.fingerprint_short)
        details.setdefault("policy_signed", self._policy.is_active)
        return super().record(event_type, **details)


def _register_staff_tools(registry: ToolRegistry, session: "StaffSession") -> None:
    obj = {"type": "object"}

    def _read(key: str, tool: str):
        def handler(args: dict, data: dict) -> dict:
            return {"tool": tool, "result": data.get(key, {})}
        return handler

    def _status(args: dict, data: dict) -> dict:
        return {"tool": "staff_status_read", "result": session.status_snapshot()}

    def _wo(args: dict, data: dict) -> dict:
        return {"tool": "draft_work_order", "status": "OPENED",
                "work_order_id": "WO-L3-24817", "draft_id": args.get("draft_id"),
                "message": "Work order WO-L3-24817 opened in Maximo (mock) from {0}.".format(args.get("draft_id"))}

    def _pr(args: dict, data: dict) -> dict:
        return {"tool": "draft_purchase_request", "status": "SUBMITTED",
                "purchase_request_id": "PR-L3-7781",
                "message": "Purchase request PR-L3-7781 submitted in SAP (mock): {0} x {1}.".format(
                    args.get("qty"), args.get("sku"))}

    def _hold(args: dict, data: dict) -> dict:
        return {"tool": "request_line_hold", "status": "HOLD_PLACED", "hold_id": "QH-L3-0930-01",
                "message": "Quality hold QH-L3-0930-01 placed (mock MES): {0}.".format(args.get("scope"))}

    def _setpoint(args: dict, data: dict) -> dict:
        return {"tool": "change_setpoint", "status": "SETPOINT_CHANGED",
                "message": "Setpoint {0} changed to {1} (mock).".format(args.get("tag"), args.get("value"))}

    def _plc_write(args: dict, data: dict) -> dict:
        # Defense in depth: never reachable through the broker, and never writes.
        raise PermissionError("PLC writes are not implemented in the Agent Staff layer.")

    specs = [
        ("maximo_storeroom_lookup", "Maximo-style storeroom spares (mock, read-only).", _read("maximo_storeroom", "maximo_storeroom_lookup"), "low"),
        ("oem_manual_search", "OEM manual and plant knowledge snippets (mock RAG, read-only).", _read("oem_manual", "oem_manual_search"), "low"),
        ("sap_inventory_lead_time", "SAP-style inventory, minimums and lead times (mock, read-only).", _read("sap_inventory", "sap_inventory_lead_time"), "low"),
        ("quality_lab_results", "Fill level, CO2 and lab results vs spec (mock, read-only).", _read("quality_lab", "quality_lab_results"), "low"),
        ("plant_event_feed", "Plant event feed (mock, read-only).", _read("plant_events", "plant_event_feed"), "low"),
        ("shift_event_log", "Shift event log (mock, read-only).", _read("shift_log", "shift_event_log"), "low"),
        ("staff_status_read", "Open approvals, blocks, conflicts (read-only).", _status, "low"),
        ("draft_work_order", "Draft a work order. Opens only after the Maintenance Supervisor approves.", _wo, "high"),
        ("draft_purchase_request", "Draft a purchase request. Buyer approves; above threshold also Plant Manager.", _pr, "high"),
        ("request_line_hold", "Production hold. Needs QA Lead and Shift Lead.", _hold, "high"),
        ("change_setpoint", "Process setpoint change. Needs Shift Lead and Maintenance Supervisor.", _setpoint, "high"),
        ("plc_write_tag", "PLC tag write. Always blocked.", _plc_write, "high"),
    ]
    for name, desc, handler, tier in specs:
        registry.register(ToolSpec(name=name, description=desc, input_schema=obj, handler=handler, tier_hint=tier))


class StaffSession:
    def __init__(
        self,
        scenario: str,
        audit_path: Optional[Path] = None,
        approval_timeout_seconds: Optional[float] = None,
        policy: Optional[ControlPolicy] = None,
    ) -> None:
        self.scenario = scenario
        self.data = get_staff_scenario(scenario)
        base_policy = policy if policy is not None else get_active_policy()
        tamper = self.data.get("policy_tamper")
        # A tamper scenario edits a copy of the policy after sign-off. The old
        # signature is kept, so the fingerprint no longer matches.
        self.policy: ControlPolicy = base_policy.with_changes(tamper) if tamper else base_policy
        self.now = resolve_now(scenario_reference=self.data.get("reference_now"))
        self.audit_path = audit_path
        self.audit = PolicyStampedAuditLog(audit_path, self.policy)
        self.registry = ToolRegistry(scenario="true_positive")
        self.registry.load_data(self.data)
        _register_staff_tools(self.registry, self)
        self.board = ApprovalBoard(self, timeout_seconds=approval_timeout_seconds)
        self.broker = ToolBroker(self)
        self.chains: Dict[str, Chain] = {}
        self.contributions: List[Contribution] = []
        self.routes: List[Dict[str, Any]] = []
        self.conflicts: List[Dict[str, Any]] = []
        self.escalations: List[Dict[str, Any]] = []
        self.blocked: List[Dict[str, Any]] = []
        self.events: List[Dict[str, Any]] = []
        self.briefs: List[Dict[str, Any]] = []
        self.handoff: Optional[Contribution] = None
        self.coordinator_note: Optional[Contribution] = None
        self.completed = False

    # ------------------------------------------------------------------
    def log(self, event_type: str, **details: Any) -> None:
        details.setdefault("scenario", self.scenario)
        details.setdefault("layer", "agent_staff")
        details.setdefault("simulation", True)
        # Snapshot mutable values so later changes cannot alter a logged event.
        self.audit.record(event_type, **deepcopy(details))

    # ------------------------------------------------------------------
    def run(self) -> "StaffSession":
        Coordinator(self).run()
        self.completed = True
        return self

    def decide(self, approval_id: str, role: str, approved: bool) -> Dict[str, Any]:
        return self.board.decide(approval_id, role, approved)

    def after_decision(self) -> None:
        """Refresh the handoff and briefs so humans see the current state."""
        if not self.completed:
            return
        self._refresh_handoff()
        self.briefs = Coordinator(self).build_briefs()
        self.log("briefs_refreshed", agent="coordinator", decision="refreshed",
                 roles=[b["role"] for b in self.briefs])

    def _refresh_handoff(self) -> None:
        new = ShiftHandoff(self).build()
        if self.handoff in self.contributions:
            idx = self.contributions.index(self.handoff)
            self.contributions[idx] = new
        else:
            self.contributions.append(new)
        self.handoff = new
        self.log("agent_contribution", agent="shift_handoff", decision="brief",
                 recommendation=new.headline)

    # ------------------------------------------------------------------
    def status_snapshot(self) -> Dict[str, Any]:
        return {
            "contributions": [c.to_dict() for c in self.contributions],
            "approvals": [a.to_dict() for a in self.board.all()],
            "conflicts": list(self.conflicts),
            "escalations": list(self.escalations),
            "blocked": [dict(b) for b in self.blocked],
        }

    def outcome(self) -> Dict[str, str]:
        if not self.policy.is_active:
            return {"code": "policy_blocked", "label": "POLICY NOT SIGNED",
                    "headline": POLICY_BLOCKED_MESSAGE + " Reads still run; humans restore the signed policy."}
        approvals = self.board.all()
        pending = [a for a in approvals if a.status == "pending"]
        executed = [a for a in approvals if a.status == "executed"]
        if self.conflicts or any(c.fail_closed for c in self.chains.values()):
            reason = self.conflicts[0]["title"] if self.conflicts else "Stale data"
            return {"code": "escalate", "label": "STOPPED AND ESCALATED",
                    "headline": "{0}. Nothing executed; humans decide.".format(reason)}
        if pending:
            partial = [a for a in pending if a.approvals_count() > 0]
            if partial:
                return {"code": "partial", "label": "WAITING ON SECOND SIGNATURE",
                        "headline": "{0} has {1}. Not executed.".format(partial[0].approval_id, partial[0].progress_label())}
            return {"code": "pending", "label": "WAITING ON HUMANS",
                    "headline": "{0} request(s) drafted. Nothing opens until the named approvers decide.".format(len(pending))}
        if executed:
            return {"code": "approved", "label": "APPROVED BY HUMANS",
                    "headline": "Executed after human approval (mock): " + "; ".join(a.approval_id for a in executed)}
        if self.blocked:
            return {"code": "blocked", "label": "GUARDRAILS HELD",
                    "headline": "{0} out-of-lane attempt(s) denied and logged. Line normal.".format(len(self.blocked))}
        if any(a.status == "denied" for a in approvals):
            return {"code": "denied", "label": "DENIED", "headline": "A human denied the request. Nothing executed."}
        return {"code": "clear", "label": "ALL CLEAR", "headline": "No action needed."}

    def audit_trail(self) -> List[Dict[str, Any]]:
        rows = []
        for e in self.audit.events:
            summary = _summarize(e)
            if summary is None:
                continue
            version = e.get("policy_version")
            rows.append({"time": e.get("timestamp"), "type": e.get("event_type"),
                         "agent": AGENTS[e["agent"]].name if e.get("agent") in AGENTS else "",
                         "summary": summary,
                         "policy": "Policy v{0}".format(version) if version else "",
                         "policy_fingerprint": e.get("policy_fingerprint"),
                         "policy_signed": e.get("policy_signed")})
        return rows

    def to_dict(self) -> Dict[str, Any]:
        ok, problems = verify_audit_integrity(self.audit, expected_scenario=self.scenario)
        ordered = []
        if self.coordinator_note is not None:
            ordered.append(self.coordinator_note)
        ordered.extend(c for c in self.contributions if c is not self.coordinator_note)
        return {
            "simulation_label": SIMULATION_LABEL,
            "plant": PLANT_LABEL,
            "scenario": self.scenario,
            "label": self.data.get("label"),
            "story": self.data.get("story"),
            "reference_now": self.data.get("reference_now"),
            "outcome": self.outcome(),
            "events": self.events,
            "routes": self.routes,
            "contributions": [c.to_dict() for c in ordered],
            "approvals": [a.to_dict() for a in self.board.all()],
            "blocked": [dict(b) for b in self.blocked],
            "conflicts": list(self.conflicts),
            "escalations": list(self.escalations),
            "briefs": list(self.briefs),
            "handoff": dict(self.handoff.data) if self.handoff else None,
            "audit_path": display_path(self.audit_path),
            "audit_ok": ok,
            "audit_problems": problems[:5],
            "audit_trail": self.audit_trail(),
            "policy": policy_panel(self.policy),
        }


# ---------------------------------------------------------------------------
class Coordinator:
    """Chief of staff. Routes, detects conflicts, fails closed, writes briefs.

    Has no write authority: its allowlist is read-only (event feed + status).
    """

    agent_id = "coordinator"

    def __init__(self, session: StaffSession) -> None:
        self.s = session

    def route(self, chain: Chain, to: str, reason: str) -> None:
        self.s.routes.append({"event": chain.chain_id, "to": to, "to_name": AGENTS[to].name, "reason": reason})
        self.s.log("coordinator_route", agent=self.agent_id, routed_to=to, chain=chain.chain_id,
                   decision="routed", reason=reason)

    def record(self, c: Contribution) -> Contribution:
        self.s.contributions.append(c)
        self.s.log("agent_contribution", agent=c.agent_id, chain=c.chain_id, decision=c.status,
                   recommendation=c.recommendation, headline=c.headline, approvals=c.approvals)
        return c

    def stop_and_escalate(self, chain: Chain, title: str, detail: str, to: List[str], action: str,
                          kind: str) -> None:
        chain.fail_closed = True
        chain.reason = "{0}: {1}".format(title, detail)
        if kind == "conflict":
            self.s.conflicts.append({"chain": chain.chain_id, "title": title, "detail": detail})
            self.s.log("conflict_detected", agent=self.agent_id, chain=chain.chain_id,
                       decision="fail_closed", title=title, detail=detail)
        else:
            self.s.log("stale_data_detected", agent=self.agent_id, chain=chain.chain_id,
                       decision="fail_closed", title=title, detail=detail)
        for aid in list(chain.approval_ids):
            self.s.board.hold(aid, chain.reason)
        esc = {"chain": chain.chain_id, "to": to, "to_labels": [human_label(r) for r in to],
               "reason": "{0}. {1}".format(title, detail), "action": action}
        self.s.escalations.append(esc)
        self.s.log("escalation", agent=self.agent_id, chain=chain.chain_id, decision="escalated",
                   escalated_to=to, reason=esc["reason"], action=action)

    # ------------------------------------------------------------------
    def run(self) -> None:
        s = self.s
        s.log("staff_run_start", agent=self.agent_id, decision="start", label=s.data.get("label"),
              note=SIMULATION_LABEL)
        self._policy_check()
        feed = s.broker.call(self.agent_id, "plant_event_feed", {})
        events = ((feed or {}).get("result") or {}).get("events") or []
        s.events = events
        handoff_requested = False

        for ev in events:
            chain = Chain(chain_id=ev.get("id", "EV"), event=ev)
            s.chains[chain.chain_id] = chain
            etype = ev.get("type")
            if etype in MAINTENANCE_EVENTS:
                self._maintenance_chain(chain)
            elif etype in QUALITY_EVENTS:
                self._quality_chain(chain)
            elif etype == "end_of_shift":
                handoff_requested = True
            else:
                s.log("coordinator_route", agent=self.agent_id, chain=chain.chain_id, decision="no_route",
                      reason="No agent owns event type {0}; logged for humans.".format(etype))

        for attempt in s.data.get("injected_attempts") or []:
            s.log("injected_test", agent=attempt["agent"], tool=attempt["tool"], decision="test",
                  note=attempt.get("why"))
            s.broker.call(attempt["agent"], attempt["tool"], attempt.get("args"), injected=True,
                          why=attempt.get("why", ""))

        summary = self._summary_note()
        s.coordinator_note = summary
        self.record(summary)

        if handoff_requested:
            self.route(Chain(chain_id="HANDOFF", event={}), "shift_handoff", "End of shift: build the handoff brief.")
        s.handoff = self.record(ShiftHandoff(s).build())

        s.broker.call(self.agent_id, "staff_status_read", {})
        s.briefs = self.build_briefs()
        s.log("brief_published", agent=self.agent_id, decision="published",
              roles=[b["role"] for b in s.briefs])
        s.log("staff_run_complete", agent=self.agent_id, decision=s.outcome()["code"],
              outcome=s.outcome()["label"])

    def _policy_check(self) -> None:
        """Verify the control policy before any agent acts. Fail closed if unsigned."""
        s = self.s
        p = s.policy
        active, problems = p.verify()
        s.log("policy_check", agent=self.agent_id, decision="active" if active else "fail_closed",
              policy_label=p.label, owner=p.owner_label, signed_fingerprint=p.signed_fingerprint[:12],
              problems=problems, scenario_in_policy=s.scenario in p.scenario_ids)
        if active:
            return
        to = ["control_architect", "plant_manager"]
        esc = {
            "chain": "POLICY",
            "to": to,
            "to_labels": [human_label(r) for r in to],
            "reason": "{0} {1}".format(POLICY_BLOCKED_MESSAGE, " ".join(problems)),
            "action": (
                "Agent Control Architect restores the signed {0} or re-issues it for Plant Manager "
                "and Operations Director sign-off. Until then the staff reads only.".format(p.label)
            ),
        }
        s.escalations.append(esc)
        s.log("escalation", agent=self.agent_id, chain="POLICY", decision="escalated",
              escalated_to=to, reason=esc["reason"], action=esc["action"])

    def _maintenance_chain(self, chain: Chain) -> None:
        s = self.s
        self.route(chain, "line_watch", "Condition event on {0}: check trends and tags.".format(
            chain.event.get("asset_id", "the line")))
        lw = self.record(LineWatch(s).assess(chain))
        if lw.status == "stale":
            self.stop_and_escalate(
                chain, "Stale sensor data",
                "Historian and PLC data for {0} are too old to act on.".format(chain.event.get("asset_id")),
                ["shift_lead", "maintenance_supervisor"],
                "Take a handheld vibration reading and check the Line 3 sensor gateway before any work order.",
                kind="stale",
            )
            return
        if lw.status != "alert":
            return

        self.route(chain, "maintenance_planner", "Line Watch sees an early fault sign: plan the repair.")
        mp = self.record(MaintenancePlanner(s).draft(chain, lw))
        parts_needed = mp.data.get("parts_needed") or []
        if not parts_needed:
            return
        self.route(chain, "parts_supply", "Work order needs parts: confirm stock and lead time.")
        parts_agent = PartsSupply(s)
        ps = parts_agent.check(chain, parts_needed)

        storeroom = mp.data.get("storeroom") or {}
        detail = mp.data.get("storeroom_detail") or {}
        for line in ps.data.get("lines", []):
            mx = int(storeroom.get(line["sku"], 0))
            mx_ok = mx >= line["needed"]
            sap_ok = line["on_hand"] >= line["needed"]
            if mx_ok != sap_ok:
                ps.status = "conflict"
                ps.headline = "Maximo and SAP disagree on {0}.".format(line["sku"])
                ps.recommendation = "No purchase request drafted. Coordinator escalated."
                self.record(ps)
                d = detail.get(line["sku"], {})
                self.stop_and_escalate(
                    chain, "Maximo and SAP disagree",
                    "Maximo storeroom says {0} x {1} in bin {2} (last counted {3}). SAP says {4} on hand "
                    "with a {5}-day lead time. They cannot both be right.".format(
                        mx, line["sku"], d.get("bin", "?"), d.get("last_count", "?"),
                        line["on_hand"], line["lead_time_days"]),
                    ["buyer", "maintenance_supervisor"],
                    "Physically check bin {0}, reconcile SAP and Maximo, then decide whether to expedite.".format(
                        d.get("bin", "?")),
                    kind="conflict",
                )
                return
        self.record(ps)
        parts_agent.draft_requests(chain, ps)

    def _quality_chain(self, chain: Chain) -> None:
        s = self.s
        self.route(chain, "quality", "Quality deviation: compare samples to spec, recommend hold or release.")
        q = self.record(Quality(s).assess(chain))
        self.route(chain, "line_watch", "Process context for Quality (carbonator trend). Read-only.")
        self.record(LineWatch(s).assess(chain, context_only=True))
        if q.status == "stale":
            self.stop_and_escalate(chain, "Stale quality data", "Lab and inline samples are too old.",
                                   ["qa_lead", "shift_lead"], "Pull a fresh sample.", kind="stale")

    def _summary_note(self) -> Contribution:
        s = self.s
        agents = []
        for r in s.routes:
            if r["to_name"] not in agents:
                agents.append(r["to_name"])
        findings = ["Events read: {0}.".format(", ".join(
            "{0} ({1})".format(e.get("id"), str(e.get("type", "")).replace("_", " ")) for e in s.events) or "none")]
        if agents:
            findings.append("Routed to: {0}.".format(", ".join(agents)))
        for cf in s.conflicts:
            findings.append("Conflict: {0}".format(cf["detail"]))
        for es in s.escalations:
            findings.append("Escalated to {0}. Ask: {1}".format(", ".join(es["to_labels"]), es["action"]))
        if s.blocked:
            findings.append("{0} blocked attempt(s) logged.".format(len(s.blocked)))
        findings.append("No write authority: the Coordinator routes and briefs only.")
        findings.append("Ran under {0}, fingerprint {1} ({2}).".format(
            s.policy.label, s.policy.fingerprint_short,
            "signed and active" if s.policy.is_active else "NOT signed, blocked"))
        stopped = [c for c in s.chains.values() if c.fail_closed]
        if not s.policy.is_active:
            head = POLICY_BLOCKED_MESSAGE + " Read-only run."
            status = "conflict"
        elif stopped:
            head = "Stopped the chain and escalated. Nothing executed."
            status = "conflict" if s.conflicts else "stale"
        elif s.board.open_items():
            head = "Routed events; {0} request(s) waiting on named humans.".format(len(s.board.open_items()))
            status = "pending"
        elif s.blocked:
            head = "Quiet shift. Out-of-lane attempts denied and logged."
            status = "info"
        else:
            head = "Routed events. Nothing needs a decision."
            status = "ok"
        return Contribution(agent_id=self.agent_id, headline=head, status=status, findings=findings,
                            recommendation="Read your role brief below.", tools_used=["plant_event_feed"])

    # ------------------------------------------------------------------
    def build_briefs(self) -> List[Dict[str, Any]]:
        s = self.s
        briefs = []
        for role_id, human in HUMANS.items():
            needs = []
            for a in s.board.pending_for(role_id):
                needs.append({"approval_id": a.approval_id, "text": "{0} ({1}{2})".format(
                    a.summary, a.progress_label(), ", dual control" if a.dual_control else "")})
            escalated = [
                "{0} Ask: {1}".format(e["reason"], e["action"]) for e in s.escalations if role_id in e["to"]
            ]
            fyi = []
            for c in s.contributions:
                if c.agent_id in ("coordinator",):
                    continue
                if role_id in AGENTS[c.agent_id].briefs:
                    fyi.append("{0}: {1}".format(AGENTS[c.agent_id].name, c.headline))
            for a in s.board.all():
                if role_id in a.required_roles and a.status in ("held", "executed", "denied"):
                    fyi.append("{0} is {1}: {2}".format(a.approval_id, a.status.upper(), a.summary))
                elif role_id in a.required_roles and a.status == "pending" and role_id in a.decisions:
                    waiting = [human_label(r) for r in a.required_roles if r not in a.decisions]
                    fyi.append("You signed {0}. {1}. Waiting on {2}.".format(
                        a.approval_id, a.progress_label(), ", ".join(waiting)))
            if role_id == "shift_lead" and s.blocked:
                fyi.append("{0} agent attempt(s) were blocked by the guardrails and logged.".format(len(s.blocked)))
            if not (needs or escalated or fyi) and role_id != "shift_lead":
                continue
            briefs.append({
                "role": role_id,
                "title": human.title,
                "name": human.name,
                "needs_decision": needs,
                "escalated": escalated,
                "fyi": fyi or ["Nothing for you this shift."],
            })
        return briefs


# ---------------------------------------------------------------------------
def _summarize(e: Dict[str, Any]) -> Optional[str]:
    et = e.get("event_type")
    agent = AGENTS[e["agent"]].name if e.get("agent") in AGENTS else ""
    tool = e.get("tool") or e.get("tool_name") or ""
    if e.get("layer") != "agent_staff":
        return None  # core ApprovalGate re-check events: kept in the JSONL, hidden in the UI
    if et == "policy_check":
        if e.get("decision") == "active":
            return "Control policy {0} verified: signed by plant leadership, fingerprint {1}. Owner: {2}.".format(
                e.get("policy_label"), e.get("policy_fingerprint"), e.get("owner"))
        return "FAIL CLOSED: {0} {1}".format(POLICY_BLOCKED_MESSAGE, " ".join(e.get("problems") or []))
    if et == "staff_run_start":
        return "Staff run started ({0}). {1}".format(e.get("label"), SIMULATION_LABEL)
    if et == "coordinator_route":
        if e.get("routed_to"):
            return "Coordinator routed {0} to {1}: {2}".format(e.get("chain"), AGENTS[e["routed_to"]].name, e.get("reason"))
        return "Coordinator: {0}".format(e.get("reason"))
    if et == "tool_executed" and e.get("staff_tier") == "read":
        return "{0} read: {1}".format(agent, tool)
    if et == "tool_executed":
        return "EXECUTED after approval: {0} ({1}) approved by {2}".format(
            tool, e.get("approval_id"), " and ".join(e.get("approved_by") or []))
    if et == "tool_blocked":
        return "BLOCKED: {0}".format(e.get("note") or "{0} {1} denied".format(agent, tool))
    if et == "approval_pending":
        return "Approval requested {0} by {1}: {2} Needs {3}.".format(
            e.get("approval_id"), agent, e.get("summary"), ", ".join(human_label(r) for r in e.get("required_roles") or []))
    if et in ("approval_granted", "approval_denied"):
        return "{0} {1} {2} ({3})".format(e.get("approver"), "APPROVED" if e.get("approved") else "DENIED",
                                          e.get("approval_id"), e.get("progress"))
    if et == "approval_waiting":
        return "{0}: {1}, NOT executed. Waiting on {2}.".format(e.get("approval_id"), e.get("progress"),
                                                             ", ".join(e.get("waiting_on") or []))
    if et == "approval_held":
        return "HELD {0} (fail closed): {1}".format(e.get("approval_id"), e.get("reason"))
    if et == "approval_rejected":
        return "Rejected decision on {0}: {1}".format(e.get("approval_id"), e.get("note") or e.get("reason"))
    if et == "approval_expired":
        return "{0} expired (fail closed).".format(e.get("approval_id"))
    if et in ("conflict_detected", "stale_data_detected"):
        return "FAIL CLOSED: {0}. {1}".format(e.get("title"), e.get("detail"))
    if et == "escalation":
        return "Escalated to {0}: {1}".format(", ".join(human_label(r) for r in e.get("escalated_to") or []), e.get("action"))
    if et == "agent_contribution":
        return "{0}: {1}".format(agent, e.get("headline") or e.get("recommendation"))
    if et == "injected_test":
        return "Injected guardrail test: {0}".format(e.get("note"))
    if et == "brief_published":
        return "Coordinator published role briefs for: {0}".format(
            ", ".join(HUMANS[r].title for r in e.get("roles") or [] if r in HUMANS))
    if et == "staff_run_complete":
        return "Staff run complete: {0}".format(e.get("outcome"))
    return None
