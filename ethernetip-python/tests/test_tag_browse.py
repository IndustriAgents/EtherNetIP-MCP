"""Stable bounded metadata paging; pages never refresh controller definitions."""

import pytest
from fake_pycomm3 import FakeController
from test_tools import call, session

from ethernetip_mcp.domain import tag_browse
from ethernetip_mcp.domain.tag_browse import TagSnapshot


async def test_pages_use_one_snapshot_and_require_matching_scope():
    controller = FakeController()
    controller.tags.update({f"Browse{i:04}": (i, "DINT") for i in range(230)})
    async with session(controller) as client:
        first = await call(client, "get_tag_list", {"prefix": "Browse", "limit": 100})
        assert first["meta"]["total_count"] == 230
        assert first["meta"]["count"] == 100 and first["meta"]["complete"] is False
        cursor = first["meta"]["next_cursor"]
        wrong = await call(client, "get_tag_list", {"prefix": "Other", "cursor": cursor})
        assert not wrong["success"]
        controller.tags["Browse9999"] = (0, "DINT")
        seen = first["data"]["tags"]
        while cursor:
            page = await call(client, "get_tag_list", {"prefix": "Browse", "cursor": cursor, "limit": 100})
            assert page["success"], page
            seen.extend(page["data"]["tags"])
            cursor = page["meta"]["next_cursor"]
        assert len(seen) == 230 and len({t["tag"] for t in seen}) == 230
        assert page["meta"]["complete"] is True
        refreshed = await call(client, "get_tag_list", {"prefix": "Browse"})
        assert refreshed["meta"]["total_count"] == 231
        stale = await call(client, "get_tag_list", {"prefix": "Browse", "cursor": first["meta"]["next_cursor"]})
        assert not stale["success"]


def test_byte_budget_and_snapshot_expiration(monkeypatch):
    # This clock value makes (created + 60) - created round below 60.
    monkeypatch.setattr(tag_browse.time, "monotonic", lambda: 32756.445238821198)
    tags = [{"tag": f"T{i}", "description": "x" * 40} for i in range(5)]
    snapshot = TagSnapshot.capture(tags, None, "")
    monkeypatch.setattr(tag_browse, "MAX_PAGE_BYTES", snapshot.sizes[0] + 1)
    data, meta = snapshot.page(None, "", 500, None)
    assert len(data["tags"]) == 1 and not meta["complete"]
    assert meta["page_bytes"] <= tag_browse.MAX_PAGE_BYTES
    monkeypatch.setattr(tag_browse.time, "monotonic", lambda: snapshot.created + 60)
    with pytest.raises(ValueError, match="expired"):
        snapshot.page(None, "", 1, meta["next_cursor"])


@pytest.mark.parametrize(
    "limit_name,limit", [("MAX_SNAPSHOT_TAGS", 1), ("MAX_SNAPSHOT_BYTES", 1), ("MAX_PAGE_BYTES", 1)]
)
def test_oversized_snapshot_is_refused_not_silently_truncated(monkeypatch, limit_name, limit):
    monkeypatch.setattr(tag_browse, limit_name, limit)
    with pytest.raises(ValueError, match="exceeds"):
        TagSnapshot.capture([{"tag": "A"}, {"tag": "B"}], None, "")


@pytest.mark.parametrize("row", [{}, {"tag": None}, {"tag": 1}])
def test_malformed_metadata_cannot_claim_completeness(row):
    with pytest.raises(ValueError, match="malformed"):
        TagSnapshot.capture([row], None, "")


async def test_cursor_page_does_not_call_native_upload_again():
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from ethernetip_mcp.application.operations import Operations
    from ethernetip_mcp.tools import ToolConfig

    native = SimpleNamespace(get_tag_list=AsyncMock(return_value=([{"tag": "A"}, {"tag": "B"}], {})))
    operations = Operations(SimpleNamespace(client=native, config=ToolConfig(), tag_map=None), lambda: None)
    first = await operations.get_tag_list(limit=1)
    second = await operations.get_tag_list(limit=1, cursor=first["meta"]["next_cursor"])
    assert second["data"]["tags"] == [{"tag": "B"}]
    native.get_tag_list.assert_awaited_once_with(None)
    old = operations.tag_snapshot
    operations._expire_tag_snapshot(old)
    assert operations.tag_snapshot is None
    assert not (await operations.get_tag_list(cursor=first["meta"]["next_cursor"]))["success"]


async def test_bridge_malformed_row_is_not_silently_dropped(monkeypatch):
    from ethernetip_mcp.eip_client import EIPClient, EIPClientConfig, EIPClientError

    client = EIPClient(EIPClientConfig(json_bridge=True))

    async def exchange(*args):
        return {"success": True, "data": [{"tag": "A"}, "broken"]}, {}

    monkeypatch.setattr(client, "_json_exchange", exchange)
    with pytest.raises(EIPClientError, match="malformed"):
        await client.get_tag_list()


def test_empty_prefix_result_is_complete():
    snapshot = TagSnapshot.capture([{"tag": "A"}], None, "Z")
    data, meta = snapshot.page(None, "Z", 100, None)
    assert data["tags"] == [] and meta["complete"] is True
    assert meta["next_cursor"] is None and meta["total_count"] == 0
    assert meta["uploaded_count"] == 1


def test_exact_page_byte_boundary(monkeypatch):
    snapshot = TagSnapshot.capture([{"tag": "A"}, {"tag": "B"}, {"tag": "C"}], None, "")
    monkeypatch.setattr(tag_browse, "MAX_PAGE_BYTES", sum(snapshot.sizes[:2]))
    data, meta = snapshot.page(None, "", 500, None)
    assert len(data["tags"]) == 2
    assert meta["page_bytes"] == tag_browse.MAX_PAGE_BYTES
    assert meta["next_cursor"] is not None


async def test_expiry_callback_does_not_remove_successor():
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from ethernetip_mcp.application.operations import Operations
    from ethernetip_mcp.tools import ToolConfig

    native = SimpleNamespace(get_tag_list=AsyncMock(return_value=([{"tag": "A"}], {})))
    operations = Operations(SimpleNamespace(client=native, config=ToolConfig(), tag_map=None), lambda: None)
    await operations.get_tag_list()
    old = operations.tag_snapshot
    await operations.get_tag_list()
    successor = operations.tag_snapshot
    operations._expire_tag_snapshot(old)
    assert operations.tag_snapshot is successor
    operations.tag_snapshot_expiry.cancel()
