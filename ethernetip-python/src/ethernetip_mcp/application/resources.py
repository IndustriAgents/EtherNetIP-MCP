from __future__ import annotations

from dataclasses import dataclass

from ethernetip_mcp.domain.ports import EIPClientPort
from ethernetip_mcp.infrastructure.maps import TagMap
from ethernetip_mcp.infrastructure.tool_config import ToolConfig


@dataclass(slots=True)
class ToolResources:
    client: EIPClientPort
    config: ToolConfig
    tag_map: TagMap | None = None
