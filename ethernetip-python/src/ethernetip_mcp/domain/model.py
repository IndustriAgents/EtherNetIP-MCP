from __future__ import annotations

import math
from collections.abc import Mapping
from typing import Annotated, Any

from pydantic import Field, StrictInt

TagName = Annotated[
    str,
    Field(
        min_length=1,
        description=(
            "Controller tag, e.g. 'MotorSpeed' or "
            "'Program:MainProgram.MotorSpeed'. Array elements"
            " and members use Logix syntax: 'Tank_Levels[2]',"
            " 'Conveyor_Status.Running'."
        ),
    ),
]
ElementCount = Annotated[
    StrictInt, Field(ge=1, description="Number of consecutive array elements, starting at the tag's index.")
]
Alias = Annotated[str, Field(min_length=1, description="Alias defined in the TAG_MAP_FILE tag map.")]
DataType = Annotated[str, Field(min_length=1, description="Expected Logix data type, e.g. REAL, DINT, BOOL, STRING.")]
_INTEGER_TYPES = {"SINT", "INT", "DINT", "LINT", "USINT", "UINT", "UDINT", "ULINT"}
WRITE_TOOLS = frozenset(
    {"write_tag", "write_array", "write_string", "write_multiple_tags", "write_tag_by_alias", "set_plc_time"}
)
NOT_SENT: dict[str, Any] = {"outcome": "not_sent", "request_sent": False}
LOCAL_TOOLS = frozenset({"list_tags", "get_connection_status"})


def envelope(
    success: bool, data: Any = None, error: str | None = None, meta: Mapping[str, Any] | None = None
) -> dict[str, Any]:
    """Build the shared ``{success, data, error, meta}`` response."""
    return {"success": success, "data": data, "error": error, "meta": dict(meta or {})}


def ok(data: Any = None, meta: Mapping[str, Any] | None = None) -> dict[str, Any]:
    return envelope(True, data=data, meta=meta)


def fail(message: str, meta: Mapping[str, Any] | None = None, data: Any = None) -> dict[str, Any]:
    return envelope(False, data=data, error=message, meta=meta)


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
    return (raw_min, raw_max, eng_min, eng_max)


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
