# Governed Plant Agent Staff Demo

> **The model provides intelligence. The architecture provides control.**

**Live demo:** Live demo link coming soon

An interactive demo of a **fail-closed, human-approved agent staff** for a **simulated beverage bottling plant**. Agents watch the line, plan maintenance, check parts, judge quality and write the shift handoff. They recommend and draft. Named humans decide. When data is stale, systems disagree, or the rules themselves were changed without sign-off, the staff stops and escalates instead of guessing.

> **Disclaimer.** This is a simulation. All plant data is mock data, all people and approvers are fictional, and nothing here is connected to or deployed at a real plant. The agents are rule-based: the demo makes **no AI model calls** and needs **no API key**. "Maximo" and "SAP" in the scenario text mean Maximo-style EAM and SAP-style ERP mocks; no real enterprise system is involved and no vendor endorses this demo.

## What the demo shows

- **Bounded authority.** Each agent has one job, a short allowlist of tools, and a named human approver. An agent that watches the line cannot change it.
- **Human approval, never auto-approval.** Reads run automatically. Drafts need one named approver. Production-affecting actions (line hold, setpoint change, spend above $5,000) need two different humans.
- **Fail closed.** Stale sensor data, conflicting systems of record, or an unsigned control policy stop the chain and escalate to people.
- **PLC writes are always blocked**, for every agent, even if someone edits the policy.
- **Signed control policy.** One versioned policy file, owned by a (fictional) Agent Control Architect and signed by (fictional) plant leadership. Its fingerprint is stamped on every audit entry and approval.
- **Hash-chained audit trail.** Every recommendation, approval, denial and blocked attempt is logged and verifiable.

## How to use it

The page has two tabs.

### Agent Staff (multi-agent)

1. Check the **Control Policy** panel at the top. It should read **Signed and active** and lists the owner, sign-offs, fingerprint, rules and each agent's tool lane.
2. Pick a staff scenario chip (or choose one and click **Run staff scenario**).
3. Read the outcome banner, then each agent's contribution.
4. Under **Pending approvals**, click **Approve as ...** or **Deny** for one named role at a time. Two-person requests show **1 of 2** after the first signature and do not execute until the second.
5. Review **Blocked attempts**, **Conflicts and escalations**, the **role briefs**, the **shift handoff**, and the **audit trail**.

| Staff scenario | What happens | Expected outcome |
|----------------|--------------|------------------|
| Filler vibration drift to drafted work order | Line Watch sees bearing drift; the Planner drafts a work order; Parts finds the bearing in stock and drafts a small replenishment request | Nothing opens until the Maintenance Supervisor approves the work order and the Buyer approves the purchase request |
| Part gap: Maximo and SAP disagree, fail closed | The Maximo-style storeroom says a spare is on the shelf; SAP-style inventory says zero on hand with a 9-day lead time | No purchase request; the Coordinator escalates for a physical bin check |
| CO2 below spec: hold needs QA and Shift Lead | Three consecutive samples below spec; an injected test tries a PLC setpoint write | One signature does not hold the line; QA Lead plus Shift Lead does. The PLC write is blocked |
| Guardrail check: agents reach outside their lane | Injected tests make each agent try a tool it is not allowed to use | Every out-of-lane attempt is denied and logged |
| Stale sensor data: stop and escalate | A scary vibration reading that is 8 hours old | No work order; escalated for a manual reading |
| Policy edited after sign-off: reads only | The spend threshold was edited after leadership signed | Fingerprint mismatch: reads still run, drafts are refused, escalated |

"Injected test" means the scenario deliberately makes an agent misbehave so you can watch the guardrail catch it.

### Line assessment (single agent)

1. Pick a scenario chip.
2. Click **Run assessment** and read the decision banner (HOLD, SOFT HOLD, HARD HOLD, or SHUTDOWN PENDING APPROVAL), what the agent saw, why it decided, and what ops should do next.
3. For the true-positive bearing failure, use **Approve shutdown** (with a confirmation dialog, representing operator plus supervisor dual control) or **Deny (no action)**. Nothing is auto-approved.

Scenarios include a possible false alarm, a true-positive bearing failure, a tool outage, stale sensor data, a social-engineering "shut down now" note, alert fatigue, a missing critical spare, conflicting work orders, and an OT pack (dead network vs dead sensor, an unauthorized PLC write, systems of record disagreeing, and soft vs hard HOLD).

## Control rules

| Rule | Enforcement |
|------|-------------|
| Read tools | Run automatically, logged |
| Drafts (work order, purchase request) | One named human approver |
| Production-affecting actions (line hold, setpoint change, spend above $5,000) | Two different named humans |
| Line shutdown (single-agent view) | Explicit human decision with dual control; never automatic |
| PLC writes | Always blocked, for every agent |
| Stale or conflicting data | Stop the chain and escalate (fail closed) |
| Unsigned or edited control policy | Reads only; drafts and production actions blocked |
| Out-of-lane tool use | Denied and logged |
| Loop limits, schema checks, circuit breaker, idempotency | Overruns and errors hold; consequential actions never fire twice |

## Your session is private

Each visitor gets an isolated session: your scenario runs, approvals and audit trail are not visible to anyone else, and other visitors cannot approve your requests. Sessions are kept in memory with a cap and expire after 30 minutes of inactivity. Audit logs are written to a temporary per-session folder that is deleted when the session expires. Please do not type personal information into the demo; there is nowhere to do so anyway.

## Run locally

```bash
pip install -e .
python -m aiot_agent.web
```

Then open http://127.0.0.1:8765/#staff in your browser.

To run the tests (no API key needed):

```bash
pip install -e '.[test]'
pytest -q                                   # 134 tests
```

With Docker (the same image Render builds from `render.yaml`):

```bash
docker build -t plant-agent-demo .
docker run --rm -p 7860:7860 plant-agent-demo   # http://localhost:7860/#staff
```

The container listens on `0.0.0.0` and on `$PORT` when it is set (Render sets it), otherwise on 7860. `GET /healthz` is a cheap health check.

Command line versions of the same logic:

```bash
python -m aiot_agent --scenario conflict
python -m aiot_agent.staff --scenario co2_out_of_spec_hold --approve-as qa_lead shift_lead
```

More detail: [AGENT_STAFF.md](AGENT_STAFF.md) (the multi-agent staff and control policy), [ROBUSTNESS.md](ROBUSTNESS.md) (hardening controls), [SCENARIO_PACK.md](SCENARIO_PACK.md) (OT / connectivity scenarios).

## Project layout

```
aiot_agent/
  agent.py, approval.py, audit.py   single-agent loop, approval gate, hash-chained audit
  staff/                            Coordinator + five specialist agents, signed control policy
  tools/                            mock plant tools and scenario fixtures
  llm/                              deterministic rule-based mock (no model calls)
  web/                              stdlib HTTP server + static UI, per-visitor sessions
tests/                              134 tests
Dockerfile                          container image (listens on $PORT, default 7860, non-root)
render.yaml                         Render Blueprint (free Docker web service)
```

Built by Richard J. Scully, Agent Control Architect | [LinkedIn](https://www.linkedin.com/in/richard-j-scully-531b8a6) <!-- AUTHOR LINE: delete this single line to remove attribution -->
