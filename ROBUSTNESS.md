# Robustness hardening (fail-closed HITL)

This document maps implemented controls to the robustness backlog.
Consequential tools **never** auto-execute without explicit human approval.
Ambiguous / adversarial conditions force **hold_escalate**.

Sacred constraint: **fail-closed HITL**. This public build uses only the deterministic, rule-based **mock LLM** (no model calls, no API key).

## Batch 1 (complete)

| Backlog | Control | Behavior |
|--------:|---------|----------|
| 1 | Policy proof (one-shot approval) | Pre-seeded approvals are **consumed**. Without approval → denied. With approval → allowed **once**. Second attempt without a new approval → denied. |
| 2 | Agent loop limits | Caps: `max_turns`, `max_tool_calls`, `max_wall_time_seconds`. On exceed → `hold_escalate`, no HIGH-risk execute, audited as `agent_stop_limit`. |
| 3 | Approval timeout | Pending consequential approvals expire → denied (`approval_expired` + `approval_denied`), fail-closed. |
| 5 | Adversarial scenarios | `tool_error_storm`, `stale_data`, `social_engineering` → hold, no shutdown. |
| 6 | Schema validation | Malformed args → `tool_schema_rejected`, never executed. |
| 7 | Audit integrity (fields) | `verify_audit_integrity()` checks timestamp, tool, decision, scenario. |

## Batch 2 (this release)

| Backlog | Control | Behavior |
|--------:|---------|----------|
| **4** | **Dual-control** | `recommend_line_shutdown` requires **two distinct roles** (operator + supervisor) when `require_dual_control=True` (default). One approval alone is insufficient; both are audited (`approval_role`). Set `require_dual_control=False` for simple single-approval demos. Optionally extend via `dual_control_tools` (e.g. include `create_emergency_work_order`). |
| **8** | **Idempotency keys** | Consequential executions record `idempotency_key` = sha256(tool + normalized args + scenario). A second identical execute is denied/no-op and audited as `idempotent_replay_denied`. |
| **9** | **Circuit breaker** | After N consecutive tool errors (default **3**) in a run, freeze HIGH-risk path: hold_escalate only, no consequential execute. Audited as `circuit_open`. Resets on a new agent run. |
| **26** | **Hash-chained audit** | Each JSONL event includes `prev_hash` and `event_hash` (sha256). `verify_audit_integrity` detects broken chains (good chain passes; mutated middle event fails). |
| **17** | **Alert fatigue scenario** | Mild repeating alarms, no escalation → `hold_escalate`, no shutdown. |
| **18** | **Parts unavailable** | True-positive-ish but critical spare OOS → escalate/hold, `consequential_executed=False`. |
| **19** | **Conflicting work orders** | Open PM says do not stop line vs rising vibration → `hold_escalate`. |
| **22** | **Chaos injector** | `registry.set_chaos({tool: {mode: error\|empty\|latency}})` (also via agent `chaos=`). Enough for tests; see below. |

## Batch 3 (this release)

| Backlog | Control | Behavior |
|--------:|---------|----------|
| **Freshness SLA** | **Evidence age gate** | After critical evidence tools (default: `iot_historian_query`), architecture evaluates newest sample timestamp against `max_evidence_age_seconds` (default **6h**) at scenario `reference_now` (or injectable `now`). Stale / missing / clock-skew evidence is audited as `evidence_freshness`. **HIGH-risk execute is denied** (`freshness_sla_violation`) even with dual-control approval (fail-closed). Disable with `enforce_freshness_sla=False` only for experiments. |

Scenario fixtures carry `reference_now` so demos stay deterministic under wall-clock drift. The `stale_data` scenario's `reference_now` is intentionally past the SLA so the gate fires architecturally (not only via mock-LLM narrative).


## Batch 4 (this release)

| Backlog | Control | Behavior |
|--------:|---------|----------|
| **Deterministic replay** | **Decision + audit forensics** | After each run, architecture builds a `DecisionReplayPackage` with a stable `decision_fingerprint` (sha256 over canonical control-plane events: tools, gates, approvals, freshness, final recommendation). Wall-clock timestamps and hash-chain fields are excluded so the same mock scenario fingerprints identically across runs. Package is on `AgentResult.replay_package` / `decision_fingerprint` and embedded on `final_recommendation`. `reconstruct_decision(audit)` rebuilds the summary without re-running the LLM; `verify_replay_package` fails closed if recommendation/control fields were tampered. Tip hash still differs across runs (hash chain covers timestamps). |

