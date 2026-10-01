"""Batch 3 robustness: evidence freshness SLA (fail-closed on stale IoT data)."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from aiot_agent.agent import ConditionMonitoringAgent
from aiot_agent.freshness import (
    DEFAULT_MAX_AGE_SECONDS,
    evaluate_freshness,
    newest_timestamp,
    parse_timestamp,
)
from aiot_agent.llm.base import LLMResponse, ToolCallRequest


def test_parse_and_newest_timestamp():
    payload = {
        "tool": "iot_historian_query",
        "result": {
            "samples": [
                {"ts": "2026-09-15T08:00:00Z", "vibration_mm_s": 2.0},
                {"ts": "2026-09-15T16:00:00Z", "vibration_mm_s": 7.0},
            ]
        },
    }
    newest = newest_timestamp(payload)
    assert newest == parse_timestamp("2026-09-15T16:00:00Z")


def test_evaluate_fresh_within_sla():
    payload = {"samples": [{"ts": "2026-09-15T16:00:00Z"}]}
    now = parse_timestamp("2026-09-15T16:30:00Z")
    v = evaluate_freshness(payload, now=now, max_age_seconds=DEFAULT_MAX_AGE_SECONDS)
    assert v.ok is True
    assert v.status == "fresh"


def test_evaluate_stale_beyond_sla():
    payload = {"samples": [{"ts": "2026-09-15T16:00:00Z"}]}
    now = parse_timestamp("2026-09-18T12:00:00Z")
    v = evaluate_freshness(payload, now=now, max_age_seconds=DEFAULT_MAX_AGE_SECONDS)
    assert v.ok is False
    assert v.status == "stale"
    assert v.age_seconds is not None and v.age_seconds > DEFAULT_MAX_AGE_SECONDS


def test_evaluate_missing_timestamp_fail_closed():
    v = evaluate_freshness(
        {"result": {"note": "no times"}},
        now=datetime(2026, 9, 15, 16, 0, tzinfo=timezone.utc),
    )
    assert v.ok is False
    assert v.status == "missing_timestamp"


class _ScriptedLLM:
    """Minimal LLM: read historian once, then propose shutdown, then stop."""

    def __init__(self) -> None:
        self._step = 0

    def complete(self, messages, tools, system=None):
        self._step += 1
        if self._step == 1:
            return LLMResponse(
                content="Gathering historian evidence.",
                tool_calls=[
                    ToolCallRequest(
                        id="c1",
                        name="iot_historian_query",
                        arguments={"asset_id": "FILLER-PUMP-A12"},
                    )
                ],
            )
        if self._step == 2:
            return LLMResponse(
                content="Sensors alarming; requesting shutdown.",
                tool_calls=[
                    ToolCallRequest(
                        id="c2",
                        name="recommend_line_shutdown",
                        arguments={
                            "line_id": "LINE-03-FILLER",
                            "reason": "elevated vibration",
                            "urgency": "immediate",
                        },
                    )
                ],
            )
        return LLMResponse(content="Done.", tool_calls=[])


def test_freshness_sla_blocks_consequential_even_with_dual_approval(tmp_path: Path):
    """Stale historian evidence => no HIGH execute despite operator+supervisor."""
    agent = ConditionMonitoringAgent(
        scenario="true_positive",
        audit_path=tmp_path / "fresh_block.jsonl",
        force_mock_llm=True,
        approval_decisions={"recommend_line_shutdown": True},
        now="2026-09-21T09:00:00Z",  # days after fixture samples
        max_evidence_age_seconds=6 * 3600,
        enforce_freshness_sla=True,
    )
    agent.llm = _ScriptedLLM()
    result = agent.run()

    assert result.consequential_executed is False
    assert "recommend_line_shutdown" in result.denied_tools
    assert result.recommendation_type == "hold_escalate"
    violations = [
        e for e in result.audit_events if e["event_type"] == "freshness_sla_violation"
    ]
    assert violations, "expected freshness_sla_violation audit event"
    freshness = [
        e for e in result.audit_events if e["event_type"] == "evidence_freshness"
    ]
    assert freshness and freshness[0].get("freshness_ok") is False


def test_freshness_sla_allows_when_evidence_fresh(tmp_path: Path):
    """Within SLA + dual approval => consequential may execute."""
    agent = ConditionMonitoringAgent(
        scenario="true_positive",
        audit_path=tmp_path / "fresh_ok.jsonl",
        force_mock_llm=True,
        approval_decisions={"recommend_line_shutdown": True},
        now="2026-09-15T14:30:00Z",  # 30m after true_positive last sample
        max_evidence_age_seconds=6 * 3600,
        enforce_freshness_sla=True,
    )
    agent.llm = _ScriptedLLM()
    result = agent.run()

    assert result.consequential_executed is True
    assert "recommend_line_shutdown" not in result.denied_tools
    freshness = [
        e for e in result.audit_events if e["event_type"] == "evidence_freshness"
    ]
    assert freshness and freshness[0].get("freshness_ok") is True


def test_stale_data_scenario_reference_now_blocks_shutdown(tmp_path: Path):
    """stale_data fixture reference_now is past SLA; architecture holds even if approved."""
    agent = ConditionMonitoringAgent(
        scenario="stale_data",
        audit_path=tmp_path / "stale_sc.jsonl",
        force_mock_llm=True,
        approval_decisions={"recommend_line_shutdown": True},
        enforce_freshness_sla=True,
    )
    agent.llm = _ScriptedLLM()
    result = agent.run()

    assert result.consequential_executed is False
    assert any(
        e["event_type"] == "freshness_sla_violation" for e in result.audit_events
    )
