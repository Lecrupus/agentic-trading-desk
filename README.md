# Agentic Trading Desk

A multi-agent trading system built on MCP. An orchestrator agent delegates to
analyst, risk and execution sub-agents. They trade on a C++ order-book simulator
that replays a real exchange snapshot (3,540 orders, 5 products, 8 time steps,
17 March 2020).

The simulator is exposed as an **MCP server**, so any MCP client, including
Claude Code, can query the order book and place trades on it.

> **Status:** Phases 1–3 of 7 are done (C++ engine, MCP server, risk gate). See [docs/PLAN.md](docs/PLAN.md).
> To learn how it works, read [docs/LEARNING.md](docs/LEARNING.md).

## Architecture

```
 Agents (Claude Agent SDK)      Claude Code / any MCP client
          \                      /
           \     MCP (stdio)    /
            v                  v
        MCP server (Python, trading_desk.mcp_server)
                    |   text command in, JSON line out
                    v
        engine_server (C++17): order book · matching engine · wallet · clock
```

## Quick start

You need: a C++17 compiler (`g++`), [uv](https://docs.astral.sh/uv/), and Python 3.11+.

```bash
make test          # Windows (MSYS2): mingw32-make test
```

This builds `build/engine_server`, then runs the 34 C++ tests and the Python tests.

Try the engine by hand:

```bash
./build/engine_server engine/data/20200317.csv
```

Run the MCP server (Claude Code starts it for you through `.mcp.json`):

```bash
uv run python -m trading_desk.mcp_server
```

## MCP tools

| Tool | What it does |
|---|---|
| `list_products` | Tradable products, e.g. `ETH/USDT` |
| `get_market_time` | Current step, total steps, whether the market is closed |
| `get_order_book` | Best bids/asks, spread, depth at the current step |
| `get_wallet` | Balances, and funds still available after open orders |
| `get_portfolio` | Wallet valued in USDT at mid prices |
| `get_risk_limits` | The hard limits, and how many orders they have blocked |
| `check_order` | Dry-run an order through the risk checks |
| `place_order` | Limit bid/ask, risk-checked; fills on the next `advance_time` |
| `list_open_orders` / `cancel_order` | Manage unfilled orders |
| `advance_time` | Run the matching engine, settle fills, move the clock |
| `reset_market` | Back to step 0 with the starting wallet |

## Layout

```
engine/            C++ engine
  include/exchange.hpp   OrderBook, Wallet, matching engine, Exchange
  src/engine_server.cpp  stdin/stdout line protocol
  tests/                 C++ unit tests
  data/                  market snapshot
src/trading_desk/  Python package
  engine.py              process bridge to the C++ engine
  mcp_server.py          MCP server + journal
  risk.py                pre-trade risk gate
  market.py              USDT prices, mark-to-market
tests/             Python tests (engine bridge, MCP client)
docs/              PLAN.md, LEARNING.md
```

Built on my earlier [Cryptocurrency Trading Platform](https://github.com/Lecrupus/Cryptocurrency-Trading-Platform-) simulator.
