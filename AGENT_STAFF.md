# Plant Agent Staff

> **Simulation / mock plant data. Illustrative, not a live plant deployment.**
> Beverage bottling plant (simulated), Line 3. All people, part numbers and readings are invented.

## The idea in one line

Instead of one agent that knows everything, the plant gets a small **staff**: a Coordinator and five specialists. Each has **one job**, **its own short list of tools** (its job description), and a **named human** who signs off. The agents recommend and draft. People decide.

## The org

| Agent | Its one job | What it may touch | Human who signs off |
|-------|-------------|-------------------|---------------------|
| **Coordinator** (chief of staff) | Routes plant events to the right agent, spots conflicts and stale data, stops and escalates, writes a brief for each human | Plant event feed, staff status (read only) | Shift Lead (escalation owner). **No write authority of its own.** |
| **Line Watch** | Watches historian and PLC tags for drift, stale sensors, early fault signs | Historian, PLC tag snapshot, OT security events (read only) | Briefs the Shift Lead. **Never writes to a PLC.** |
| **Maintenance Planner** | Reads Maximo-style asset and work history plus OEM manual snippets, drafts work orders | Maximo asset, history, storeroom; OEM manual search; plant procedures; **draft work order** | Maintenance Supervisor approves before anything opens |
| **Parts & Supply** | Reads SAP-style inventory and lead times, flags part gaps, drafts purchase requests | SAP inventory and lead times; **draft purchase request** | Buyer approves. Above $5,000 the Plant Manager also signs |
| **Quality** | Compares fill level, CO2 and lab results to spec, recommends hold or release | Quality lab results; **request production hold** | QA Lead **and** Shift Lead (two signatures) |
| **Shift Handoff** | Builds the end-of-shift brief: what happened, what is open, what is risky | Shift log, staff status (read only) | Read by the incoming Shift Lead |

Named humans in the demo (fictional): Shift Lead Dana Ortiz, Maintenance Supervisor Sam Patel, Buyer Lee Chen, QA Lead Maria Gomez, Plant Manager Chris Novak, Agent Control Architect Jordan Reyes, Operations Director Morgan Ellis.

## Control Policy (who owns the rules, who signed them)

A named Agent Control Architect owns the permissions, scenarios, and HOLD rules, and plant leadership signs off on them. In code that is one versioned file:

- **Policy:** `PLANT-AGENT-POLICY v1.0` in `aiot_agent/staff/control_policy.json`, loaded by `aiot_agent/staff/control_policy.py`. Effective 2026-09-28.
- **Owner:** Jordan Reyes (Agent Control Architect). Owns the tool lanes, approval tiers, dual-control triggers, fail-closed and HOLD rules, and the approved scenario list.
- **Signed by plant leadership:** Chris Novak (Plant Manager), signed 2026-09-25, and Morgan Ellis (Operations Director), signed 2026-09-26.
- **Single source of truth:** the per-agent tool allowlists, the tier of every tool, the named approvers, the $5,000 dual-control spend threshold, the PLC-block rule, the HOLD rules (3 consecutive out-of-spec samples, sensor and quality data age limits) and the scenario list are read from the policy. The broker, the approval board and the agents do not keep their own copies.
- **Fingerprint:** SHA-256 of the canonical policy JSON with the `signoff` block removed (shown as the first 12 hex characters). Leadership signs that fingerprint.
- **Stamped everywhere:** every staff audit entry (including the core approval gate re-check) records `policy_id`, `policy_version`, `policy_fingerprint` and `policy_signed`. Every approval request records the policy version and fingerprint it was raised under, and the UI shows "Policy v1.0" on each approval card and audit row.
- **Fail closed on the policy itself:** the fingerprint is recomputed on every gated call. If the sign-off is missing or incomplete, or the fingerprint no longer matches the signed fingerprint (the policy was edited after sign-off), the staff may only read. Drafts and production actions are denied with "Policy not signed: drafts and production actions blocked", the Coordinator escalates to the Agent Control Architect and the Plant Manager, and the Control Policy panel turns red ("Not signed, blocked"). An approval raised under one fingerprint cannot execute under another. A scenario that is not on the signed list cannot draft. An unreadable policy file means no lanes at all.
- **PLC writes stay blocked** even if someone edits the policy to allow them: the broker keeps a hard block as defense in depth.
- **Change log:** v1.0 (2026-09-28), initial signed version. Any change means a new version, a new fingerprint and a new leadership sign-off.

The Agent Staff tab shows this as a **Control Policy** panel at the top: name and version, owner, sign-offs with dates, fingerprint, status, a compact rule summary, and each agent's tool lane.

## The rules (enforced in code, covered by tests)

1. **Stay in your lane.** An agent can only use the tools on its own list. Anything else is denied and written to the audit log.
2. **Four levels of authority.**
   - **Read**: runs automatically.
   - **Draft** (work order, purchase request): one named human must approve before anything opens.
   - **Production-affecting** (line hold, setpoint change, spend above $5,000): **two different humans** must approve. One signature shows "1 of 2" and nothing happens.
   - **PLC write**: always blocked, for every agent, with or without approval.
