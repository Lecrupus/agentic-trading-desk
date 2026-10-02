"""Connects a real MCP client to the server in-process, the way Claude Code would over stdio."""

import pytest

from mcp import Client

from trading_desk.mcp_server import Journal, build_server

pytestmark = pytest.mark.anyio

EXPECTED_TOOLS = {
    "list_products", "get_market_time", "get_order_book", "get_wallet", "get_portfolio",
    "get_risk_limits", "check_order", "place_order", "list_open_orders", "cancel_order",
    "advance_time", "reset_market",
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
    # Passes the risk checks, but the wallet holds no DOGE to sell.
    book = (await client.call_tool("get_order_book", {"product": "DOGE/USDT"})).structured_content
    result = await client.call_tool(
        "place_order", {"side": "ask", "product": "DOGE/USDT", "price": book["best_bid"], "amount": 10}
    )
    assert result.is_error
    assert "insufficient DOGE" in result.content[0].text


async def test_bad_side_is_rejected_by_the_schema(client):
    result = await client.call_tool(
        "place_order", {"side": "short", "product": "ETH/USDT", "price": 100, "amount": 1}
    )
    assert result.is_error


async def test_risk_gate_blocks_orders_over_the_limit(client):
    book = (await client.call_tool("get_order_book", {"product": "ETH/USDT"})).structured_content
    order = {"side": "bid", "product": "ETH/USDT", "price": book["best_ask"], "amount": 100}

    dry_run = (await client.call_tool("check_order", order)).structured_content
    assert dry_run["approved"] is False

    result = await client.call_tool("place_order", order)
    assert result.is_error
    assert "RISK REJECTED: order notional" in result.content[0].text

    limits = (await client.call_tool("get_risk_limits", {})).structured_content
    assert limits["orders_blocked"] == 1  # check_order is a dry run and does not count
    assert (await client.call_tool("list_open_orders", {})).structured_content["result"] == []


async def test_journal_records_calls_and_snapshots(engine, tmp_path):
    import json

    path = tmp_path / "run.jsonl"
    async with Client(build_server(engine, journal=Journal(path))) as c:
        await c.call_tool("place_order", {"side": "bid", "product": "ETH/USDT", "price": 1, "amount": 1})
        await c.call_tool("advance_time", {})

    entries = [json.loads(line) for line in path.read_text().splitlines()]
    kinds = [(e["event"], e.get("tool"), e.get("status") or e.get("reason")) for e in entries]
    assert kinds == [
        ("snapshot", None, "start"),
        ("tool", "place_order", "risk_rejected"),  # price 1 is miles from the mid
        ("tool", "advance_time", "ok"),
        ("snapshot", None, "advance_time"),
    ]
    assert entries[-1]["step"] == 1 and entries[-1]["value_usdt"] > 0
