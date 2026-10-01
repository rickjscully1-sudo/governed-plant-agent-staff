"""OT / connectivity scenario pack: dead network, PLC write, Maximo vs SAP, soft vs hard HOLD."""

from __future__ import annotations

from pathlib import Path

import pytest

from aiot_agent.agent import ConditionMonitoringAgent
from aiot_agent.ops_report import format_ops_report, ops_report_dict
from aiot_agent.permissions import HIGH_RISK_TOOLS, LOW_RISK_TOOLS, get_tier
from aiot_agent.tools.registry import get_default_registry

OT_LOW_TOOLS = (
    "get_device_connectivity",
    "get_private_network_health",
    "get_gateway_status",
    "get_plc_tag_snapshot",
    "get_ot_security_events",
)

OT_SCENARIOS = (
    "dead_network",
    "unauthorized_plc_write",
    "maximo_sap_disagree",
    "soft_vs_hard_hold",
)

HIGH_NAMES = (
    "create_emergency_work_order",
    "recommend_line_shutdown",
    "reorder_critical_parts",
    "update_plant_state",
)


def _high_executed(result):
    return [
        e
        for e in result.audit_events
        if e["event_type"] == "tool_executed" and e.get("tier") == "high"
    ]


def _assert_no_auto_high(result, label: str):
    assert result.consequential_executed is False, label
    assert _high_executed(result) == [], (label, _high_executed(result))
    assert result.recommendation_type in (
        "hold_escalate",
        "hold_soft",
        "hold_hard",
        "other",
    ), (label, result.recommendation_type)
    # Meaningful hold audited
    finals = [e for e in result.audit_events if e["event_type"] == "final_recommendation"]
    assert finals, label
    assert finals[-1].get("decision") in (
        "hold_escalate",
        "hold_soft",
        "hold_hard",
        "other",
    )


def test_ot_tools_registered_low():
    reg = get_default_registry("dead_network")
    names = {t["name"] for t in reg.list_tools()}
    for name in OT_LOW_TOOLS:
        assert name in names
        assert name in LOW_RISK_TOOLS
        assert get_tier(name).value == "low"
        assert name not in HIGH_RISK_TOOLS
        out = reg.execute(name, {})
        assert out.get("tool") == name
        assert "result" in out


@pytest.mark.parametrize("scenario", OT_SCENARIOS)
def test_ot_scenario_no_auto_high(tmp_path: Path, scenario: str):
    agent = ConditionMonitoringAgent(
        scenario=scenario,
        audit_path=tmp_path / "{0}.jsonl".format(scenario),
        force_mock_llm=True,
        interactive=False,
    )
    result = agent.run()
    _assert_no_auto_high(result, scenario)
    # HIGH names never silently executed even if proposed
    for name in HIGH_NAMES:
        assert name not in [
            e.get("tool_name")
            for e in result.audit_events
            if e["event_type"] == "tool_executed" and e.get("tier") == "high"
        ]


def test_dead_network_connectivity_path_not_blind_shutdown(tmp_path: Path):
    agent = ConditionMonitoringAgent(
        scenario="dead_network",
        audit_path=tmp_path / "dead_network.jsonl",
        force_mock_llm=True,
    )
    result = agent.run()
    _assert_no_auto_high(result, "dead_network")
    assert "get_device_connectivity" in result.tool_calls_made
    assert "get_private_network_health" in result.tool_calls_made
    text = result.final_message.lower()
    assert any(
        k in text
        for k in ("network", "connectivity", "private 5g", "check-in", "check_in")
    )
    assert "recommend_line_shutdown" not in result.tool_calls_made
    report = format_ops_report(result)
    assert "em dash" not in report.lower()  # sanity
    # Scenario-specific WHY (no shared conflict recalibration boilerplate)
    assert "recalibration" not in report.lower() or "network" in report.lower()
    why = "\n".join(ops_report_dict(result)["why"]).lower()
    assert "network" in why or "connectivity" in why or "private 5g" in why
    assert "recalibrat" not in why


