"""Tool registry smoke tests."""

from aiot_agent.tools.registry import get_default_registry


def test_all_expected_tools_registered():
    reg = get_default_registry("conflict")
    names = {t["name"] for t in reg.list_tools()}
    expected = {
        "iot_historian_query",
        "maximo_asset_lookup",
        "maximo_maintenance_history",
        "sap_pm_work_orders",
        "fleet_failure_history",
        "parts_inventory_check",
        "repair_recipe_lookup",
        "technician_notes",
        "tribal_field_observations",
        "get_device_connectivity",
        "get_private_network_health",
        "get_gateway_status",
        "get_plc_tag_snapshot",
        "get_ot_security_events",
        "create_emergency_work_order",
        "recommend_line_shutdown",
        "reorder_critical_parts",
        "update_plant_state",
    }
    assert expected <= names


def test_conflict_and_true_positive_data_differ():
    c = get_default_registry("conflict")
    t = get_default_registry("true_positive")
    c_iot = c.execute("iot_historian_query", {})
    t_iot = t.execute("iot_historian_query", {})
    assert c_iot["result"]["samples"][-1]["vibration_mm_s"] < t_iot["result"]["samples"][-1]["vibration_mm_s"]

    c_notes = c.execute("technician_notes", {})
    assert "recalibrat" in str(c_notes).lower()
    t_notes = t.execute("technician_notes", {})
    assert "growl" in str(t_notes).lower() or "mechanical" in str(t_notes).lower()
