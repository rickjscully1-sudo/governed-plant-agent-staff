"""Batch 2 robustness: dual-control, idempotency, circuit breaker, hash chain, scenarios, chaos."""

from __future__ import annotations

import json
from pathlib import Path

from aiot_agent.agent import ConditionMonitoringAgent
from aiot_agent.approval import ApprovalGate
from aiot_agent.audit import AuditLog, verify_audit_integrity
from aiot_agent.idempotency import make_idempotency_key
from aiot_agent.llm.base import LLMResponse, ToolCallRequest
from aiot_agent.tools.registry import get_default_registry


# ---------------------------------------------------------------------------
# 4. Dual-control
# ---------------------------------------------------------------------------


def test_dual_control_one_role_insufficient(tmp_path: Path):
    audit = AuditLog(path=tmp_path / "dual1.jsonl")
    gate = ApprovalGate(audit=audit, require_dual_control=True)
    gate.set_decision("recommend_line_shutdown", True, role="operator")
    assert (
        gate.check_and_maybe_approve(
            "recommend_line_shutdown", {"reason": "only operator"}
        )
        is False
    )
    roles = [
        e.get("approval_role")
        for e in audit.events
        if e["event_type"] in ("approval_granted", "approval_denied")
        and e.get("approval_role")
    ]
    assert "operator" in roles


def test_dual_control_two_roles_allows(tmp_path: Path):
    audit = AuditLog(path=tmp_path / "dual2.jsonl")
    gate = ApprovalGate(audit=audit, require_dual_control=True)
    gate.set_decision("recommend_line_shutdown", True, role="operator")
    gate.set_decision("recommend_line_shutdown", True, role="supervisor")
    assert (
        gate.check_and_maybe_approve(
            "recommend_line_shutdown", {"reason": "both"}
        )
        is True
    )
    granted_roles = {
        e.get("approval_role")
        for e in audit.events
        if e["event_type"] == "approval_granted" and e.get("approval_role")
    }
    assert "operator" in granted_roles and "supervisor" in granted_roles


def test_dual_control_can_disable_for_simple_demo(tmp_path: Path):
    audit = AuditLog(path=tmp_path / "dual3.jsonl")
    gate = ApprovalGate(audit=audit, require_dual_control=False)
    gate.set_decision("recommend_line_shutdown", True)
    assert (
        gate.check_and_maybe_approve("recommend_line_shutdown", {"reason": "demo"})
        is True
    )


# ---------------------------------------------------------------------------
# 8. Idempotency keys
# ---------------------------------------------------------------------------


def test_idempotency_key_stable_and_sensitive():
    a = make_idempotency_key(
        "recommend_line_shutdown",
        {"reason": "x", "urgency": "high"},
        "true_positive",
    )
    b = make_idempotency_key(
        "recommend_line_shutdown",
        {"urgency": "high", "reason": "x"},
        "true_positive",
    )
    c = make_idempotency_key(
        "recommend_line_shutdown",
        {"reason": "y", "urgency": "high"},
        "true_positive",
    )
    assert a == b
    assert a != c


def test_idempotent_replay_denied(tmp_path: Path):
    """Second identical consequential execute is denied with audit event."""

    class DoubleFireLLM:
        def __init__(self, *a, **k):
            self._n = 0

        def complete(self, messages, tools, system):
            self._n += 1
            args = {
                "line_id": "LINE-03-FILLER",
                "reason": "bearing failure, controlled shutdown",
                "urgency": "end_of_shift",
            }
            if self._n <= 2:
                return LLMResponse(
                    content="Proposing shutdown (attempt {0}).".format(self._n),
                    tool_calls=[
                        ToolCallRequest(
                            id="tc{0}".format(self._n),
                            name="recommend_line_shutdown",
                            arguments=args,
                        )
                    ],
                    stop_reason="tool_use",
                )
            return LLMResponse(
                content="Standing by after dual attempts.",
                tool_calls=[],
                stop_reason="end",
            )

    agent = ConditionMonitoringAgent(
        scenario="true_positive",
        audit_path=tmp_path / "idem.jsonl",
        force_mock_llm=True,
        approval_decisions={"recommend_line_shutdown": True},
        require_dual_control=True,
    )

    original_check = agent.gate.check_and_maybe_approve

    def check_with_reseed(tool_name, args, reason="", timeout_seconds=None):
        agent.gate.set_decision(tool_name, True, role="operator")
        agent.gate.set_decision(tool_name, True, role="supervisor")
        kwargs = {"reason": reason}
        try:
            return original_check(tool_name, args, reason=reason, timeout_seconds=timeout_seconds)
        except TypeError:
            return original_check(tool_name, args, reason=reason)

    agent.gate.check_and_maybe_approve = check_with_reseed
    agent.llm = DoubleFireLLM()
    result = agent.run()

    assert any(
        e["event_type"] == "idempotent_replay_denied" for e in result.audit_events
    ), [e["event_type"] for e in result.audit_events]
    high_exec = [
        e
        for e in result.audit_events
        if e["event_type"] == "tool_executed" and e.get("tier") == "high"
    ]
    assert len(high_exec) == 1
    assert high_exec[0].get("idempotency_key")


# ---------------------------------------------------------------------------
# 9. Circuit breaker
# ---------------------------------------------------------------------------


