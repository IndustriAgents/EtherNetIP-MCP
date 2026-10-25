"""Backend support is distinct from device capability and authorization."""

from typing import Any


def capability_report(
    *, writes_enabled: bool, system_commands_enabled: bool = False, json_bridge: bool = False
) -> dict[str, Any]:
    operations = {
        "read_tags": {"supported": True, "enabled": True},
        "write_tags": {"supported": True, "enabled": writes_enabled},
        "get_tag_list": {"supported": True, "enabled": True},
        "list_tags": {"supported": True, "enabled": True},
        "get_connection_status": {"supported": True, "enabled": True},
        "get_plc_info": {"supported": True, "enabled": True},
        "get_plc_time": {"supported": True, "enabled": True},
        "set_plc_time": {"supported": True, "enabled": writes_enabled and system_commands_enabled},
    }
    for operation in operations.values():
        if not operation["enabled"]:
            operation["reason"] = "Disabled by native configuration gates"
    return {
        "protocol": "ethernetip",
        "backend": "json_bridge" if json_bridge else "pycomm3",
        "simulated": json_bridge,
        "scope": "backend_support",
        "device_verified": False,
        "operations": operations,
        "metadata_browse": {
            "max_page_tags": 500,
            "max_page_bytes": 131072,
            "max_snapshot_tags": 20000,
            "max_snapshot_bytes": 4194304,
            "snapshot_ttl_seconds": 60,
            "native_upload_bounded": False,
        },
        "prerequisites": [
            "A configured Logix-compatible controller and valid CIP routing path are required for live operations.",
            "Tag metadata and device access permissions determine which addresses and services are available.",
        ],
        "limitations": [
            "Implicit cyclic I/O, arbitrary CIP services and CIP Security are not implemented.",
            "The JSON bridge is a simulator protocol, not EtherNet/IP wire interoperability.",
            "Batch writes are not atomic; protocol acknowledgement does not verify a physical process.",
        ],
        "authorization": "enabled reflects native configuration only; MCP policy and device permissions still apply",
    }
