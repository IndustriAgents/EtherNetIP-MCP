"""EtherNet/IP MCP tool definitions.

Every tool returns the IndustriConnect envelope ``{success, data, error, meta}``.
Device errors and bad arguments come back as ``success: false``; nothing here
reports success for an operation the device did not confirm.
"""

from __future__ import annotations

import json
import math
import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Any

from mcp.server.fastmcp import FastMCP
from pydantic import Field, StrictInt

from .eip_client import EIPClient, EIPClientError, parse_bool

TagName = Annotated[
    str,
    Field(
        min_length=1,
        description="Controller tag, e.g. 'MotorSpeed' or 'Program:MainProgram.MotorSpeed'. "
        "Array elements and members use Logix syntax: 'Tank_Levels[2]', 'Conveyor_Status.Running'.",
    ),
]
# StrictInt: true, "2" and 2.0 are refused; booleans are never counts.
ElementCount = Annotated[
    StrictInt, Field(ge=1, description="Number of consecutive array elements, starting at the tag's index.")
]
Alias = Annotated[str, Field(min_length=1, description="Alias defined in the TAG_MAP_FILE tag map.")]
DataType = Annotated[str, Field(min_length=1, description="Expected Logix data type, e.g. REAL, DINT, BOOL, STRING.")]

_INTEGER_TYPES = {"SINT", "INT", "DINT", "LINT", "USINT", "UINT", "UDINT", "ULINT"}

# Tools that change the device. Their refusals carry outcome/request_sent too.
WRITE_TOOLS = frozenset(
    {"write_tag", "write_array", "write_string", "write_multiple_tags", "write_tag_by_alias", "set_plc_time"}
)
NOT_SENT: dict[str, Any] = {"outcome": "not_sent", "request_sent": False}


def envelope(
    success: bool, data: Any = None, error: str | None = None, meta: Mapping[str, Any] | None = None
) -> dict[str, Any]:
    """Build the shared ``{success, data, error, meta}`` response."""
    return {"success": success, "data": data, "error": error, "meta": dict(meta or {})}


def ok(data: Any = None, meta: Mapping[str, Any] | None = None) -> dict[str, Any]:
    return envelope(True, data=data, meta=meta)


def fail(message: str, meta: Mapping[str, Any] | None = None, data: Any = None) -> dict[str, Any]:
    return envelope(False, data=data, error=message, meta=meta)


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


class TagMap:
    """Aliases loaded from ``TAG_MAP_FILE``; reloaded when the file changes."""

    def __init__(self, path: Path | None) -> None:
        self.path = path
        self.error: str | None = None
        self._tags: dict[str, dict[str, Any]] = {}
        self._stamp: tuple[int, int] | None = None
        self.refresh()

    def refresh(self) -> None:
        if not self.path:
            self._tags, self._stamp, self.error = {}, None, None
            return
        try:
            stat = self.path.stat()
        except OSError as exc:
            self._tags, self._stamp = {}, None
            self.error = f"TAG_MAP_FILE {self.path} cannot be read: {exc.strerror or exc}"
            return
        stamp = (stat.st_mtime_ns, stat.st_size)
        if stamp == self._stamp:
            return
        self._stamp = stamp
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            self._tags = {}
            self.error = f"TAG_MAP_FILE {self.path} is not valid JSON: {exc}"
            return
        if not isinstance(data, dict) or not all(isinstance(spec, dict) for spec in data.values()):
            self._tags = {}
            self.error = f"TAG_MAP_FILE {self.path} must be a JSON object mapping each alias to an object"
            return
        self._tags = {str(alias): spec for alias, spec in data.items()}
        self.error = None

    def get(self, alias: str) -> dict[str, Any] | None:
        self.refresh()
        return self._tags.get(alias)

    def list(self) -> list[dict[str, Any]]:
        self.refresh()
        return [
            {
                "alias": alias,
                "tag": spec.get("tag"),
                "data_type": spec.get("data_type"),
                "description": spec.get("description"),
                "scaling": spec.get("scaling"),
            }
            for alias, spec in self._tags.items()
        ]

    def count(self) -> int:
        self.refresh()
        return len(self._tags)


