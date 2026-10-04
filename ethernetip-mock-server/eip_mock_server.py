"""JSON-over-TCP mock of a Logix controller, for testing the EtherNet/IP MCP server.

It does not speak CIP. The MCP server reaches it through its JSON bridge
(``ENIP_JSON_BRIDGE=true``): one JSON request per line, one JSON reply per line.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import math
import os
import random
import re
import signal
import time
from dataclasses import dataclass, field
from typing import Any

from dotenv import load_dotenv
from rich.console import Console

console = Console()

_TRUE = {"1", "true", "yes", "y", "on"}
_ARRAY_TYPE = re.compile(r"^(?P<base>[A-Z]+)\[(?P<length>\d+)\]$")
_INT_RANGES = {
    "SINT": (-(2**7), 2**7 - 1),
    "INT": (-(2**15), 2**15 - 1),
    "DINT": (-(2**31), 2**31 - 1),
    "LINT": (-(2**63), 2**63 - 1),
    "USINT": (0, 2**8 - 1),
    "UINT": (0, 2**16 - 1),
    "UDINT": (0, 2**32 - 1),
    "ULINT": (0, 2**64 - 1),
}
_STRING_MAX = 82  # characters in a Logix STRING
IDENTITY = {
    "name": "MockController",
    "vendor": "IndustriAgents (mock)",
    "product_type": "Programmable Logic Controller",
    "product_code": 0,
    "product_name": "ethernetip-mock-server",
    "revision": {"major": 0, "minor": 1},
    "serial": "00000000",
    "keyswitch": "REMOTE RUN",
}


class MockError(Exception):
    """A request the mock cannot satisfy; its message goes back to the client as-is."""


@dataclass
class MockConfig:
    host: str = "127.0.0.1"
    port: int = 5025
    update_interval: float = 1.5
    verbose: bool = False


@dataclass
class TagEntry:
    name: str
    value: Any
    data_type: str = "REAL"
    description: str | None = None
    mutable: bool = True
    simulated: bool = False

    @property
    def base_type(self) -> str:
        match = _ARRAY_TYPE.match(self.data_type)
        return match.group("base") if match else self.data_type

    @property
    def length(self) -> int | None:
        match = _ARRAY_TYPE.match(self.data_type)
        return int(match.group("length")) if match else None


def _coerce(tag: str, base_type: str, value: Any) -> Any:
    """Check a value against a Logix type the way a controller would refuse it."""
    if base_type == "BOOL":
        # pycomm3 encodes any truthy value as 0xFF; the mock accepts the common forms.
        if isinstance(value, bool):
            return value
        if isinstance(value, int) and value in (0, 1):
            return bool(value)
        raise MockError(f"Tag '{tag}' is BOOL; expected true/false or 1/0, got {value!r}")
    if base_type == "REAL":
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
            raise MockError(f"Tag '{tag}' is REAL; expected a number, got {value!r}")
        return float(value)
    if base_type in _INT_RANGES:
        if isinstance(value, bool) or not isinstance(value, int):
            raise MockError(f"Tag '{tag}' is {base_type}; expected an integer, got {value!r}")
        low, high = _INT_RANGES[base_type]
        if not low <= value <= high:
            raise MockError(f"Tag '{tag}' is {base_type}; {value} is out of range ({low} to {high})")
        return value
    if base_type == "STRING":
        if not isinstance(value, str):
            raise MockError(f"Tag '{tag}' is STRING; expected text, got {value!r}")
        if len(value) > _STRING_MAX:
            raise MockError(f"Tag '{tag}' is STRING; at most {_STRING_MAX} characters fit")
        return value
    raise MockError(f"Tag '{tag}' has type {base_type}, which the mock cannot write")


@dataclass
class TagDatabase:
    tags: dict[str, TagEntry] = field(default_factory=dict)

    def seed_defaults(self) -> None:
        entries = [
            # Controller-scoped tags
            TagEntry("Line_Speed", 12.5, "REAL", "Conveyor line speed, m/min"),
            TagEntry("Batch_Count", 42, "DINT", "Batches completed this shift"),
            # Program-scoped tags
            TagEntry("Program:MainProgram.MotorSpeed", 1450.0, "REAL", "Motor speed RPM", simulated=True),
            TagEntry("Program:MainProgram.MotorTorque", 38.0, "REAL", simulated=True),
            TagEntry("Program:MainProgram.Conveyor_Status.Running", True, "BOOL", simulated=True),
            TagEntry("Program:MainProgram.Tank_Levels", [32.4, 31.9, 33.1], "REAL[3]", simulated=True),
            TagEntry("Program:MainProgram.Alarm_Message", "OK", "STRING"),
        ]
        self.tags = {entry.name: entry for entry in entries}

    def _entry(self, tag: Any) -> TagEntry:
        if not isinstance(tag, str) or not tag:
            raise MockError("'tag' must be a non-empty string")
        entry = self.tags.get(tag)
        if entry is None:
            raise MockError(f"Unknown tag '{tag}'")
        return entry

    def read(self, tag: Any, count: Any = None) -> tuple[Any, str]:
        """Read like pycomm3: one element unless a count is given."""
        entry = self._entry(tag)
        if count is not None and (isinstance(count, bool) or not isinstance(count, int) or count < 1):
            raise MockError(f"'count' must be an integer of at least 1, got {count!r}")
        if entry.length is None:
            if count not in (None, 1):
                raise MockError(f"Tag '{tag}' is not an array ({entry.data_type}); count must be 1")
            return entry.value, entry.data_type
        elements = count or 1
        if elements > entry.length:
            raise MockError(f"count {elements} is out of range for '{tag}' ({entry.data_type})")
        values = list(entry.value[:elements])
        if elements == 1:
            return values[0], entry.base_type
        return values, f"{entry.base_type}[{elements}]"

    def write(self, tag: Any, value: Any, data_type: Any = None) -> tuple[Any, str]:
        """Write like pycomm3: a list goes to the first len(list) array elements.

        Returns the value written and its type, as pycomm3's write result does.
        """
        entry = self._entry(tag)
        if not entry.mutable:
            raise MockError(f"Tag '{tag}' is read-only")
        if data_type is not None:
            wanted = str(data_type).strip().upper()
            if wanted not in {entry.data_type, entry.base_type}:
                raise MockError(f"Tag '{tag}' is {entry.data_type}, not {data_type}")
        if entry.length is None:
            if isinstance(value, list):
                raise MockError(f"Tag '{tag}' is not an array ({entry.data_type})")
            entry.value = _coerce(tag, entry.base_type, value)
            return entry.value, entry.data_type
        items = value if isinstance(value, list) else [value]
        if not 1 <= len(items) <= entry.length:
            raise MockError(f"Tag '{tag}' has {entry.length} elements; got {len(items)} values")
        coerced = [_coerce(tag, entry.base_type, item) for item in items]
        entry.value = coerced + list(entry.value[len(coerced) :])
        if len(coerced) == 1:
            return coerced[0], entry.base_type
        return coerced, f"{entry.base_type}[{len(coerced)}]"

    def list(self, program: Any = None) -> list[TagEntry]:
        """List tags like pycomm3's get_tag_list(program=...)."""
        if program is None:
            return [e for e in self.tags.values() if not e.name.startswith("Program:")]
        if not isinstance(program, str) or not program:
            raise MockError("'program' must be a non-empty string")
        if program == "*":
            return list(self.tags.values())
        prefix = program if program.startswith("Program:") else f"Program:{program}"
        entries = [e for e in self.tags.values() if e.name.startswith(prefix + ".")]
        if not entries:
            raise MockError(f"Unknown program '{program}'")
        return entries

    def randomize(self) -> None:
        def _set(name: str, value: Any) -> None:
            if name in self.tags:
                self.tags[name].value = value

        _set("Program:MainProgram.MotorSpeed", 1450.0 + random.uniform(-50, 50))
        _set("Program:MainProgram.MotorTorque", 35.0 + random.uniform(-5, 5))
        _set("Program:MainProgram.Conveyor_Status.Running", random.random() > 0.3)
        levels = self.tags.get("Program:MainProgram.Tank_Levels")
        if levels is not None:
            levels.value = [30.0 + random.uniform(-3, 3) for _ in levels.value]


