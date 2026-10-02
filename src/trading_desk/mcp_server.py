"""Exposes the C++ exchange as an MCP server.

Any MCP client (Claude Code, Claude Desktop, our own agents) can launch this
process, ask it which tools it has, and call them. The tools are thin: each
one validates its arguments and forwards a single command to the engine.

Run it over stdio:  uv run python -m trading_desk.mcp_server
"""

from __future__ import annotations

import logging
import sys
from typing import Any, Literal

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

from trading_desk.engine import Engine, EngineError

INSTRUCTIONS = """\
A simulated crypto exchange replaying a real order-book snapshot (17 March 2020).

How time works: the market moves in discrete time steps. Orders you place sit at
the current step; nothing fills until advance_time() runs the matching engine.
A trade happens when a bid price >= an ask price, at the ask price. Orders that
do not fill by the end of a step expire. Once every step has been played the
market is closed and reset_market() starts again.

Products are quoted BASE/QUOTE: a bid on ETH/USDT spends USDT to buy ETH, an ask
sells ETH for USDT. Check get_order_book before trading and get_wallet after.
"""


def build_server(engine: Engine) -> MCPServer:
    """Builds the server around an engine. Tests pass in their own engine."""
    mcp = MCPServer(name="trading-desk", instructions=INSTRUCTIONS)

    def call(fn, *args) -> Any:
        # Engine errors are the agent's mistake (unknown product, no funds):
        # report them as tool errors so the model can read them and recover.
        try:
            return fn(*args)
        except (EngineError, ValueError) as e:
            raise ToolError(str(e)) from e

    @mcp.tool()
    def list_products() -> list[str]:
        """Lists the tradable products, e.g. ETH/USDT."""
        return call(engine.products)

    @mcp.tool()
    def get_market_time() -> dict[str, Any]:
        """Returns the current time step, how many steps there are, and whether the market is closed."""
        return call(engine.time)

    @mcp.tool()
    def get_order_book(product: str, depth: int = 10) -> dict[str, Any]:
        """Returns the best bids and asks for a product at the current time step.

        Each level is [price, amount]. Also returns best_bid, best_ask and spread
        (null when one side of the book is empty).
        """
        return call(engine.book, product, max(1, min(depth, 50)))

    @mcp.tool()
    def get_wallet() -> dict[str, Any]:
        """Returns balances per currency, and what is still available after reserving funds for open orders."""
        return call(engine.wallet)

    @mcp.tool()
    def place_order(side: Literal["bid", "ask"], product: str, price: float, amount: float) -> dict[str, Any]:
        """Places a limit order at the current time step.

        side: "bid" to buy the base currency, "ask" to sell it.
        price: limit price in the quote currency. amount: quantity of the base currency.
        Fills (if any) happen on the next advance_time(). Returns the order id.
        """
        return call(engine.place, side, product, price, amount)

    @mcp.tool()
    def list_open_orders() -> list[dict[str, Any]]:
        """Lists this desk's orders that have not been matched yet."""
        return call(engine.orders)

    @mcp.tool()
    def cancel_order(order_id: int) -> dict[str, Any]:
        """Cancels an open order by id."""
        return call(engine.cancel, order_id)

    @mcp.tool()
    def advance_time() -> dict[str, Any]:
        """Runs the matching engine for the current step, settles fills into the wallet, and moves to the next step.

        Returns the fills, the ids of orders that expired unfilled, and the new time.
        """
        return call(engine.step)

    @mcp.tool()
    def reset_market() -> dict[str, Any]:
        """Rewinds to the first time step with the starting wallet and no open orders."""
        return call(engine.reset)

    return mcp


def main() -> None:
    # stdout carries the MCP protocol, so logs must go to stderr.
    logging.basicConfig(stream=sys.stderr, level=logging.INFO)
    with Engine() as engine:
        build_server(engine).run("stdio")


if __name__ == "__main__":
    main()