def test_unauthorized_plc_write_security_hold_visible(tmp_path: Path):
    agent = ConditionMonitoringAgent(
        scenario="unauthorized_plc_write",
        audit_path=tmp_path / "plc_write.jsonl",
        force_mock_llm=True,
    )
    result = agent.run()
    _assert_no_auto_high(result, "unauthorized_plc_write")
    assert "get_ot_security_events" in result.tool_calls_made
    assert result.recommendation_type == "hold_hard"
    assert result.hold_mode == "hard"
    text = result.final_message.lower()
    assert any(k in text for k in ("plc", "modbus", "security", "unauthorized", "opc"))
    # Never proposes PLC write (no such HIGH tool); no plant HIGH execute
    assert "recommend_line_shutdown" not in result.tool_calls_made
    d = ops_report_dict(result)
    assert d["status_code"] == "hold_hard"
    why = "\n".join(d["why"]).lower()
    assert "plc" in why or "modbus" in why or "security" in why
    assert "\u2014" not in "\n".join(d["why"])  # no em dashes in WHY


def test_maximo_sap_disagree_conflict_hold(tmp_path: Path):
    agent = ConditionMonitoringAgent(
        scenario="maximo_sap_disagree",
        audit_path=tmp_path / "maximo_sap.jsonl",
        force_mock_llm=True,
    )
    result = agent.run()
    _assert_no_auto_high(result, "maximo_sap_disagree")
    text = result.final_message.lower()
    assert any(k in text for k in ("maximo", "sap", "audit", "disagree", "conflict"))
    assert "create_emergency_work_order" not in result.tool_calls_made
    assert "recommend_line_shutdown" not in result.tool_calls_made
    why = "\n".join(ops_report_dict(result)["why"]).lower()
    assert "maximo" in why and "sap" in why
    assert "\u2014" not in "\n".join(ops_report_dict(result)["why"])


def test_soft_vs_hard_hold_soft_path_no_shutdown_execute(tmp_path: Path):
    agent = ConditionMonitoringAgent(
        scenario="soft_vs_hard_hold",
        audit_path=tmp_path / "soft_hard.jsonl",
        force_mock_llm=True,
        # Even if someone pre-approves, mock must not propose shutdown
        approval_decisions={"recommend_line_shutdown": True},
    )
    result = agent.run()
    _assert_no_auto_high(result, "soft_vs_hard_hold")
    assert result.recommendation_type == "hold_soft"
    assert result.hold_mode == "soft"
    assert result.consequential_proposed is False
    assert "recommend_line_shutdown" not in result.tool_calls_made
    text = result.final_message.lower()
    assert "soft" in text or "investigate while running" in text
    d = ops_report_dict(result)
    assert d["status_code"] == "hold_soft"
    assert "SOFT HOLD" in d["decision"]
    why = "\n".join(d["why"]).lower()
    assert "soft" in why or "investigate" in why
    assert "\u2014" not in "\n".join(d["why"])
    # Soft HOLD must not equal silent shutdown
    assert result.consequential_executed is False
    finals = [e for e in result.audit_events if e["event_type"] == "final_recommendation"]
    assert finals[-1].get("hold_mode") == "soft"


def test_ot_scenario_why_unique_no_shared_boilerplate(tmp_path: Path):
    whys = {}
    for scenario in OT_SCENARIOS:
        agent = ConditionMonitoringAgent(
            scenario=scenario,
            audit_path=tmp_path / "why_{0}.jsonl".format(scenario),
            force_mock_llm=True,
        )
        result = agent.run()
        whys[scenario] = tuple(ops_report_dict(result)["why"])
    # Each scenario gets its own WHY (not identical shared boilerplate)
    assert len(set(whys.values())) == len(OT_SCENARIOS)
    for scenario, bullets in whys.items():
        joined = " ".join(bullets)
        assert "\u2014" not in joined, scenario
