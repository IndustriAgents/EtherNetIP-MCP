from __future__ import annotations

from typing import Any

from ethernetip_mcp.domain.errors import EIPClientError
from ethernetip_mcp.domain.model import NOT_SENT, _error_meta, fail, ok
from ethernetip_mcp.infrastructure.maps import TagMap


class CommonOperations:
    def __init__(self, resources, cancel_scope):
        self.resources = resources
        self.cancel_scope = cancel_scope
        self.client = self.resources.client
        self.config = self.resources.config
        self.tag_map = self.resources.tag_map or TagMap(self.config.tag_map_path)

    def writes_refused(self, tool: str) -> dict[str, Any] | None:
        if not self.config.writes_enabled:
            return fail(
                "Write operations are disabled (set ENIP_WRITES_ENABLED=true to allow them)", {"tool": tool, **NOT_SENT}
            )
        return None

    def system_refused(self, tool: str) -> dict[str, Any] | None:
        missing = [
            name
            for name, enabled in (
                ("ENIP_WRITES_ENABLED", self.config.writes_enabled),
                ("ENIP_SYSTEM_CMDS_ENABLED", self.config.system_cmds_enabled),
            )
            if not enabled
        ]
        if missing:
            return fail(
                (
                    f"{tool}"
                    " changes the controller and is disabled: it "
                    "needs ENIP_WRITES_ENABLED=true and "
                    "ENIP_SYSTEM_CMDS_ENABLED=true (not set: "
                    f"{', '.join(missing)}"
                    ")"
                ),
                {"tool": tool, **NOT_SENT},
            )
        return None

    def alias_spec(self, alias: str, tool: str) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
        spec = self.tag_map.get(alias.strip())
        if self.tag_map.error:
            return (None, fail(self.tag_map.error, {"tool": tool, "alias": alias}))
        if spec is None:
            known = "none defined" if self.tag_map.path is None else f"{self.tag_map.count()} defined"
            return (None, fail(f"Unknown alias '{alias}' ({known}; see list_tags)", {"tool": tool, "alias": alias}))
        tag = spec.get("tag")
        if not isinstance(tag, str) or not tag.strip():
            return (None, fail(f"Alias '{alias}' has no 'tag' field in the tag map", {"tool": tool, "alias": alias}))
        data_type = spec.get("data_type")
        if data_type is not None and (not isinstance(data_type, str) or not data_type.strip()):
            return (
                None,
                fail(
                    f"Alias '{alias}' has an invalid 'data_type' in the tag map (expected a type name such as REAL)",
                    {"tool": tool, "alias": alias},
                ),
            )
        return (spec, None)

    async def do_read(self, tool: str, tag_name: str, count: int | None) -> dict[str, Any]:
        try:
            result, meta = await self.client.read_tag(tag_name, count)
        except (EIPClientError, ValueError) as exc:
            return fail(str(exc), _error_meta(exc, tool=tool, tag_name=tag_name))
        return ok(result, {**meta, "tool": tool, "tag_name": tag_name})

    async def do_write(self, tool: str, tag_name: str, value: Any, data_type: str | None) -> dict[str, Any]:
        refused = self.writes_refused(tool)
        if refused:
            return refused
        try:
            result, meta = await self.client.write_tag(tag_name, value, data_type)
        except ValueError as exc:
            return fail(str(exc), {"tool": tool, "tag_name": tag_name, **NOT_SENT})
        except EIPClientError as exc:
            return fail(str(exc), _error_meta(exc, tool=tool, tag_name=tag_name))
        return ok(result, {**meta, "tool": tool, "tag_name": tag_name})
