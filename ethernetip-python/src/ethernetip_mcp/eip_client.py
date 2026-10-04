"""EtherNet/IP client used by the MCP tools.

There are two backends:

* **CIP**, through pycomm3's ``LogixDriver``, for real Logix controllers.
* **JSON bridge** (``ENIP_JSON_BRIDGE=true``), a newline-delimited JSON protocol
  spoken by the mock PLC in ``ethernetip-mock-server``.

Every public operation returns plain JSON-serialisable data plus a ``meta``
dict, or raises :class:`EIPClientError`. Bad arguments raise ``ValueError``.
The MCP tools turn both into ``success: false`` envelopes.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import math
import os
import re
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

import anyio
from pycomm3 import CommError, LogixDriver
from pycomm3.const import MICRO800_PREFIX

logger = logging.getLogger(__name__)

DEFAULT_CIP_PORT = 44818
_TRUE = {"1", "true", "yes", "y", "on"}
_FALSE = {"0", "false", "no", "n", "off"}
_COUNT_SUFFIX = re.compile(r"^(?P<base>.+)\{(?P<count>\d+)\}$")
_MAX_REPLY_BYTES = 16 * 1024 * 1024
MAX_BACKOFF_S = 30.0

OperationMeta = dict[str, Any]


class ConfigError(ValueError):
    """The ``ENIP_*`` configuration is unusable. Raised at startup."""


class EIPClientError(RuntimeError):
    """An operation on the controller (or the mock) did not succeed."""

    def __init__(self, message: str, meta: OperationMeta | None = None) -> None:
        super().__init__(message)
        self.meta: OperationMeta = dict(meta or {})


class OutcomeUnknownError(EIPClientError):
    """A write-like request may have reached the device, but it was not confirmed.

    Such requests are never repeated automatically: the caller must read the
    value back before deciding to write again.
    """


class _TransientError(EIPClientError):
    """A connection-level failure that is worth retrying."""


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


def _env_value(env: Mapping[str, str], name: str) -> str | None:
    raw = env.get(name)
    if raw is None or not raw.strip():
        return None
    return raw.strip()


def parse_bool(env: Mapping[str, str], name: str, default: bool) -> bool:
    """Read a boolean setting. Unrecognised values are a ``ConfigError``."""
    raw = _env_value(env, name)
    if raw is None:
        return default
    lowered = raw.lower()
    if lowered in _TRUE:
        return True
    if lowered in _FALSE:
        return False
    raise ConfigError(f"{name}={raw!r} is not a boolean (use true or false)")


def _parse_int(env: Mapping[str, str], name: str, default: int, low: int, high: int) -> int:
    raw = _env_value(env, name)
    if raw is None:
        return default
    try:
        value = int(raw)
    except ValueError:
        raise ConfigError(f"{name}={raw!r} is not an integer") from None
    if not low <= value <= high:
        raise ConfigError(f"{name}={value} is out of range ({low}-{high})")
    return value


def _parse_float(env: Mapping[str, str], name: str, default: float, low: float, high: float) -> float:
    raw = _env_value(env, name)
    if raw is None:
        return default
    try:
        value = float(raw)
    except ValueError:
        raise ConfigError(f"{name}={raw!r} is not a number") from None
    if not math.isfinite(value) or not low <= value <= high:
        raise ConfigError(f"{name}={raw} is out of range ({low}-{high})")
    return value


def _split_host_port(text: str, name: str) -> tuple[str, int | None]:
    """Split ``host``, ``host:port``, a bare IPv6 address or ``[ipv6]:port``."""
    text = text.strip()
    if text.startswith("["):
        host, closed, rest = text[1:].partition("]")
        if not closed or not host.strip():
            raise ConfigError(f"{name}={text!r} is not a valid [IPv6]:port address")
        if not rest:
            return host.strip(), None
        if not rest.startswith(":"):
            raise ConfigError(f"{name}={text!r} is not a valid [IPv6]:port address")
        port_text = rest[1:]
    elif text.count(":") > 1:
        return text, None  # a bare IPv6 address, no port
    else:
        host, sep, port_text = text.partition(":")
        if not host.strip():
            raise ConfigError(f"{name}={text!r} has no host")
        if not sep:
            return host.strip(), None
    try:
        port = int(port_text)
    except ValueError:
        raise ConfigError(f"{name}={text!r} has an invalid port") from None
    return host.strip(), port


@dataclass(slots=True)
class EIPClientConfig:
    """Connection settings. Build it with :meth:`from_env` after ``load_dotenv()``."""

    host: str = "127.0.0.1"
    port: int = DEFAULT_CIP_PORT
    slot: int = 0
    route: str | None = None  # set (possibly "") when ENIP_PATH is used
    json_bridge: bool = False
    timeout: float = 5.0
    max_retries: int = 3
    retry_backoff_base: float = 0.5
    micro800: bool = False
    debug: bool = False

    def __post_init__(self) -> None:
        if not self.host:
            raise ConfigError("ENIP_HOST is empty")
        if ":" in self.host and not self.json_bridge:
            # pycomm3's Socket is AF_INET only and its path parser splits on ':'.
            raise ConfigError(
                f"ENIP_HOST={self.host!r} is an IPv6 address; pycomm3 connects to controllers over IPv4 only"
            )
        # pycomm3's parse_connection_path() rejects port 65535, so the CIP
        # backend stops one short of the TCP maximum.
        max_port = 65535 if self.json_bridge else 65534
        if not 1 <= self.port <= max_port:
            raise ConfigError(f"ENIP_PORT={self.port} is out of range (1-{max_port})")
        if not 0 <= self.slot <= 255:
            raise ConfigError(f"ENIP_SLOT={self.slot} is out of range (0-255)")
        if not self.timeout > 0:
            raise ConfigError(f"ENIP_TIMEOUT={self.timeout} must be greater than 0")
        if self.max_retries < 0:
            raise ConfigError("ENIP_MAX_RETRIES must be 0 or more")
        if self.retry_backoff_base < 0:
            raise ConfigError("ENIP_RETRY_BACKOFF_BASE must be 0 or more")
        if self.micro800 and self.slot:
            raise ConfigError(
                "ENIP_SLOT and ENIP_MICRO800 conflict: a Micro800 has no backplane slot, so leave ENIP_SLOT unset"
            )
        if self.json_bridge and self.route is not None:
            raise ConfigError("ENIP_PATH is a CIP route and cannot be combined with ENIP_JSON_BRIDGE=true")

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> EIPClientConfig:
        env = os.environ if environ is None else environ
        json_bridge = parse_bool(env, "ENIP_JSON_BRIDGE", False)
        explicit_port = _parse_int(env, "ENIP_PORT", 0, 1, 65535) or None

        path = _env_value(env, "ENIP_PATH")
        if path:
            normalized = path.replace("\\", "/").replace(",", "/")
            first, _, route = normalized.partition("/")
            host, embedded_port = _split_host_port(first, "ENIP_PATH")
            source = "ENIP_PATH"
            route = route.strip("/")  # "" still means "ENIP_PATH given": the slot is not used
        else:
            host, embedded_port = _split_host_port(_env_value(env, "ENIP_HOST") or "127.0.0.1", "ENIP_HOST")
            source = "ENIP_HOST"
            route = None

        if embedded_port is not None and explicit_port is not None and embedded_port != explicit_port:
            raise ConfigError(f"ENIP_PORT={explicit_port} conflicts with port {embedded_port} in {source}")
        port = explicit_port or embedded_port or DEFAULT_CIP_PORT

        return cls(
            host=host,
            port=port,
            slot=_parse_int(env, "ENIP_SLOT", 0, 0, 255),
            route=route,
            json_bridge=json_bridge,
            timeout=_parse_float(env, "ENIP_TIMEOUT", 5.0, 0.001, 3600.0),
            max_retries=_parse_int(env, "ENIP_MAX_RETRIES", 3, 0, 10),
            retry_backoff_base=_parse_float(env, "ENIP_RETRY_BACKOFF_BASE", 0.5, 0.0, 60.0),
            micro800=parse_bool(env, "ENIP_MICRO800", False),
            debug=parse_bool(env, "ENIP_DEBUG", False),
        )

    def cip_path(self) -> str:
        """The connection path handed to ``LogixDriver``.

        pycomm3 1.2.14 ignores every ``LogixDriver`` keyword argument except
        ``init_tags``/``init_program_tags`` (``CIPDriver.__init__`` never reads
        ``**kwargs``). The only supported way to choose the TCP port is to write
        it into the path as ``ip:port``, which ``parse_connection_path`` reads.
        """
        base = f"{self.host}:{self.port}"
        if self.route is not None:
            return f"{base}/{self.route}" if self.route else base
        if self.micro800 or self.slot == 0:
            # pycomm3 adds backplane/0 itself and drops it again once it has
            # identified a Micro800.
            return base
        return f"{base}/{self.slot}"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def split_element_count(tag: str, count: int | None) -> tuple[str, int | None]:
    """Separate pycomm3's ``Tag{N}`` element-count suffix from a tag name."""
    if not isinstance(tag, str) or not tag.strip():
        raise ValueError("tag name must be a non-empty string")
    tag = tag.strip()
    if count is not None and (isinstance(count, bool) or not isinstance(count, int) or count < 1):
        raise ValueError(f"element count must be an integer of at least 1, got {count!r}")
    match = _COUNT_SUFFIX.match(tag)
    if not match:
        if "{" in tag or "}" in tag:
            raise ValueError(f"malformed element count in tag name {tag!r} (expected Tag{{N}})")
        return tag, count
    embedded = int(match.group("count"))
    if embedded < 1:
        raise ValueError(f"element count in {tag!r} must be at least 1")
    if count is not None and count != embedded:
        raise ValueError(f"tag {tag!r} asks for {embedded} elements but count is {count}")
    return match.group("base"), embedded


