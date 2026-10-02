"""Connects a real MCP client to the server in-process, the way Claude Code would over stdio."""

import pytest

from mcp import Client

from trading_desk.mcp_server import build_server

pytestmark = pytest.mark.anyio

EXPECTED_TOOLS = {
    "list_products", "get_market_time", "get_order_book", "get_wallet", "place_order",
    "list_open_orders", "cancel_order", "advance_time", "reset_market",
}


@pytest.fixture
async def client(engine):
    async with Client(build_server(engine)) as c:
        yield c


async def test_lists_every_tool(client):
    tools = (await client.list_tools()).tools
    assert {t.name for t in tools} == EXPECTED_TOOLS
    place = next(t for t in tools if t.name == "place_order")
    assert place.input_schema["properties"]["side"]["enum"] == ["bid", "ask"]


async def test_trade_round_trip(client):
    book = (await client.call_tool("get_order_book", {"product": "ETH/USDT", "depth": 3})).structured_content
    placed = await client.call_tool(
        "place_order", {"side": "bid", "product": "ETH/USDT", "price": book["best_ask"], "amount": 2}
    )
    assert not placed.is_error

    step = (await client.call_tool("advance_time", {})).structured_content
    assert step["fills"][0]["amount"] == 2

    wallet = (await client.call_tool("get_wallet", {})).structured_content
    assert wallet["balances"]["ETH"] == 102


async def test_engine_rejection_comes_back_as_a_tool_error(client):
    result = await client.call_tool(
        "place_order", {"side": "ask", "product": "ETH/USDT", "price": 100, "amount": 10_000}
    )
    assert result.is_error
    assert "insufficient ETH" in result.content[0].text


async def test_bad_side_is_rejected_by_the_schema(client):
    result = await client.call_tool(
        "place_order", {"side": "short", "product": "ETH/USDT", "price": 100, "amount": 1}
    )
    assert result.is_error
