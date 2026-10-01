"""Unit tests for tiered permissions and approval gate."""

from pathlib import Path

from aiot_agent.approval import ApprovalGate
from aiot_agent.audit import AuditLog
from aiot_agent.permissions import HIGH_RISK_TOOLS, LOW_RISK_TOOLS, get_tier, is_consequential


def test_low_risk_tools_classified():
    for name in LOW_RISK_TOOLS:
        assert get_tier(name).value == "low"
        assert not is_consequential(name)


def test_high_risk_tools_classified():
    for name in HIGH_RISK_TOOLS:
        assert get_tier(name).value == "high"
        assert is_consequential(name)


def test_unknown_tool_fail_closed():
    assert is_consequential("totally_unknown_destructive_thing")


def test_approval_gate_auto_allows_low(tmp_path: Path):
    audit = AuditLog(path=tmp_path / "a.jsonl")
    gate = ApprovalGate(audit=audit, auto_deny=True)
    assert gate.check_and_maybe_approve("iot_historian_query", {"asset_id": "X"}) is True
    assert not any(e["event_type"] == "approval_pending" for e in audit.events)


def test_approval_gate_never_silently_approves_high(tmp_path: Path):
    audit = AuditLog(path=tmp_path / "a.jsonl")
    gate = ApprovalGate(audit=audit, auto_deny=True)
    assert (
        gate.check_and_maybe_approve(
            "recommend_line_shutdown", {"reason": "test"}
        )
        is False
    )
    assert any(e["event_type"] == "approval_pending" for e in audit.events)
    assert any(e["event_type"] == "approval_denied" for e in audit.events)


def test_programmatic_approve(tmp_path: Path):
    audit = AuditLog(path=tmp_path / "a.jsonl")
    # Dual-control ON by default for shutdown, so both roles are required.
    gate = ApprovalGate(audit=audit, require_dual_control=True)
    gate.set_decision("recommend_line_shutdown", True, role="operator")
    gate.set_decision("recommend_line_shutdown", True, role="supervisor")
    assert gate.check_and_maybe_approve("recommend_line_shutdown", {"reason": "ok"}) is True
    assert any(e["event_type"] == "approval_granted" for e in audit.events)


def test_programmatic_approve_single_mode_demo(tmp_path: Path):
    audit = AuditLog(path=tmp_path / "a.jsonl")
    gate = ApprovalGate(audit=audit, require_dual_control=False)
    gate.set_decision("recommend_line_shutdown", True)
    assert gate.check_and_maybe_approve("recommend_line_shutdown", {"reason": "ok"}) is True