def _jsonable(value: Any) -> Any:
    """Convert values decoded by pycomm3 into JSON-friendly ones."""
    if value is None or isinstance(value, (bool, int, str)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else str(value)
    if isinstance(value, Mapping):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    if isinstance(value, (bytes, bytearray)):
        return bytes(value).hex()
    if isinstance(value, (dt.datetime, dt.date)):
        return value.isoformat()
    return str(value)


def _describe(exc: BaseException) -> str:
    """Join an exception's message with the messages of its causes."""
    parts: list[str] = []
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        text = str(current).strip() or type(current).__name__
        if text not in parts:
            parts.append(text)
        current = current.__cause__ or (None if current.__suppress_context__ else current.__context__)
    return ": ".join(parts)


def _is_transient(exc: BaseException) -> bool:
    """True for connection-level failures, which are retried; False otherwise."""
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if isinstance(current, (_TransientError, CommError, OSError)):
            return True
        current = current.__cause__ or current.__context__
    return False


def _close_quietly(driver: Any) -> None:
    try:
        driver.close()
    except Exception:  # noqa: BLE001 - best effort on an already broken session
        logger.debug("Ignoring error while closing the CIP session", exc_info=True)


def _apply_socket_timeout(driver: Any, timeout: float) -> None:
    """Make ``ENIP_TIMEOUT`` the socket timeout of a pycomm3 driver.

    pycomm3 1.2.14 has no constructor argument for it. ``CIPDriver.open()``
    builds its socket with ``Socket(self._cfg["socket_timeout"])`` (default 5 s),
    and the public ``socket_timeout`` setter writes a misspelled key
    (``"socket_timout"``) that nothing reads, so the config entry is set here.
    """
    cfg = getattr(driver, "_cfg", None)
    if not isinstance(cfg, dict) or "socket_timeout" not in cfg:
        raise EIPClientError(
            "this pycomm3 version does not expose _cfg['socket_timeout']; ENIP_TIMEOUT cannot be applied"
        )
    cfg["socket_timeout"] = float(timeout)


def _tag_error(tag: Any) -> str | None:
    """Return the error of a pycomm3 ``Tag`` result, or None if it succeeded."""
    error = getattr(tag, "error", None)
    if error:
        return str(error)
    if getattr(tag, "value", None) is None:
        return "no value returned"
    return None


def _as_tag_list(result: Any) -> list[Any]:
    return list(result) if isinstance(result, list) else [result]


def _identity(info: Mapping[str, Any], name: Any = None) -> dict[str, Any]:
    revision = info.get("revision") or {}
    major = revision.get("major") if isinstance(revision, Mapping) else None
    minor = revision.get("minor") if isinstance(revision, Mapping) else None
    return _jsonable(
        {
            "name": name if name is not None else info.get("name"),
            "vendor": info.get("vendor"),
            "product_type": info.get("product_type"),
            "product_code": info.get("product_code"),
            "product_name": info.get("product_name"),
            "revision": {"major": major, "minor": minor},
            "firmware": f"{major}.{minor}" if major is not None and minor is not None else None,
            "serial": info.get("serial"),
            "keyswitch": info.get("keyswitch"),
        }
    )


def plc_time_payload(microseconds: int) -> dict[str, Any]:
    """Format a wall-clock value the way pycomm3 does (µs since 1970-01-01)."""
    when = dt.datetime(1970, 1, 1) + dt.timedelta(microseconds=microseconds)
    return {"plc_time": when.isoformat(), "microseconds": microseconds}


TAG_LIST_KEYS = ("tag", "data_type", "dimensions", "tag_type", "alias", "external_access", "description")


def _tag_definition(raw: Mapping[str, Any]) -> dict[str, Any]:
    """Reduce a pycomm3 tag definition to the shared tag-list entry.

    Drops pycomm3's ``type_class`` objects. pycomm3 does not read tag
    descriptions, so ``description`` is always None on the CIP backend.
    """
    dims = int(raw.get("dim") or 0)
    dimensions = [d for d in (raw.get("dimensions") or [])[:dims]]
    return _jsonable(
        {
            "tag": raw.get("tag_name"),
            "data_type": raw.get("data_type_name"),
            "dimensions": dimensions,
            "tag_type": raw.get("tag_type"),
            "alias": raw.get("alias"),
            "external_access": raw.get("external_access"),
            "description": None,
        }
    )


def _base_type(data_type: str | None) -> str | None:
    if not data_type:
        return None
    return data_type.split("[", 1)[0].strip().upper()


def _type_warning(requested: str | None, actual: str | None) -> str | None:
    """pycomm3 always encodes with the controller's type; flag a different request."""
    if requested and _base_type(actual) and _base_type(actual) != _base_type(requested):
        return (
            f"data_type {requested} was given, but the controller tag is {actual}; "
            "pycomm3 encoded the value as the controller type"
        )
    return None


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------


class EIPClient:
    """Async facade over pycomm3's synchronous ``LogixDriver`` and the JSON bridge."""

    def __init__(
        self,
        config: EIPClientConfig | None = None,
        driver_factory: Callable[[str], Any] | None = None,
    ) -> None:
        self.config = config if config is not None else EIPClientConfig.from_env()
        self._driver_factory = driver_factory or LogixDriver
        self._driver: Any = None
        self._lock = threading.Lock()
        self._connected = False
        self._micro800_detected: bool | None = None
        self._last_error: str | None = None
        self._last_contact: float | None = None

    @property
    def backend(self) -> str:
        return "json_bridge" if self.config.json_bridge else "cip"

    # -- lifecycle ----------------------------------------------------------

    async def ensure_connection(self) -> None:
        """Try once to open the CIP session at startup.

        Never raises: an unreachable controller must not stop the MCP server
        from answering ``initialize``. Each tool call reconnects as needed and
        reports its own errors. The JSON bridge has no session to open.
        """
        if self.config.json_bridge:
            return
        try:
            await anyio.to_thread.run_sync(self._connect_once)
        except Exception as exc:  # noqa: BLE001 - logged, reported by the tools
            message = _describe(exc)
            self._last_error = f"connect: {message}"
            logger.warning(
                "Could not connect to %s at startup (%s). The server keeps running; tool calls will retry.",
                self.config.cip_path(),
                message,
            )

    async def close(self) -> None:
        await anyio.to_thread.run_sync(self._disconnect_sync)

    # -- reads ----------------------------------------------------------------

    async def read_tag(self, tag: str, count: int | None = None) -> tuple[dict[str, Any], OperationMeta]:
        base, count = split_element_count(tag, count)
        label = f"read_tag({base})"
        if self.config.json_bridge:
            payload: dict[str, Any] = {"op": "read", "tag": base}
            if count is not None:
                payload["count"] = count
            response, meta = await self._json_exchange(label, payload)
            data = self._json_data(response, label, meta)
            result = {"tag": base, "value": data.get("value"), "data_type": data.get("data_type")}
        else:
            request = base if count is None else f"{base}{{{count}}}"
            tag_result, meta = await self._run_cip(label, lambda driver: driver.read(request))
            error = _tag_error(tag_result)
            if error:
                raise EIPClientError(f"{label} failed: {error}", meta)
            result = {"tag": base, "value": _jsonable(tag_result.value), "data_type": tag_result.type}
        if count is not None:
            # pycomm3 returns a bare value for a single element; arrays are lists.
            if not isinstance(result["value"], list):
                result["value"] = [result["value"]]
            result["elements"] = count
        return result, meta

    async def read_multiple_tags(self, tags: list[str]) -> tuple[list[dict[str, Any]], OperationMeta]:
        if not tags:
            raise ValueError("tags must contain at least one tag name")
        parsed = [split_element_count(tag, None) for tag in tags]
        if self.config.json_bridge:
            results = []
            attempts = 0
            for base, count in parsed:
                payload: dict[str, Any] = {"op": "read", "tag": base}
                if count is not None:
                    payload["count"] = count
                response, meta = await self._json_exchange(f"read_tag({base})", payload)
                attempts += meta.get("attempts", 1)
                if response.get("success"):
                    data = response.get("data") or {}
                    results.append(
                        {"tag": base, "value": data.get("value"), "data_type": data.get("data_type"), "error": None}
                    )
                else:
                    error = str(response.get("error") or "read failed")
                    results.append({"tag": base, "value": None, "data_type": None, "error": error})
            return results, {"backend": self.backend, "attempts": attempts}

        requests = [base if count is None else f"{base}{{{count}}}" for base, count in parsed]
        raw, meta = await self._run_cip("read_multiple_tags", lambda driver: driver.read(*requests))
        results = []
        for (base, _), tag_result in zip(parsed, _as_tag_list(raw), strict=True):
            error = _tag_error(tag_result)
            results.append(
                {
                    "tag": base,
                    "value": None if error else _jsonable(tag_result.value),
                    "data_type": None if error else tag_result.type,
                    "error": error,
                }
            )
        return results, meta

    async def get_tag_list(self, program: str | None = None) -> tuple[list[dict[str, Any]], OperationMeta]:
        label = "get_tag_list" if program is None else f"get_tag_list({program})"
        if self.config.json_bridge:
            payload: dict[str, Any] = {"op": "list"}
            if program is not None:
                payload["program"] = program
            response, meta = await self._json_exchange(label, payload)
            data = self._json_data(response, label, meta, expect=list)
            tags = [
                _jsonable({key: item.get(key) for key in TAG_LIST_KEYS}) for item in data if isinstance(item, Mapping)
            ]
        else:
            # cache=False: with the default cache=True pycomm3 replaces its own
            # tag definitions with this (possibly narrower) list, and later
            # reads/writes of tags outside it would fail.
            raw, meta = await self._run_cip(label, lambda driver: driver.get_tag_list(program=program, cache=False))
            tags = [_tag_definition(item) for item in raw]
        return tags, {**meta, "count": len(tags)}

    async def get_controller_info(self) -> tuple[dict[str, Any], OperationMeta]:
        if self.config.json_bridge:
            response, meta = await self._json_exchange("get_plc_info", {"op": "info"})
            return _identity(self._json_data(response, "get_plc_info", meta)), meta

        def _op(driver: Any) -> dict[str, Any]:
            # LogixDriver.info is a dict filled at open(); get_plc_info() asks
            # the controller again so the keyswitch position is current.
            fresh = driver.get_plc_info()
            name = (driver.info or {}).get("name")
            return _identity(fresh, name)

        return await self._run_cip("get_plc_info", _op)

    async def get_plc_time(self) -> tuple[dict[str, Any], OperationMeta]:
        if self.config.json_bridge:
            response, meta = await self._json_exchange("get_plc_time", {"op": "get_time"})
            data = self._json_data(response, "get_plc_time", meta)
            return plc_time_payload(int(data["microseconds"])), meta
        tag_result, meta = await self._run_cip("get_plc_time", lambda driver: driver.get_plc_time())
        error = _tag_error(tag_result)
        if error:
            raise EIPClientError(f"get_plc_time failed: {error}", meta)
        return plc_time_payload(int(tag_result.value["microseconds"])), meta

    async def ping(self) -> tuple[dict[str, Any], OperationMeta]:
        """Ask the device for its identity: real I/O, unlike connection_status()."""
        if self.config.json_bridge:
            response, meta = await self._json_exchange("ping", {"op": "info"})
            info = self._json_data(response, "ping", meta)
        else:
            info, meta = await self._run_cip("ping", lambda driver: driver.get_plc_info())
        return {"product_name": _jsonable(info.get("product_name"))}, meta

    # -- writes ---------------------------------------------------------------

    def _write_request(self, tag: str, value: Any) -> tuple[str, str]:
        if value is None:
            raise ValueError(f"no value given for {tag!r}")
        base, count = split_element_count(tag, None)
        if isinstance(value, list):
            if not value:
                raise ValueError(f"cannot write an empty list to {tag!r}")
            if count is None:
                count = len(value)
            elif count != len(value):
                raise ValueError(f"{tag!r} asks for {count} elements but {len(value)} values were given")
        elif count is not None and count > 1:
            raise ValueError(f"{tag!r} asks for {count} elements; give a list of {count} values")
        return base, base if count is None else f"{base}{{{count}}}"

    async def write_tag(
        self, tag: str, value: Any, data_type: str | None = None
    ) -> tuple[dict[str, Any], OperationMeta]:
        base, request = self._write_request(tag, value)
        label = f"write_tag({base})"
        if self.config.json_bridge:
            payload: dict[str, Any] = {"op": "write", "tag": base, "value": value}
            if data_type:
                payload["data_type"] = data_type
            response, meta = await self._json_exchange(label, payload, repeatable=False)
            data = self._json_data(response, label, meta)
            return {"tag": base, "value": data.get("value", value), "data_type": data.get("data_type")}, meta

        # LogixDriver.write(*tags_values) takes (tag, value) tuples and has no
        # data type argument: pycomm3 encodes with the controller's own tag
        # definition. A list is written as Tag{N} so every element is sent.
        tag_result, meta = await self._run_cip(label, lambda driver: driver.write((request, value)), repeatable=False)
        if tag_result.error:
            raise EIPClientError(f"{label} failed: {tag_result.error}", meta)
        actual = tag_result.type
        warning = _type_warning(data_type, actual)
        if warning:
            meta = {**meta, "warning": warning}
        return {"tag": base, "value": _jsonable(value), "data_type": actual}, meta

    async def write_multiple_tags(
        self, items: list[tuple[str, Any, str | None]]
    ) -> tuple[list[dict[str, Any]], OperationMeta]:
        """Write ``(tag, value, data_type)`` entries; ``data_type`` may be None.

        Always returns one result per entry, each with an ``outcome``:
        ``written``, ``rejected`` (the device refused it), ``unknown`` (it may
        have been applied but was not confirmed) or ``not_sent``. Nothing is
        retried once it may have reached the device. Raises only ValueError,
        for bad input, before anything is sent.
        """
        if not items:
            raise ValueError("payloads must contain at least one entry")
        prepared = [(*self._write_request(tag, value), value, data_type) for tag, value, data_type in items]
        if self.config.json_bridge:
            return await self._json_write_multiple(prepared)

        pairs = [(request, value) for _, request, value, _ in prepared]
        try:
            raw, meta = await self._run_cip(
                "write_multiple_tags", lambda driver: driver.write(*pairs), repeatable=False
            )
        except EIPClientError as exc:
            # One request carried every entry, so they share the outcome.
            outcome = exc.meta.get("outcome", "unknown")
            results = [
                {"tag": base, "value": _jsonable(value), "data_type": None, "error": str(exc), "outcome": outcome}
                for base, _, value, _ in prepared
            ]
            return results, exc.meta
        results = []
        for (base, _, value, data_type), tag_result in zip(prepared, _as_tag_list(raw), strict=True):
            error = str(tag_result.error) if tag_result.error else None
            result = {
                "tag": base,
                "value": _jsonable(value),
                "data_type": None if error else tag_result.type,
                "error": error,
                "outcome": "rejected" if error else "written",
            }
            warning = None if error else _type_warning(data_type, tag_result.type)
            if warning:
                result["warning"] = warning
            results.append(result)
        return results, meta

    async def _json_write_multiple(
        self, prepared: list[tuple[str, str, Any, str | None]]
    ) -> tuple[list[dict[str, Any]], OperationMeta]:
        results: list[dict[str, Any]] = []
        attempts = 0
        stopped: str | None = None
        for base, _, value, data_type in prepared:
            if stopped is not None:
                results.append(
                    {"tag": base, "value": value, "data_type": None, "error": stopped, "outcome": "not_sent"}
                )
                continue
            payload: dict[str, Any] = {"op": "write", "tag": base, "value": value}
            if data_type:
                payload["data_type"] = data_type
            try:
                response, meta = await self._json_exchange(f"write_tag({base})", payload, repeatable=False)
            except EIPClientError as exc:
                attempts += exc.meta.get("attempts", 1)
                outcome = exc.meta.get("outcome", "unknown")
                results.append({"tag": base, "value": value, "data_type": None, "error": str(exc), "outcome": outcome})
                stopped = f"not attempted: the batch stopped after the connection failed on {base}"
                continue
            attempts += meta.get("attempts", 1)
            if response.get("success"):
                data = response.get("data") or {}
                results.append(
                    {
                        "tag": base,
                        "value": data.get("value", value),
                        "data_type": data.get("data_type"),
                        "error": None,
                        "outcome": "written",
                    }
                )
            else:
                error = str(response.get("error") or "write failed")
                results.append({"tag": base, "value": value, "data_type": None, "error": error, "outcome": "rejected"})
        return results, {"backend": self.backend, "attempts": attempts}

    async def set_plc_time(self) -> tuple[dict[str, Any], OperationMeta]:
        """Set the controller clock to this host's current time."""
        microseconds = int(time.time() * 1_000_000)
        if self.config.json_bridge:
            response, meta = await self._json_exchange(
                "set_plc_time", {"op": "set_time", "microseconds": microseconds}, repeatable=False
            )
            data = self._json_data(response, "set_plc_time", meta)
            return plc_time_payload(int(data.get("microseconds", microseconds))), meta
        tag_result, meta = await self._run_cip(
            "set_plc_time", lambda driver: driver.set_plc_time(microseconds=microseconds), repeatable=False
        )
        if tag_result.error:
            raise EIPClientError(f"set_plc_time failed: {tag_result.error}", meta)
        return plc_time_payload(microseconds), meta

    # -- status ---------------------------------------------------------------

    def connection_status(self) -> dict[str, Any]:
        """Report the last known state without talking to the device."""
        status: dict[str, Any] = {
            "backend": self.backend,
            "connected": self._connected,
            "host": self.config.host,
            "port": self.config.port,
            "last_contact": (
                dt.datetime.fromtimestamp(self._last_contact, tz=dt.UTC).isoformat()
                if self._last_contact is not None
                else None
            ),
            "last_error": self._last_error,
        }
        if self.config.json_bridge:
            status["note"] = (
                "The JSON bridge opens one TCP connection per request; 'connected' is the last request's outcome."
            )
        else:
            status.update(
                {
                    "slot": self.config.slot,
                    "route": self.config.route,
                    "connection_path": self.config.cip_path(),
                    "micro800": self.config.micro800,
                    "micro800_detected": self._micro800_detected,
                    "timeout_s": self.config.timeout,
                }
            )
        return status

    # -- CIP internals --------------------------------------------------------

    def _backoff(self, attempt: int) -> float:
        return min(self.config.retry_backoff_base * (2 ** (attempt - 1)), MAX_BACKOFF_S)

    @property
    def _address(self) -> str:
        host = self.config.host
        return f"[{host}]:{self.config.port}" if ":" in host else f"{host}:{self.config.port}"

    def _open_locked(self) -> Any:
        """Return a connected driver, opening one (single attempt) if needed.

        Caller must hold ``self._lock``. Retries are the caller's business, so
        a failing call makes at most ``ENIP_MAX_RETRIES + 1`` connection
        attempts instead of retrying inside every retry.
        """
        driver = self._driver
        if driver is not None and self._connected and getattr(driver, "connected", False):
            return driver
        self._drop_driver_locked()
        driver = self._driver_factory(self.config.cip_path())
        try:
            _apply_socket_timeout(driver, self.config.timeout)
            if not driver.open():
                raise _TransientError("the controller did not register a CIP session")
            self._check_micro800(driver)
        except BaseException:
            _close_quietly(driver)
            raise
        self._driver = driver
        self._connected = True
        return driver

    def _check_micro800(self, driver: Any) -> None:
        info = getattr(driver, "info", None) or {}
        product_name = str(info.get("product_name") or "")
        detected = product_name.startswith(MICRO800_PREFIX)
        self._micro800_detected = detected
        if self.config.micro800 and not detected:
            raise EIPClientError(
                f"ENIP_MICRO800=true, but the controller at {self.config.host} identifies as "
                f"{product_name or 'an unknown product'!r}, not a Micro800 (catalog {MICRO800_PREFIX}-*). "
                "Unset ENIP_MICRO800 or check ENIP_HOST."
            )

    def _drop_driver_locked(self) -> None:
        if self._driver is not None:
            _close_quietly(self._driver)
        self._driver = None
        self._connected = False

    def _connect_once(self) -> None:
        with self._lock:
            self._open_locked()
            self._last_contact = time.time()
            self._last_error = None

    def _disconnect_sync(self) -> None:
        with self._lock:
            self._drop_driver_locked()

    def _execute_sync(
        self, label: str, operation: Callable[[Any], Any], repeatable: bool = True
    ) -> tuple[Any, OperationMeta]:
        """Run ``operation`` on a connected driver.

        Opening the session is retried on connection-level failures. The
        operation itself is retried only if ``repeatable``: a write (or
        set_plc_time) that fails after it may have reached the controller,
        for example because its reply was lost, is never sent again.
        """
        start = time.perf_counter()
        attempts = 0
        while True:
            attempts += 1
            phase = "open"
            try:
                with self._lock:
                    driver = self._open_locked()
                    phase = "operation"
                    result = operation(driver)
                    self._last_contact = time.time()
                    self._last_error = None
            except Exception as exc:
                transient = _is_transient(exc)
                message = _describe(exc)
                with self._lock:
                    self._last_error = f"{label}: {message}"
                    if transient:
                        # The session is broken: pycomm3's open() would return
                        # early on it, so build a fresh driver next time.
                        self._drop_driver_locked()
                meta: OperationMeta = {
                    "backend": "cip",
                    "attempts": attempts,
                    "duration_ms": round((time.perf_counter() - start) * 1000.0, 3),
                }
                if phase == "operation" and not repeatable:
                    meta["outcome"] = "unknown"
                    raise OutcomeUnknownError(
                        f"{label}: the request may have reached the controller, but it failed before a reply "
                        f"confirmed it ({message}). It may have been applied; read the value back before "
                        "trying again.",
                        meta,
                    ) from exc
                if transient and attempts <= self.config.max_retries:
                    delay = self._backoff(attempts)
                    logger.info("%s: %s; retrying in %.2f s", label, message, delay)
                    time.sleep(delay)
                    continue
                if not repeatable:
                    meta["outcome"] = "not_sent"
                if transient:
                    raise EIPClientError(f"{label} failed after {attempts} attempt(s): {message}", meta) from exc
                raise EIPClientError(f"{label} failed: {message}", meta) from exc
            meta = {
                "backend": "cip",
                "attempts": attempts,
                "duration_ms": round((time.perf_counter() - start) * 1000.0, 3),
            }
            return result, meta

    async def _run_cip(
        self, label: str, operation: Callable[[Any], Any], repeatable: bool = True
    ) -> tuple[Any, OperationMeta]:
        return await anyio.to_thread.run_sync(self._execute_sync, label, operation, repeatable)

    # -- JSON bridge internals -----------------------------------------------

    async def _json_request(self, payload: dict[str, Any], progress: dict[str, bool]) -> dict[str, Any]:
        """One request/reply. Sets ``progress["sent"]`` once bytes may have left."""
        address = self._address
        try:
            stream = await anyio.connect_tcp(self.config.host, self.config.port)
        except Exception as exc:
            raise _TransientError(f"cannot reach the JSON bridge at {address}: {_describe(exc)}") from exc
        raw = b""
        async with stream:
            try:
                progress["sent"] = True
                await stream.send(json.dumps(payload).encode("utf-8") + b"\n")
                while b"\n" not in raw:
                    try:
                        chunk = await stream.receive(65536)
                    except anyio.EndOfStream:
                        break
                    raw += chunk
                    if len(raw) > _MAX_REPLY_BYTES:
                        raise EIPClientError(f"JSON bridge reply from {address} is too large")
            except EIPClientError:
                raise
            except Exception as exc:
                raise _TransientError(f"JSON bridge I/O error with {address}: {_describe(exc)}") from exc
        line = raw.split(b"\n", 1)[0]
        if not line.strip():
            raise _TransientError(f"JSON bridge at {address} closed the connection without a reply")
        try:
            response = json.loads(line.decode("utf-8"))
        except ValueError as exc:
            raise EIPClientError(f"JSON bridge at {address} sent an undecodable reply: {exc}") from exc
        if not isinstance(response, dict):
            raise EIPClientError(f"JSON bridge at {address} sent a reply that is not an object")
        return response

    async def _json_exchange(
        self, label: str, payload: dict[str, Any], repeatable: bool = True
    ) -> tuple[dict[str, Any], OperationMeta]:
        """Send one request.

        Failing to connect is retried. Once the request may have been sent it
        is repeated only if ``repeatable``; a write whose reply is lost is
        reported as possibly applied instead (``OutcomeUnknownError``).
        """
        start = time.perf_counter()
        attempts = 0
        while True:
            attempts += 1
            progress = {"sent": False}
            try:
                with anyio.fail_after(self.config.timeout):
                    response = await self._json_request(payload, progress)
            except TimeoutError as exc:
                failure: EIPClientError = _TransientError(f"no reply within {self.config.timeout} s")
                failure.__cause__ = exc
            except EIPClientError as exc:
                failure = exc
            else:
                self._connected = True
                self._last_contact = time.time()
                self._last_error = None
                meta = {
                    "backend": "json_bridge",
                    "attempts": attempts,
                    "duration_ms": round((time.perf_counter() - start) * 1000.0, 3),
                }
                return response, meta
            self._connected = False
            self._last_error = f"{label}: {failure}"
            meta = {
                "backend": "json_bridge",
                "attempts": attempts,
                "duration_ms": round((time.perf_counter() - start) * 1000.0, 3),
            }
            if progress["sent"] and not repeatable:
                meta["outcome"] = "unknown"
                raise OutcomeUnknownError(
                    f"{label}: the request was sent, but no valid reply confirmed it ({failure}). "
                    "It may have been applied; read the value back before trying again.",
                    meta,
                ) from failure
            transient = isinstance(failure, _TransientError)
            if transient and attempts <= self.config.max_retries:
                await anyio.sleep(self._backoff(attempts))
                continue
            if not repeatable:
                meta["outcome"] = "not_sent"
            if transient:
                raise EIPClientError(f"{label} failed after {attempts} attempt(s): {failure}", meta) from failure
            raise EIPClientError(f"{label} failed: {failure}", meta) from failure

    @staticmethod
    def _json_data(response: dict[str, Any], label: str, meta: OperationMeta, expect: type = dict) -> Any:
        if not response.get("success"):
            raise EIPClientError(f"{label} failed: {response.get('error') or 'the mock reported an error'}", meta)
        data = response.get("data")
        if not isinstance(data, expect):
            raise EIPClientError(f"{label} failed: the mock sent unexpected data", meta)
        return data


__all__ = [
    "ConfigError",
    "EIPClient",
    "EIPClientConfig",
    "EIPClientError",
    "OutcomeUnknownError",
    "parse_bool",
    "plc_time_payload",
    "split_element_count",
]
