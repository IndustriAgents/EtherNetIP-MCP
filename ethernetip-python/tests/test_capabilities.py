"""Backend support and native gates are separate from device authorization."""

import json
from pathlib import Path

from jsonschema import Draft202012Validator

from ethernetip_mcp.domain.capabilities import capability_report


def test_report_matches_advertised_status_schema_and_all_canonical_operations():
    contract = json.loads((Path(__file__).resolve().parents[2] / "contract/tools.json").read_text())
    status = next(t for t in contract["tools"] if t["name"] == "get_connection_status")
    for enabled in (False, True):
        capabilities = capability_report(writes_enabled=enabled, system_commands_enabled=enabled)
        Draft202012Validator(status["outputSchema"]).validate(
            {
                "success": True,
                "data": {"capabilities": capabilities},
                "error": None,
                "meta": {},
            }
        )
        assert set(capabilities["operations"]) == {t["name"] for t in contract["tools"]}
        assert capabilities["protocol"] == contract["protocol"]
        assert capabilities["device_verified"] is False
        assert "MCP policy" in capabilities["authorization"]
        for tool in contract["tools"]:
            operation = capabilities["operations"][tool["name"]]
            assert operation["supported"] is True
            if not tool["annotations"].get("readOnlyHint", False):
                assert operation["enabled"] is enabled


def test_capability_reports_do_not_share_mutable_state():
    first = capability_report(writes_enabled=False)
    first["operations"].clear()
    assert capability_report(writes_enabled=False)["operations"]


def test_mock_bridge_is_explicit_and_system_control_needs_both_gates():
    result = capability_report(writes_enabled=True, json_bridge=True)
    assert result["backend"] == "json_bridge" and result["simulated"] is True
    assert result["operations"]["write_tags"]["enabled"] is True
    assert result["operations"]["set_plc_time"]["enabled"] is False
