"""Tiered tool permissions: LOW (auto) vs HIGH (human approval required)."""

from __future__ import annotations

from enum import Enum
from typing import FrozenSet


class PermissionTier(str, Enum):
    LOW = "low"
    HIGH = "high"


# Tools that may run without human approval (read-only / evidence gathering).
LOW_RISK_TOOLS: FrozenSet[str] = frozenset(
    {
        "iot_historian_query",
        "maximo_asset_lookup",
        "maximo_maintenance_history",
        "sap_pm_work_orders",
        "fleet_failure_history",
        "parts_inventory_check",
        "repair_recipe_lookup",
        "technician_notes",
        "tribal_field_observations",
        # OT / connectivity evidence (read-only)
        "get_device_connectivity",
        "get_private_network_health",
        "get_gateway_status",
        "get_plc_tag_snapshot",
        "get_ot_security_events",
    }
)

# Consequential actions require explicit human approval before execution.
HIGH_RISK_TOOLS: FrozenSet[str] = frozenset(
    {
        "create_emergency_work_order",
        "recommend_line_shutdown",
        "reorder_critical_parts",
        "update_plant_state",
    }
)


def get_tier(tool_name: str) -> PermissionTier:
    if tool_name in HIGH_RISK_TOOLS:
        return PermissionTier.HIGH
    if tool_name in LOW_RISK_TOOLS:
        return PermissionTier.LOW
    # Unknown tools default to HIGH (fail closed).
    return PermissionTier.HIGH


def is_consequential(tool_name: str) -> bool:
    return get_tier(tool_name) == PermissionTier.HIGH