class MockEtherNetIPServer:
    def __init__(self, config: MockConfig) -> None:
        self.config = config
        self.tags = TagDatabase()
        self.tags.seed_defaults()
        self.clock_offset_us = 0
        self._server: asyncio.base_events.Server | None = None
        self._update_task: asyncio.Task[None] | None = None

    def now_us(self) -> int:
        return int(time.time() * 1_000_000) + self.clock_offset_us

    async def start(self) -> None:
        if self._server is not None:
            return
        self._server = await asyncio.start_server(self._handle_client, self.config.host, self.config.port)
        addr = ", ".join(str(sock.getsockname()) for sock in self._server.sockets or [])
        console.print(f"[bold green]EtherNet/IP mock listening on {addr}[/bold green]")
        self._update_task = asyncio.create_task(self._update_loop())

    async def stop(self) -> None:
        if self._update_task is not None:
            self._update_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._update_task
            self._update_task = None
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None

    async def _update_loop(self) -> None:
        while True:
            await asyncio.sleep(self.config.update_interval)
            self.tags.randomize()
            if self.config.verbose:
                console.print("[cyan]Updated mock telemetry[/cyan]")

    async def _handle_client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        peer = writer.get_extra_info("peername")
        if self.config.verbose:
            console.print(f"[yellow]Client connected {peer}[/yellow]")
        try:
            while data := await reader.readline():
                data = data.strip()
                if not data:
                    continue
                try:
                    request = json.loads(data)
                except json.JSONDecodeError:
                    await self._send(writer, {"success": False, "error": "Invalid JSON"})
                    continue
                if not isinstance(request, dict):
                    await self._send(writer, {"success": False, "error": "Request must be a JSON object"})
                    continue
                await self._send(writer, self.dispatch(request))
        except (ConnectionError, asyncio.IncompleteReadError, asyncio.LimitOverrunError):
            pass
        finally:
            writer.close()
            with contextlib.suppress(ConnectionError):
                await writer.wait_closed()
            if self.config.verbose:
                console.print(f"[yellow]Client disconnected {peer}[/yellow]")

    def dispatch(self, request: dict[str, Any]) -> dict[str, Any]:
        try:
            return {"success": True, "data": self._handle(request)}
        except MockError as exc:
            return {"success": False, "error": str(exc)}

    def _handle(self, request: dict[str, Any]) -> Any:
        op = request.get("op")
        if op == "read":
            value, data_type = self.tags.read(request.get("tag"), request.get("count"))
            return {"tag": request["tag"], "value": value, "data_type": data_type}
        if op == "write":
            value, data_type = self.tags.write(request.get("tag"), request.get("value"), request.get("data_type"))
            return {"tag": request["tag"], "value": value, "data_type": data_type}
        if op == "list":
            # Same keys the server returns for a real controller's tag list.
            return [
                {
                    "tag": e.name,
                    "data_type": e.base_type,
                    "dimensions": [e.length] if e.length is not None else [],
                    "tag_type": "atomic",
                    "alias": False,
                    "external_access": "Read/Write" if e.mutable else "Read Only",
                    "description": e.description,
                    "value": e.value,
                }
                for e in self.tags.list(request.get("program"))
            ]
        if op == "info":
            return dict(IDENTITY)
        if op == "get_time":
            return {"microseconds": self.now_us()}
        if op == "set_time":
            microseconds = request.get("microseconds")
            if isinstance(microseconds, bool) or not isinstance(microseconds, int) or microseconds < 0:
                raise MockError("'microseconds' must be a non-negative integer")
            self.clock_offset_us = microseconds - int(time.time() * 1_000_000)
            return {"microseconds": microseconds}
        raise MockError(f"Unknown op '{op}'")

    async def _send(self, writer: asyncio.StreamWriter, message: dict[str, Any]) -> None:
        writer.write(json.dumps(message).encode("utf-8") + b"\n")
        await writer.drain()


