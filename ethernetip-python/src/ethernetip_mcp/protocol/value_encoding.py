"""Preserve integers across JSON consumers using IEEE-754 binary64 numbers."""

from typing import Any

MAX_SAFE_INTEGER = 2**53 - 1


def encode_integers(envelope: dict[str, Any]) -> dict[str, Any]:
    """Apply only at the MCP edge, after all engineering arithmetic and audit."""
    paths: list[str] = []

    def encode(value: Any, path: str) -> Any:
        if isinstance(value, int) and not isinstance(value, bool):
            if abs(value) > MAX_SAFE_INTEGER:
                paths.append(path)
                return str(value)
            return value
        if isinstance(value, dict):
            return {
                key: encode(item, path + "/" + str(key).replace("~", "~0").replace("/", "~1"))
                for key, item in value.items()
            }
        if isinstance(value, (list, tuple)):
            return [encode(item, path + "/" + str(index)) for index, item in enumerate(value)]
        return value

    result = encode(envelope, "")
    if paths:
        result["meta"] = {
            **result.get("meta", {}),
            "value_encoding": "decimal_string",
            "encoded_integer_paths": paths,
        }
    return result
