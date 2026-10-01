"""CLI: python -m aiot_agent --scenario conflict|true_positive"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from .agent import ConditionMonitoringAgent
from .ops_report import format_developer_summary, format_ops_report


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description=(
            "AIoT condition-monitoring agent, bounded authority demo. "
            "Model provides intelligence; architecture provides control."
        )
    )
    p.add_argument(
        "--scenario",
        choices=[
            "conflict",
            "true_positive",
            "tool_error_storm",
            "stale_data",
            "social_engineering",
            "alert_fatigue",
            "parts_unavailable",
            "conflicting_work_orders",
            "dead_network",
            "unauthorized_plc_write",
            "maximo_sap_disagree",
            "soft_vs_hard_hold",
        ],
        default="conflict",
        help="Digital-twin scenario to run",
    )
    p.add_argument(
        "--interactive",
        action="store_true",
        help="Prompt on the CLI for human approval of consequential actions",
    )
    p.add_argument(
        "--approve",
        nargs="*",
        default=[],
        help="Pre-approve these HIGH-risk tools (programmatic)",
    )
    p.add_argument(
        "--deny",
        nargs="*",
        default=[],
        help="Pre-deny these HIGH-risk tools (programmatic)",
    )
    p.add_argument(
        "--audit-log",
        type=Path,
        default=None,
        help="Path for JSONL audit log (default: logs/audit_<scenario>.jsonl)",
    )
    p.add_argument(
        "--json",
        action="store_true",
        help="Print machine-readable JSON result",
    )
    p.add_argument(
        "--verbose",
        action="store_true",
        help="Also print the developer summary (tool list, raw recommendation text)",
    )
    p.add_argument(
        "--ops",
        action="store_true",
        default=True,
        help="Print ops-facing line status report (default: on)",
    )
    p.add_argument(
        "--no-ops",
        action="store_true",
        help="Skip the ops-facing report (developer summary only)",
    )
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    # Default cwd-relative logs under package root if possible
    root = Path(__file__).resolve().parent.parent
    os.chdir(root)

    decisions = {}
    for name in args.approve:
        decisions[name] = True
    for name in args.deny:
        decisions[name] = False

    audit_path = args.audit_log or Path(f"logs/audit_{args.scenario}.jsonl")
    # Fresh audit file per CLI run so the trail matches this run
    if audit_path.exists() and args.audit_log is None:
        audit_path.write_text("", encoding="utf-8")

    agent = ConditionMonitoringAgent(
        scenario=args.scenario,
        audit_path=audit_path,
        interactive=args.interactive,
        force_mock_llm=True,
        approval_decisions=decisions or None,
    )

    print(f"\n=== AIoT Agent, scenario: {args.scenario} ===")
    print("Thesis: the model provides intelligence; the architecture provides control.\n")

    result = agent.run()

    explicit_approve = bool(args.approve) and result.consequential_executed
    explicit_deny = bool(args.deny) and result.consequential_proposed and not result.consequential_executed
    # Interactive deny: human said no
    if args.interactive and result.consequential_proposed and not result.consequential_executed:
        if any(a.get("status") == "denied" for a in result.approvals):
            explicit_deny = True
    if args.interactive and result.consequential_executed:
        explicit_approve = True

    if args.json:
        print(
            json.dumps(
                {
                    "scenario": result.scenario,
                    "recommendation_type": result.recommendation_type,
                    "consequential_proposed": result.consequential_proposed,
                    "consequential_executed": result.consequential_executed,
                    "denied_tools": result.denied_tools,
                    "tool_calls_made": result.tool_calls_made,
                    "approvals": result.approvals,
                    "final_message": result.final_message,
                    "hold_mode": getattr(result, "hold_mode", None),
                },
                indent=2,
            )
        )
    else:
        show_ops = not args.no_ops
        if show_ops:
            print(
                format_ops_report(
                    result,
                    agent.audit.path,
                    interactive=args.interactive,
                    explicit_deny=explicit_deny,
                    explicit_approve=explicit_approve,
                )
            )
        if args.verbose or args.no_ops:
            print(format_developer_summary(result, agent.audit.path))

    return 0


if __name__ == "__main__":
    sys.exit(main())