def _positive_float(text: str) -> float:
    value = float(text)
    if not math.isfinite(value) or value <= 0:
        raise argparse.ArgumentTypeError(f"must be a positive number, got {text}")
    return value


def _port(text: str) -> int:
    value = int(text)
    if not 1 <= value <= 65535:
        raise argparse.ArgumentTypeError(f"must be 1-65535, got {text}")
    return value


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Parse flags; defaults come from MOCK_ENIP_* variables (read now, not at import)."""
    parser = argparse.ArgumentParser(description="EtherNet/IP mock PLC (JSON over TCP, no CIP).")
    parser.add_argument("--host", default=os.getenv("MOCK_ENIP_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=_port, default=os.getenv("MOCK_ENIP_PORT", "5025"))
    parser.add_argument(
        "--update-interval",
        type=_positive_float,
        default=os.getenv("MOCK_ENIP_UPDATE_INTERVAL", "1.5"),
        help="seconds between simulated value updates",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        default=os.getenv("MOCK_ENIP_VERBOSE", "false").strip().lower() in _TRUE,
    )
    return parser.parse_args(argv)


async def run_server(config: MockConfig) -> None:
    server = MockEtherNetIPServer(config)
    await server.start()

    loop = asyncio.get_running_loop()
    stop_event = asyncio.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(sig, stop_event.set)

    await stop_event.wait()
    console.print("\n[red]Shutting down mock server...[/red]")
    await server.stop()


def main() -> None:
    load_dotenv()
    args = parse_args()
    config = MockConfig(host=args.host, port=args.port, update_interval=args.update_interval, verbose=args.verbose)
    asyncio.run(run_server(config))


if __name__ == "__main__":
    main()
