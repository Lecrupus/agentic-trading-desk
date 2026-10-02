"""Exposes the C++ exchange as an MCP server.

Any MCP client (Claude Code, Claude Desktop, our own agents) can launch this
process, ask it which tools it has, and call them.

The trading logic lives in DeskService: validation, the risk gate in front of
every order, and the journal. The MCP tools are thin wrappers around it, and
the web playground (trading_desk.web) uses the same service, so there is one
code path to the exchange whichever way you reach it.

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
from dataclasses import asdict
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


class DeskError(Exception):
    """A call the desk refused. kind is "risk_rejected" or "error"."""

    def __init__(self, kind: str, message: str):
        super().__init__(message)
        self.kind = kind


class Journal:
    """Append-only log of tool calls and wallet snapshots: a JSONL file, memory, or both."""

    def __init__(self, path: str | Path | None = None, keep: bool = False):
        self.path = Path(path) if path else None
        self.entries: list[dict[str, Any]] | None = [] if keep else None
        self.seq = 0

    @property
    def enabled(self) -> bool:
        return self.path is not None or self.entries is not None

    def write(self, entry: dict[str, Any]) -> None:
        if not self.enabled:
            return
        self.seq += 1
        entry = {"seq": self.seq, **entry}
        if self.entries is not None:
            self.entries.append(entry)
        if self.path is not None:
            with self.path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(entry) + "\n")


class DeskService:
    """Everything a trader can do, each call validated, risk-checked and journalled."""

    def __init__(self, engine: Engine, limits: RiskLimits | None = None, journal: Journal | None = None):
        self.engine = engine
        self.tracker = PriceTracker(engine)
        self.gate = RiskGate(engine, limits, self.tracker)
        self.journal = journal or Journal()
        self.snapshot("start")

    # ---- plumbing ----------------------------------------------------------

    def snapshot(self, reason: str) -> None:
        if not self.journal.enabled:
            return
        prices = self.tracker.prices()
        balances = self.engine.wallet()["balances"]
        self.journal.write({
            "event": "snapshot", "reason": reason, **self.engine.time(),
            "balances": balances, "prices_usdt": prices, "stale_prices": self.tracker.stale,
            "value_usdt": mark_to_market(balances, prices),
        })

    def call(self, tool: str, args: dict[str, Any], fn: Callable[[], Any]) -> Any:
        entry: dict[str, Any] = {"event": "tool", "tool": tool, "args": args}
        try:
            result = fn()
        except DeskError as e:
            self.journal.write({**entry, "status": e.kind, "error": str(e)})
            raise
        except (EngineError, ValueError) as e:
            self.journal.write({**entry, "status": "error", "error": str(e)})
            raise DeskError("error", str(e)) from e
        self.journal.write({**entry, "status": "ok", "result": result})
        return result

    # ---- the desk ----------------------------------------------------------

    def products(self) -> list[str]:
        return self.call("list_products", {}, self.engine.products)

    def time(self) -> dict[str, Any]:
        return self.call("get_market_time", {}, self.engine.time)

    def book(self, product: str, depth: int = 10) -> dict[str, Any]:
        depth = max(1, min(depth, 50))
        return self.call("get_order_book", {"product": product, "depth": depth},
                         lambda: self.engine.book(product, depth))

    def wallet(self) -> dict[str, Any]:
        return self.call("get_wallet", {}, self.engine.wallet)

    def portfolio(self) -> dict[str, Any]:
        def value() -> dict[str, Any]:
            prices = self.tracker.prices()
            balances = self.engine.wallet()["balances"]
            return {
                "prices_usdt": prices,
                "stale_prices": self.tracker.stale,
                "value_by_currency_usdt": {c: a * prices.get(c, 0.0) for c, a in balances.items()},
                "total_value_usdt": mark_to_market(balances, prices),
            }
        return self.call("get_portfolio", {}, value)

    def risk_limits(self) -> dict[str, Any]:
        return self.call("get_risk_limits", {},
                         lambda: {**asdict(self.gate.limits), "orders_blocked": len(self.gate.breaches)})

    def check(self, side: str, product: str, price: float, amount: float) -> dict[str, Any]:
        args = {"side": side, "product": product, "price": price, "amount": amount}
        return self.call("check_order", args, lambda: self.gate.check(side, product, price, amount).to_dict())

    def place(self, side: str, product: str, price: float, amount: float) -> dict[str, Any]:
        args = {"side": side, "product": product, "price": price, "amount": amount}

        def place() -> dict[str, Any]:
            decision = self.gate.check(side, product, price, amount)
            if not decision.approved:
                self.gate.record_breach(decision)
                raise DeskError("risk_rejected", "; ".join(decision.reasons))
            return {**self.engine.place(side, product, price, amount), "notional_usdt": decision.notional_usdt}

        return self.call("place_order", args, place)

    def orders(self) -> list[dict[str, Any]]:
        return self.call("list_open_orders", {}, self.engine.orders)

    def cancel(self, order_id: int) -> dict[str, Any]:
        return self.call("cancel_order", {"order_id": order_id}, lambda: self.engine.cancel(order_id))

    def advance(self) -> dict[str, Any]:
        result = self.call("advance_time", {}, self.engine.step)
        self.snapshot("advance_time")
        return result

    def reset(self) -> dict[str, Any]:
        result = self.call("reset_market", {}, self.engine.reset)
        self.gate.breaches.clear()
        self.snapshot("reset_market")
        return result


def build_server(engine: Engine, limits: RiskLimits | None = None, journal: Journal | None = None) -> MCPServer:
    """Builds the server around an engine. Tests and the eval harness pass in their own."""
    return build_server_for(DeskService(engine, limits, journal))


def build_server_for(desk: DeskService) -> MCPServer:
    mcp = MCPServer(name="trading-desk", instructions=INSTRUCTIONS)

    def tool_call(fn: Callable[..., Any], *args: Any) -> Any:
        # Rejections are the caller's mistake (unknown product, no funds, over a
        # limit): report them as tool errors so a model can read them and recover.
        try:
            return fn(*args)
        except DeskError as e:
            prefix = "RISK REJECTED: " if e.kind == "risk_rejected" else ""
            raise ToolError(prefix + str(e)) from e

    @mcp.tool()
    def list_products() -> list[str]:
        """Lists the tradable products, e.g. ETH/USDT."""
        return tool_call(desk.products)

    @mcp.tool()
    def get_market_time() -> dict[str, Any]:
        """Returns the current time step, how many steps there are, and whether the market is closed."""
        return tool_call(desk.time)

    @mcp.tool()
    def get_order_book(product: str, depth: int = 10) -> dict[str, Any]:
        """Returns the best bids and asks for a product at the current time step.

        Each level is [price, amount]. Also returns best_bid, best_ask and spread
        (null when one side of the book is empty).
        """
        return tool_call(desk.book, product, depth)

    @mcp.tool()
    def get_wallet() -> dict[str, Any]:
        """Returns balances per currency, and what is still available after reserving funds for open orders."""
        return tool_call(desk.wallet)

    @mcp.tool()
    def get_portfolio() -> dict[str, Any]:
        """Values the wallet in USDT at current mid prices: per-currency value and the total.

        stale_prices lists currencies with no USDT book this step; they are valued
        at their last known price.
        """
        return tool_call(desk.portfolio)

    @mcp.tool()
    def get_risk_limits() -> dict[str, Any]:
        """Returns the hard limits every order is checked against, and how many orders they have blocked."""
        return tool_call(desk.risk_limits)

    @mcp.tool()
    def check_order(side: Literal["bid", "ask"], product: str, price: float, amount: float) -> dict[str, Any]:
        """Dry-runs an order through the risk checks without placing it.

        Returns approved (true/false), the order's USDT notional, the resulting
        position, and the reasons for any rejection.
        """
        return tool_call(desk.check, side, product, price, amount)

    @mcp.tool()
    def place_order(side: Literal["bid", "ask"], product: str, price: float, amount: float) -> dict[str, Any]:
        """Places a limit order at the current time step, after the risk checks.

        side: "bid" to buy the base currency, "ask" to sell it.
        price: limit price in the quote currency. amount: quantity of the base currency.
        Fills (if any) happen on the next advance_time(). Returns the order id.
        Orders that break a risk limit are rejected with "RISK REJECTED: <reasons>".
        """
        return tool_call(desk.place, side, product, price, amount)

    @mcp.tool()
    def list_open_orders() -> list[dict[str, Any]]:
        """Lists this desk's orders that have not been matched yet."""
        return tool_call(desk.orders)

    @mcp.tool()
    def cancel_order(order_id: int) -> dict[str, Any]:
        """Cancels an open order by id."""
        return tool_call(desk.cancel, order_id)

    @mcp.tool()
    def advance_time() -> dict[str, Any]:
        """Runs the matching engine for the current step, settles fills into the wallet, and moves to the next step.

        Returns the fills, the ids of orders that expired unfilled, and the new time.
        """
        return tool_call(desk.advance)

    @mcp.tool()
    def reset_market() -> dict[str, Any]:
        """Rewinds to the first time step with the starting wallet and no open orders."""
        return tool_call(desk.reset)

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
