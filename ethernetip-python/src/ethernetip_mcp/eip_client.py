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

import contextlib
import datetime as dt
import json
import logging
import math
import os
import re
import socket
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

import anyio
from pycomm3 import ClassCode, CommError, LogixDriver, Services
from pycomm3.cip.status_info import SERVICE_STATUS
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


class _NotSentError(EIPClientError):
    """A write refused locally, before anything was sent to the device."""


class _CallStopped(Exception):
    """Raised so a call stops before sending anything.

    ``reason`` is "deadline" (the call's absolute deadline has passed),
    "closing" (the MCP client disconnected) or "cancelled" (the client
    cancelled the call, which then no longer waits for the worker).
    """

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


_STOP_DETAIL = {
    "deadline": "the call's deadline (ENIP_DEADLINE) was reached before the request was sent",
    "closing": "the MCP client disconnected before the request was sent",
    "cancelled": "the call was cancelled before the request was sent",
}


class _CallState:
    """Shared by a tool call and its worker thread, to agree on what happened.

    ``cancel()`` and ``enter_operation()`` take the same guard, so a worker
    either enters the operation before the call is cancelled (and the call
    knows the request may have been sent) or never sends it at all.
    """

    def __init__(self, closing: Callable[[], bool] = lambda: False, not_after: float | None = None) -> None:
        self._guard = threading.Lock()
        self._closing = closing
        self.not_after = not_after  # time.monotonic() after which nothing may be sent
        self.phase = "open"
        self.cancelled = False
        self.holding_session = False

    def _stop_reason(self) -> str | None:
        if self.cancelled:
            return "cancelled"
        if self._closing():
            return "closing"
        if self.not_after is not None and time.monotonic() >= self.not_after:
            return "deadline"
        return None

    def check(self) -> None:
        reason = self._stop_reason()
        if reason:
            raise _CallStopped(reason)

    def enter_operation(self) -> None:
        """The last check before sending: no I/O between it and the send."""
        with self._guard:
            reason = self._stop_reason()
            if reason:
                raise _CallStopped(reason)
            self.phase = "operation"

    def set_holding(self, holding: bool) -> None:
        with self._guard:
            self.holding_session = holding

    def cancel(self) -> tuple[str, bool]:
        """Stop the worker; returns the phase it reached and whether it holds the session."""
        with self._guard:
            self.cancelled = True
            return self.phase, self.holding_session


def with_outcome(meta: OperationMeta, outcome: str) -> OperationMeta:
    """Add the write outcome and whether the request may have reached the device."""
    return {**meta, "outcome": outcome, "request_sent": outcome != "not_sent"}


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
    write_probe_idle: float = 10.0
    deadline: float = 0.0  # 0: derived from timeout, retries and backoff

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
        if self.write_probe_idle < 0:
            raise ConfigError("ENIP_WRITE_PROBE_IDLE must be 0 or more")
        if self.deadline < 0:
            raise ConfigError("ENIP_DEADLINE must be 0 (automatic) or more")
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
            write_probe_idle=_parse_float(env, "ENIP_WRITE_PROBE_IDLE", 10.0, 0.0, 86400.0),
            deadline=_parse_float(env, "ENIP_DEADLINE", 0.0, 0.0, 86400.0),
        )

    def deadline_s(self) -> float:
        """Overall limit for one tool call's exchange with the device.

        ENIP_DEADLINE if set, else ENIP_TIMEOUT x (ENIP_MAX_RETRIES + 2) plus
        the backoff waits plus 15 s, which leaves room for a liveness probe
        and a reconnect before a write.
        """
        if self.deadline > 0:
            return self.deadline
        waits = sum(min(self.retry_backoff_base * 2 ** (n - 1), MAX_BACKOFF_S) for n in range(1, self.max_retries + 1))
        return self.timeout * (self.max_retries + 2) + waits + 15.0

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


def _entry(tag: str, value: Any, data_type: Any, error: str | None, outcome: str) -> dict[str, Any]:
    """One write_multiple_tags result."""
    return {
        "tag": tag,
        "value": _jsonable(value),
        "data_type": data_type,
        "error": error,
        "outcome": outcome,
        "request_sent": outcome != "not_sent",
    }


