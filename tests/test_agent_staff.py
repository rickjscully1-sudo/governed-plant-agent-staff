"""Plant Agent Staff: control rules enforced in code (SIMULATION, mock data)."""

from __future__ import annotations

from pathlib import Path

import pytest

from aiot_agent.audit import verify_audit_integrity
from aiot_agent.staff import STAFF_SCENARIO_ORDER, StaffSession, run_staff_scenario
from aiot_agent.staff.policy import (
    AGENTS,
    SIMULATION_LABEL,
    SPEND_DUAL_CONTROL_THRESHOLD_USD,
    TOOL_TIERS,
    StaffTier,
    required_approvers,
    tool_tier,
)
from aiot_agent.staff.session import Chain

ROOT = Path(__file__).resolve().parent.parent


def _session(scenario: str, tmp_path: Path, **kw) -> StaffSession:
    return run_staff_scenario(scenario, audit_path=tmp_path / "staff.jsonl", **kw)


def _high_executed(s: StaffSession):
    return [e for e in s.audit.events if e["event_type"] == "tool_executed" and e.get("tier") == "high"]


def _events(s: StaffSession, et: str):
    return [e for e in s.audit.events if e["event_type"] == et and e.get("layer") == "agent_staff"]


# ---------------------------------------------------------------------------
# Allowlists
# ---------------------------------------------------------------------------

EXTRA_TOOLS = ("create_emergency_work_order", "recommend_line_shutdown", "totally_unknown_tool")


@pytest.mark.parametrize("agent_id", list(AGENTS))
def test_allowlist_enforced_per_agent(agent_id, tmp_path):
    s = StaffSession("out_of_lane_guardrails", audit_path=tmp_path / "a.jsonl")
    allowed = set(AGENTS[agent_id].allowed_tools)
    for tool in list(TOOL_TIERS) + list(EXTRA_TOOLS):
        before = len(s.blocked)
        res = s.broker.call(agent_id, tool, {"estimated_cost_usd": 10})
        if tool in allowed and TOOL_TIERS.get(tool) == StaffTier.READ:
            assert res.get("status") != "DENIED", (agent_id, tool)
        elif tool in allowed:
            assert res["status"] == "PENDING_APPROVAL", (agent_id, tool)
        else:
            assert res["status"] == "DENIED", (agent_id, tool)
            assert len(s.blocked) == before + 1
            last = [e for e in s.audit.events if e["event_type"] == "tool_blocked"][-1]
            assert last["agent"] == agent_id and last["tool"] == tool
            assert last["decision"] == "denied"
    assert not _high_executed(s)


def test_coordinator_has_no_write_authority():
    for tool in AGENTS["coordinator"].allowed_tools:
        assert tool_tier(tool) == StaffTier.READ
    assert all(tool_tier(t) == StaffTier.READ for t in AGENTS["shift_handoff"].allowed_tools)
    assert all(tool_tier(t) == StaffTier.READ for t in AGENTS["line_watch"].allowed_tools)


def test_shift_handoff_cannot_draft_work_order(tmp_path):
    s = _session("out_of_lane_guardrails", tmp_path)
    hits = [b for b in s.blocked if b["agent"] == "shift_handoff" and b["tool"] == "draft_work_order"]
    assert hits and "out_of_lane" in hits[0]["reasons"]
    assert not s.board.all()
    assert any(e["event_type"] == "tool_blocked" and e.get("agent") == "shift_handoff" for e in s.audit.events)


def test_out_of_lane_scenario_blocks_every_injected_attempt(tmp_path):
    s = _session("out_of_lane_guardrails", tmp_path)
    injected = s.data["injected_attempts"]
    assert len(s.blocked) == len(injected) == 5
    assert len([e for e in _events(s, "tool_blocked") if e.get("injected")]) == 5
    assert not _high_executed(s)
    assert s.outcome()["code"] == "blocked"


