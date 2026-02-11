import json

import httpx
import pytest

from src.odoo.client import OdooClient, OdooAuthError, OdooRPCError, OdooRateLimitError


def _make_jsonrpc_response(result):
    return httpx.Response(200, json={"jsonrpc": "2.0", "result": result})


def _make_jsonrpc_error(message):
    return httpx.Response(200, json={"jsonrpc": "2.0", "error": {"message": message, "data": {"message": message}}})


@pytest.mark.asyncio
async def test_authenticate_success():
    transport = httpx.MockTransport(
        lambda req: _make_jsonrpc_response(42)
    )
    async with httpx.AsyncClient(transport=transport) as http:
        client = OdooClient("https://test.odoo.com", "testdb", "admin", "key", http_client=http)
        uid = await client.authenticate()
        assert uid == 42
        assert client.uid == 42


@pytest.mark.asyncio
async def test_authenticate_failure():
    transport = httpx.MockTransport(
        lambda req: _make_jsonrpc_response(False)
    )
    async with httpx.AsyncClient(transport=transport) as http:
        client = OdooClient("https://test.odoo.com", "testdb", "admin", "bad-key", http_client=http)
        with pytest.raises(OdooAuthError):
            await client.authenticate()


@pytest.mark.asyncio
async def test_search_read():
    call_count = 0

    def handler(request):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return _make_jsonrpc_response(1)  # authenticate
        return _make_jsonrpc_response([
            {"id": 10, "name": "SO001"},
            {"id": 11, "name": "SO002"},
        ])

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as http:
        client = OdooClient("https://test.odoo.com", "testdb", "admin", "key", http_client=http)
        result = await client.search_read("sale.order", [["state", "=", "sale"]], ["name"])
        assert len(result) == 2
        assert result[0]["name"] == "SO001"


@pytest.mark.asyncio
async def test_rate_limit_detection():
    transport = httpx.MockTransport(
        lambda req: httpx.Response(429, text="Rate limited")
    )
    async with httpx.AsyncClient(transport=transport) as http:
        client = OdooClient("https://test.odoo.com", "testdb", "admin", "key", http_client=http)
        client._uid = 1  # Skip auth
        with pytest.raises(OdooRateLimitError):
            await client.search_read("sale.order", [], ["name"])


@pytest.mark.asyncio
async def test_rpc_error():
    transport = httpx.MockTransport(
        lambda req: _make_jsonrpc_error("Field not found")
    )
    async with httpx.AsyncClient(transport=transport) as http:
        client = OdooClient("https://test.odoo.com", "testdb", "admin", "key", http_client=http)
        client._uid = 1
        with pytest.raises(OdooRPCError, match="Field not found"):
            await client.search_read("sale.order", [], ["bad_field"])
