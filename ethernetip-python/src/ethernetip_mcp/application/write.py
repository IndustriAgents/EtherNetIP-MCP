from __future__ import annotations

from typing import Annotated, Any

from pydantic import Field

from ethernetip_mcp.domain.errors import EIPClientError
from ethernetip_mcp.domain.model import (
    NOT_SENT,
    Alias,
    DataType,
    TagName,
    _error_meta,
    _round_for_type,
    _scale,
    _scaling,
    fail,
    ok,
)


class WriteOperations:
    async def write_tag(self, tag_name: TagName, value: Any, data_type: DataType | None = None) -> dict[str, Any]:
        """Write one tag on the controller. Refused unless ENIP_WRITES_ENABLED=true."""
        return await self.do_write("write_tag", tag_name, value, data_type)

    async def write_array(self, tag_name: TagName, values: Annotated[list[Any], Field(min_length=1)]) -> dict[str, Any]:
        """Write a list of values to consecutive array elements. Refused unless ENIP_WRITES_ENABLED=true."""
        return await self.do_write("write_array", tag_name, values, None)

    async def write_string(self, tag_name: TagName, value: str) -> dict[str, Any]:
        """Write text to a STRING tag. Refused unless ENIP_WRITES_ENABLED=true."""
        return await self.do_write("write_string", tag_name, value, None)

    async def write_multiple_tags(
        self,
        payloads: Annotated[
            list[dict[str, Any]],
            Field(
                min_length=1, description="Entries of the form {'tag_name': ..., 'value': ..., 'data_type': optional}."
            ),
        ],
    ) -> dict[str, Any]:
        """Write several tags in one call. Refused unless ENIP_WRITES_ENABLED=true."""
        refused = self.writes_refused("write_multiple_tags")
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
            results, meta = await self.client.write_multiple_tags(items)
        except ValueError as exc:
            return fail(str(exc), {"tool": "write_multiple_tags", **NOT_SENT})
        meta = {**meta, "count": len(results)}
        failed = [r for r in results if r["outcome"] != "written"]
        if failed:
            detail = "; ".join(f"{r['tag']} ({r['outcome']}): {r['error']}" for r in failed)
            return fail(
                f"{len(failed)} of {len(results)} writes were not confirmed: {detail}", meta, {"results": results}
            )
        return ok({"results": results}, meta)

    async def write_tag_by_alias(self, alias: Alias, value: Any) -> dict[str, Any]:
        """Write the tag behind a tag-map alias. Refused unless ENIP_WRITES_ENABLED=true."""
        refused = self.writes_refused("write_tag_by_alias")
        if refused:
            return refused
        spec, error = self.alias_spec(alias, "write_tag_by_alias")
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
        response = await self.do_write("write_tag_by_alias", spec["tag"], raw, spec.get("data_type"))
        if not response["success"]:
            return response
        return ok({**response["data"], "alias": alias, "raw_value": raw, "value": value}, response["meta"])

    async def set_plc_time(self) -> dict[str, Any]:
        """Set the controller's wall clock to this host's current time."""
        refused = self.system_refused("set_plc_time")
        if refused:
            return refused
        try:
            payload, meta = await self.client.set_plc_time()
        except EIPClientError as exc:
            return fail(str(exc), _error_meta(exc, tool="set_plc_time"))
        return ok({"updated": True, **payload}, meta)
