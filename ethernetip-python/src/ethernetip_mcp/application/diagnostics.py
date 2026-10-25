from __future__ import annotations

import asyncio
from typing import Annotated, Any

from pydantic import Field

from ethernetip_mcp.domain.capabilities import capability_report
from ethernetip_mcp.domain.errors import EIPClientError
from ethernetip_mcp.domain.model import _error_meta, fail, ok
from ethernetip_mcp.domain.tag_browse import SNAPSHOT_TTL, TagSnapshot


class DiagnosticsOperations:
    def _expire_tag_snapshot(self, snapshot):
        if self.tag_snapshot is snapshot:
            self.tag_snapshot = None
            self.tag_snapshot_expiry = None

    async def get_tag_list(
        self,
        program: Annotated[
            str,
            Field(
                min_length=1,
                description=(
                    "Omit for controller-scoped tags, '*' for every "
                    "tag including program tags, or a program name "
                    "such as 'MainProgram'."
                ),
            ),
        ]
        | None = None,
        limit: Annotated[int, Field(strict=True, ge=1, le=500)] = 100,
        prefix: Annotated[str, Field(max_length=256)] = "",
        cursor: Annotated[str, Field(min_length=1, max_length=64)] | None = None,
    ) -> dict[str, Any]:
        """List tag definitions (name and data type) uploaded from the controller."""
        try:
            meta = {}
            if cursor is None:
                tags, meta = await self.client.get_tag_list(program)
                snapshot = TagSnapshot.capture(tags, program, prefix)
                self.tag_snapshot = snapshot
                if self.tag_snapshot_expiry is not None:
                    self.tag_snapshot_expiry.cancel()
                self.tag_snapshot_expiry = asyncio.get_running_loop().call_later(
                    SNAPSHOT_TTL, self._expire_tag_snapshot, snapshot
                )
            else:
                snapshot = self.tag_snapshot
                if snapshot is None:
                    raise ValueError("Unknown tag cursor; start a new browse without cursor")
            data, page_meta = snapshot.page(program, prefix, limit, cursor)
        except (EIPClientError, ValueError) as exc:
            return fail(str(exc), _error_meta(exc, tool="get_tag_list", program=program))
        return ok(data, {**meta, **page_meta})

    async def ping(self) -> dict[str, Any]:
        """Check that the controller (or mock) answers, by requesting its identity."""
        try:
            info, meta = await self.client.ping()
        except EIPClientError as exc:
            return fail(str(exc), _error_meta(exc, tool="ping", connection=self.client.connection_status()))
        return ok(
            {
                "reachable": True,
                "latency_ms": meta.get("duration_ms"),
                "product_name": info.get("product_name"),
                "connection": self.client.connection_status(),
                "writes_enabled": self.config.writes_enabled,
                "system_cmds_enabled": self.config.system_cmds_enabled,
                "tag_aliases": self.tag_map.count(),
            },
            meta,
        )

    async def get_connection_status(self) -> dict[str, Any]:
        """Report the configured target and the outcome of the last exchange, without contacting the device."""
        return ok(
            {
                **self.client.connection_status(),
                "writes_enabled": self.config.writes_enabled,
                "system_cmds_enabled": self.config.system_cmds_enabled,
                "tag_aliases": self.tag_map.count(),
                "tag_map_error": self.tag_map.error,
                "capabilities": capability_report(
                    writes_enabled=self.config.writes_enabled,
                    system_commands_enabled=self.config.system_cmds_enabled,
                    json_bridge=self.client.config.json_bridge,
                ),
            }
        )

    async def get_plc_info(self) -> dict[str, Any]:
        """Read the controller identity: name, vendor, product, revision (firmware), serial and keyswitch."""
        try:
            info, meta = await self.client.get_controller_info()
        except EIPClientError as exc:
            return fail(str(exc), _error_meta(exc, tool="get_plc_info"))
        return ok(info, meta)

    async def get_plc_time(self) -> dict[str, Any]:
        """Read the controller's wall clock."""
        try:
            payload, meta = await self.client.get_plc_time()
        except EIPClientError as exc:
            return fail(str(exc), _error_meta(exc, tool="get_plc_time"))
        return ok(payload, meta)