def _base_type(data_type: Any) -> str | None:
    if not isinstance(data_type, str) or not data_type.strip():
        return None
    return data_type.split("[", 1)[0].strip().upper()


_NUMERIC_TYPES = {"SINT", "INT", "DINT", "LINT", "USINT", "UINT", "UDINT", "ULINT", "REAL", "LREAL"}

# How a failed CIP write is reported, from pycomm3 1.2.14's own texts:
#
# not_sent - pycomm3 refused it before sending anything: _parse_requested_tags
#   and _get_tag_info ("Tag doesn't exist", "Failed to parse tag request",
#   "failed to get tag data"), the request builders ("Invalid Tag Request - ...",
#   "Error encoding value - ...", "Failed to build/create request path for
#   tag"), and CIPDriver.send(), which does not send a request that already has
#   an error ("No response data received"). Case-sensitive on purpose: "Invalid
#   tag request - ..." (lower case) is raised after sending.
# rejected - the controller answered with a CIP general status that refuses
#   the request before executing it (the allow-list below, built from
#   pycomm3.cip.status_info so the texts match exactly).
# unknown  - everything else: a garbled reply ("Failed to parse reply"), the
#   "Unknown Error" fallback, a fragmented write that failed part-way, partial
#   transfers, embedded or vendor-specific errors, timeouts, ...
_LOCAL_WRITE_ERRORS = (
    "Tag doesn't exist",
    "Failed to parse tag request",
    "failed to get tag data",
    "Invalid Tag Request",
    "Error encoding value",
    "Failed to build request path for tag",
    "Failed to create request path for tag",
    "No response data received",
)
_PRE_EXECUTION_STATUS = (
    0x02,  # Insufficient resource
    0x03,  # Invalid value
    0x04,  # IOI syntax error (path segment error)
    0x05,  # Destination unknown / object undefined
    0x08,  # Service not supported
    0x09,  # Error in data segment or invalid attribute value
    0x0C,  # Object state conflict
    0x0E,  # Attribute not settable
    0x0F,  # Permission denied (privilege violation)
    0x10,  # Device state conflict
    0x13,  # Insufficient command data
    0x14,  # Attribute not supported
    0x15,  # Too much data
    0x16,  # Object does not exist
    0x1A,  # Bridge request too large (never delivered)
    0x25,  # Key segment error
    0x26,  # Invalid IOI error (path size)
    0x28,  # Invalid member ID
    0x29,  # Member not settable
)
# Logix extended codes of general status 0xFF that refuse the request outright.
_PRE_EXECUTION_GENERAL_ERROR = (
    0x0007,  # Wrong data type
    0x2001,  # Excessive IOI
    0x2002,  # Bad parameter value
    0x2018,  # Semaphore reject
    0x201B,  # Size too small
    0x201C,  # Invalid size
    0x2100,  # Privilege failure
    0x2101,  # Invalid keyswitch position
    0x2102,  # Password invalid
    0x2103,  # No password issued
    0x2104,  # Address out of range
    0x2106,  # Data in use
    0x2107,  # Tag type used in request does not match the target tag's data type
    0x2108,  # Controller in upload or download mode
    0x2109,  # Attempt to change number of array dimensions
    0x210A,  # Invalid symbol name
    0x210B,  # Symbol does not exist
)


def _is_pre_execution_refusal(error: str) -> bool:
    for code in _PRE_EXECUTION_STATUS:
        text = SERVICE_STATUS[code]
        if error == text or error.startswith(f"{text} - "):
            return True
    general = SERVICE_STATUS[0xFF]
    if error.startswith(f"{general} - "):
        return any(error.endswith(f"(ff, {ext:0>2x})") for ext in _PRE_EXECUTION_GENERAL_ERROR)
    return False