3. **Fail closed.** If data is stale, or two systems disagree (for example Maximo says a spare is on the shelf and SAP says zero), the Coordinator stops the chain, holds anything already drafted, and escalates to the right people. No recommendation is executed.
4. **Everything is logged.** Every recommendation, approval, denial, and blocked attempt goes into the same tamper-evident (hash-chained) audit log the single-agent demo uses.

## The demo scenarios

| Scenario | What happens | What it proves |
|----------|--------------|----------------|
| `filler_drift_to_work_order` | Line Watch sees filler bearing vibration drifting up. Coordinator routes to the Planner, who drafts a work order. Parts finds the bearing in stock and drafts a small replenishment request. Both wait on their named approver. The handoff brief carries it. | Agents hand work to each other; nothing opens without the named human |
| `part_gap_escalation` | Same drift, but Maximo shows 1 spare while SAP shows 0 on hand and a 9-day lead time. | Conflict means stop: work order held, no purchase request, escalated to Buyer and Maintenance Supervisor |
| `co2_out_of_spec_hold` | CO2 below spec on 3 consecutive samples. Quality recommends holding the lot. An injected test has Line Watch try to write the carbonator setpoint to the PLC. | One approval does not execute a hold; QA Lead plus Shift Lead does. The PLC write is blocked and logged |
| `out_of_lane_guardrails` | A quiet shift plus injected tests: Shift Handoff tries to draft a work order, Line Watch tries a PLC write, the Coordinator tries to hold the line, Parts tries the emergency work order tool, Quality tries a setpoint change. | Every out-of-lane attempt is denied and logged |
| `stale_sensor_fail_closed` | Dashboard shows a scary 7.4 mm/s, but the newest sample is 8 hours old and the PLC tags are flagged stale. | Stale data is not routed into a work order; the Coordinator escalates for a manual check |
| `policy_tampered_fail_closed` | Same filler drift, but someone edited the control policy after sign-off (spend threshold raised from $5,000 to $50,000). The fingerprint no longer matches. | Reads still run, but the work order and purchase request drafts are refused and logged; escalated to the Agent Control Architect and Plant Manager |

"Injected test" means the scenario deliberately makes an agent misbehave so you can watch the guardrail catch it.

## How to run it

```bash
cd aiot-agent
source .venv/bin/activate

# Control center
python -m aiot_agent.web
# open http://127.0.0.1:8765/#staff   (or click the "Agent Staff" tab)
# Container / hosted demo: python -m aiot_agent.web --host 0.0.0.0 --port 7860
```

In the Agent Staff tab:

1. Check the **Control Policy** panel: it should read "Signed and active".
2. Pick a staff scenario chip.
3. Read the outcome banner, then each agent's contribution.
4. Under **Pending approvals**, click **Approve as ...** or **Deny** for one named role at a time. Dual-control requests show **1 of 2** after the first click and do not execute.
5. Check **Blocked attempts**, **Conflicts and escalations**, the **role briefs**, and the **shift handoff**.

Command line (same logic, printed as a plain-English report):

```bash
python -m aiot_agent.staff --scenario filler_drift_to_work_order
python -m aiot_agent.staff --scenario part_gap_escalation
python -m aiot_agent.staff --scenario co2_out_of_spec_hold --approve-as qa_lead              # 1 of 2, not executed
python -m aiot_agent.staff --scenario co2_out_of_spec_hold --approve-as qa_lead shift_lead   # executes (mock)
python -m aiot_agent.staff --scenario out_of_lane_guardrails
python -m aiot_agent.staff --scenario stale_sensor_fail_closed
python -m aiot_agent.staff --scenario policy_tampered_fail_closed
```

Tests: `pytest -q` (expect 134 passed). The staff tests live in `tests/test_agent_staff.py`; the Control Policy tests live in `tests/test_control_policy.py`; per-visitor session isolation tests live in `tests/test_session_isolation.py`.

Policy endpoint: `GET /api/staff/policy`.

Audit logs: the command line writes `logs/audit_staff_<scenario>.jsonl`. The web UI keeps a separate session per visitor and writes that visitor's audit log to a private temp folder that is deleted when the session expires; the UI shows only the file name.

## What it proves (the commercial point)

- **Accountability maps to the org chart.** Each agent has a job description and a boss, just like the people it supports. You can explain it to a plant manager in one slide.
- **Least privilege by default.** The agent that watches the line cannot change it. The agent that plans maintenance cannot buy parts. The Coordinator cannot do anything except route and brief.
- **Two-person rule where it counts.** Holding product or changing the process takes two people, every time.
- **Disagreement is a stop sign, not a guess.** When systems of record disagree, the staff escalates instead of picking a side.
- **Audit-ready.** Every step is in a hash-chained log that can be verified after the fact.
- **Owned and signed rules.** One named owner for the rules, leadership signatures on the exact version in force, and a fingerprint on every log line. An edit nobody signed stops the staff from acting.

## How it is built (short)

The staff layer lives in `aiot_agent/staff/` and reuses the existing project pieces: the mock tool registry, the hash-chained audit log, the core approval gate (which re-checks every final approval), the evidence freshness check, idempotency keys, and the control center server. Agents are rule-based over mock data, so the demo runs offline with no API key and makes no AI model calls. Every decision is fully reproducible.
