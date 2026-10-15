from __future__ import annotations

from typing import Annotated, Any

from pydantic import Field

from ethernetip_mcp.domain.errors import EIPClientError
from ethernetip_mcp.domain.model import Alias, ElementCount, TagName, _error_meta, _scale, _scaling, fail, ok


class ReadOperations:
    async def read_tag(self, tag_name: TagName, count: ElementCount | None = None) -> dict[str, Any]:
        """Read one tag from the controller."""
        return await self.do_read("read_tag", tag_name, count)

    async def read_array(self, tag_name: TagName, elements: ElementCount) -> dict[str, Any]:
        """Read `elements` consecutive elements of an array tag as a list."""
        return await self.do_read("read_array", tag_name, elements)

    async def read_string(self, tag_name: TagName) -> dict[str, Any]:
        """Read a STRING tag. Fails with success=false if the tag does not hold a string."""
        response = await self.do_read("read_string", tag_name, None)
        if response["success"] and (not isinstance(response["data"]["value"], str)):
            data = response["data"]
            return fail(
                f"Tag '{data['tag']}' is not a string (data_type {data.get('data_type')}); use read_tag",
                {**response["meta"]},
            )
        return response

    async def read_multiple_tags(
        self, tags: Annotated[list[Annotated[str, Field(min_length=1)]], Field(min_length=1)]
    ) -> dict[str, Any]:
        """Read several tags in one call."""
        try:
            results, meta = await self.client.read_multiple_tags(tags)
        except (EIPClientError, ValueError) as exc:
            return fail(str(exc), _error_meta(exc, tool="read_multiple_tags", tags=tags))
        meta = {**meta, "count": len(results)}
        failed = [r for r in results if r["error"]]
        if failed:
            detail = "; ".join(f"{r['tag']}: {r['error']}" for r in failed)
            return fail(f"{len(failed)} of {len(results)} reads failed: {detail}", meta, {"results": results})
        return ok({"results": results}, meta)

    async def read_tag_by_alias(self, alias: Alias) -> dict[str, Any]:
        """Read the tag behind a tag-map alias, converted to engineering units if the alias defines scaling."""
        spec, error = self.alias_spec(alias, "read_tag_by_alias")
        if error:
            return error
        response = await self.do_read("read_tag_by_alias", spec["tag"], None)
        if not response["success"]:
            return response
        data = response["data"]
        try:
            scaled = _scale(data["value"], _scaling(spec), to_engineering=True)
        except ValueError as exc:
            return fail(f"Alias '{alias}': {exc}", response["meta"])
        return ok({**data, "alias": alias, "raw_value": data["value"], "value": scaled}, response["meta"])