def write_error_outcome(error: str) -> str:
    """Classify a pycomm3 error for a write-like request: not_sent, rejected or unknown."""
    if any(marker in error for marker in _LOCAL_WRITE_ERRORS):
        return "not_sent"
    if _is_pre_execution_refusal(error):
        return "rejected"
    return "unknown"


def _maybe_applied(label: str, detail: str) -> str:
    return (
        f"{label}: {detail}. The write may have been applied; read the value back before trying again "
        "(it is never re-sent automatically)."
    )


def _outcome_error(label: str, error: str) -> EIPClientError:
    """The exception for a write-like request that came back with ``error``."""
    outcome = write_error_outcome(error)
    if outcome == "unknown":
        return OutcomeUnknownError(_maybe_applied(label, f"the controller's reply did not confirm it ({error})"))
    if outcome == "not_sent":
        return EIPClientError(f"{label} refused before sending: {error}")
    return EIPClientError(f"{label} failed: the controller refused it ({error})")


def _split_bit(tag: str) -> tuple[str, bool]:
    """'Word.5' -> ('Word', True): a trailing number is a bit of an integer."""
    parts = tag.split(".")
    minimum = 3 if tag.startswith("Program:") else 2
    if len(parts) >= minimum and parts[-1].isdigit():
        return ".".join(parts[:-1]), True
    return tag, False


_TRAILING_INDEX = re.compile(r"^(?P<name>.+)\[(?P<index>\d+)\]$")


def _bool_into_number(value: Any, info: Mapping[str, Any], path: str) -> str | None:
    """Find a boolean headed for a numeric tag or structure member; returns where."""
    if isinstance(value, dict):
        data_type = info.get("data_type")
        members = data_type.get("internal_tags") if isinstance(data_type, Mapping) else None
        if not isinstance(members, Mapping):
            return None
        for key, item in value.items():
            member = members.get(key)
            if isinstance(member, Mapping):
                found = _bool_into_number(item, member, f"{path}.{key}")
                if found:
                    return found
        return None
    if isinstance(value, list):
        for item in value:
            found = _bool_into_number(item, info, path)
            if found:
                return found
        return None
    data_type = info.get("data_type_name")
    if isinstance(value, bool) and isinstance(data_type, str) and data_type.upper() in _NUMERIC_TYPES:
        return f"{path} is {data_type}"
    return None


def check_cip_write(driver: Any, tag: str, value: Any, elements: int | None = None) -> None:
    """Refuse, before sending, a write pycomm3 would get wrong or reject.

    Uses the tag definitions pycomm3 uploaded at connect (no network I/O):
    the tag must exist; a boolean is never written into a numeric tag or
    structure member (pycomm3 would silently write 1/0); and a write to a
    one-dimensional array must fit inside it, so the controller cannot apply
    part of it.
    """
    name, is_bit = _split_bit(tag)
    try:
        info = driver.get_tag_info(name) or {}
    except Exception as exc:  # pycomm3 raises RequestError for an unknown tag
        raise _NotSentError(_describe(exc)) from exc
    if is_bit or not isinstance(info, Mapping):
        return
    found = _bool_into_number(value, info, tag)
    if found:
        raise _NotSentError(f"{found}; a boolean is not accepted for a numeric value")
    dimensions = info.get("dimensions") or []
    if info.get("dim") == 1 and dimensions and dimensions[0] and info.get("data_type_name") != "DWORD":
        match = _TRAILING_INDEX.match(name)
        start = int(match.group("index")) if match else 0
        count = elements or 1
        if start + count > int(dimensions[0]):
            raise _NotSentError(
                f"{tag} has {dimensions[0]} elements; writing {count} from index {start} would go past the end"
            )


