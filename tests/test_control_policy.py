"""Control Policy: one signed, versioned policy drives the Agent Staff rules.

SIMULATION / mock plant data for a beverage bottling plant. Fictional people.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from aiot_agent.audit import verify_audit_integrity
from aiot_agent.staff import StaffSession, run_staff_scenario
from aiot_agent.staff.control_policy import (
    POLICY_BLOCKED_MESSAGE,
    POLICY_PATH,
    ControlPolicy,
    StaffTier,
    canonical_policy_json,
    compute_fingerprint,
    get_active_policy,
    load_policy,
)
from aiot_agent.staff.policy import (
    AGENTS,
    HUMANS,
    SPEND_DUAL_CONTROL_THRESHOLD_USD,
    TOOL_APPROVERS,
    TOOL_TIERS,
    policy_panel,
)

ROOT = Path(__file__).resolve().parent.parent


def _signed_copy(changes: dict) -> ControlPolicy:
    """Edit a copy of the policy AND re-sign it (as leadership would)."""
    data = get_active_policy().to_data()
    for key, value in changes.items():
        if isinstance(value, dict):
            data.setdefault(key, {}).update(value)
        else:
            data[key] = value
    data["signoff"]["signed_fingerprint"] = compute_fingerprint(data)
    return ControlPolicy(data, source="test copy")


def _high_executed(s: StaffSession):
    return [e for e in s.audit.events if e["event_type"] == "tool_executed" and e.get("tier") == "high"]


# ---------------------------------------------------------------------------
# Loading, identity, sign-off
# ---------------------------------------------------------------------------


def test_policy_loads_with_owner_signoff_and_rules():
    p = load_policy()
    assert p.load_error is None
    assert p.label == "PLANT-AGENT-POLICY v1.0"
    assert p.short_label == "Policy v1.0"
    assert p.effective_date
    assert p.owner["title"] == "Agent Control Architect" and p.owner["name"]
    titles = {s["title"] for s in p.signatures}
    assert {"Plant Manager", "Operations Director"} <= titles
    assert all(s["name"] and s["signed_on"] for s in p.signatures)
    assert p.spend_threshold_usd == 5000.0
    assert p.tool_tiers["plc_write_tag"] == "blocked"
    assert set(p.agent_lanes) == set(AGENTS)
    assert "policy_tampered_fail_closed" in p.scenario_ids
    assert p.change_log[0]["version"] == "1.0"
    ok, problems = p.verify()
    assert ok, problems
    assert p.is_active


def test_fingerprint_is_stable_and_ignores_signoff_block():
    p1, p2 = load_policy(), load_policy()
    assert p1.fingerprint == p2.fingerprint
    assert len(p1.fingerprint) == 64 and len(p1.fingerprint_short) == 12
    data = json.loads(POLICY_PATH.read_text(encoding="utf-8"))
    # Key order does not matter; the canonical form sorts keys.
    reordered = dict(reversed(list(data.items())))
    assert compute_fingerprint(reordered) == p1.fingerprint
    # The sign-off block is not part of what is signed.
    data["signoff"]["note"] = "changed"
    assert compute_fingerprint(data) == p1.fingerprint
    assert "signoff" not in json.loads(canonical_policy_json(data))
    assert p1.signed_fingerprint == p1.fingerprint


def test_enforcement_constants_come_from_the_policy():
    p = get_active_policy()
    assert SPEND_DUAL_CONTROL_THRESHOLD_USD == p.spend_threshold_usd
    assert {t: v.value for t, v in TOOL_TIERS.items()} == p.tool_tiers
    assert TOOL_APPROVERS == p.tool_approvers
    for aid, agent in AGENTS.items():
        assert agent.allowed_tools == p.agent_lanes[aid]["allowed_tools"]
    assert set(HUMANS) == set(p.human_roles)


# ---------------------------------------------------------------------------
# Enforcement reads the policy (change the copy, behavior changes)
# ---------------------------------------------------------------------------


def test_changing_spend_threshold_in_a_signed_copy_changes_behavior(tmp_path):
    args = {"sku": "BRG-6309-C3", "qty": 2, "estimated_cost_usd": 1700.0}
    default = StaffSession("filler_drift_to_work_order", audit_path=tmp_path / "a.jsonl")
    res = default.broker.call("parts_supply", "draft_purchase_request", args)
    assert res["status"] == "PENDING_APPROVAL"
    assert default.board.get(res["approval_id"]).required_roles == ("buyer",)
    assert default.board.get(res["approval_id"]).tier == "draft"

    strict = _signed_copy({"dual_control": {"spend_threshold_usd": 1000.0}})
    assert strict.is_active
    s = StaffSession("filler_drift_to_work_order", audit_path=tmp_path / "b.jsonl", policy=strict)
    res2 = s.broker.call("parts_supply", "draft_purchase_request", args)
    item = s.board.get(res2["approval_id"])
    assert item.tier == "production"
    assert set(item.required_roles) == {"buyer", "plant_manager"}
    assert strict.tool_tier("draft_purchase_request", args) == StaffTier.PRODUCTION


def test_changing_an_allowlist_in_a_signed_copy_changes_behavior(tmp_path):
    lanes = get_active_policy().to_data()["agents"]
    lanes["line_watch"]["allowed_tools"] = ["iot_historian_query"]
    narrow = _signed_copy({"agents": lanes})
    s = StaffSession("out_of_lane_guardrails", audit_path=tmp_path / "c.jsonl", policy=narrow)
    res = s.broker.call("line_watch", "get_plc_tag_snapshot", {})
    assert res["status"] == "DENIED" and "out_of_lane" in res["reasons"]


def test_plc_write_stays_blocked_even_if_policy_lists_it(tmp_path):
    data = get_active_policy().to_data()
    data["tool_tiers"]["plc_write_tag"] = "read"
    data["agents"]["line_watch"]["allowed_tools"].append("plc_write_tag")
    data["plc_rule"] = {}
    data["signoff"]["signed_fingerprint"] = compute_fingerprint(data)
    loose = ControlPolicy(data)
    s = StaffSession("co2_out_of_spec_hold", audit_path=tmp_path / "p.jsonl", policy=loose)
    res = s.broker.call("line_watch", "plc_write_tag", {"tag": "X", "value": 1})
    assert res["status"] == "DENIED" and "plc_write_always_blocked" in res["reasons"]


# ---------------------------------------------------------------------------
# Fail closed on the policy itself
# ---------------------------------------------------------------------------


def _unsigned() -> ControlPolicy:
    data = get_active_policy().to_data()
    data.pop("signoff")
    return ControlPolicy(data, source="unsigned copy")


def _tampered() -> ControlPolicy:
    return get_active_policy().with_changes({"dual_control": {"spend_threshold_usd": 50000.0}})


@pytest.mark.parametrize("make", [_unsigned, _tampered], ids=["unsigned", "tampered"])
def test_bad_policy_blocks_drafts_and_production_but_allows_reads(make, tmp_path):
    bad = make()
    ok, problems = bad.verify()
    assert not ok and problems
    s = StaffSession("co2_out_of_spec_hold", audit_path=tmp_path / "x.jsonl", policy=bad)
    read = s.broker.call("quality", "quality_lab_results", {})
    assert read.get("status") != "DENIED" and "result" in read
    draft = s.broker.call("maintenance_planner", "draft_work_order", {"asset_id": "A"})
    hold = s.broker.call("quality", "request_line_hold", {"line_id": "LINE-03"})
    spend = s.broker.call("parts_supply", "draft_purchase_request", {"estimated_cost_usd": 9000})
    for res in (draft, hold, spend):
        assert res["status"] == "DENIED"
        assert res["reasons"] == ["policy_not_signed"]
        assert POLICY_BLOCKED_MESSAGE in res["message"]
    assert not s.board.all()
    assert not _high_executed(s)
    assert s.outcome()["code"] == "policy_blocked"
    assert s.to_dict()["policy"]["status_label"] == "Not signed, blocked"


def test_missing_operations_director_signature_blocks(tmp_path):
    data = get_active_policy().to_data()
    data["signoff"]["signatures"] = [
        sig for sig in data["signoff"]["signatures"] if sig["title"] != "Operations Director"
    ]
    p = ControlPolicy(data)
    ok, problems = p.verify()
    assert not ok and any("Operations Director" in x for x in problems)


def test_unreadable_policy_file_fails_closed(tmp_path):
    bad = tmp_path / "policy.json"
    bad.write_text("{not json", encoding="utf-8")
    p = load_policy(bad)
    assert not p.is_active and p.load_error
    assert p.tool_tier("iot_historian_query") == StaffTier.BLOCKED
    assert not p.is_allowed("line_watch", "iot_historian_query")


def test_policy_tampered_scenario_fails_closed_end_to_end(tmp_path):
    s = run_staff_scenario("policy_tampered_fail_closed", audit_path=tmp_path / "t.jsonl")
    assert not s.policy.is_active
    assert s.policy.fingerprint != get_active_policy().fingerprint
    assert not s.board.all(), "no draft or production request may exist under a tampered policy"
    reasons = {r for b in s.blocked for r in b["reasons"]}
    assert reasons == {"policy_not_signed"}
    tools = {b["tool"] for b in s.blocked}
    assert {"draft_work_order", "draft_purchase_request"} <= tools
    # Reads still ran: Line Watch saw the drift.
    lw = next(c for c in s.contributions if c.agent_id == "line_watch")
    assert lw.status == "alert"
    check = [e for e in s.audit.events if e["event_type"] == "policy_check"]
    assert check and check[0]["decision"] == "fail_closed"
    esc = next(e for e in s.escalations if e["chain"] == "POLICY")
    assert set(esc["to"]) == {"control_architect", "plant_manager"}
    out = s.outcome()
    assert out["code"] == "policy_blocked" and out["label"] == "POLICY NOT SIGNED"
    ok, problems = verify_audit_integrity(s.audit, expected_scenario="policy_tampered_fail_closed")
    assert ok, problems


def test_policy_change_after_request_blocks_execution(tmp_path):
    s = run_staff_scenario("co2_out_of_spec_hold", audit_path=tmp_path / "h.jsonl")
    hold = next(a for a in s.board.all() if a.tool == "request_line_hold")
    s.decide(hold.approval_id, "qa_lead", True)
    s.policy = _tampered()  # policy edited mid-shift, after the request was raised
    out = s.decide(hold.approval_id, "shift_lead", True)
    assert not out["ok"]
    assert hold.status == "pending" and hold.approvals_count() == 1
    assert not _high_executed(s)


def test_scenario_not_in_signed_policy_cannot_draft(tmp_path):
    data = get_active_policy().to_data()
    data["scenarios"] = [x for x in data["scenarios"] if x["id"] != "filler_drift_to_work_order"]
    data["signoff"]["signed_fingerprint"] = compute_fingerprint(data)
    p = ControlPolicy(data)
    assert p.is_active
    s = run_staff_scenario("filler_drift_to_work_order", audit_path=tmp_path / "n.jsonl", policy=p)
    assert not s.board.all()
    assert {r for b in s.blocked for r in b["reasons"]} == {"scenario_not_in_policy"}


# ---------------------------------------------------------------------------
# Audit entries and approvals carry the policy version and fingerprint
# ---------------------------------------------------------------------------


def test_audit_entries_and_approvals_carry_policy_version(tmp_path):
    p = get_active_policy()
    s = run_staff_scenario("co2_out_of_spec_hold", audit_path=tmp_path / "v.jsonl")
    hold = next(a for a in s.board.all() if a.tool == "request_line_hold")
    s.decide(hold.approval_id, "qa_lead", True)
    s.decide(hold.approval_id, "shift_lead", True)
    events = s.audit.events
    assert events
    for e in events:  # includes the core ApprovalGate re-check events
        assert e["policy_id"] == "PLANT-AGENT-POLICY"
        assert e["policy_version"] == "1.0"
        assert e["policy_fingerprint"] == p.fingerprint_short
        assert e["policy_signed"] is True
    file_events = [json.loads(x) for x in (tmp_path / "v.jsonl").read_text().splitlines() if x.strip()]
    assert all(e["policy_version"] == "1.0" for e in file_events)
    d = hold.to_dict()
    assert d["policy_label"] == "Policy v1.0"
    assert d["policy_fingerprint"] == p.fingerprint_short
    pending = [e for e in events if e["event_type"] == "approval_pending"]
    assert pending and pending[0]["policy_version"] == "1.0"
    rows = s.to_dict()["audit_trail"]
    assert rows and all(r["policy"] == "Policy v1.0" for r in rows)
    ok, problems = verify_audit_integrity(tmp_path / "v.jsonl", expected_scenario="co2_out_of_spec_hold")
    assert ok, problems


# ---------------------------------------------------------------------------
# Panel, endpoint, UI text
# ---------------------------------------------------------------------------


def test_policy_panel_and_endpoint_payload():
    panel = policy_panel()
    assert panel["status_label"] == "Signed and active" and panel["signed"]
    assert panel["owner"]["label"].endswith("(Agent Control Architect)")
    assert len(panel["signoffs"]) == 2
    assert [l["agent_id"] for l in panel["lanes"]][0] == "coordinator"
    assert any("$5,000" in r for r in panel["rules_compact"])
    assert panel["change_log"][0]["summary"].startswith("Initial signed version")


def test_web_policy_endpoint_and_panel_markup():
    import threading
    import urllib.request
    from http.server import ThreadingHTTPServer

    from aiot_agent.web.server import ControlCenterHandler

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), ControlCenterHandler)
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    try:
        base = "http://127.0.0.1:{0}".format(httpd.server_address[1])
        data = json.loads(urllib.request.urlopen(base + "/api/staff/policy", timeout=5).read())
        assert data["ok"] and data["policy"]["label"] == "PLANT-AGENT-POLICY v1.0"
        html = urllib.request.urlopen(base + "/", timeout=5).read().decode("utf-8")
        assert 'id="staff-card-policy"' in html and "Control Policy" in html
        assert "Policy not signed: drafts and production actions blocked" in html
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_no_em_or_en_dashes_in_policy_files():
    files = [
        ROOT / "aiot_agent" / "staff" / "control_policy.py",
        ROOT / "aiot_agent" / "staff" / "control_policy.json",
        ROOT / "aiot_agent" / "web" / "static" / "index.html",
        ROOT / "aiot_agent" / "web" / "static" / "style.css",
        Path(__file__),
    ]
    for f in files:
        text = f.read_text(encoding="utf-8")
        assert "\u2014" not in text and "\u2013" not in text, f
