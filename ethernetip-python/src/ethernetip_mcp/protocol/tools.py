from __future__ import annotations

from typing import Annotated, Any

from pydantic import Field

from ethernetip_mcp.application.resources import ToolResources
from ethernetip_mcp.domain.model import Alias, DataType, ElementCount, TagName
from ethernetip_mcp.infrastructure.maps import TagMap

from ..application.operations import Operations
from .support import FastMCP, annotations_for


def register_tools(server: FastMCP, resources: ToolResources) -> None:
    config = resources.config
    resources.tag_map or TagMap(config.tag_map_path)
    operations = Operations(resources, lambda: None)

    @server.tool(annotations=annotations_for("read_tag"))
    async def read_tag(tag_name: TagName, count: ElementCount | None = None) -> dict[str, Any]:
        """Read one tag from the controller.

        Returns data {tag, value, data_type}. With `count`, reads that many array
        elements and returns them as a list (same as read_array). Fails with
        success=false if the controller rejects the tag.
        """
        return await operations.read_tag(tag_name, count)

    @server.tool(annotations=annotations_for("write_tag"))
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
        return await operations.write_tag(tag_name, value, data_type)

    @server.tool(annotations=annotations_for("read_array"))
    async def read_array(tag_name: TagName, elements: ElementCount) -> dict[str, Any]:
        """Read `elements` consecutive elements of an array tag as a list.

        Starts at element 0, or at the index in the tag name (e.g. 'Tank_Levels[1]').
        """
        return await operations.read_array(tag_name, elements)

    @server.tool(annotations=annotations_for("write_array"))
    async def write_array(tag_name: TagName, values: Annotated[list[Any], Field(min_length=1)]) -> dict[str, Any]:
        """Write a list of values to consecutive array elements. Refused unless ENIP_WRITES_ENABLED=true.

        Writes len(values) elements starting at element 0, or at the index in
        the tag name (e.g. 'Tank_Levels[1]').
        """
        return await operations.write_array(tag_name, values)

    @server.tool(annotations=annotations_for("read_string"))
    async def read_string(tag_name: TagName) -> dict[str, Any]:
        """Read a STRING tag. Fails with success=false if the tag does not hold a string."""
        return await operations.read_string(tag_name)

    @server.tool(annotations=annotations_for("write_string"))
    async def write_string(tag_name: TagName, value: str) -> dict[str, Any]:
        """Write text to a STRING tag. Refused unless ENIP_WRITES_ENABLED=true."""
        return await operations.write_string(tag_name, value)

    @server.tool(annotations=annotations_for("get_tag_list"))
    async def get_tag_list(
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
    ) -> dict[str, Any]:
        """List tag definitions (name and data type) uploaded from the controller.

        Scope follows pycomm3: no `program` lists controller-scoped tags, '*'
        lists all tags, and a program name lists that program's tags (named
        'Program:<name>.<tag>').
        """
        return await operations.get_tag_list(program)

    @server.tool(annotations=annotations_for("read_multiple_tags"))
    async def read_multiple_tags(
        tags: Annotated[list[Annotated[str, Field(min_length=1)]], Field(min_length=1)],
    ) -> dict[str, Any]:
        """Read several tags in one call.

        data.results has one {tag, value, data_type, error} entry per tag, in
        order. success is true only if every read succeeded.
        """
        return await operations.read_multiple_tags(tags)

    @server.tool(annotations=annotations_for("write_multiple_tags"))
    async def write_multiple_tags(
        payloads: Annotated[
            list[dict[str, Any]],
            Field(
                min_length=1, description="Entries of the form {'tag_name': ..., 'value': ..., 'data_type': optional}."
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
        return await operations.write_multiple_tags(payloads)

    @server.tool(annotations=annotations_for("list_tags"))
    async def list_tags() -> dict[str, Any]:
        """List the aliases defined in the TAG_MAP_FILE tag map, with their tags and scaling."""
        return await operations.list_tags()

    @server.tool(annotations=annotations_for("read_tag_by_alias"))
    async def read_tag_by_alias(alias: Alias) -> dict[str, Any]:
        """Read the tag behind a tag-map alias, converted to engineering units if the alias defines scaling.

        data.raw_value is the controller value, data.value the scaled one.
        """
        return await operations.read_tag_by_alias(alias)

    @server.tool(annotations=annotations_for("write_tag_by_alias"))
    async def write_tag_by_alias(alias: Alias, value: Any) -> dict[str, Any]:
        """Write the tag behind a tag-map alias. Refused unless ENIP_WRITES_ENABLED=true.

        `value` is in engineering units if the alias defines scaling; it is then
        converted to raw units, rounded for integer data types, before writing.
        """
        return await operations.write_tag_by_alias(alias, value)

    @server.tool(annotations=annotations_for("ping"))
    async def ping() -> dict[str, Any]:
        """Check that the controller (or mock) answers, by requesting its identity.

        Fails with success=false if it does not answer. Also reports the write
        and system-command gates.
        """
        return await operations.ping()

    @server.tool(annotations=annotations_for("get_connection_status"))
    async def get_connection_status() -> dict[str, Any]:
        """Report the configured target and the outcome of the last exchange, without contacting the device.

        Use ping to test the device now.
        """
        return await operations.get_connection_status()

    @server.tool(annotations=annotations_for("get_plc_info"))
    async def get_plc_info() -> dict[str, Any]:
        """Read the controller identity: name, vendor, product, revision (firmware), serial and keyswitch."""
        return await operations.get_plc_info()

    @server.tool(annotations=annotations_for("get_plc_time"))
    async def get_plc_time() -> dict[str, Any]:
        """Read the controller's wall clock.

        data.plc_time is ISO 8601 without a time zone, as the controller reports
        it; data.microseconds is the raw value (µs since 1970-01-01).
        """
        return await operations.get_plc_time()

    @server.tool(annotations=annotations_for("set_plc_time"))
    async def set_plc_time() -> dict[str, Any]:
        """Set the controller's wall clock to this host's current time.

        Refused unless both ENIP_WRITES_ENABLED=true and ENIP_SYSTEM_CMDS_ENABLED=true.
        Not retried once sent: if the reply is lost, the error says the clock
        may have been set.
        """
        return await operations.set_plc_time()

    from .contract import install_contract

    install_contract(server)
