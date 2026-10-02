"""Exposes the C++ exchange as an MCP server.

Any MCP client (Claude Code, Claude Desktop, our own agents) can launch this
process, ask it which tools it has, and call them. Each tool validates its
arguments and forwards to the engine; place_order goes through the risk gate
first.

Every call is written to a journal (JSON lines) when one is configured, so a
run can be scored afterwards without trusting the agent's own account of it.

Run it over stdio:  uv run python -m trading_desk.mcp_server
Environment:
  TRADING_JOURNAL       path of a JSONL journal to append to (optional)
  TRADING_RISK_LIMITS   JSON object overriding RiskLimits fields (optional)
"""

from __future__ import annotations

import json
import logging
import os
import sys
from pathlib import Path
from typing import Any, Callable, Literal

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

from trading_desk.engine import Engine, EngineError
from trading_desk.market import PriceTracker, mark_to_market
from trading_desk.risk import RiskGate, RiskLimits

INSTRUCTIONS = """\
A simulated crypto exchange replaying a real order-book snapshot (17 March 2020).

How time works: the market moves in discrete time steps. Orders you place sit at
the current step; nothing fills until advance_time() runs the matching engine.
A trade happens when a bid price >= an ask price, at the ask price. Orders that
do not fill by the end of a step expire. Once every step has been played the
market is closed and reset_market() starts again.

Products are quoted BASE/QUOTE: a bid on ETH/USDT spends USDT to buy ETH, an ask
sells ETH for USDT.

Risk: place_order is checked against hard limits (get_risk_limits) and rejected
with "RISK REJECTED" if it breaks one. Use check_order to test an order first.
"""


class RiskRejected(Exception):
    pass


class Journal:
    """Append-only JSON-lines log of tool calls and wallet snapshots."""

    def __init__(self, path: str | Path | None):
        self.path = Path(path) if path else None
        self.seq = 0

    def write(self, entry: dict[str, Any]) -> None:
        if self.path is None:
            return
        self.seq += 1
        with self.path.open("a", encoding="utf-8") as f:
            f.write(json.dumps({"seq": self.seq, **entry}) + "\n")


