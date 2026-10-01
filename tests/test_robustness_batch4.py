"""Batch 4 robustness: deterministic decision + audit replay for forensics."""

from __future__ import annotations

import json
from pathlib import Path

from aiot_agent.agent import ConditionMonitoringAgent
from aiot_agent.audit import verify_audit_integrity
from aiot_agent.replay import (
    decision_fingerprint,
    dump_replay_package,
    extract_decision_trace,
    reconstruct_decision,
    verify_replay_package,
)


def test_decision_fingerprint_stable_across_two_mock_runs(tmp_path: Path):
    """Same scenario + mock LLM => identical forensic fingerprint despite wall time."""
    fps = []
    pkgs = []
    for i in range(2):
        agent = ConditionMonitoringAgent(
            scenario="conflict",
            audit_path=tmp_path / "run_{0}.jsonl".format(i),
            force_mock_llm=True,
        )
        result = agent.run()
        assert result.decision_fingerprint
        assert result.replay_package is not None
        fps.append(result.decision_fingerprint)
        pkgs.append(result.replay_package)
        assert result.recommendation_type == "hold_escalate"
        assert result.consequential_executed is False

    assert fps[0] == fps[1]
    assert pkgs[0]["recommendation_type"] == pkgs[1]["recommendation_type"]
    assert pkgs[0]["control_event_count"] == pkgs[1]["control_event_count"]
    # Tip hashes differ (timestamps change chain). That is expected.
    assert pkgs[0]["audit_tip_hash"] != pkgs[1]["audit_tip_hash"]


def test_reconstruct_decision_matches_agent_result(tmp_path: Path):
    agent = ConditionMonitoringAgent(
        scenario="conflict",
        audit_path=tmp_path / "recon.jsonl",
        force_mock_llm=True,
    )
    result = agent.run()
    reconstructed = reconstruct_decision(result.audit_events)
    assert reconstructed["scenario"] == result.scenario
    assert reconstructed["recommendation_type"] == result.recommendation_type
    assert reconstructed["consequential_executed"] is False
    assert reconstructed["consequential_executed"] == result.consequential_executed
    # Tool trail reconstructed from executed events.
    assert reconstructed["tool_calls_made"] == result.tool_calls_made


def test_verify_replay_package_detects_tampered_decision(tmp_path: Path):
    agent = ConditionMonitoringAgent(
        scenario="conflict",
        audit_path=tmp_path / "tamper.jsonl",
        force_mock_llm=True,
    )
    result = agent.run()
    package = result.replay_package
    assert package is not None

    ok, problems = verify_replay_package(package, result.audit_events)
    assert ok is True
    assert problems == []

    # Tamper final recommendation decision in a copy of the trail.
    tampered = [dict(e) for e in result.audit_events]
    for event in tampered:
        if event.get("event_type") == "final_recommendation":
            event["recommendation_type"] = "shutdown_pending_approval"
            event["decision"] = "shutdown_pending_approval"
            event["consequential_executed"] = True

    ok2, problems2 = verify_replay_package(package, tampered)
    assert ok2 is False
    assert any("decision_fingerprint mismatch" in p for p in problems2)


def test_final_recommendation_embeds_matching_fingerprint(tmp_path: Path):
    agent = ConditionMonitoringAgent(
        scenario="stale_data",
        audit_path=tmp_path / "embed.jsonl",
        force_mock_llm=True,
        approval_decisions={"recommend_line_shutdown": True},
    )
    result = agent.run()
    finals = [
        e for e in result.audit_events if e["event_type"] == "final_recommendation"
    ]
    assert len(finals) == 1
    assert finals[0].get("decision_fingerprint") == result.decision_fingerprint
    # Recompute from disk JSONL matches in-memory package.
    disk_fp = decision_fingerprint(tmp_path / "embed.jsonl")
    assert disk_fp == result.decision_fingerprint
    ok, _ = verify_audit_integrity(tmp_path / "embed.jsonl", expected_scenario="stale_data")
    assert ok is True


def test_dump_and_reload_replay_package(tmp_path: Path):
    agent = ConditionMonitoringAgent(
        scenario="conflict",
        audit_path=tmp_path / "dump_audit.jsonl",
        force_mock_llm=True,
    )
    result = agent.run()
    out = dump_replay_package(result.replay_package, tmp_path / "replay.json")
    loaded = json.loads(out.read_text(encoding="utf-8"))
    ok, problems = verify_replay_package(loaded, tmp_path / "dump_audit.jsonl")
    assert ok is True, problems
    assert loaded["decision_fingerprint"] == result.decision_fingerprint
    # Trace is non-empty control plane.
    assert len(extract_decision_trace(tmp_path / "dump_audit.jsonl")) >= 3
