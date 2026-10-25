"""Native reply cardinality cannot be invented from the request."""

from unittest.mock import AsyncMock

import pytest
from pycomm3 import Tag

from ethernetip_mcp.eip_client import EIPClient, EIPClientConfig, EIPClientError


@pytest.mark.parametrize("returned", [0, 1, 3])
async def test_malformed_batch_count_preserves_every_requested_slot(returned):
    client = EIPClient(EIPClientConfig())
    client._run_cip = AsyncMock(return_value=([Tag("A", 1, "DINT", None)] * returned, {}))
    results, meta = await client.read_multiple_tags(["A", "B"])
    assert [item["tag"] for item in results] == ["A", "B"]
    assert all(item["value"] is None and item["error"] for item in results)
    assert meta["complete"] is False
    assert client._run_cip.await_count == 1


@pytest.mark.parametrize("value", [[], [1], [1, 2, 3], 1])
async def test_array_read_refuses_wrong_element_count(value):
    client = EIPClient(EIPClientConfig())
    client._run_cip = AsyncMock(return_value=(Tag("A", value, "DINT[2]", None), {}))
    with pytest.raises(EIPClientError, match="expected 2") as failure:
        await client.read_tag("A", 2)
    assert failure.value.meta["complete"] is False
    assert client._run_cip.await_count == 1


async def test_batch_array_failure_preserves_unrelated_scalar_result():
    client = EIPClient(EIPClientConfig())
    client._run_cip = AsyncMock(return_value=([Tag("A", [1], "DINT[2]", None), Tag("B", 2, "DINT", None)], {}))
    results, _ = await client.read_multiple_tags(["A{2}", "B"])
    assert results[0]["value"] is None and "expected 2" in results[0]["error"]
    assert results[1]["value"] == 2 and results[1]["error"] is None


async def test_json_bridge_array_batch_uses_same_length_rule():
    client = EIPClient(EIPClientConfig(json_bridge=True))
    client._json_exchange = AsyncMock(return_value=({"success": True, "data": {"value": [1]}}, {}))
    results, _ = await client.read_multiple_tags(["A{2}"])
    assert results[0]["value"] is None and "expected 2" in results[0]["error"]