# ---------------------------------------------------------------------------
# PLC write
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("agent_id", list(AGENTS))
def test_plc_write_always_blocked(agent_id, tmp_path):
    s = StaffSession("co2_out_of_spec_hold", audit_path=tmp_path / "p.jsonl")
    res = s.broker.call(agent_id, "plc_write_tag", {"tag": "X", "value": 1})
    assert res["status"] == "DENIED"
    assert "plc_write_always_blocked" in res["reasons"]
    ev = [e for e in s.audit.events if e["event_type"] == "tool_blocked"][-1]
    assert ev["reason"] == "plc_write_always_blocked"
    assert not s.board.all()


def test_plc_write_tier_is_blocked_and_has_no_approvers():
    assert tool_tier("plc_write_tag") == StaffTier.BLOCKED
    assert required_approvers("plc_write_tag") == ()


def test_line_watch_plc_write_in_co2_scenario_logged(tmp_path):
    s = _session("co2_out_of_spec_hold", tmp_path)
    hits = [b for b in s.blocked if b["agent"] == "line_watch" and b["tool"] == "plc_write_tag"]
    assert hits and hits[0]["injected"]
    assert not any(e["event_type"] == "tool_executed" and e.get("tool") == "plc_write_tag" for e in s.audit.events)


# ---------------------------------------------------------------------------
# Dual control: CO2 hold
# ---------------------------------------------------------------------------


def _hold(s: StaffSession):
    holds = [a for a in s.board.all() if a.tool == "request_line_hold"]
    assert len(holds) == 1
    return holds[0]


def test_co2_hold_requires_qa_and_shift_lead(tmp_path):
    s = _session("co2_out_of_spec_hold", tmp_path)
    hold = _hold(s)
    assert hold.tier == "production"
    assert set(hold.required_roles) == {"qa_lead", "shift_lead"}
    assert hold.status == "pending"
    assert not _high_executed(s)


def test_one_approval_does_not_execute_hold(tmp_path):
    s = _session("co2_out_of_spec_hold", tmp_path)
    hold = _hold(s)
    out = s.decide(hold.approval_id, "qa_lead", True)
    assert out["ok"] and "Not executed" in out["message"]
    assert hold.status == "pending"
    assert hold.progress_label() == "1 of 2 approvals"
    assert not _high_executed(s)
    assert _events(s, "approval_waiting")
    assert s.outcome()["code"] == "partial"


def test_same_person_twice_and_wrong_role_do_not_count(tmp_path):
    s = _session("co2_out_of_spec_hold", tmp_path)
    hold = _hold(s)
    s.decide(hold.approval_id, "qa_lead", True)
    dup = s.decide(hold.approval_id, "qa_lead", True)
    wrong = s.decide(hold.approval_id, "buyer", True)
    assert not dup["ok"] and not wrong["ok"]
    assert hold.status == "pending" and hold.approvals_count() == 1
    reasons = [e.get("reason") for e in _events(s, "approval_rejected")]
    assert "duplicate_decision" in reasons and "role_not_authorized" in reasons
    assert not _high_executed(s)


def test_two_approvals_execute_hold(tmp_path):
    s = _session("co2_out_of_spec_hold", tmp_path)
    hold = _hold(s)
    s.decide(hold.approval_id, "shift_lead", True)
    assert hold.status == "pending"
    out = s.decide(hold.approval_id, "qa_lead", True)
    assert out["ok"]
    assert hold.status == "executed"
    ex = _high_executed(s)
    assert len(ex) == 1 and ex[0]["tool"] == "request_line_hold"
    # Core ApprovalGate re-checked the dual-control decision.
    assert any(e["event_type"] == "approval_granted" and e.get("reason") == "dual_control_satisfied"
               for e in s.audit.events)
    # A second attempt cannot fire again.
    again = s.decide(hold.approval_id, "qa_lead", True)
    assert not again["ok"]
    assert len(_high_executed(s)) == 1


def test_one_denial_ends_the_hold_request(tmp_path):
    s = _session("co2_out_of_spec_hold", tmp_path)
    hold = _hold(s)
    s.decide(hold.approval_id, "shift_lead", False)
    assert hold.status == "denied"
    late = s.decide(hold.approval_id, "qa_lead", True)
    assert not late["ok"]
    assert not _high_executed(s)
    assert any(e.get("reason") == "denied_by_human" for e in _events(s, "tool_blocked"))