def _type_warning(requested: Any, actual: Any) -> str | None:
    """pycomm3 always encodes with the controller's type; flag a different request."""
    if _base_type(requested) and _base_type(actual) and _base_type(actual) != _base_type(requested):
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
        self._last_io = 0.0  # time.monotonic() of the last successful exchange on the CIP session
        self._active_driver: Any = None  # the driver a worker thread may be blocked in
        self._closing = False  # set by shutdown(): the MCP client is gone

    @property
    def backend(self) -> str:
        return "json_bridge" if self.config.json_bridge else "cip"

    # -- lifecycle ----------------------------------------------------------

    def shutdown(self) -> None:
        """The MCP client is gone: nothing more may be sent to the device.

        Calls already past the point of sending are unaffected; every other
        pending or later call stops before connecting or sending (not_sent).
        The server also cancels the in-flight tool calls.
        """
        self._closing = True

    @property
    def closing(self) -> bool:
        return self._closing

    async def ensure_connection(self) -> None:
        """Try once to open the CIP session at startup.

        Never raises: an unreachable controller must not stop the MCP server
        from answering ``initialize``. Each tool call reconnects as needed and
        reports its own errors. The JSON bridge has no session to open.
        """
        if self.config.json_bridge or self._closing:
            return
        deadline = self.config.deadline_s()
        state = _CallState(lambda: self._closing, time.monotonic() + deadline)
        try:
            try:
                with anyio.fail_after(deadline):
                    await anyio.to_thread.run_sync(self._connect_once, state, abandon_on_cancel=True)
            except TimeoutError:
                _, holding = state.cancel()
                if holding:
                    self._abort_active_socket()
                raise EIPClientError(f"no complete answer within the {deadline:g} s deadline (ENIP_DEADLINE)") from None
            except BaseException:
                _, holding = state.cancel()
                if holding:
                    self._abort_active_socket()
                raise
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
            # One deadline for the whole call, shared by every tag's request.
            not_after = self.call_deadline()
            results = []
            attempts = 0
            stopped: str | None = None
            for base, count in parsed:
                if stopped is not None:
                    results.append({"tag": base, "value": None, "data_type": None, "error": stopped})
                    continue
                payload: dict[str, Any] = {"op": "read", "tag": base}
                if count is not None:
                    payload["count"] = count
                try:
                    response, meta = await self._json_exchange(f"read_tag({base})", payload, not_after=not_after)
                except EIPClientError as exc:
                    attempts += exc.meta.get("attempts", 1)
                    results.append({"tag": base, "value": None, "data_type": None, "error": str(exc)})
                    stopped = (
                        "not read: the call's deadline (ENIP_DEADLINE) was reached"
                        if time.monotonic() >= not_after
                        else f"not read: the batch stopped after the connection failed on {base}"
                    )
                    continue
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
        """Write one tag, at most once. meta carries outcome and request_sent."""
        base, request = self._write_request(tag, value)
        label = f"write_tag({base})"
        if self.config.json_bridge:
            payload: dict[str, Any] = {"op": "write", "tag": base, "value": value}
            if data_type:
                payload["data_type"] = data_type
            response, meta = await self._json_exchange(label, payload, repeatable=False)
            if not response.get("success"):
                error = response.get("error") or "the mock refused the write"
                raise EIPClientError(f"{label} failed: {error}", with_outcome(meta, "rejected"))
            data = response.get("data") if isinstance(response.get("data"), dict) else {}
            result = {"tag": base, "value": data.get("value", value), "data_type": data.get("data_type")}
            return result, with_outcome(meta, "written")

        def _op(driver: Any) -> Any:
            check_cip_write(driver, base, value, split_element_count(request, None)[1])
            # LogixDriver.write(*tags_values) takes (tag, value) tuples and has
            # no data type argument: pycomm3 encodes with the controller's own
            # tag definition. A list is written as Tag{N}.
            return driver.write((request, value))

        tag_result, meta = await self._run_cip(label, _op, repeatable=False)
        if tag_result.error:
            failure = _outcome_error(label, str(tag_result.error))
            failure.meta = with_outcome(meta, write_error_outcome(str(tag_result.error)))
            raise failure
        actual = tag_result.type
        meta = with_outcome(meta, "written")
        warning = _type_warning(data_type, actual)
        if warning:
            meta["warning"] = warning
        return {"tag": base, "value": _jsonable(value), "data_type": actual}, meta

    async def write_multiple_tags(
        self, items: list[tuple[str, Any, str | None]]
    ) -> tuple[list[dict[str, Any]], OperationMeta]:
        """Write ``(tag, value, data_type)`` entries; ``data_type`` may be None.

        Always returns one result per entry with an ``outcome`` (written,
        rejected, unknown or not_sent) and ``request_sent``. Nothing is sent
        twice. Raises only ValueError, for bad input, before anything is sent.
        """
        if not items:
            raise ValueError("payloads must contain at least one entry")
        prepared = [(*self._write_request(tag, value), value, data_type) for tag, value, data_type in items]
        if self.config.json_bridge:
            return await self._json_write_multiple(prepared)

        refused: dict[int, str] = {}
        sent: list[int] = []

        def _op(driver: Any) -> list[Any]:
            refused.clear()
            sent.clear()
            pairs = []
            for index, (base, request, value, _) in enumerate(prepared):
                try:
                    check_cip_write(driver, base, value, split_element_count(request, None)[1])
                except _NotSentError as exc:
                    refused[index] = str(exc)
                    continue
                sent.append(index)
                pairs.append((request, value))
            return _as_tag_list(driver.write(*pairs)) if pairs else []

        try:
            tag_results, meta = await self._run_cip("write_multiple_tags", _op, repeatable=False)
        except EIPClientError as exc:
            # The entries that went out share one request, so they share its fate.
            failed = exc.meta.get("outcome", "unknown")
            results = []
            for index, (base, _, value, _) in enumerate(prepared):
                if index in refused:
                    results.append(_entry(base, value, None, refused[index], "not_sent"))
                else:
                    outcome = failed if index in sent or failed == "not_sent" else "not_sent"
                    error = str(exc) if outcome == failed else "not sent: the batch request failed first"
                    results.append(_entry(base, value, None, error, outcome))
            return results, exc.meta

        by_index = dict(zip(sent, tag_results, strict=True))
        results = []
        for index, (base, _, value, data_type) in enumerate(prepared):
            if index in refused:
                results.append(_entry(base, value, None, refused[index], "not_sent"))
                continue
            tag_result = by_index[index]
            if tag_result.error:
                error = str(tag_result.error)
                outcome = write_error_outcome(error)
                results.append(_entry(base, value, None, str(_outcome_error(f"write_tag({base})", error)), outcome))
                continue
            entry = _entry(base, value, tag_result.type, None, "written")
            warning = _type_warning(data_type, tag_result.type)
            if warning:
                entry["warning"] = warning
            results.append(entry)
        return results, meta

    async def _json_write_multiple(
        self, prepared: list[tuple[str, str, Any, str | None]]
    ) -> tuple[list[dict[str, Any]], OperationMeta]:
        # One deadline for the whole call: entries not sent by then are not_sent.
        not_after = self.call_deadline()
        results: list[dict[str, Any]] = []
        attempts = 0
        stopped: str | None = None
        for base, _, value, data_type in prepared:
            if stopped is not None:
                results.append(_entry(base, value, None, stopped, "not_sent"))
                continue
            payload: dict[str, Any] = {"op": "write", "tag": base, "value": value}
            if data_type:
                payload["data_type"] = data_type
            try:
                response, meta = await self._json_exchange(
                    f"write_tag({base})", payload, repeatable=False, not_after=not_after
                )
            except EIPClientError as exc:
                attempts += exc.meta.get("attempts", 1)
                results.append(_entry(base, value, None, str(exc), exc.meta.get("outcome", "unknown")))
                stopped = (
                    "not sent: the call's deadline (ENIP_DEADLINE) was reached before sending"
                    if time.monotonic() >= not_after
                    else f"not sent: the batch stopped after the connection failed on {base}"
                )
                continue
            attempts += meta.get("attempts", 1)
            if response.get("success"):
                data = response.get("data") if isinstance(response.get("data"), dict) else {}
                results.append(_entry(base, data.get("value", value), data.get("data_type"), None, "written"))
            else:
                error = str(response.get("error") or "the mock refused the write")
                results.append(_entry(base, value, None, error, "rejected"))
        return results, {"backend": self.backend, "attempts": attempts}

    async def set_plc_time(self) -> tuple[dict[str, Any], OperationMeta]:
        """Set the controller clock to this host's current time, at most once."""
        microseconds = int(time.time() * 1_000_000)
        if self.config.json_bridge:
            response, meta = await self._json_exchange(
                "set_plc_time", {"op": "set_time", "microseconds": microseconds}, repeatable=False
            )
            if not response.get("success"):
                error = response.get("error") or "the mock refused the request"
                raise EIPClientError(f"set_plc_time failed: {error}", with_outcome(meta, "rejected"))
            data = response.get("data") if isinstance(response.get("data"), dict) else {}
            return plc_time_payload(int(data.get("microseconds", microseconds))), with_outcome(meta, "written")
        tag_result, meta = await self._run_cip(
            "set_plc_time", lambda driver: driver.set_plc_time(microseconds=microseconds), repeatable=False
        )
        if tag_result.error:
            failure = _outcome_error("set_plc_time", str(tag_result.error))
            failure.meta = with_outcome(meta, write_error_outcome(str(tag_result.error)))
            raise failure
        return plc_time_payload(microseconds), with_outcome(meta, "written")

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
            self._active_driver = driver
            return driver
        self._drop_driver_locked()
        driver = self._driver_factory(self.config.cip_path())
        self._active_driver = driver
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
        self._last_io = time.monotonic()
        return driver

    def _ensure_live_locked(self, driver: Any) -> Any:
        """Before a write, check that a session idle for a while still answers.

        A controller may drop an idle session; a write sent on it then fails in
        a way that cannot be told apart from a lost reply, so it would be
        reported as outcome "unknown". When the session has been idle for
        ENIP_WRITE_PROBE_IDLE seconds or more, a cheap, repeatable read of the
        identity object goes first; if it fails, the session is rebuilt before
        the write is sent. Caller must hold ``self._lock``.
        """
        idle = time.monotonic() - self._last_io
        if idle < self.config.write_probe_idle:
            return driver
        try:
            reply = driver.generic_message(
                service=Services.get_attribute_single,
                class_code=ClassCode.identity_object,
                instance=1,
                attribute=1,
                connected=True,
                name="liveness_probe",
            )
            problem = getattr(reply, "error", None)
        except Exception as exc:  # noqa: BLE001 - any failure means: reconnect first
            problem = _describe(exc)
        if problem:
            # The probe is a read, so reconnecting and going on is safe.
            logger.info(
                "Session idle %.1f s failed a liveness probe (%s); reconnecting before the write", idle, problem
            )
            self._drop_driver_locked()
            return self._open_locked()
        self._last_io = time.monotonic()
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

    @contextlib.contextmanager
    def _session(self, state: _CallState | None = None, timeout: float | None = None) -> Any:
        """Hold the CIP session lock, waiting at most one deadline for it.

        With ``state``, record that this call's worker holds the session, so a
        deadline or cancellation closes the socket only when it is this
        call's own connection, never a session a queued call is waiting for.
        """
        if not self._lock.acquire(timeout=self.config.deadline_s() if timeout is None else timeout):
            raise EIPClientError("the CIP session is still busy with an earlier request")
        try:
            if state is not None:
                state.set_holding(True)
            yield
        finally:
            if state is not None:
                state.set_holding(False)
            self._lock.release()

    def _abort_active_socket(self) -> None:
        """Unblock a worker stuck in socket I/O after its deadline: close the socket.

        pycomm3 keeps its socket in ``driver._sock.sock``; closing it makes a
        blocked send/recv fail at once, so the abandoned thread releases the
        session lock and stops (it checks the cancelled call state).
        """
        sock = getattr(getattr(self._active_driver, "_sock", None), "sock", None)
        if sock is None:
            return
        with contextlib.suppress(OSError):
            sock.shutdown(socket.SHUT_RDWR)
        with contextlib.suppress(OSError):
            sock.close()

    def _connect_once(self, state: _CallState | None = None) -> None:
        state = state or _CallState()
        with self._session(state):
            state.check()
            self._open_locked()
            self._last_contact = time.time()
            self._last_error = None

    def _disconnect_sync(self) -> None:
        with contextlib.suppress(EIPClientError), self._session(timeout=min(self.config.deadline_s(), 5.0)):
            self._drop_driver_locked()

    def _execute_sync(
        self,
        label: str,
        operation: Callable[[Any], Any],
        repeatable: bool = True,
        state: _CallState | None = None,
    ) -> tuple[Any, OperationMeta]:
        """Run ``operation`` on a connected driver.

        Opening the session is retried on connection-level failures. The
        operation itself is retried only if ``repeatable``: a write (or
        set_plc_time) that fails after it may have reached the controller,
        for example because its reply was lost, is never sent again. For a
        non-repeatable operation, meta always says whether the request was
        sent (``outcome``/``request_sent``) when it fails.
        """
        state = state or _CallState(lambda: self._closing)
        start = time.perf_counter()
        attempts = 0
        while True:
            attempts += 1
            phase = "open"
            try:
                state.check()
                with self._session(state):
                    state.check()
                    driver = self._open_locked()
                    if not repeatable:
                        driver = self._ensure_live_locked(driver)
                    state.enter_operation()
                    phase = "operation"
                    result = operation(driver)
                    self._last_contact = time.time()
                    self._last_io = time.monotonic()
                    self._last_error = None
            except _CallStopped:
                raise
            except _NotSentError as exc:
                meta = {
                    "backend": "cip",
                    "attempts": attempts,
                    "duration_ms": round((time.perf_counter() - start) * 1000.0, 3),
                }
                raise EIPClientError(f"{label} refused before sending: {exc}", with_outcome(meta, "not_sent")) from exc
            except Exception as exc:
                transient = _is_transient(exc)
                message = _describe(exc)
                with self._session():
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
                    raise OutcomeUnknownError(
                        _maybe_applied(
                            label,
                            f"the request may have reached the controller, but it failed before a "
                            f"reply confirmed it ({message})",
                        ),
                        with_outcome(meta, "unknown"),
                    ) from exc
                if transient and attempts <= self.config.max_retries:
                    state.check()
                    delay = self._backoff(attempts)
                    logger.info("%s: %s; retrying in %.2f s", label, message, delay)
                    time.sleep(delay)
                    state.check()
                    continue
                if not repeatable:
                    meta = with_outcome(meta, "not_sent")
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
        """Run ``_execute_sync`` in a worker thread, bounded by the overall deadline.

        pycomm3 bounds each socket call by ENIP_TIMEOUT, but a peer that keeps
        trickling bytes (or a garbled length field) can keep one request going
        far longer. On expiry the call answers at once: a read fails cleanly,
        and a write is reported unknown if it may have been sent, not_sent if
        it was still connecting. The socket is closed to stop the worker.
        """
        deadline = self.config.deadline_s()
        state = _CallState(lambda: self._closing, time.monotonic() + deadline)
        start = time.perf_counter()
        try:
            with anyio.fail_after(deadline):
                return await anyio.to_thread.run_sync(
                    self._execute_sync, label, operation, repeatable, state, abandon_on_cancel=True
                )
        except _CallStopped as exc:
            # The worker refused to start or to send: the deadline passed or the client is gone.
            meta: OperationMeta = {"backend": "cip", "duration_ms": round((time.perf_counter() - start) * 1000.0, 3)}
            detail = _STOP_DETAIL.get(exc.reason, exc.reason)
            raise EIPClientError(
                f"{label} stopped: {detail}", meta if repeatable else with_outcome(meta, "not_sent")
            ) from exc
        except TimeoutError as exc:
            phase, holding = state.cancel()
            if holding:
                self._abort_active_socket()
            detail = f"no complete answer within the {deadline:g} s deadline (ENIP_DEADLINE)"
            self._last_error = f"{label}: {detail}"
            meta = {
                "backend": "cip",
                "duration_ms": round((time.perf_counter() - start) * 1000.0, 3),
                "deadline_s": deadline,
            }
            if repeatable:
                raise EIPClientError(f"{label} failed: {detail}", meta) from exc
            if phase == "operation":
                raise OutcomeUnknownError(_maybe_applied(label, detail), with_outcome(meta, "unknown")) from exc
            raise EIPClientError(f"{label} failed: {detail}; nothing was sent", with_outcome(meta, "not_sent")) from exc
        except BaseException:
            # Cancelled by the MCP client (notifications/cancelled, or it
            # disconnected): the abandoned worker must never send afterwards.
            # It stops at its next check or at enter_operation. Only this
            # call's own connection attempt is closed; an operation already in
            # flight is left to finish.
            phase, holding = state.cancel()
            if holding and phase == "open":
                self._abort_active_socket()
            raise

    # -- JSON bridge internals -----------------------------------------------

    async def _json_request(
        self, payload: dict[str, Any], progress: dict[str, bool], not_after: float | None = None
    ) -> dict[str, Any]:
        """One request/reply. Sets ``progress["sent"]`` once bytes may have left."""
        address = self._address
        self._refuse_if_stopped(not_after)  # before connecting
        try:
            stream = await anyio.connect_tcp(self.config.host, self.config.port)
        except Exception as exc:
            raise _TransientError(f"cannot reach the JSON bridge at {address}: {_describe(exc)}") from exc
        raw = b""
        async with stream:
            self._refuse_if_stopped(not_after)  # with no await between this check and the send
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

    def _refuse_if_stopped(self, not_after: float | None) -> None:
        if self._closing:
            raise _CallStopped("closing")
        if not_after is not None and time.monotonic() >= not_after:
            raise _CallStopped("deadline")

    def call_deadline(self) -> float:
        """The absolute deadline (time.monotonic()) for a tool call starting now."""
        return time.monotonic() + self.config.deadline_s()

    async def _json_exchange(
        self, label: str, payload: dict[str, Any], repeatable: bool = True, not_after: float | None = None
    ) -> tuple[dict[str, Any], OperationMeta]:
        """Send one request.

        Failing to connect is retried. Once the request may have been sent it
        is repeated only if ``repeatable``; a write whose reply is lost is
        reported as possibly applied instead (``OutcomeUnknownError``). Each
        attempt is limited by ENIP_TIMEOUT, and everything by ``not_after``:
        the tool call's single absolute deadline, shared by every request the
        call makes (a batch passes the same one to each entry). Nothing is
        connected or sent once it has passed.
        """
        start = time.perf_counter()
        not_after = self.call_deadline() if not_after is None else not_after
        attempts = 0
        while True:
            attempts += 1
            progress = {"sent": False}
            limit = max(min(self.config.timeout, not_after - time.monotonic()), 0.0)
            try:
                self._refuse_if_stopped(not_after)
                with anyio.fail_after(limit):
                    response = await self._json_request(payload, progress, not_after)
            except _CallStopped as stopped:
                meta = {"backend": "json_bridge", "attempts": attempts}
                detail = _STOP_DETAIL.get(stopped.reason, stopped.reason)
                raise EIPClientError(
                    f"{label} stopped: {detail}", meta if repeatable else with_outcome(meta, "not_sent")
                ) from None
            except TimeoutError as exc:
                failure: EIPClientError = _TransientError(f"no complete reply within {limit:g} s")
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
                raise OutcomeUnknownError(
                    _maybe_applied(label, f"the request was sent, but no valid reply confirmed it ({failure})"),
                    with_outcome(meta, "unknown"),
                ) from failure
            transient = isinstance(failure, _TransientError)
            delay = self._backoff(attempts)
            in_time = time.monotonic() + delay < not_after
            if transient and attempts <= self.config.max_retries and in_time:
                await anyio.sleep(delay)
                continue
            if not repeatable:
                meta = with_outcome(meta, "not_sent")
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
    "check_cip_write",
    "parse_bool",
    "plc_time_payload",
    "split_element_count",
    "with_outcome",
    "write_error_outcome",
]
