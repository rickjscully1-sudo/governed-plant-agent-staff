"""Versioned, signed control policy for the Agent Staff layer.

SIMULATION ONLY. Mock plant data for a beverage bottling plant. All people are
fictional. Illustrative, not a live plant deployment.

A named Agent Control Architect owns the permissions, scenarios and HOLD rules,
and plant leadership signs off on them. This module makes that sentence real:

* ``control_policy.json`` is the single source of truth: per-agent tool lanes,
  approval tiers, dual-control triggers (including the spend threshold), the
  PLC-block rule, fail-closed rules, HOLD rules and the approved scenarios.
* The policy fingerprint is the SHA-256 of the canonical JSON of the policy
  with the ``signoff`` block removed. Leadership signs that fingerprint.
* ``ControlPolicy.verify()`` recomputes the fingerprint on every call. If the
  sign-off is missing, incomplete, or the fingerprint no longer matches (the
  policy was edited after sign-off), the policy is NOT active and the staff
  layer may only read. Drafts and production actions are blocked.
"""

from __future__ import annotations

import copy
import hashlib
import json
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

POLICY_PATH = Path(__file__).resolve().parent / "control_policy.json"
SIGNOFF_KEY = "signoff"
POLICY_BLOCKED_MESSAGE = "Policy not signed: drafts and production actions blocked."
STATUS_ACTIVE_LABEL = "Signed and active"
STATUS_BLOCKED_LABEL = "Not signed, blocked"
FINGERPRINT_SHORT_LEN = 12


class StaffTier(str, Enum):
    READ = "read"
    DRAFT = "draft"
    PRODUCTION = "production"
    BLOCKED = "blocked"


_TIER_VALUES = {t.value for t in StaffTier}


def canonical_policy_json(data: Dict[str, Any]) -> str:
    """Stable JSON of the policy body (everything except the sign-off block)."""
    body = {k: v for k, v in (data or {}).items() if k != SIGNOFF_KEY}
    return json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def compute_fingerprint(data: Dict[str, Any]) -> str:
    """Full SHA-256 hex fingerprint of the canonical policy body."""
    return hashlib.sha256(canonical_policy_json(data).encode("utf-8")).hexdigest()


def short_fingerprint(fingerprint: Optional[str]) -> str:
    return (fingerprint or "")[:FINGERPRINT_SHORT_LEN]


def _deep_merge(base: Dict[str, Any], changes: Dict[str, Any]) -> Dict[str, Any]:
    out = copy.deepcopy(base)
    for key, value in (changes or {}).items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = copy.deepcopy(value)
    return out


