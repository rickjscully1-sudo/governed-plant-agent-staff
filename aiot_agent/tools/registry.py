"""MCP-style tool registry: name, description, JSON schema, structured results."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

from .data import get_scenario_data


Handler = Callable[[dict, dict], dict]  # (args, scenario_data) -> result


@dataclass
class ToolSpec:
    name: str
    description: str
    input_schema: dict
    handler: Handler
    tier_hint: str  # "low" | "high". Informational only; enforcement is in ApprovalGate


@dataclass
class ToolRegistry:
    scenario: str = "conflict"
    _tools: Dict[str, ToolSpec] = field(default_factory=dict)
    _data: dict = field(default_factory=dict)
    _chaos: Dict[str, dict] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self._data = get_scenario_data(self.scenario)
        self._register_defaults()

    def set_chaos(self, chaos: Optional[Dict[str, dict]]) -> None:
        """Inject error/empty/latency failures for named tools (test/demo chaos).

        Example::
            registry.set_chaos({
                "iot_historian_query": {"mode": "error"},
                "maximo_asset_lookup": {"mode": "empty"},
                "sap_pm_work_orders": {"mode": "latency", "latency_ms": 50},
            })
        """
        self._chaos = dict(chaos or {})

    def set_scenario(self, scenario: str) -> None:
        self.scenario = scenario
        self._data = get_scenario_data(scenario)

    def load_data(self, data: dict) -> None:
        """Load an explicit mock data fixture (used by the Agent Staff layer).

        Lets other simulation packs reuse the same tool handlers without
        adding their fixtures to the single-agent SCENARIOS table.
        """
        self._data = dict(data or {})

    def register(self, spec: ToolSpec) -> None:
        self._tools[spec.name] = spec

    def list_tools(self) -> List[Dict[str, Any]]:
        return [
            {
                "name": t.name,
                "description": t.description,
                "input_schema": t.input_schema,
                "tier_hint": t.tier_hint,
            }
            for t in self._tools.values()
        ]

    def get(self, name: str) -> ToolSpec:
        if name not in self._tools:
            raise KeyError(f"Unknown tool: {name}")
        return self._tools[name]

    def execute(self, name: str, args: Optional[dict] = None) -> dict:
        """Execute tool handler (caller must have already passed approval gate)."""
        spec = self.get(name)
        # Runtime chaos injector (tests / ROBUSTNESS demos).
        chaos = (self._chaos or {}).get(name)
        if chaos:
            mode = str(chaos.get("mode", "error")).lower()
            if mode == "latency":
                import time as _time
                _time.sleep(float(chaos.get("latency_ms", 100)) / 1000.0)
                # Fall through to real handler after delay unless also error/empty.
                if not chaos.get("also_fail"):
                    pass
                else:
                    mode = str(chaos.get("fail_mode", "error")).lower()
            if mode == "error":
                return {
                    "tool": name,
                    "status": "ERROR",
                    "error": chaos.get("error", "CHAOS_INJECTED_ERROR"),
                    "message": chaos.get(
                        "message",
                        "Chaos injector forced an error for tool {0}.".format(name),
                    ),
                }
            if mode == "empty":
                return {
                    "tool": name,
                    "status": "EMPTY",
                    "result": {},
                    "message": chaos.get(
                        "message",
                        "Chaos injector returned empty payload for {0}.".format(name),
                    ),
                }
            if mode == "latency":
                # latency-only already slept; continue to real handler
                pass
        # Scenario-embedded inject errors (fail-closed upstream).
        inject = (self._data or {}).get("_inject_errors") or {}
        if name in inject:
            payload = dict(inject[name])
            payload.setdefault("tool", name)
            payload.setdefault("status", "ERROR")
            return payload
        return spec.handler(args or {}, self._data)

    def _register_defaults(self) -> None:
        asset_arg = {
            "type": "object",
            "properties": {
                "asset_id": {
                    "type": "string",
                    "description": "Asset ID, e.g. FILLER-PUMP-A12",
                }
            },
            "required": [],
        }

        def _iot(args: dict, data: dict) -> dict:
            return {"tool": "iot_historian_query", "result": data["iot_historian"]}

        def _maximo_asset(args: dict, data: dict) -> dict:
            return {"tool": "maximo_asset_lookup", "result": data["maximo_asset"]}

        def _maximo_maint(args: dict, data: dict) -> dict:
            return {"tool": "maximo_maintenance_history", "result": data["maximo_maintenance"]}

        def _sap(args: dict, data: dict) -> dict:
            return {"tool": "sap_pm_work_orders", "result": data["sap_pm"]}

        def _fleet(args: dict, data: dict) -> dict:
            return {"tool": "fleet_failure_history", "result": data["fleet_failures"]}

        def _parts(args: dict, data: dict) -> dict:
            return {"tool": "parts_inventory_check", "result": data["parts_inventory"]}

        def _recipes(args: dict, data: dict) -> dict:
            return {"tool": "repair_recipe_lookup", "result": data["repair_recipes"]}

        def _notes(args: dict, data: dict) -> dict:
            return {"tool": "technician_notes", "result": data["technician_notes"]}

        def _tribal(args: dict, data: dict) -> dict:
            return {"tool": "tribal_field_observations", "result": data["tribal_observations"]}

        def _device_connectivity(args: dict, data: dict) -> dict:
            return {
                "tool": "get_device_connectivity",
                "result": data.get(
                    "device_connectivity",
                    {
                        "asset_id": data.get("maximo_asset", {}).get("asset_id", "FILLER-PUMP-A12"),
                        "status": "OK",
                        "last_check_in": None,
                        "note": "No connectivity fixture for this scenario.",
                    },
                ),
            }

        def _private_network(args: dict, data: dict) -> dict:
            return {
                "tool": "get_private_network_health",
                "result": data.get(
                    "private_network_health",
                    {
                        "network": "private_5g_core",
                        "status": "OK",
                        "note": "No private network fixture for this scenario.",
                    },
                ),
            }

        def _gateway(args: dict, data: dict) -> dict:
            return {
                "tool": "get_gateway_status",
                "result": data.get(
                    "gateway_status",
                    {
                        "gateway_id": "OT-GW-LINE03",
                        "status": "OK",
                        "note": "No gateway fixture for this scenario.",
                    },
                ),
            }

        def _plc_tags(args: dict, data: dict) -> dict:
            return {
                "tool": "get_plc_tag_snapshot",
                "result": data.get(
                    "plc_tag_snapshot",
                    {
                        "plc_id": "PLC-FILLER-03",
                        "tags": [],
                        "note": "No PLC tag fixture for this scenario. Read-only snapshot only.",
                    },
                ),
            }

        def _ot_security(args: dict, data: dict) -> dict:
            return {
                "tool": "get_ot_security_events",
                "result": data.get(
                    "ot_security_events",
                    {
                        "events": [],
                        "note": "No OT security events for this scenario.",
                    },
                ),
            }

        def _create_wo(args: dict, data: dict) -> dict:
            return {
                "tool": "create_emergency_work_order",
                "status": "CREATED",
                "work_order_id": "EMRG-WO-DEMO-001",
                "priority": "1-EMERGENCY",
                "args": args,
                "message": "Emergency work order created in Maximo/SAP PM (mock).",
            }

        def _shutdown(args: dict, data: dict) -> dict:
            return {
                "tool": "recommend_line_shutdown",
                "status": "SHUTDOWN_SIGNAL_SENT",
                "line_id": args.get("line_id", data["maximo_asset"].get("line_id", "LINE-03-FILLER")),
                "args": args,
                "message": "Line shutdown recommendation executed (mock plant-state change).",
            }

        def _reorder(args: dict, data: dict) -> dict:
            return {
                "tool": "reorder_critical_parts",
                "status": "PO_SUBMITTED",
                "purchase_order": "PO-DEMO-7781",
                "args": args,
                "estimated_cost_sar": args.get("estimated_cost_sar", 850),
                "message": "Critical parts reorder submitted (mock, spends money).",
            }

        def _plant_state(args: dict, data: dict) -> dict:
            return {
                "tool": "update_plant_state",
                "status": "UPDATED",
                "args": args,
                "message": "Plant state updated (mock).",
            }

        specs = [
            ToolSpec(
                name="iot_historian_query",
                description="Query IoT historian for vibration, temperature, and performance time series.",
                input_schema={
                    "type": "object",
                    "properties": {
                        "asset_id": {"type": "string"},
                        "window_hours": {"type": "integer", "default": 24},
                    },
                },
                handler=_iot,
                tier_hint="low",
            ),
            ToolSpec(
                name="maximo_asset_lookup",
                description="Look up Maximo asset master data (status, criticality, OEM).",
                input_schema=asset_arg,
                handler=_maximo_asset,
                tier_hint="low",
            ),
            ToolSpec(
                name="maximo_maintenance_history",
                description="Retrieve Maximo maintenance / work history for an asset.",
                input_schema=asset_arg,
                handler=_maximo_maint,
                tier_hint="low",
            ),
            ToolSpec(
                name="sap_pm_work_orders",
                description="List open and recent SAP Plant Maintenance work orders.",
                input_schema=asset_arg,
                handler=_sap,
                tier_hint="low",
            ),
            ToolSpec(
                name="fleet_failure_history",
                description="Fleet-wide failure modes and false-positive patterns for this equipment class.",
                input_schema={
                    "type": "object",
                    "properties": {"equipment_class": {"type": "string"}},
                },
                handler=_fleet,
                tier_hint="low",
            ),
            ToolSpec(
                name="parts_inventory_check",
                description="Check parts inventory levels and reorder policy (read-only).",
                input_schema=asset_arg,
                handler=_parts,
                tier_hint="low",
            ),
            ToolSpec(
                name="repair_recipe_lookup",
                description="Look up OEM/plant repair procedures and recipes.",
                input_schema=asset_arg,
                handler=_recipes,
                tier_hint="low",
            ),
            ToolSpec(
                name="technician_notes",
                description="Retrieve recent technician notes and walkdown observations.",
                input_schema=asset_arg,
                handler=_notes,
                tier_hint="low",
            ),
            ToolSpec(
                name="tribal_field_observations",
                description="Anecdotal / tribal field observations from OEM and plant veterans.",
                input_schema=asset_arg,
                handler=_tribal,
                tier_hint="low",
            ),
            ToolSpec(
                name="get_device_connectivity",
                description=(
                    "Carrier IoT platform device connectivity: last check-in, "
                    "link quality, and session state (read-only)."
                ),
                input_schema=asset_arg,
                handler=_device_connectivity,
                tier_hint="low",
            ),
            ToolSpec(
                name="get_private_network_health",
                description=(
                    "Private 5G core / plant wireless health for AGVs and sensors "
                    "(congestion, slice status). Read-only."
                ),
                input_schema={
                    "type": "object",
                    "properties": {
                        "network_id": {"type": "string"},
                        "zone": {"type": "string"},
                    },
                },
                handler=_private_network,
                tier_hint="low",
            ),
            ToolSpec(
                name="get_gateway_status",
                description=(
                    "OT zone-conduit gateway status (Modbus/OPC path health). Read-only."
                ),
                input_schema={
                    "type": "object",
                    "properties": {"gateway_id": {"type": "string"}},
                },
                handler=_gateway,
                tier_hint="low",
            ),
            ToolSpec(
                name="get_plc_tag_snapshot",
                description=(
                    "Read-only PLC tag snapshot for filler PLC. Never writes PLC tags."
                ),
                input_schema={
                    "type": "object",
                    "properties": {
                        "plc_id": {"type": "string"},
                        "asset_id": {"type": "string"},
                    },
                },
                handler=_plc_tags,
                tier_hint="low",
            ),
            ToolSpec(
                name="get_ot_security_events",
                description=(
                    "OT security / IDS events on zone-conduit (unexpected writes, "
                    "protocol anomalies). Read-only evidence."
                ),
                input_schema={
                    "type": "object",
                    "properties": {
                        "zone": {"type": "string"},
                        "hours": {"type": "integer", "default": 24},
                    },
                },
                handler=_ot_security,
                tier_hint="low",
            ),
            ToolSpec(
                name="create_emergency_work_order",
                description="Create an emergency Maximo/SAP PM work order. CONSEQUENTIAL. Requires human approval.",
                input_schema={
                    "type": "object",
                    "properties": {
                        "asset_id": {"type": "string"},
                        "priority": {"type": "string"},
                        "description": {"type": "string"},
                    },
                    "required": ["description"],
                },
                handler=_create_wo,
                tier_hint="high",
            ),
            ToolSpec(
                name="recommend_line_shutdown",
                description="Recommend / request a production line shutdown. CONSEQUENTIAL. Requires human approval.",
                input_schema={
                    "type": "object",
                    "properties": {
                        "line_id": {"type": "string"},
                        "reason": {"type": "string"},
                        "urgency": {"type": "string", "enum": ["immediate", "end_of_shift", "planned"]},
                    },
                    "required": ["reason"],
                },
                handler=_shutdown,
                tier_hint="high",
            ),
            ToolSpec(
                name="reorder_critical_parts",
                description="Reorder critical spare parts (spends money). CONSEQUENTIAL. Requires human approval.",
                input_schema={
                    "type": "object",
                    "properties": {
                        "sku": {"type": "string"},
                        "qty": {"type": "integer"},
                        "estimated_cost_sar": {"type": "number"},
                    },
                    "required": ["sku", "qty"],
                },
                handler=_reorder,
                tier_hint="high",
            ),
            ToolSpec(
                name="update_plant_state",
                description="Change plant operating state flags. CONSEQUENTIAL. Requires human approval.",
                input_schema={
                    "type": "object",
                    "properties": {
                        "asset_id": {"type": "string"},
                        "new_state": {"type": "string"},
                    },
                    "required": ["new_state"],
                },
                handler=_plant_state,
                tier_hint="high",
            ),
        ]
        for spec in specs:
            self.register(spec)


def get_default_registry(scenario: str = "conflict") -> ToolRegistry:
    return ToolRegistry(scenario=scenario)