@dataclass(slots=True)
class ToolResources:
    client: EIPClient
    config: ToolConfig
    tag_map: TagMap | None = None


def _scaling(spec: Mapping[str, Any]) -> tuple[float, float, float, float] | None:
    scaling = spec.get("scaling")
    if not scaling:
        return None
    if not isinstance(scaling, Mapping):
        raise ValueError("'scaling' must be an object")
    if any(isinstance(v, bool) for v in scaling.values()):
        raise ValueError("'scaling' values must be numbers, not booleans")
    try:
        raw_min = float(scaling.get("raw_min", 0))
        raw_max = float(scaling.get("raw_max", 1))
        eng_min = float(scaling.get("eng_min", raw_min))
        eng_max = float(scaling.get("eng_max", raw_max))
    except (TypeError, ValueError):
        raise ValueError("'scaling' values must be numbers") from None
    if not all(math.isfinite(v) for v in (raw_min, raw_max, eng_min, eng_max)):
        raise ValueError("'scaling' values must be finite numbers")
    if raw_max == raw_min or eng_max == eng_min:
        raise ValueError("'scaling' has a zero span (raw_min == raw_max or eng_min == eng_max)")
    return raw_min, raw_max, eng_min, eng_max


def _scale(value: Any, scaling: tuple[float, float, float, float] | None, to_engineering: bool) -> Any:
    if scaling is None:
        return value
    if isinstance(value, list):
        return [_scale(item, scaling, to_engineering) for item in value]
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"cannot scale the non-numeric value {value!r}")
    raw_min, raw_max, eng_min, eng_max = scaling
    if to_engineering:
        return eng_min + (value - raw_min) / (raw_max - raw_min) * (eng_max - eng_min)
    return raw_min + (value - eng_min) / (eng_max - eng_min) * (raw_max - raw_min)


def _round_for_type(value: Any, data_type: Any) -> Any:
    if not isinstance(data_type, str) or data_type.split("[", 1)[0].strip().upper() not in _INTEGER_TYPES:
        return value
    if isinstance(value, list):
        return [_round_for_type(item, data_type) for item in value]
    if isinstance(value, float):
        return int(round(value))
    return value


def _error_meta(exc: Exception, **extra: Any) -> dict[str, Any]:
    return {**getattr(exc, "meta", {}), **extra}


