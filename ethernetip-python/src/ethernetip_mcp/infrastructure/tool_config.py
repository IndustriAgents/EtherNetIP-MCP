from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from ethernetip_mcp.domain.errors import parse_bool


@dataclass(slots=True)
class ToolConfig:
    """Tool gates. Writes and system commands are off unless enabled."""

    writes_enabled: bool = False
    system_cmds_enabled: bool = False
    tag_map_path: Path | None = None

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> ToolConfig:
        env = os.environ if environ is None else environ
        tag_path = (env.get("TAG_MAP_FILE") or "").strip()
        return cls(
            writes_enabled=parse_bool(env, "ENIP_WRITES_ENABLED", False),
            system_cmds_enabled=parse_bool(env, "ENIP_SYSTEM_CMDS_ENABLED", False),
            tag_map_path=Path(tag_path).expanduser() if tag_path else None,
        )
