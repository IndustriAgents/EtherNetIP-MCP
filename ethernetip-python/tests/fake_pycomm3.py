"""A LogixDriver stand-in that enforces pycomm3's real call signatures.

``FakeLogixDriver`` subclasses the installed ``pycomm3.LogixDriver``, so its
constructor is the real one (connection-path parsing, ``_cfg`` with the TCP
port and socket timeout). Every method the server calls is overridden to do no
network I/O, but first binds its arguments to the *real* method's signature
with ``inspect.signature(...).bind``. A call that pycomm3 would reject with a
``TypeError`` (``read(tag, count=3)``, ``write(tag, value, datatype=...)``,
``write(**payloads)``) is rejected here the same way.

The constructor is stricter than pycomm3's: it refuses keyword arguments that
``LogixDriver.__init__`` accepts through ``**kwargs`` but ``CIPDriver.__init__``
silently drops (``port=``, ``timeout=``, ``micro800=``), since passing them does
nothing on a real controller.
"""

from __future__ import annotations

import datetime as dt
import inspect
import re
from dataclasses import dataclass, field
from typing import Any

from pycomm3 import CommError, LogixDriver, Tag

REAL = LogixDriver
_COUNT = re.compile(r"^(?P<base>.+)\{(?P<count>\d+)\}$")
_ARRAY = re.compile(r"^(?P<base>\w+)\[(?P<length>\d+)\]$")


def bind_real(method: str, *args: Any, **kwargs: Any) -> inspect.BoundArguments:
    """Bind a call to the real pycomm3 method; raises TypeError if pycomm3 would."""
    return inspect.signature(getattr(REAL, method)).bind(object(), *args, **kwargs)


def _consumed_init_kwargs() -> set[str]:
    params = inspect.signature(REAL.__init__).parameters.values()
    named = {p.name for p in params if p.kind in (p.KEYWORD_ONLY, p.POSITIONAL_OR_KEYWORD)}
    return named - {"self", "path"}


@dataclass
class FakeController:
    """State behind every driver built by :meth:`factory`."""

    tags: dict[str, tuple[Any, str]] = field(
        default_factory=lambda: {
            "MotorSpeed": (1450.0, "REAL"),
            "Batch_Count": (42, "DINT"),
            "Running": (True, "BOOL"),
            "Tank_Levels": ([32.4, 31.9, 33.1], "REAL[3]"),
            "Program:MainProgram.Alarm_Message": ("OK", "STRING"),
        }
    )
    product_name: str = "1756-L83E/B"
    program_name: str = "FakeProgram"
    plc_time_us: int = 1_791_092_767_930_000
    open_result: bool = True
    open_errors: list[BaseException] = field(default_factory=list)
    op_errors: list[BaseException] = field(default_factory=list)
    # Raised after a write/set_plc_time was applied: the reply was lost.
    reply_errors: list[BaseException] = field(default_factory=list)
    drivers: list[FakeLogixDriver] = field(default_factory=list)
    set_time_calls: list[int | None] = field(default_factory=list)

    def factory(self, path: str) -> FakeLogixDriver:
        driver = FakeLogixDriver(path)
        driver.controller = self
        self.drivers.append(driver)
        return driver

    @property
    def opens(self) -> int:
        return sum(1 for d in self.drivers for call in d.calls if call[0] == "open")

    def all_calls(self, name: str) -> list[tuple[Any, ...]]:
        return [call for d in self.drivers for call in d.calls if call[0] == name]