class ControlPolicy:
    """Read-only view over one policy document, with sign-off verification."""

    def __init__(self, data: Dict[str, Any], source: str = "", load_error: Optional[str] = None) -> None:
        # Private deep copy: callers cannot change the rules behind our back.
        self._data: Dict[str, Any] = copy.deepcopy(data or {})
        self.source = source
        self.load_error = load_error

    # ------------------------------------------------------------------
    # Construction helpers
    # ------------------------------------------------------------------
    def to_data(self) -> Dict[str, Any]:
        """Deep copy of the raw policy document (safe to edit in tests)."""
        return copy.deepcopy(self._data)

    def with_changes(self, changes: Dict[str, Any], keep_signoff: bool = True) -> "ControlPolicy":
        """A copy with ``changes`` merged in. The old sign-off is kept, so the
        fingerprint no longer matches unless the copy is signed again."""
        data = _deep_merge(self._data, changes)
        if keep_signoff and SIGNOFF_KEY in self._data:
            data[SIGNOFF_KEY] = copy.deepcopy(self._data[SIGNOFF_KEY])
        return ControlPolicy(data, source=(self.source or "policy") + " (edited copy)")

    # ------------------------------------------------------------------
    # Identity
    # ------------------------------------------------------------------
    @property
    def policy_id(self) -> str:
        return str(self._data.get("policy_id") or "UNKNOWN-POLICY")

    @property
    def version(self) -> str:
        return str(self._data.get("version") or "0")

    @property
    def name(self) -> str:
        return str(self._data.get("name") or "Control policy")

    @property
    def label(self) -> str:
        return "{0} v{1}".format(self.policy_id, self.version)

    @property
    def short_label(self) -> str:
        return "Policy v{0}".format(self.version)

    @property
    def effective_date(self) -> str:
        return str(self._data.get("effective_date") or "")

    @property
    def owner(self) -> Dict[str, str]:
        return dict(self._data.get("owner") or {})

    @property
    def owner_label(self) -> str:
        o = self.owner
        if not o:
            return "No owner on file"
        return "{0} ({1})".format(o.get("name", "?"), o.get("title", "?"))

    @property
    def fingerprint(self) -> str:
        return compute_fingerprint(self._data)

    @property
    def fingerprint_short(self) -> str:
        return short_fingerprint(self.fingerprint)

    @property
    def signoff(self) -> Dict[str, Any]:
        return dict(self._data.get(SIGNOFF_KEY) or {})

    @property
    def signed_fingerprint(self) -> str:
        return str(self.signoff.get("signed_fingerprint") or "")

    @property
    def signatures(self) -> List[Dict[str, str]]:
        return [dict(s) for s in (self.signoff.get("signatures") or []) if isinstance(s, dict)]

    @property
    def required_signers(self) -> Tuple[str, ...]:
        titles = self.signoff.get("required_titles") or ["Plant Manager", "Operations Director"]
        return tuple(str(t) for t in titles)

    # ------------------------------------------------------------------
    # Verification (fail closed)
    # ------------------------------------------------------------------
    def verify(self) -> Tuple[bool, List[str]]:
        """(active, problems). Recomputes the fingerprint every time."""
        problems: List[str] = []
        if self.load_error:
            problems.append("Policy could not be loaded: {0}".format(self.load_error))
        if not self._data.get("policy_id") or not self._data.get("version"):
            problems.append("Policy has no id or version.")
        signoff = self._data.get(SIGNOFF_KEY)
        if not isinstance(signoff, dict) or not signoff:
            problems.append("No leadership sign-off on file.")
        else:
            by_title = {}
            for sig in self.signatures:
                if sig.get("name") and sig.get("signed_on"):
                    by_title[str(sig.get("title"))] = sig
            for title in self.required_signers:
                if title not in by_title:
                    problems.append("Missing sign-off: {0}.".format(title))
            signed = self.signed_fingerprint
            actual = self.fingerprint
            if not signed:
                problems.append("No signed fingerprint on file.")
            elif signed != actual:
                problems.append(
                    "Fingerprint {0} does not match the signed fingerprint {1}. "
                    "The policy was edited after sign-off.".format(
                        short_fingerprint(actual), short_fingerprint(signed)
                    )
                )
        return (not problems), problems

    @property
    def is_active(self) -> bool:
        return self.verify()[0]

    # ------------------------------------------------------------------
    # Rules
    # ------------------------------------------------------------------
    @property
    def spend_threshold_usd(self) -> float:
        dc = self._data.get("dual_control") or {}
        try:
            return float(dc.get("spend_threshold_usd"))
        except (TypeError, ValueError):
            # Missing or bad threshold: every purchase needs dual control.
            return 0.0

    @property
    def tool_tiers(self) -> Dict[str, str]:
        out = {}
        for tool, tier in (self._data.get("tool_tiers") or {}).items():
            out[str(tool)] = tier if tier in _TIER_VALUES else StaffTier.BLOCKED.value
        return out

    @property
    def tool_approvers(self) -> Dict[str, Tuple[str, ...]]:
        return {
            str(t): tuple(str(r) for r in roles)
            for t, roles in (self._data.get("tool_approvers") or {}).items()
        }

    @property
    def high_spend_approvers(self) -> Tuple[str, ...]:
        dc = self._data.get("dual_control") or {}
        return tuple(dc.get("high_spend_approvers") or ("buyer", "plant_manager"))

    @property
    def fallback_approvers(self) -> Tuple[str, ...]:
        dc = self._data.get("dual_control") or {}
        return tuple(dc.get("fallback_approvers") or ("shift_lead", "plant_manager"))

    @property
    def dual_control_triggers(self) -> List[str]:
        return list((self._data.get("dual_control") or {}).get("triggers") or [])

    @property
    def fail_closed_rules(self) -> List[str]:
        return list(self._data.get("fail_closed_rules") or [])

    @property
    def plc_rule(self) -> Dict[str, Any]:
        return dict(self._data.get("plc_rule") or {})

    @property
    def hold_rules(self) -> Dict[str, Any]:
        return dict(self._data.get("hold_rules") or {})

    @property
    def consecutive_out_of_spec_for_hold(self) -> int:
        try:
            return max(1, int(self.hold_rules.get("consecutive_out_of_spec_samples_for_hold")))
        except (TypeError, ValueError):
            return 1  # Missing rule: recommend a (still gated) hold sooner, not later.

    def _hours_rule(self, key: str) -> float:
        try:
            return max(0.0, float(self.hold_rules.get(key))) * 3600.0
        except (TypeError, ValueError):
            return 0.0  # Missing rule: treat all data as stale (fail closed).

    @property
    def sensor_max_age_seconds(self) -> float:
        return self._hours_rule("sensor_max_age_hours")

    @property
    def quality_max_age_seconds(self) -> float:
        return self._hours_rule("quality_max_age_hours")

    @property
    def humans(self) -> List[Dict[str, str]]:
        return [dict(h) for h in (self._data.get("humans") or []) if isinstance(h, dict) and h.get("role_id")]

    @property
    def human_roles(self) -> Tuple[str, ...]:
        return tuple(h["role_id"] for h in self.humans)

    @property
    def agent_lanes(self) -> Dict[str, Dict[str, Any]]:
        out = {}
        for aid, spec in (self._data.get("agents") or {}).items():
            spec = spec or {}
            out[str(aid)] = {
                "approver": str(spec.get("approver") or ""),
                "allowed_tools": tuple(str(t) for t in (spec.get("allowed_tools") or [])),
            }
        return out

    @property
    def scenarios(self) -> List[Dict[str, str]]:
        return [dict(s) for s in (self._data.get("scenarios") or []) if isinstance(s, dict)]

    @property
    def scenario_ids(self) -> Tuple[str, ...]:
        return tuple(str(s.get("id")) for s in self.scenarios)

    @property
    def change_log(self) -> List[Dict[str, str]]:
        return [dict(c) for c in (self._data.get("change_log") or []) if isinstance(c, dict)]

    def tier_rules(self) -> Dict[str, str]:
        base = dict(self._data.get("approval_tiers") or {})
        out = {}
        for tier in StaffTier:
            text = base.get(tier.value, "")
            if tier == StaffTier.PRODUCTION:
                text = text.replace(
                    "spend above the threshold",
                    "spend above ${0:,.0f}".format(self.spend_threshold_usd),
                )
            out[tier.value] = text
        return out

    # ------------------------------------------------------------------
    # Decisions used by the broker and the approval board
    # ------------------------------------------------------------------
    def is_allowed(self, agent_id: str, tool: str) -> bool:
        lane = self.agent_lanes.get(agent_id)
        return bool(lane) and tool in lane["allowed_tools"]

    def is_plc_write(self, tool: str) -> bool:
        rule = self.plc_rule
        if tool in (rule.get("blocked_tools") or []):
            return True
        prefix = rule.get("blocked_prefix") or "plc_"
        keyword = rule.get("blocked_keyword") or "write"
        return tool.startswith(prefix) and keyword in tool

    def estimated_cost(self, args: Optional[Dict[str, Any]]) -> float:
        if not args:
            return 0.0
        try:
            return float(args.get("estimated_cost_usd") or 0.0)
        except (TypeError, ValueError):
            # Unparseable cost: treat as above threshold (fail closed).
            return self.spend_threshold_usd + 1.0

    def tool_tier(self, tool: str, args: Optional[Dict[str, Any]] = None) -> StaffTier:
        """Tier for a tool call. Unknown tools and PLC writes fail closed as BLOCKED."""
        if self.is_plc_write(tool):
            return StaffTier.BLOCKED
        tier = self.tool_tiers.get(tool)
        if tier is None:
            return StaffTier.BLOCKED
        tier_enum = StaffTier(tier)
        if tool == "draft_purchase_request" and self.estimated_cost(args) > self.spend_threshold_usd:
            return StaffTier.PRODUCTION
        return tier_enum

    def required_approvers(self, tool: str, args: Optional[Dict[str, Any]] = None) -> Tuple[str, ...]:
        """Named human roles that must ALL approve before the tool executes."""
        tier = self.tool_tier(tool, args)
        if tier in (StaffTier.READ, StaffTier.BLOCKED):
            return ()
        if tool == "draft_purchase_request" and tier == StaffTier.PRODUCTION:
            return self.high_spend_approvers
        roles = self.tool_approvers.get(tool)
        if not roles:
            return self.fallback_approvers
        if tier == StaffTier.PRODUCTION and len(set(roles)) < 2:
            return self.fallback_approvers
        return roles

    # ------------------------------------------------------------------
    def summary(self) -> Dict[str, Any]:
        """Plain data for the control center panel and docs."""
        active, problems = self.verify()
        tiers = self.tier_rules()
        return {
            "policy_id": self.policy_id,
            "version": self.version,
            "name": self.name,
            "label": self.label,
            "short_label": self.short_label,
            "effective_date": self.effective_date,
            "plant": self._data.get("plant"),
            "simulation_label": self._data.get("simulation_label"),
            "owner": dict(self.owner, label=self.owner_label),
            "signoffs": [
                {
                    "title": s.get("title"),
                    "name": s.get("name"),
                    "signed_on": s.get("signed_on"),
                    "label": "{0} ({1}), signed {2}".format(s.get("name"), s.get("title"), s.get("signed_on")),
                }
                for s in self.signatures
            ],
            "required_signers": list(self.required_signers),
            "fingerprint": self.fingerprint_short,
            "fingerprint_full": self.fingerprint,
            "signed_fingerprint": short_fingerprint(self.signed_fingerprint),
            "signed": active,
            "status": "signed_active" if active else "not_signed_blocked",
            "status_label": STATUS_ACTIVE_LABEL if active else STATUS_BLOCKED_LABEL,
            "status_message": (
                "Leadership signed this exact policy. Agents may read, draft and request "
                "production actions under these rules."
                if active
                else POLICY_BLOCKED_MESSAGE + " Reads still run."
            ),
            "problems": problems,
            "spend_threshold_usd": self.spend_threshold_usd,
            "tiers": [{"tier": k, "rule": v} for k, v in tiers.items()],
            "rules_compact": [
                "Read: automatic.",
                "Draft: one named approver.",
                "Two humans: line hold, setpoint change, spend above ${0:,.0f}.".format(self.spend_threshold_usd),
                "PLC writes: always blocked, for every agent.",
                "Stale or conflicting data: stop and escalate (fail closed).",
                "Unsigned or edited policy: reads only.",
            ],
            "dual_control_triggers": self.dual_control_triggers,
            "fail_closed_rules": self.fail_closed_rules,
            "plc_rule": self.plc_rule.get("rule", ""),
            "hold_rules": self.hold_rules,
            "scenarios": self.scenarios,
            "change_log": self.change_log,
            "source": self.source,
        }


def load_policy(path: Optional[Path] = None) -> ControlPolicy:
    """Load the policy JSON. Any load problem returns an empty, unsigned policy
    (no lanes, no tiers) so the staff layer fails closed instead of crashing."""
    target = Path(path) if path is not None else POLICY_PATH
    try:
        data = json.loads(target.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError("policy file is not a JSON object")
    except Exception as exc:  # noqa: BLE001 - fail closed on any load error
        # Short file name only: never expose an absolute server path in the UI/API.
        return ControlPolicy({}, source=target.name,
                             load_error="{0} reading {1}".format(type(exc).__name__, target.name))
    return ControlPolicy(data, source=target.name)


_ACTIVE: Optional[ControlPolicy] = None


def get_active_policy() -> ControlPolicy:
    """The policy loaded from ``control_policy.json`` at import time."""
    global _ACTIVE
    if _ACTIVE is None:
        _ACTIVE = load_policy()
    return _ACTIVE
