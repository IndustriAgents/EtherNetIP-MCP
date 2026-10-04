"""FastMCP wiring for the EtherNet/IP tools."""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass
from importlib.metadata import PackageNotFoundError, version
from typing import Any

from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.exceptions import ToolError
from pydantic import ValidationError

from .eip_client import EIPClient
from .tools import NOT_SENT, WRITE_TOOLS, TagMap, ToolConfig, ToolResources, fail, register_tools

logger = logging.getLogger(__name__)


def package_version() -> str:
    try:
        return version("ethernetip-mcp")
    except PackageNotFoundError:  # pragma: no cover - running from a bare source tree
        return "0.0.0+unknown"


def _describe_validation_error(exc: ValidationError) -> str:
    problems = []
    for error in exc.errors():
        location = ".".join(str(part) for part in error.get("loc", ())) or "arguments"
        problems.append(f"{location}: {error.get('msg', 'invalid value')}")
    return "; ".join(problems)


class EnvelopeFastMCP(FastMCP):
    """FastMCP that answers bad arguments and unexpected errors with the envelope.

    Stock FastMCP turns a pydantic validation error into a bare ``isError``
    text result. Clients of the IndustriConnect servers expect
    ``{success: false, error}`` instead, so arguments are validated here first.
    Relies on mcp 1.x internals (``_tool_manager``, ``fn_metadata``); the
    dependency is pinned to ``mcp<2``.
    """

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> Sequence[Any] | dict[str, Any]:
        tool = self._tool_manager.get_tool(name)
        if tool is None:
            return await super().call_tool(name, arguments)
        metadata = tool.fn_metadata
        try:
            metadata.arg_model.model_validate(metadata.pre_parse_json(arguments or {}))
        except ValidationError as exc:
            message = f"Invalid arguments for {name}: {_describe_validation_error(exc)}"
            meta = {"tool": name, **(NOT_SENT if name in WRITE_TOOLS else {})}
            return metadata.convert_result(fail(message, meta))
        try:
            return await super().call_tool(name, arguments)
        except ToolError as exc:
            logger.exception("Tool %s raised an unexpected error", name)
            cause = exc.__cause__ or exc
            message = f"Internal error in {name}: {type(cause).__name__}: {cause}"
            meta: dict[str, Any] = {"tool": name}
            if name in WRITE_TOOLS:
                # The failure may have come after the request went out.
                message += ". The write may have been applied; read the value back before trying again."
                meta.update({"outcome": "unknown", "request_sent": True})
            return metadata.convert_result(fail(message, meta))


@dataclass(slots=True)
class AppContext:
    client: EIPClient


class EtherNetIPMCPServer:
    """Builds the client and tool gates from the environment and registers the tools.

    Construct it after ``load_dotenv()`` (``cli.main`` does), since it reads
    the configuration here, not at import time. Nothing is sent to the device
    until the MCP lifespan starts.
    """

    def __init__(
        self,
        client: EIPClient | None = None,
        tool_config: ToolConfig | None = None,
    ) -> None:
        self.client = client or EIPClient()
        self.tool_config = tool_config or ToolConfig.from_env()
        self.resources = ToolResources(
            client=self.client,
            config=self.tool_config,
            tag_map=TagMap(self.tool_config.tag_map_path),
        )
        debug = self.client.config.debug
        self._server = EnvelopeFastMCP(
            name="EtherNet/IP MCP Server",
            instructions=(
                "Reads and writes tags on Rockwell/Allen-Bradley Logix controllers over EtherNet/IP. "
                "Every tool returns {success, data, error, meta}. Write tools are refused unless the "
                "operator set ENIP_WRITES_ENABLED=true; set_plc_time also needs ENIP_SYSTEM_CMDS_ENABLED=true. "
                "A write is never re-sent: if meta.outcome is 'unknown', read the tag back before writing again."
            ),
            dependencies=["pycomm3"],
            lifespan=self._lifespan,
            log_level="DEBUG" if debug else "INFO",
        )
        # FastMCP reports the mcp SDK's version as serverInfo.version by default.
        self._server._mcp_server.version = package_version()
        # Logging goes to stderr (FastMCP installs a stderr handler); stdout is
        # the MCP channel. pycomm3 logs every request at INFO, so keep it quiet
        # unless ENIP_DEBUG is on.
        logging.getLogger("pycomm3").setLevel(logging.DEBUG if debug else logging.WARNING)
        logging.getLogger("ethernetip_mcp").setLevel(logging.DEBUG if debug else logging.INFO)
        register_tools(self._server, self.resources)

    @property
    def mcp(self) -> FastMCP:
        return self._server

    def run(self) -> None:
        self._server.run()

    @asynccontextmanager
    async def _lifespan(self, server: FastMCP) -> AsyncIterator[AppContext]:  # noqa: ARG002 - signature contract
        await self.client.ensure_connection()
        try:
            yield AppContext(client=self.client)
        finally:
            await self.client.close()