def test_expired_approval_fails_closed(tmp_path):
    s = _session("co2_out_of_spec_hold", tmp_path, approval_timeout_seconds=0.0)
    hold = _hold(s)
    import time
    time.sleep(0.01)
    out = s.decide(hold.approval_id, "qa_lead", True)
    assert not out["ok"] and hold.status == "expired"
    assert not _high_executed(s)


# ---------------------------------------------------------------------------
# Conflict fail-closed: part gap
# ---------------------------------------------------------------------------


def test_part_gap_conflict_fails_closed_and_escalates(tmp_path):
    s = _session("part_gap_escalation", tmp_path)
    assert s.conflicts, "Maximo vs SAP conflict must be detected"
    wo = [a for a in s.board.all() if a.tool == "draft_work_order"]
    assert len(wo) == 1 and wo[0].status == "held"
    assert not [a for a in s.board.all() if a.tool == "draft_purchase_request"]
    esc = s.escalations[0]
    assert set(esc["to"]) == {"buyer", "maintenance_supervisor"}
    assert _events(s, "conflict_detected") and _events(s, "escalation") and _events(s, "approval_held")
    # Supervisor approval cannot open a held work order.
    out = s.decide(wo[0].approval_id, "maintenance_supervisor", True)
    assert not out["ok"]
    assert wo[0].status == "held"
    assert not _high_executed(s)
    assert s.outcome()["code"] == "escalate"


def test_stopped_chain_refuses_new_drafts(tmp_path):
    s = _session("part_gap_escalation", tmp_path)
    chain = next(c for c in s.chains.values() if c.fail_closed)
    res = s.broker.call("parts_supply", "draft_purchase_request",
                        {"sku": "BRG-6309-C3", "qty": 2, "estimated_cost_usd": 1700}, chain=chain)
    assert res["status"] == "DENIED"
    assert "fail_closed_stale_or_conflict" in res["reasons"]


def test_stale_sensor_data_stops_and_escalates(tmp_path):
    s = _session("stale_sensor_fail_closed", tmp_path)
    assert not s.board.all(), "no recommendation may be drafted on stale data"
    assert _events(s, "stale_data_detected")
    assert set(s.escalations[0]["to"]) == {"shift_lead", "maintenance_supervisor"}
    assert not any(c.agent_id == "maintenance_planner" for c in s.contributions)
    assert not _high_executed(s)


# ---------------------------------------------------------------------------
# Happy path: drift to drafted work order
# ---------------------------------------------------------------------------


def test_filler_drift_drafts_work_order_pending_supervisor(tmp_path):
    s = _session("filler_drift_to_work_order", tmp_path)
    routed = [r["to"] for r in s.routes]
    assert routed[:3] == ["line_watch", "maintenance_planner", "parts_supply"]
    wo = [a for a in s.board.all() if a.tool == "draft_work_order"]
    assert len(wo) == 1
    assert wo[0].status == "pending" and wo[0].required_roles == ("maintenance_supervisor",)
    assert wo[0].tier == "draft"
    parts = next(c for c in s.contributions if c.agent_id == "parts_supply")
    assert "in stock" in parts.headline.lower()
    assert not s.conflicts
    assert not _high_executed(s)
    # Handoff brief carries the open work order.
    assert any(wo[0].args["draft_id"] in line for line in s.handoff.data["open"])
    sup = next(b for b in s.briefs if b["role"] == "maintenance_supervisor")
    assert sup["needs_decision"] and sup["needs_decision"][0]["approval_id"] == wo[0].approval_id


def test_supervisor_approval_opens_work_order(tmp_path):
    s = _session("filler_drift_to_work_order", tmp_path)
    wo = next(a for a in s.board.all() if a.tool == "draft_work_order")
    out = s.decide(wo.approval_id, "maintenance_supervisor", True)
    assert out["ok"] and wo.status == "executed"
    assert [e["tool"] for e in _high_executed(s)] == ["draft_work_order"]
    assert any("Done after human approval" in h for h in s.handoff.data["happened"])