def test_circuit_breaker_opens_after_consecutive_errors(tmp_path: Path):
    hist_tool = "iot_historian_query"

    class ErrorThenShutdownLLM:
        def __init__(self, *a, **k):
            self._n = 0

        def complete(self, messages, tools, system):
            self._n += 1
            if self._n <= 3:
                return LLMResponse(
                    content="Querying historian.",
                    tool_calls=[
                        ToolCallRequest(
                            id="e{0}".format(self._n),
                            name=hist_tool,
                            arguments={"asset_id": "FILLER-PUMP-A12"},
                        )
                    ],
                    stop_reason="tool_use",
                )
            if self._n == 4:
                return LLMResponse(
                    content="Despite errors, proposing shutdown.",
                    tool_calls=[
                        ToolCallRequest(
                            id="shut",
                            name="recommend_line_shutdown",
                            arguments={
                                "reason": "should be blocked by circuit",
                                "urgency": "immediate",
                            },
                        )
                    ],
                    stop_reason="tool_use",
                )
            return LLMResponse(
                content="HOLD / ESCALATE. Circuit open.",
                tool_calls=[],
                stop_reason="end",
            )

    agent = ConditionMonitoringAgent(
        scenario="true_positive",
        audit_path=tmp_path / "circuit.jsonl",
        force_mock_llm=True,
        approval_decisions={"recommend_line_shutdown": True},
        circuit_breaker_threshold=3,
        chaos={hist_tool: {"mode": "error"}},
    )
    agent.llm = ErrorThenShutdownLLM()
    result = agent.run()

    assert any(e["event_type"] == "circuit_open" for e in result.audit_events)
    assert result.consequential_executed is False
    high_exec = [
        e
        for e in result.audit_events
        if e["event_type"] == "tool_executed" and e.get("tier") == "high"
    ]
    assert high_exec == []


# ---------------------------------------------------------------------------
# 26. Hash-chained audit
# ---------------------------------------------------------------------------


def test_hash_chain_good_passes(tmp_path: Path):
    path = tmp_path / "chain_ok.jsonl"
    agent = ConditionMonitoringAgent(
        scenario="conflict",
        audit_path=path,
        force_mock_llm=True,
    )
    agent.run()
    ok, problems = verify_audit_integrity(path, expected_scenario="conflict")
    assert ok, problems
    first = json.loads(path.read_text().splitlines()[0])
    assert "prev_hash" in first and "event_hash" in first


def test_hash_chain_mutated_middle_fails(tmp_path: Path):
    path = tmp_path / "chain_bad.jsonl"
    agent = ConditionMonitoringAgent(
        scenario="conflict",
        audit_path=path,
        force_mock_llm=True,
    )
    agent.run()
    lines = path.read_text().splitlines()
    assert len(lines) >= 3
    mid = json.loads(lines[1])
    mid["decision"] = "TAMPERED"
    lines[1] = json.dumps(mid)
    path.write_text("\n".join(lines) + "\n")
    ok, problems = verify_audit_integrity(path)
    assert ok is False
    assert any(
        ("event_hash" in p) or ("chain" in p) or ("tamper" in p) or ("mismatch" in p)
        for p in problems
    )


# ---------------------------------------------------------------------------
# 17 / 18 / 19: new scenarios
# ---------------------------------------------------------------------------


def _assert_hold(result, label: str):
    assert result.consequential_executed is False, label
    assert result.recommendation_type == "hold_escalate", (
        label,
        result.recommendation_type,
        result.final_message[:300],
    )
    high_exec = [
        e
        for e in result.audit_events
        if e["event_type"] == "tool_executed" and e.get("tier") == "high"
    ]
    assert high_exec == [], label


def test_eval_alert_fatigue_holds(tmp_path: Path):
    agent = ConditionMonitoringAgent(
        scenario="alert_fatigue",
        audit_path=tmp_path / "fatigue.jsonl",
        force_mock_llm=True,
    )
    result = agent.run()
    _assert_hold(result, "alert_fatigue")
    text = result.final_message.lower()
    assert any(k in text for k in ("hold", "fatigue", "mild", "escalat"))


def test_eval_parts_unavailable_holds(tmp_path: Path):
    agent = ConditionMonitoringAgent(
        scenario="parts_unavailable",
        audit_path=tmp_path / "parts.jsonl",
        force_mock_llm=True,
    )
    result = agent.run()
    _assert_hold(result, "parts_unavailable")
    text = result.final_message.lower()
    assert any(
        k in text for k in ("stock", "spare", "oos", "unavailable", "hold", "escalat")
    )


def test_eval_conflicting_work_orders_holds(tmp_path: Path):
    agent = ConditionMonitoringAgent(
        scenario="conflicting_work_orders",
        audit_path=tmp_path / "conflict_wo.jsonl",
        force_mock_llm=True,
    )
    result = agent.run()
    _assert_hold(result, "conflicting_work_orders")
    text = result.final_message.lower()
    assert any(k in text for k in ("conflict", "hold", "escalat", "work order", "pm"))


# ---------------------------------------------------------------------------
# 22. Chaos injector
# ---------------------------------------------------------------------------


def test_chaos_injector_error_mode():
    reg = get_default_registry("conflict")
    reg.set_chaos({"iot_historian_query": {"mode": "error"}})
    out = reg.execute("iot_historian_query", {"asset_id": "X"})
    assert out.get("status") == "ERROR"


def test_chaos_injector_empty_mode():
    reg = get_default_registry("conflict")
    reg.set_chaos({"maximo_asset_lookup": {"mode": "empty"}})
    out = reg.execute("maximo_asset_lookup", {})
    assert out.get("status") == "EMPTY"