def build_server(engine: Engine, limits: RiskLimits | None = None, journal: Journal | None = None) -> MCPServer:
    """Builds the server around an engine. Tests and the eval harness pass in their own."""
    mcp = MCPServer(name="trading-desk", instructions=INSTRUCTIONS)
    tracker = PriceTracker(engine)
    gate = RiskGate(engine, limits, tracker)
    journal = journal or Journal(None)

    def snapshot(reason: str) -> None:
        if journal.path is None:
            return
        prices = tracker.prices()
        balances = engine.wallet()["balances"]
        journal.write({
            "event": "snapshot", "reason": reason, **engine.time(),
            "balances": balances, "prices_usdt": prices, "stale_prices": tracker.stale,
            "value_usdt": mark_to_market(balances, prices),
        })

    def run(tool: str, args: dict[str, Any], fn: Callable[[], Any]) -> Any:
        # Engine and argument errors are the caller's mistake (unknown product,
        # no funds): report them as tool errors so a model can read and recover.
        entry: dict[str, Any] = {"event": "tool", "tool": tool, "args": args}
        try:
            result = fn()
        except RiskRejected as e:
            journal.write({**entry, "status": "risk_rejected", "error": str(e)})
            raise ToolError(f"RISK REJECTED: {e}") from e
        except (EngineError, ValueError) as e:
            journal.write({**entry, "status": "error", "error": str(e)})
            raise ToolError(str(e)) from e
        journal.write({**entry, "status": "ok", "result": result})
        return result

    @mcp.tool()
    def list_products() -> list[str]:
        """Lists the tradable products, e.g. ETH/USDT."""
        return run("list_products", {}, engine.products)

    @mcp.tool()
    def get_market_time() -> dict[str, Any]:
        """Returns the current time step, how many steps there are, and whether the market is closed."""
        return run("get_market_time", {}, engine.time)

    @mcp.tool()
    def get_order_book(product: str, depth: int = 10) -> dict[str, Any]:
        """Returns the best bids and asks for a product at the current time step.

        Each level is [price, amount]. Also returns best_bid, best_ask and spread
        (null when one side of the book is empty).
        """
        depth = max(1, min(depth, 50))
        return run("get_order_book", {"product": product, "depth": depth}, lambda: engine.book(product, depth))

    @mcp.tool()
    def get_wallet() -> dict[str, Any]:
        """Returns balances per currency, and what is still available after reserving funds for open orders."""
        return run("get_wallet", {}, engine.wallet)

    @mcp.tool()
    def get_portfolio() -> dict[str, Any]:
        """Values the wallet in USDT at current mid prices: per-currency value and the total.

        stale_prices lists currencies with no USDT book this step; they are valued
        at their last known price.
        """
        def value() -> dict[str, Any]:
            prices = tracker.prices()
            balances = engine.wallet()["balances"]
            return {
                "prices_usdt": prices,
                "stale_prices": tracker.stale,
                "value_by_currency_usdt": {c: a * prices.get(c, 0.0) for c, a in balances.items()},
                "total_value_usdt": mark_to_market(balances, prices),
            }
        return run("get_portfolio", {}, value)

    @mcp.tool()
    def get_risk_limits() -> dict[str, Any]:
        """Returns the hard limits every order is checked against, and how many orders they have blocked."""
        def limits() -> dict[str, Any]:
            return {**gate.limits.__dict__, "orders_blocked": len(gate.breaches)}
        return run("get_risk_limits", {}, limits)

    @mcp.tool()
    def check_order(side: Literal["bid", "ask"], product: str, price: float, amount: float) -> dict[str, Any]:
        """Dry-runs an order through the risk checks without placing it.

        Returns approved (true/false), the order's USDT notional, the resulting
        position, and the reasons for any rejection.
        """
        args = {"side": side, "product": product, "price": price, "amount": amount}
        return run("check_order", args, lambda: gate.check(side, product, price, amount).to_dict())

    @mcp.tool()
    def place_order(side: Literal["bid", "ask"], product: str, price: float, amount: float) -> dict[str, Any]:
        """Places a limit order at the current time step, after the risk checks.

        side: "bid" to buy the base currency, "ask" to sell it.
        price: limit price in the quote currency. amount: quantity of the base currency.
        Fills (if any) happen on the next advance_time(). Returns the order id.
        Orders that break a risk limit are rejected with "RISK REJECTED: <reasons>".
        """
        args = {"side": side, "product": product, "price": price, "amount": amount}

        def place() -> dict[str, Any]:
            decision = gate.check(side, product, price, amount)
            if not decision.approved:
                gate.record_breach(decision)
                raise RiskRejected("; ".join(decision.reasons))
            return {**engine.place(side, product, price, amount), "notional_usdt": decision.notional_usdt}

        return run("place_order", args, place)

    @mcp.tool()
    def list_open_orders() -> list[dict[str, Any]]:
        """Lists this desk's orders that have not been matched yet."""
        return run("list_open_orders", {}, engine.orders)

    @mcp.tool()
    def cancel_order(order_id: int) -> dict[str, Any]:
        """Cancels an open order by id."""
        return run("cancel_order", {"order_id": order_id}, lambda: engine.cancel(order_id))

    @mcp.tool()
    def advance_time() -> dict[str, Any]:
        """Runs the matching engine for the current step, settles fills into the wallet, and moves to the next step.

        Returns the fills, the ids of orders that expired unfilled, and the new time.
        """
        result = run("advance_time", {}, engine.step)
        snapshot("advance_time")
        return result

    @mcp.tool()
    def reset_market() -> dict[str, Any]:
        """Rewinds to the first time step with the starting wallet and no open orders."""
        result = run("reset_market", {}, engine.reset)
        gate.breaches.clear()
        snapshot("reset_market")
        return result

    snapshot("start")
    return mcp


def limits_from_env() -> RiskLimits:
    raw = os.environ.get("TRADING_RISK_LIMITS")
    return RiskLimits(**json.loads(raw)) if raw else RiskLimits()


def main() -> None:
    # stdout carries the MCP protocol, so logs must go to stderr.
    logging.basicConfig(stream=sys.stderr, level=logging.INFO)
    with Engine() as engine:
        server = build_server(engine, limits_from_env(), Journal(os.environ.get("TRADING_JOURNAL")))
        server.run("stdio")


if __name__ == "__main__":
    main()
