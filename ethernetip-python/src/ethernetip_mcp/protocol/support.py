from __future__ import annotations

from mcp.server.fastmcp import FastMCP as FastMCP
from mcp.types import ToolAnnotations as ToolAnnotations

from ethernetip_mcp.domain.model import LOCAL_TOOLS as LOCAL_TOOLS
from ethernetip_mcp.domain.model import WRITE_TOOLS as WRITE_TOOLS

WRITE_ANNOTATIONS = ToolAnnotations(readOnlyHint=False, destructiveHint=True, idempotentHint=False, openWorldHint=True)
DEVICE_READ_ANNOTATIONS = ToolAnnotations(readOnlyHint=True, openWorldHint=True)
LOCAL_READ_ANNOTATIONS = ToolAnnotations(readOnlyHint=True, openWorldHint=False)


def annotations_for(name: str) -> ToolAnnotations:
    if name in WRITE_TOOLS:
        return WRITE_ANNOTATIONS
    return LOCAL_READ_ANNOTATIONS if name in LOCAL_TOOLS else DEVICE_READ_ANNOTATIONS
