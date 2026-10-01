"""The five specialist agents plus the Coordinator (SIMULATION, mock data).

Agents are deterministic, rule-based reasoning over mock plant data so the
demo runs offline with no API key. Every tool call goes through the
``ToolBroker``; an agent object holds no tool handles of its own. HOLD rules
(sample age limits, consecutive out-of-spec count) and the spend threshold are
read from the session's signed control policy.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Dict, List, Optional

from ..freshness import evaluate_freshness
from .control_policy import StaffTier
from .policy import AGENTS, human_label

if TYPE_CHECKING:  # pragma: no cover
    from .session import Chain, StaffSession


@dataclass
class Contribution:
    agent_id: str
    headline: str
    status: str = "ok"  # ok | alert | stale | conflict | pending | info
    findings: List[str] = field(default_factory=list)
    recommendation: str = ""
    tools_used: List[str] = field(default_factory=list)
    approvals: List[str] = field(default_factory=list)
    chain_id: Optional[str] = None
    data: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "agent_id": self.agent_id,
            "name": AGENTS[self.agent_id].name,
            "headline": self.headline,
            "status": self.status,
            "findings": list(self.findings),
            "recommendation": self.recommendation,
            "tools_used": list(self.tools_used),
            "approvals": list(self.approvals),
            "chain_id": self.chain_id,
        }


def _hhmm(ts: str) -> str:
    try:
        return ts[11:16] + " UTC"
    except Exception:  # noqa: BLE001
        return str(ts)


def _hours(seconds: Optional[float]) -> str:
    if seconds is None:
        return "unknown"
    return "{0:.1f} hours".format(seconds / 3600.0)


class _Agent:
    agent_id = ""

    def __init__(self, session: "StaffSession") -> None:
        self.session = session

    def call(self, tool: str, args: Optional[dict] = None, contribution: Optional[Contribution] = None,
             **kw: Any) -> Dict[str, Any]:
        result = self.session.broker.call(self.agent_id, tool, args, **kw)
        if contribution is not None:
            contribution.tools_used.append(tool)
        return result

    @staticmethod
    def payload(result: Dict[str, Any]) -> Dict[str, Any]:
        if not isinstance(result, dict):
            return {}
        inner = result.get("result")
        return inner if isinstance(inner, dict) else result


# ---------------------------------------------------------------------------
class LineWatch(_Agent):
    agent_id = "line_watch"

    def assess(self, chain: Optional["Chain"] = None, context_only: bool = False) -> Contribution:
        c = Contribution(agent_id=self.agent_id, headline="", chain_id=chain.chain_id if chain else None)
        hist_raw = self.call("iot_historian_query", {"window_hours": 6}, c, chain=chain)
        plc_raw = self.call("get_plc_tag_snapshot", {"plc_id": "PLC-L3-FILLER"}, c, chain=chain)
        ot_raw = self.call("get_ot_security_events", {"zone": "L3"}, c, chain=chain)
        hist, plc, ot = self.payload(hist_raw), self.payload(plc_raw), self.payload(ot_raw)

        stale_reasons: List[str] = []
        max_age = self.session.policy.sensor_max_age_seconds
        verdict = evaluate_freshness(hist, now=self.session.now, max_age_seconds=max_age)
        if not verdict.ok:
            stale_reasons.append(
                "Newest historian sample is {0} old (limit {1}). This reading cannot be trusted.".format(
                    _hours(verdict.age_seconds), _hours(max_age)
                )
            )
        bad_tags = [t for t in (plc.get("tags") or []) if str(t.get("quality", "GOOD")).upper() != "GOOD"]
        if bad_tags:
            stale_reasons.append(
                "{0} PLC tag(s) flagged {1} since {2}.".format(
                    len(bad_tags), bad_tags[0].get("quality"), _hhmm(str(bad_tags[0].get("ts", "")))
                )
            )

        alerts: List[str] = []
        samples = hist.get("samples") or []
        for watch in hist.get("watch") or []:
            metric = watch["metric"]
            vals = [(s.get("ts", ""), s[metric]) for s in samples if metric in s]
            if not vals:
                continue
            up = watch.get("direction", "up") == "up"
            first, last = vals[0][1], vals[-1][1]
            adverse = 0
            for i in range(len(vals) - 1, 0, -1):
                step_bad = vals[i][1] > vals[i - 1][1] if up else vals[i][1] < vals[i - 1][1]
                if not step_bad:
                    break
                adverse += 1
            warn, alarm = watch.get("warn"), watch.get("alarm")
            past_warn = warn is not None and (last >= warn if up else last <= warn)
            past_alarm = alarm is not None and (last >= alarm if up else last <= alarm)
            c.findings.append(
                "{0}: {1} to {2} {3} over {4} readings (baseline {5}, warning {6}, alarm {7}).".format(
                    watch.get("label", metric), first, last, watch.get("unit", ""), len(vals),
                    watch.get("baseline"), warn, alarm,
                )
            )
            if past_alarm:
                alerts.append("{0} is past the alarm limit.".format(watch.get("label", metric)))
            elif past_warn and adverse >= 2:
                alerts.append(
                    "Early fault sign: {0} is past warning and has moved the wrong way {1} readings in a row.".format(
                        watch.get("label", metric).lower(), adverse
                    )
                )
        for ev in (ot.get("events") or []):
            alerts.append("OT security event: {0}".format(ev.get("summary") or ev))

        c.findings.extend(stale_reasons)
        c.findings.extend(alerts)
        c.data = {"stale": bool(stale_reasons), "alert": bool(alerts) and not stale_reasons,
                  "freshness": verdict.to_audit_dict()}
        if stale_reasons:
            c.status = "stale"
            c.headline = "Data is stale. Not trusting the scary number."
            c.recommendation = (
                "Brief Shift Lead: get a handheld vibration reading and check the sensor gateway. "
                "Do not plan work off this data."
            )
        elif alerts:
            c.status = "alert"
            c.headline = alerts[0]
            c.recommendation = (
                "Brief Shift Lead. Process context for Quality; no maintenance routing."
                if context_only
                else "Brief Shift Lead and ask the Coordinator to route to the Maintenance Planner."
            )
        else:
            c.status = "ok"
            c.headline = "Line looks normal. No drift, no stale sensors."
            c.recommendation = "No action."
        c.findings.append("Read-only: Line Watch has no PLC write path.")
        return c


# ---------------------------------------------------------------------------
class MaintenancePlanner(_Agent):
    agent_id = "maintenance_planner"

    def draft(self, chain: "Chain", trigger: Contribution) -> Contribution:
        c = Contribution(agent_id=self.agent_id, headline="", chain_id=chain.chain_id)
        asset = self.payload(self.call("maximo_asset_lookup", {}, c, chain=chain))
        history = self.payload(self.call("maximo_maintenance_history", {}, c, chain=chain))
        manual = self.payload(self.call("oem_manual_search", {"query": "main drive bearing vibration"}, c, chain=chain))
        store = self.payload(self.call("maximo_storeroom_lookup", {}, c, chain=chain))

        sku = asset.get("bearing_sku", "BRG-UNKNOWN")
        store_items = {i["sku"]: i for i in (store.get("items") or [])}
        c.findings.append(
            "Maximo: {0}, criticality {1}. Last bearing replacement {2}.".format(
                asset.get("asset_id"), asset.get("criticality"), asset.get("last_bearing_replacement")
            )
        )
        recs = history.get("records") or []
        if recs:
            c.findings.append("Last work: {0} {1} ({2}).".format(recs[0].get("wo"), recs[0].get("summary"), recs[0].get("date")))
        snippets = manual.get("snippets") or []
        if snippets:
            c.findings.append("OEM manual: " + snippets[0]["text"])
        si = store_items.get(sku)
        c.findings.append(
            "Maximo storeroom: {0} x {1} in bin {2} (last counted {3}).".format(
                si.get("qty"), sku, si.get("bin"), si.get("last_count")
            ) if si else "Maximo storeroom: no record for {0}.".format(sku)
        )

        draft_id = "WO-DRAFT-" + chain.chain_id.split("-")[-1]
        args = {
            "draft_id": draft_id,
            "asset_id": asset.get("asset_id"),
            "title": "Replace filler main drive bearing",
            "priority": "2 (within 72 hours)",
            "procedure": manual.get("procedure_id", "PP-FIL-BRG-02"),
            "est_hours": manual.get("est_hours", 4),
            "window": "Next planned sanitation stop",
            "parts": [{"sku": sku, "qty": 1}],
            "evidence": trigger.headline,
        }
        summary = "Open work order {0}: replace filler main drive bearing ({1}, about {2} h, next planned sanitation stop).".format(
            draft_id, args["procedure"], args["est_hours"]
        )
        res = self.call("draft_work_order", args, c, chain=chain, summary=summary)
        if res.get("status") == "PENDING_APPROVAL":
            c.approvals.append(res["approval_id"])
            c.status = "pending"
            c.headline = "Drafted {0}. Waiting on {1}.".format(draft_id, human_label("maintenance_supervisor"))
            c.recommendation = "Maintenance Supervisor to review and approve before anything opens in Maximo."
        else:
            c.status = "info"
            c.headline = "Work order not drafted: {0}".format(res.get("message"))
        c.data = {
            "parts_needed": args["parts"],
            "storeroom": {k: v.get("qty", 0) for k, v in store_items.items()},
            "storeroom_detail": store_items,
            "approval_id": res.get("approval_id"),
            "draft_id": draft_id,
        }
        return c


# ---------------------------------------------------------------------------
class PartsSupply(_Agent):
    agent_id = "parts_supply"

    def check(self, chain: "Chain", parts_needed: List[Dict[str, Any]]) -> Contribution:
        c = Contribution(agent_id=self.agent_id, headline="", chain_id=chain.chain_id)
        sap = self.payload(self.call("sap_inventory_lead_time",
                                     {"skus": [p["sku"] for p in parts_needed]}, c, chain=chain))
        items = {i["sku"]: i for i in (sap.get("items") or [])}
        lines = []
        for p in parts_needed:
            it = items.get(p["sku"], {})
            on_hand = int(it.get("on_hand", 0))
            line = {
                "sku": p["sku"],
                "needed": int(p["qty"]),
                "on_hand": on_hand,
                "min_qty": int(it.get("min_qty", 0)),
                "lead_time_days": it.get("lead_time_days"),
                "unit_cost_usd": float(it.get("unit_cost_usd", 0.0)),
                "status": "in_stock" if on_hand >= int(p["qty"]) else "gap",
            }
            lines.append(line)
            c.findings.append(
                "SAP: {0} on hand {1}, minimum {2}, lead time {3} days.".format(
                    p["sku"], on_hand, line["min_qty"], line["lead_time_days"]
                )
            )
        gaps = [l for l in lines if l["status"] == "gap"]
        if gaps:
            c.status = "alert"
            c.headline = "Part gap: SAP shows {0} x {1} on hand, {2}-day lead time.".format(
                gaps[0]["on_hand"], gaps[0]["sku"], gaps[0]["lead_time_days"])
            c.recommendation = "Flag gap to Buyer and Maintenance Supervisor."
        else:
            c.status = "ok"
            c.headline = "Bearing in stock per SAP. Work order is not waiting on parts."
            c.recommendation = "Reserve on work order approval."
        c.data = {"lines": lines}
        return c

    def draft_requests(self, chain: "Chain", contribution: Contribution) -> None:
        for line in contribution.data.get("lines", []):
            after_use = line["on_hand"] - line["needed"]
            if line["status"] == "gap":
                qty = line["needed"] + max(line["min_qty"], 0)
                reason = "Part gap for work order; restore minimum stock."
            elif after_use < line["min_qty"]:
                qty = line["min_qty"] - after_use
                reason = "Using {0} drops stock to {1}, below the minimum of {2}. Replenish.".format(
                    line["needed"], after_use, line["min_qty"])
            else:
                continue
            cost = round(qty * line["unit_cost_usd"], 2)
            pr_id = "PR-DRAFT-" + chain.chain_id.split("-")[-1]
            args = {
                "pr_draft_id": pr_id,
                "sku": line["sku"],
                "qty": qty,
                "estimated_cost_usd": cost,
                "reason": reason,
                "lead_time_days": line["lead_time_days"],
            }
            policy = self.session.policy
            dual = policy.tool_tier("draft_purchase_request", args) == StaffTier.PRODUCTION
            summary = "Purchase request {0}: {1} x {2}, about ${3:,.0f}{4}.".format(
                pr_id, qty, line["sku"], cost,
                " (above ${0:,.0f}, needs two signatures)".format(policy.spend_threshold_usd) if dual else "",
            )
            res = self.call("draft_purchase_request", args, contribution, chain=chain, summary=summary)
            contribution.findings.append(reason)
            if res.get("status") == "PENDING_APPROVAL":
                contribution.approvals.append(res["approval_id"])
                contribution.recommendation = "Drafted {0} (${1:,.0f}). Waiting on {2}.".format(
                    pr_id, cost, " and ".join(res.get("required", [])))
            elif res.get("status") == "DENIED":
                contribution.recommendation = "Purchase request {0} not drafted: {1}".format(
                    pr_id, res.get("message"))


# ---------------------------------------------------------------------------
class Quality(_Agent):
    agent_id = "quality"

    def assess(self, chain: "Chain") -> Contribution:
        c = Contribution(agent_id=self.agent_id, headline="", chain_id=chain.chain_id)
        lab = self.payload(self.call("quality_lab_results", {}, c, chain=chain))
        quality_max_age = self.session.policy.quality_max_age_seconds
        verdict = evaluate_freshness({"samples": lab.get("samples")}, now=self.session.now,
                                     max_age_seconds=quality_max_age)
        if not verdict.ok:
            c.status = "stale"
            c.headline = "Quality data is stale. Cannot recommend hold or release."
            c.findings.append("Newest sample is {0} old (limit {1}).".format(
                _hours(verdict.age_seconds), _hours(quality_max_age)))
            c.recommendation = "Pull a fresh sample before any decision."
            c.data = {"stale": True}
            return c

        # HOLD rule owned by the control policy (the scenario data cannot loosen it).
        need = self.session.policy.consecutive_out_of_spec_for_hold
        samples = lab.get("samples") or []
        hold_reasons: List[str] = []
        for key, spec in (lab.get("specs") or {}).items():
            vals = [(s["ts"], s[key]) for s in samples if key in s]
            trailing = []
            for ts, v in reversed(vals):
                if spec["min"] <= v <= spec["max"]:
                    break
                trailing.append((ts, v))
            trailing.reverse()
            if trailing:
                c.findings.append(
                    "{0}: last {1} sample(s) out of spec ({2}) vs {3} to {4} {5}.".format(
                        spec["label"], len(trailing),
                        ", ".join("{0} at {1}".format(v, _hhmm(ts)) for ts, v in trailing),
                        spec["min"], spec["max"], spec["unit"],
                    )
                )
            else:
                last = vals[-1][1] if vals else "n/a"
                c.findings.append("{0}: in spec (latest {1} {2}).".format(spec["label"], last, spec["unit"]))
            if len(trailing) >= need:
                hold_reasons.append(
                    "{0} {1} vs spec {2} to {3} {4}".format(
                        spec["label"], ", ".join(str(v) for _, v in trailing), spec["min"], spec["max"], spec["unit"]
                    )
                )
        for test in lab.get("lab") or []:
            c.findings.append("Lab {0}: {1} (spec {2}) {3}.".format(
                test["test"], test["result"], test["spec"], "pass" if test.get("pass") else "FAIL"))

        if hold_reasons:
            args = {
                "line_id": "LINE-03",
                "lot": lab.get("lot"),
                "scope": "Hold lot {0} and pause filling on Line 3".format(lab.get("lot")),
                "reason": "; ".join(hold_reasons),
            }
            summary = "Production hold: {0}. Reason: {1}.".format(args["scope"], args["reason"])
            res = self.call("request_line_hold", args, c, chain=chain, summary=summary)
            if res.get("status") == "PENDING_APPROVAL":
                c.approvals.append(res["approval_id"])
            c.status = "pending"
            c.headline = "Recommend HOLD on lot {0}: {1} out of spec on {2} consecutive samples.".format(
                lab.get("lot"), hold_reasons[0].split(" ")[0], need)
            c.recommendation = "Needs QA Lead AND Shift Lead. One signature alone does not place the hold."
        else:
            c.status = "ok"
            c.headline = "Recommend RELEASE: product in spec."
            c.recommendation = "QA Lead to confirm release."
        c.data = {"hold": bool(hold_reasons)}
        return c


# ---------------------------------------------------------------------------
class ShiftHandoff(_Agent):
    agent_id = "shift_handoff"

    def build(self) -> Contribution:
        c = Contribution(agent_id=self.agent_id, headline="")
        log = self.payload(self.call("shift_event_log", {}, c))
        status = self.payload(self.call("staff_status_read", {}, c))

        happened = list(log.get("entries") or [])
        for item in status.get("contributions") or []:
            if item["agent_id"] in ("shift_handoff", "coordinator"):
                continue
            happened.append("{0}: {1}".format(item["name"], item["headline"]))
        for a in status.get("approvals") or []:
            if a["status"] == "executed":
                happened.append("Done after human approval: {0}".format(a["summary"]))
            elif a["status"] == "denied":
                happened.append("Denied by a human, nothing executed: {0}".format(a["summary"]))

        open_items = []
        for a in status.get("approvals") or []:
            if a["status"] == "pending":
                waiting = [r["label"] for r in a["required"] if r["decision"] is None]
                open_items.append("{0} ({1}; waiting on {2})".format(a["summary"], a["progress"], ", ".join(waiting)))
            elif a["status"] == "held":
                open_items.append("HELD, cannot be approved: {0} Reason: {1}".format(a["summary"], a["hold_reason"]))

        risky = []
        for cf in status.get("conflicts") or []:
            risky.append("Conflict: " + cf["detail"])
        for es in status.get("escalations") or []:
            risky.append("Escalated to {0}: {1}".format(", ".join(es["to_labels"]), es["reason"]))
        blocked = status.get("blocked") or []
        if blocked:
            risky.append("{0} blocked agent attempt(s) this shift (see audit log): {1}.".format(
                len(blocked), "; ".join("{0} tried '{1}'".format(b["agent_name"], b["tool_label"]) for b in blocked)))
        if not risky:
            risky.append("Nothing flagged as risky.")
        if not open_items:
            open_items.append("No open approvals.")

        c.status = "info"
        c.headline = "End-of-shift brief ready: {0} open item(s), {1} risk flag(s).".format(
            0 if open_items == ["No open approvals."] else len(open_items),
            0 if risky == ["Nothing flagged as risky."] else len(risky))
        c.recommendation = "Incoming Shift Lead reads this before taking the line."
        c.data = {"happened": happened, "open": open_items, "risky": risky}
        return c
