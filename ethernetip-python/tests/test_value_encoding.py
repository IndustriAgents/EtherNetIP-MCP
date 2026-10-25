"""Precision is preserved at the wire boundary without changing application math."""

import asyncio
import json
from types import SimpleNamespace

import pytest

from ethernetip_mcp.protocol.contract import ContractBoundary, as_result
from ethernetip_mcp.protocol.value_encoding import encode_integers


@pytest.mark.parametrize("number", [-(2**63), -(2**53), 2**53, 2**63 - 1, 2**64 - 1])
def test_unsafe_integers_are_exact_decimal_strings_on_mcp_wire(number):
    async def call(name, arguments):
        return as_result({"success": True, "data": {"value": number}, "error": None, "meta": {}})

    boundary = ContractBoundary(SimpleNamespace(call_tool=call))
    result = asyncio.run(boundary.wire_call("get_connection_status", {}))
    assert result.structuredContent["data"]["value"] == str(number)
    assert result.structuredContent["meta"]["value_encoding"] == "decimal_string"
    assert result.structuredContent["meta"]["encoded_integer_paths"] == ["/data/value"]
    assert json.loads(result.content[0].text) == result.structuredContent


def test_safe_integers_boolean_and_nested_structures_keep_their_semantics():
    source = {
        "success": True,
        "data": {"a/b": [True, 2**53 - 1, -(2**53 - 1), {"~": 2**63}]},
        "error": None,
        "meta": {},
    }
    encoded = encode_integers(source)
    assert encoded["data"]["a/b"][:3] == [True, 2**53 - 1, -(2**53 - 1)]
    assert encoded["meta"]["encoded_integer_paths"] == ["/data/a~1b/3/~0"]
    assert source["data"]["a/b"][3]["~"] == 2**63


def test_safe_results_do_not_claim_decimal_encoding():
    result = encode_integers({"success": True, "data": {"value": 5}, "error": None, "meta": {}})
    assert result["meta"] == {}