## How to run tests


```bash
cd aiot-agent
python3 -m venv .venv
source .venv/bin/activate
pip install -e '.[test]'
pytest -q
```

Robustness only:

```bash
pytest -q tests/test_robustness.py tests/test_robustness_batch2.py tests/test_robustness_batch3.py tests/test_robustness_batch4.py
```

## Demo new Batch 2 scenarios (mock LLM)

```bash
python -m aiot_agent --scenario alert_fatigue
python -m aiot_agent --scenario parts_unavailable
python -m aiot_agent --scenario conflicting_work_orders
```

Batch 1 adversarial demos still work:

```bash
python -m aiot_agent --scenario tool_error_storm
python -m aiot_agent --scenario stale_data
python -m aiot_agent --scenario social_engineering
```

## Dual-control demo notes

- Default: shutdown needs operator **and** supervisor approval.
- Simple demo mode: construct agent/gate with `require_dual_control=False`.
- Programmatic evals: `gate.set_decision("recommend_line_shutdown", True, role="operator")` and the same for `role="supervisor"`.

## Chaos injector (backlog 22)

```python
from aiot_agent.tools.registry import get_default_registry

reg = get_default_registry("conflict")
reg.set_chaos({
    "iot_historian_query": {"mode": "error"},
    "maximo_asset_lookup": {"mode": "empty"},
    "sap_pm_work_orders": {"mode": "latency", "latency_ms": 50},
})
```

Or pass `chaos={...}` into `ConditionMonitoringAgent(...)`.

## Key modules

- `aiot_agent/approval.py`: one-shot + timeout + **dual-control**
- `aiot_agent/agent.py`: limits, schema gate, **idempotency**, **circuit breaker**
- `aiot_agent/idempotency.py`: key helper
- `aiot_agent/audit.py`: field checks + **hash chain** (`prev_hash` / `event_hash`)
- `aiot_agent/tools/registry.py`: **chaos injector**
- `aiot_agent/tools/data.py`: scenario fixtures (incl. batch 2)
- `aiot_agent/freshness.py`: **evidence freshness SLA**
- `aiot_agent/replay.py`: **deterministic decision + audit replay** (fingerprint / reconstruct / verify)
- `tests/test_robustness.py`: batch 1
- `tests/test_robustness_batch2.py`: batch 2
- `tests/test_robustness_batch3.py`: batch 3 (freshness SLA)

## Public demo hardening (hosted web UI)

| Control | Behavior |
|---------|----------|
| Per-visitor sessions | Each visitor gets a random, server-issued session id (HttpOnly cookie plus an `X-Demo-Session` header kept in sessionStorage for iframes). Single-agent reports, Agent Staff sessions, approvals and audit trails are never shared across visitors. A decision can only touch the caller's own session. |
| Bounded memory | Sessions live in an LRU capped at 200 (`AIOT_MAX_SESSIONS`) with 30 minutes idle expiry (`AIOT_SESSION_TTL_SECONDS`). Evicted sessions delete their temp audit folder. Read-only endpoints never create sessions. |
| No server paths | Audit logs go to a per-session temp folder; UI and API show only the file name. Error responses never echo exception text. |
| Request limits | JSON bodies above 16 KB are rejected (413). No CORS headers. |
| No model calls | `get_llm()` always returns the mock backend. No code path reads an API key. |

## Still open (candidates for later batches)

- Break-glass (audited emergency path; still no silent auto-execute of routine HIGH actions)
- Capability tokens (approval binding to tool+args+expiry)
- Evidence scoring (multi-source confidence before HIGH path)
- Multi-asset / clock-skew scenario matrix beyond the single freshness gate

## OT / connectivity scenario pack

See **SCENARIO_PACK.md**. Adds LOW tools for carrier IoT connectivity, private 5G
core health, OT gateway, PLC tag snapshot (read-only), and OT security events.
Scenarios: `dead_network`, `unauthorized_plc_write`, `maximo_sap_disagree`,
`soft_vs_hard_hold`. Soft HOLD (`hold_soft`) means investigate while running;
hard HOLD (`hold_hard`) blocks consequential plant actions. HIGH tools still
never auto-execute; dual-control on shutdown unchanged.

```bash
pytest -q tests/test_scenario_pack_ot.py
python -m aiot_agent --scenario dead_network
```