def register_tools(server: FastMCP, resources: ToolResources) -> None:
    client = resources.client
    config = resources.config
    tag_map = resources.tag_map or TagMap(config.tag_map_path)

    def writes_refused(tool: str) -> dict[str, Any] | None:
        if not config.writes_enabled:
            return fail(
                "Write operations are disabled (set ENIP_WRITES_ENABLED=true to allow them)", {"tool": tool, **NOT_SENT}
            )
        return None

    def system_refused(tool: str) -> dict[str, Any] | None:
        # System commands change the controller, so they need both gates.
        missing = [
            name
            for name, enabled in (
                ("ENIP_WRITES_ENABLED", config.writes_enabled),
                ("ENIP_SYSTEM_CMDS_ENABLED", config.system_cmds_enabled),
            )
            if not enabled
        ]
        if missing:
            return fail(
                f"{tool} changes the controller and is disabled: it needs ENIP_WRITES_ENABLED=true and "
                f"ENIP_SYSTEM_CMDS_ENABLED=true (not set: {', '.join(missing)})",
                {"tool": tool, **NOT_SENT},
            )
        return None

    def alias_spec(alias: str, tool: str) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
        spec = tag_map.get(alias.strip())
        if tag_map.error:
            return None, fail(tag_map.error, {"tool": tool, "alias": alias})
        if spec is None:
            known = "none defined" if tag_map.path is None else f"{tag_map.count()} defined"
            return None, fail(f"Unknown alias '{alias}' ({known}; see list_tags)", {"tool": tool, "alias": alias})
        tag = spec.get("tag")
        if not isinstance(tag, str) or not tag.strip():
            return None, fail(f"Alias '{alias}' has no 'tag' field in the tag map", {"tool": tool, "alias": alias})
        return spec, None

    async def do_read(tool: str, tag_name: str, count: int | None) -> dict[str, Any]:
        try:
            result, meta = await client.read_tag(tag_name, count)
        except (EIPClientError, ValueError) as exc:
            return fail(str(exc), _error_meta(exc, tool=tool, tag_name=tag_name))
        return ok(result, {**meta, "tool": tool, "tag_name": tag_name})

    async def do_write(tool: str, tag_name: str, value: Any, data_type: str | None) -> dict[str, Any]:
        refused = writes_refused(tool)
        if refused:
            return refused
        try:
            result, meta = await client.write_tag(tag_name, value, data_type)
        except ValueError as exc:  # bad input, found before anything was sent
            return fail(str(exc), {"tool": tool, "tag_name": tag_name, **NOT_SENT})
        except EIPClientError as exc:  # meta carries outcome and request_sent
            return fail(str(exc), _error_meta(exc, tool=tool, tag_name=tag_name))
        return ok(result, {**meta, "tool": tool, "tag_name": tag_name})

    @server.tool()
    async def read_tag(tag_name: TagName, count: ElementCount | None = None) -> dict[str, Any]:
        """Read one tag from the controller.

        Returns data {tag, value, data_type}. With `count`, reads that many array
        elements and returns them as a list (same as read_array). Fails with
        success=false if the controller rejects the tag.
        """
        return await do_read("read_tag", tag_name, count)

    @server.tool()
    async def write_tag(tag_name: TagName, value: Any, data_type: DataType | None = None) -> dict[str, Any]:
        """Write one tag on the controller. Refused unless ENIP_WRITES_ENABLED=true.

        Sent at most once: if the reply is lost, the result is success=false
        with meta.outcome='unknown' (the write may have been applied; read the
        tag back before writing again).

        `value` is a number, boolean, string, list (written to consecutive array
        elements) or object (structure members). On a real controller pycomm3
        encodes the value with the controller's own type for the tag, so
        `data_type` is only checked: the mock rejects a mismatch, and over CIP a
        mismatch is reported in meta.warning. Writes change a running process.
        """
        return await do_write("write_tag", tag_name, value, data_type)

    @server.tool()
    async def read_array(tag_name: TagName, elements: ElementCount) -> dict[str, Any]:
        """Read `elements` consecutive elements of an array tag as a list.

        Starts at element 0, or at the index in the tag name (e.g. 'Tank_Levels[1]').
        """
        return await do_read("read_array", tag_name, elements)

    @server.tool()
    async def write_array(tag_name: TagName, values: Annotated[list[Any], Field(min_length=1)]) -> dict[str, Any]:
        """Write a list of values to consecutive array elements. Refused unless ENIP_WRITES_ENABLED=true.

        Writes len(values) elements starting at element 0, or at the index in
        the tag name (e.g. 'Tank_Levels[1]').
        """
        return await do_write("write_array", tag_name, values, None)

    @server.tool()
    async def read_string(tag_name: TagName) -> dict[str, Any]:
        """Read a STRING tag. Fails with success=false if the tag does not hold a string."""
        response = await do_read("read_string", tag_name, None)
        if response["success"] and not isinstance(response["data"]["value"], str):
            data = response["data"]
            return fail(
                f"Tag '{data['tag']}' is not a string (data_type {data.get('data_type')}); use read_tag",
                {**response["meta"]},
            )
        return response

    @server.tool()
    async def write_string(tag_name: TagName, value: str) -> dict[str, Any]:
        """Write text to a STRING tag. Refused unless ENIP_WRITES_ENABLED=true."""
        return await do_write("write_string", tag_name, value, None)

    @server.tool()
    async def get_tag_list(
        program: Annotated[
            str,
            Field(
                min_length=1,
                description="Omit for controller-scoped tags, '*' for every tag including program tags, "
                "or a program name such as 'MainProgram'.",
            ),
        ]
        | None = None,
    ) -> dict[str, Any]:
        """List tag definitions (name and data type) uploaded from the controller.

        Scope follows pycomm3: no `program` lists controller-scoped tags, '*'
        lists all tags, and a program name lists that program's tags (named
        'Program:<name>.<tag>').
        """
        try:
            tags, meta = await client.get_tag_list(program)
        except (EIPClientError, ValueError) as exc:
            return fail(str(exc), _error_meta(exc, tool="get_tag_list", program=program))
        return ok({"program": program, "tags": tags}, meta)

    @server.tool()
    async def read_multiple_tags(
        tags: Annotated[list[Annotated[str, Field(min_length=1)]], Field(min_length=1)],
    ) -> dict[str, Any]:
        """Read several tags in one call.

        data.results has one {tag, value, data_type, error} entry per tag, in
        order. success is true only if every read succeeded.
        """
        try:
            results, meta = await client.read_multiple_tags(tags)
        except (EIPClientError, ValueError) as exc:
            return fail(str(exc), _error_meta(exc, tool="read_multiple_tags", tags=tags))
        meta = {**meta, "count": len(results)}
        failed = [r for r in results if r["error"]]
        if failed:
            detail = "; ".join(f"{r['tag']}: {r['error']}" for r in failed)
            return fail(f"{len(failed)} of {len(results)} reads failed: {detail}", meta, {"results": results})
        return ok({"results": results}, meta)

    @server.tool()
    async def write_multiple_tags(
        payloads: Annotated[
            list[dict[str, Any]],
            Field(
                min_length=1,
                description="Entries of the form {'tag_name': ..., 'value': ..., 'data_type': optional}.",
            ),
        ],
    ) -> dict[str, Any]:
        """Write several tags in one call. Refused unless ENIP_WRITES_ENABLED=true.

        data.results always has one {tag, value, data_type, error, outcome,
        request_sent} entry per payload. outcome is 'written', 'rejected' (the
        device refused it), 'unknown' (it may have been applied but was not
        confirmed; read it back) or 'not_sent'. success is true only if every
        entry was written. The writes are not atomic and are never re-sent.
        """
        refused = writes_refused("write_multiple_tags")
        if refused:
            return refused
        items: list[tuple[str, Any, str | None]] = []
        seen: set[str] = set()
        for index, item in enumerate(payloads):
            tag = item.get("tag_name", item.get("tag"))
            if not isinstance(tag, str) or not tag.strip():
                return fail(
                    f"payloads[{index}] needs a non-empty 'tag_name'", {"tool": "write_multiple_tags", **NOT_SENT}
                )
            if item.get("value") is None:
                return fail(f"payloads[{index}] ({tag}) needs a 'value'", {"tool": "write_multiple_tags", **NOT_SENT})
            if tag.strip() in seen:
                return fail(f"payloads lists '{tag}' more than once", {"tool": "write_multiple_tags", **NOT_SENT})
            data_type = item.get("data_type")
            if data_type is not None and (not isinstance(data_type, str) or not data_type.strip()):
                return fail(
                    f"payloads[{index}] ({tag}) has an invalid 'data_type'", {"tool": "write_multiple_tags", **NOT_SENT}
                )
            seen.add(tag.strip())
            items.append((tag.strip(), item["value"], data_type))
        try:
            results, meta = await client.write_multiple_tags(items)
        except ValueError as exc:  # bad input, found before anything was sent
            return fail(str(exc), {"tool": "write_multiple_tags", **NOT_SENT})
        meta = {**meta, "count": len(results)}
        failed = [r for r in results if r["outcome"] != "written"]
        if failed:
            detail = "; ".join(f"{r['tag']} ({r['outcome']}): {r['error']}" for r in failed)
            return fail(
                f"{len(failed)} of {len(results)} writes were not confirmed: {detail}", meta, {"results": results}
            )
        return ok({"results": results}, meta)

    @server.tool()
    async def list_tags() -> dict[str, Any]:
        """List the aliases defined in the TAG_MAP_FILE tag map, with their tags and scaling."""
        aliases = tag_map.list()
        if tag_map.error:
            return fail(tag_map.error, {"tool": "list_tags"})
        meta = {"tag_map_file": str(tag_map.path) if tag_map.path else None}
        return ok({"aliases": aliases, "count": len(aliases)}, meta)

    @server.tool()
    async def read_tag_by_alias(alias: Alias) -> dict[str, Any]:
        """Read the tag behind a tag-map alias, converted to engineering units if the alias defines scaling.

        data.raw_value is the controller value, data.value the scaled one.
        """
        spec, error = alias_spec(alias, "read_tag_by_alias")
        if error:
            return error
        response = await do_read("read_tag_by_alias", spec["tag"], None)
        if not response["success"]:
            return response
        data = response["data"]
        try:
            scaled = _scale(data["value"], _scaling(spec), to_engineering=True)
        except ValueError as exc:
            return fail(f"Alias '{alias}': {exc}", response["meta"])
        return ok({**data, "alias": alias, "raw_value": data["value"], "value": scaled}, response["meta"])

    @server.tool()
    async def write_tag_by_alias(alias: Alias, value: Any) -> dict[str, Any]:
        """Write the tag behind a tag-map alias. Refused unless ENIP_WRITES_ENABLED=true.

        `value` is in engineering units if the alias defines scaling; it is then
        converted to raw units, rounded for integer data types, before writing.
        """
        refused = writes_refused("write_tag_by_alias")
        if refused:
            return refused
        spec, error = alias_spec(alias, "write_tag_by_alias")
        if error:
            error["meta"].update(NOT_SENT)
            return error
        try:
            scaling = _scaling(spec)
            raw = _scale(value, scaling, to_engineering=False)
            if scaling is not None:
                raw = _round_for_type(raw, spec.get("data_type"))
        except ValueError as exc:
            return fail(f"Alias '{alias}': {exc}", {"tool": "write_tag_by_alias", "alias": alias, **NOT_SENT})
        response = await do_write("write_tag_by_alias", spec["tag"], raw, spec.get("data_type"))
        if not response["success"]:
            return response
        return ok({**response["data"], "alias": alias, "raw_value": raw, "value": value}, response["meta"])

    @server.tool()
    async def ping() -> dict[str, Any]:
        """Check that the controller (or mock) answers, by requesting its identity.

        Fails with success=false if it does not answer. Also reports the write
        and system-command gates.
        """
        try:
            info, meta = await client.ping()
        except EIPClientError as exc:
            return fail(str(exc), _error_meta(exc, tool="ping", connection=client.connection_status()))
        return ok(
            {
                "reachable": True,
                "latency_ms": meta.get("duration_ms"),
                "product_name": info.get("product_name"),
                "connection": client.connection_status(),
                "writes_enabled": config.writes_enabled,
                "system_cmds_enabled": config.system_cmds_enabled,
                "tag_aliases": tag_map.count(),
            },
            meta,
        )

    @server.tool()
    async def get_connection_status() -> dict[str, Any]:
        """Report the configured target and the outcome of the last exchange, without contacting the device.

        Use ping to test the device now.
        """
        return ok(client.connection_status())

    @server.tool()
    async def get_plc_info() -> dict[str, Any]:
        """Read the controller identity: name, vendor, product, revision (firmware), serial and keyswitch."""
        try:
            info, meta = await client.get_controller_info()
        except EIPClientError as exc:
            return fail(str(exc), _error_meta(exc, tool="get_plc_info"))
        return ok(info, meta)

    @server.tool()
    async def get_plc_time() -> dict[str, Any]:
        """Read the controller's wall clock.

        data.plc_time is ISO 8601 without a time zone, as the controller reports
        it; data.microseconds is the raw value (µs since 1970-01-01).
        """
        try:
            payload, meta = await client.get_plc_time()
        except EIPClientError as exc:
            return fail(str(exc), _error_meta(exc, tool="get_plc_time"))
        return ok(payload, meta)

    @server.tool()
    async def set_plc_time() -> dict[str, Any]:
        """Set the controller's wall clock to this host's current time.

        Refused unless both ENIP_WRITES_ENABLED=true and ENIP_SYSTEM_CMDS_ENABLED=true.
        Not retried once sent: if the reply is lost, the error says the clock
        may have been set.
        """
        refused = system_refused("set_plc_time")
        if refused:
            return refused
        try:
            payload, meta = await client.set_plc_time()
        except EIPClientError as exc:
            return fail(str(exc), _error_meta(exc, tool="set_plc_time"))
        return ok({"updated": True, **payload}, meta)