# ---------------------------------------------------------------------------
# Tiers and spend threshold
# ---------------------------------------------------------------------------


def test_spend_threshold_switches_to_dual_control():
    low = {"estimated_cost_usd": SPEND_DUAL_CONTROL_THRESHOLD_USD - 1}
    high = {"estimated_cost_usd": SPEND_DUAL_CONTROL_THRESHOLD_USD + 1}
    assert tool_tier("draft_purchase_request", low) == StaffTier.DRAFT
    assert required_approvers("draft_purchase_request", low) == ("buyer",)
    assert tool_tier("draft_purchase_request", high) == StaffTier.PRODUCTION
    assert set(required_approvers("draft_purchase_request", high)) == {"buyer", "plant_manager"}
    assert tool_tier("draft_purchase_request", {"estimated_cost_usd": "lots"}) == StaffTier.PRODUCTION


def test_setpoint_change_needs_two_humans():
    assert tool_tier("change_setpoint") == StaffTier.PRODUCTION
    assert len(set(required_approvers("change_setpoint"))) == 2


def test_unknown_tool_fails_closed():
    assert tool_tier("delete_everything") == StaffTier.BLOCKED


# ---------------------------------------------------------------------------
# Audit and labeling
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("scenario", STAFF_SCENARIO_ORDER)
def test_every_scenario_audit_chain_intact(scenario, tmp_path):
    s = _session(scenario, tmp_path)
    for a in list(s.board.all()):
        for role in a.required_roles:
            s.decide(a.approval_id, role, True)
    ok, problems = verify_audit_integrity(s.audit, expected_scenario=scenario)
    assert ok, problems
    ok_file, problems_file = verify_audit_integrity(tmp_path / "staff.jsonl", expected_scenario=scenario)
    assert ok_file, problems_file
    assert _events(s, "agent_contribution")
    d = s.to_dict()
    assert d["simulation_label"] == SIMULATION_LABEL
    assert d["audit_ok"] is True


def test_web_api_run_and_decide(tmp_path):
    from aiot_agent.web import server

    store = server.SessionStore(max_sessions=5, ttl_seconds=600, base_dir=tmp_path)
    visitor, created = store.get_or_create(None)
    assert created
    state = server._run_staff(visitor, "co2_out_of_spec_hold")
    appr = state["approvals"][0]
    assert appr["dual_control"] and appr["progress"] == "0 of 2 approvals"
    r1 = server._decide_staff(visitor, appr["approval_id"], "qa_lead", True)
    assert r1["state"]["approvals"][0]["progress"] == "1 of 2 approvals"
    assert r1["state"]["approvals"][0]["status"] == "pending"
    r2 = server._decide_staff(visitor, appr["approval_id"], "shift_lead", True)
    assert r2["state"]["approvals"][0]["status"] == "executed"
    assert (visitor.audit_dir() / "audit_staff_co2_out_of_spec_hold.jsonl").exists()
    # UI/API shows only the file name, never an absolute server path.
    assert r2["state"]["audit_path"] == "audit_staff_co2_out_of_spec_hold.jsonl"
    store.clear()


def test_control_center_has_agent_staff_view_and_label():
    html = (ROOT / "aiot_agent" / "web" / "static" / "index.html").read_text(encoding="utf-8")
    assert 'id="view-staff"' in html and "Agent Staff" in html
    assert SIMULATION_LABEL in html
    assert (ROOT / "aiot_agent" / "web" / "static" / "staff.js").is_file()


def test_no_em_or_en_dashes_in_staff_text():
    files = list((ROOT / "aiot_agent" / "staff").glob("*.py"))
    files.append(ROOT / "aiot_agent" / "web" / "static" / "staff.js")
    doc = ROOT / "AGENT_STAFF.md"
    if doc.exists():
        files.append(doc)
    for f in files:
        text = f.read_text(encoding="utf-8")
        assert "\u2014" not in text and "\u2013" not in text, f
