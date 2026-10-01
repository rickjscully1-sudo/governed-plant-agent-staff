"""Plant Agent Staff: a Coordinator plus five specialist agents.

SIMULATION / mock plant data. Illustrative, not a live plant deployment.
See AGENT_STAFF.md at the project root.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from .control_policy import ControlPolicy, get_active_policy, load_policy
from .data import STAFF_SCENARIO_ORDER, staff_scenario_list
from .policy import AGENTS, HUMANS, SIMULATION_LABEL, org_chart, policy_panel
from .session import StaffSession


def run_staff_scenario(
    scenario: str,
    audit_path: Optional[Path] = None,
    approval_timeout_seconds: Optional[float] = None,
    policy: Optional[ControlPolicy] = None,
) -> StaffSession:
    """Run a staff scenario end to end (up to the human approvals)."""
    if audit_path is not None:
        audit_path = Path(audit_path)
        audit_path.parent.mkdir(parents=True, exist_ok=True)
        audit_path.write_text("", encoding="utf-8")
    return StaffSession(
        scenario,
        audit_path=audit_path,
        approval_timeout_seconds=approval_timeout_seconds,
        policy=policy,
    ).run()


__all__ = [
    "AGENTS",
    "ControlPolicy",
    "HUMANS",
    "SIMULATION_LABEL",
    "STAFF_SCENARIO_ORDER",
    "StaffSession",
    "get_active_policy",
    "load_policy",
    "org_chart",
    "policy_panel",
    "run_staff_scenario",
    "staff_scenario_list",
]
