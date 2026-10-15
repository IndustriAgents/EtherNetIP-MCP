from __future__ import annotations

from typing import Any

from ethernetip_mcp.domain.model import fail, ok


class MetadataOperations:
    async def list_tags(self) -> dict[str, Any]:
        """List the aliases defined in the TAG_MAP_FILE tag map, with their tags and scaling."""
        aliases = self.tag_map.list()
        if self.tag_map.error:
            return fail(self.tag_map.error, {"tool": "list_tags"})
        meta = {"tag_map_file": str(self.tag_map.path) if self.tag_map.path else None}
        return ok({"aliases": aliases, "count": len(aliases)}, meta)
