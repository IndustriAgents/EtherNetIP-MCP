from __future__ import annotations

from collections.abc import Mapping
from typing import Any

_TRUE = {"1", "true", "yes", "y", "on"}
_FALSE = {"0", "false", "no", "n", "off"}
OperationMeta = dict[str, Any]


class ConfigError(ValueError):
    """The ``ENIP_*`` configuration is unusable. Raised at startup."""


class EIPClientError(RuntimeError):
    """An operation on the controller (or the mock) did not succeed."""

    def __init__(self, message: str, meta: OperationMeta | None = None) -> None:
        super().__init__(message)
        self.meta: OperationMeta = dict(meta or {})


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