class FakeLogixDriver(LogixDriver):
    controller: FakeController

    def __init__(self, path: str, *args: Any, **kwargs: Any) -> None:
        dropped = set(kwargs) - _consumed_init_kwargs()
        if args or dropped:
            raise TypeError(f"LogixDriver silently ignores {sorted(dropped) or list(args)}; pass them another way")
        bind_real("__init__", path, **kwargs)
        super().__init__(path, **kwargs)
        self.calls: list[tuple[Any, ...]] = []
        self._visible: set[str] | None = None  # tag definitions known to the driver

    # -- helpers --------------------------------------------------------------

    def _maybe_fail(self) -> None:
        if self.controller.op_errors:
            raise self.controller.op_errors.pop(0)

    def _maybe_lose_reply(self) -> None:
        if self.controller.reply_errors:
            raise self.controller.reply_errors.pop(0)

    def _lookup(self, request: str) -> tuple[str, int | None, Any, str]:
        match = _COUNT.match(request)
        base, count = (match.group("base"), int(match.group("count"))) if match else (request, None)
        if self._visible is not None and base not in self._visible:
            raise KeyError(base)
        value, data_type = self.controller.tags[base]
        return base, count, value, data_type

    # -- the methods eip_client calls ----------------------------------------

    def open(self, *args: Any, **kwargs: Any) -> bool:
        bind_real("open", *args, **kwargs)
        self.calls.append(("open", self._cfg["port"], self._cfg["socket_timeout"]))
        if self.controller.open_errors:
            raise self.controller.open_errors.pop(0)
        if not self.controller.open_result:
            return False
        self._connection_opened = True
        self._info = {**self._identity(), "name": self.controller.program_name}
        self._micro800 = self.controller.product_name.startswith("2080")
        return True

    def close(self, *args: Any, **kwargs: Any) -> None:
        bind_real("close", *args, **kwargs)
        self.calls.append(("close",))
        self._connection_opened = False

    def read(self, *tags: Any, **kwargs: Any) -> Tag | list[Tag]:
        bind_real("read", *tags, **kwargs)
        self.calls.append(("read", tags))
        self._maybe_fail()
        results = []
        for request in tags:
            try:
                base, count, value, data_type = self._lookup(request)
            except KeyError as exc:
                results.append(Tag(request, None, None, f"Tag doesn't exist - {exc.args[0]}"))
                continue
            array = _ARRAY.match(data_type)
            if array:
                elements = count or 1
                if elements == 1:
                    results.append(Tag(base, value[0], array.group("base"), None))
                else:
                    results.append(Tag(base, value[:elements], f"{array.group('base')}[{elements}]", None))
            else:
                results.append(Tag(base, value, data_type, None))
        return results if len(tags) > 1 else results[0]

    def write(self, *tags_values: Any, **kwargs: Any) -> Tag | list[Tag]:
        bind_real("write", *tags_values, **kwargs)
        self.calls.append(("write", tags_values))
        self._maybe_fail()
        # Same normalisation and unpacking as LogixDriver.write.
        if len(tags_values) == 2 and isinstance(tags_values[0], str):
            tags_values = ((*tags_values,),)
        results = []
        for tag, value in tags_values:
            try:
                base, count, current, data_type = self._lookup(tag)
            except KeyError as exc:
                results.append(Tag(tag, None, None, f"Tag doesn't exist - {exc.args[0]}"))
                continue
            array = _ARRAY.match(data_type)
            if array:
                elements = count or 1
                items = value if isinstance(value, list) else [value]
                if len(items) < elements:
                    results.append(Tag(base, None, None, "Insufficient data for requested elements"))
                    continue
                new = list(items[:elements]) + list(current[elements:])
                self.controller.tags[base] = (new, data_type)
                shown = array.group("base") if elements == 1 else f"{array.group('base')}[{elements}]"
                results.append(Tag(base, value, shown, None))
                continue
            if data_type == "DINT" and not isinstance(value, int):
                results.append(Tag(base, value, None, "Invalid Tag Request - Unable to create a writable value"))
                continue
            self.controller.tags[base] = (value, data_type)
            results.append(Tag(base, value, data_type, None))
        self._maybe_lose_reply()
        return results if len(tags_values) > 1 else results[0]

    def get_tag_list(self, *args: Any, **kwargs: Any) -> list[dict[str, Any]]:
        bound = bind_real("get_tag_list", *args, **kwargs)
        bound.apply_defaults()
        program, cache = bound.arguments["program"], bound.arguments["cache"]
        self.calls.append(("get_tag_list", program, cache))
        self._maybe_fail()
        if program is None:
            names = [n for n in self.controller.tags if not n.startswith("Program:")]
        elif program == "*":
            names = list(self.controller.tags)
        else:
            names = [n for n in self.controller.tags if n.startswith(f"Program:{program}.")]
        if cache:
            # Real pycomm3 replaces its tag definitions with this list.
            self._visible = set(names)
        definitions = []
        for name in names:
            _, data_type = self.controller.tags[name]
            array = _ARRAY.match(data_type)
            definitions.append(
                {
                    "tag_name": name,
                    "dim": 1 if array else 0,
                    "dimensions": [int(array.group("length")), 0, 0] if array else [0, 0, 0],
                    "data_type": array.group("base") if array else data_type,
                    "data_type_name": array.group("base") if array else data_type,
                    "tag_type": "atomic",
                    "alias": False,
                    "external_access": "Read/Write",
                    "instance_id": 1,
                    "type_class": object,  # like pycomm3: not JSON serialisable
                }
            )
        return definitions

    def get_plc_info(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        bind_real("get_plc_info", *args, **kwargs)
        self.calls.append(("get_plc_info",))
        self._maybe_fail()
        return self._identity()

    def _identity(self) -> dict[str, Any]:
        return {
            "vendor": "Rockwell Automation/Allen-Bradley",
            "product_type": "Programmable Logic Controller",
            "product_code": 166,
            "revision": {"major": 33, "minor": 11},
            "status": b"\x60\x30",
            "serial": "c0ffee00",
            "product_name": self.controller.product_name,
            "keyswitch": "REMOTE RUN",
        }

    def get_plc_time(self, *args: Any, **kwargs: Any) -> Tag:
        bind_real("get_plc_time", *args, **kwargs)
        self.calls.append(("get_plc_time",))
        self._maybe_fail()
        us = self.controller.plc_time_us
        when = dt.datetime(1970, 1, 1) + dt.timedelta(microseconds=us)
        return Tag("get_plc_time", {"datetime": when, "microseconds": us, "string": str(when)}, None, None)

    def set_plc_time(self, *args: Any, **kwargs: Any) -> Tag:
        bound = bind_real("set_plc_time", *args, **kwargs)
        bound.apply_defaults()
        self.calls.append(("set_plc_time", bound.arguments["microseconds"]))
        self._maybe_fail()
        self.controller.set_time_calls.append(bound.arguments["microseconds"])
        self._maybe_lose_reply()
        return Tag("set_plc_time", None, None, None)


__all__ = ["CommError", "FakeController", "FakeLogixDriver", "REAL", "bind_real"]
