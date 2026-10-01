"""CLI for the Agent Staff layer (SIMULATION, mock plant data).

  python -m aiot_agent.staff --scenario filler_drift_to_work_order
  python -m aiot_agent.staff --scenario co2_out_of_spec_hold --approve-as qa_lead
  python -m aiot_agent.staff --scenario co2_out_of_spec_hold --approve-as qa_lead shift_lead
"""

from __future__ import annotations

import argparse
import textwrap
from pathlib import Path
from typing import List, Optional

from . import run_staff_scenario
from .data import STAFF_SCENARIO_ORDER
from .policy import HUMANS, SIMULATION_LABEL


def _wrap(text: str, indent: str = "    ") -> str:
    return textwrap.fill(text, width=100, initial_indent=indent, subsequent_indent=indent + "  ")


def main(argv: Optional[List[str]] = None) -> int:
    p = argparse.ArgumentParser(description="Plant Agent Staff demo. " + SIMULATION_LABEL)
    p.add_argument("--scenario", choices=list(STAFF_SCENARIO_ORDER), default=STAFF_SCENARIO_ORDER[0])
    p.add_argument("--approve-as", nargs="*", default=[], choices=list(HUMANS),
                   help="Approve every open request that needs this role, in order given")
    p.add_argument("--deny-as", nargs="*", default=[], choices=list(HUMANS),
                   help="Deny every open request that needs this role")
    p.add_argument("--audit-log", type=Path, default=None)
    args = p.parse_args(argv)

    audit = args.audit_log or Path("logs") / "audit_staff_{0}.jsonl".format(args.scenario)
    s = run_staff_scenario(args.scenario, audit_path=audit)

    for role in args.approve_as:
        for a in list(s.board.pending_for(role)):
            print("> {0}".format(s.decide(a.approval_id, role, True)["message"]))
    for role in args.deny_as:
        for a in list(s.board.pending_for(role)):
            print("> {0}".format(s.decide(a.approval_id, role, False)["message"]))

    d = s.to_dict()
    print("=" * 100)
    print("PLANT AGENT STAFF  |  {0}".format(d["label"]))
    print(SIMULATION_LABEL)
    print("=" * 100)
    print("OUTCOME: {0}. {1}".format(d["outcome"]["label"], d["outcome"]["headline"]))
    print("\nWHO DID WHAT")
    for c in d["contributions"]:
        print("  [{0}] {1}".format(c["name"], c["headline"]))
        for f in c["findings"]:
            print(_wrap("- " + f, "      "))
    print("\nAPPROVALS")
    for a in d["approvals"] or []:
        print("  {0} [{1}] {2}  ({3})".format(a["approval_id"], a["status"].upper(), a["summary"], a["progress"]))
    if not d["approvals"]:
        print("  none")
    print("\nBLOCKED ATTEMPTS")
    for b in d["blocked"] or []:
        print(_wrap("- " + b["message"], "  "))
    if not d["blocked"]:
        print("  none")
    print("\nROLE BRIEFS")
    for b in d["briefs"]:
        print("  {0} ({1})".format(b["title"], b["name"]))
        for n in b["needs_decision"]:
            print(_wrap("DECIDE: " + n["text"], "    "))
        for e in b["escalated"]:
            print(_wrap("ESCALATED: " + e, "    "))
        for f in b["fyi"]:
            print(_wrap("FYI: " + f, "    "))
    print("\nAudit log: {0} (hash chain ok: {1})".format(d["audit_path"], d["audit_ok"]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
