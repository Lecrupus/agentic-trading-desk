# Agentic Trading Desk

[![CI](https://github.com/Lecrupus/agentic-trading-desk/actions/workflows/ci.yml/badge.svg)](https://github.com/Lecrupus/agentic-trading-desk/actions/workflows/ci.yml)

A multi-agent trading system built on MCP. An orchestrator agent delegates to
**analyst**, **risk** and **execution** sub-agents (Claude Agent SDK). They
trade on a **C++ order-book simulator** that replays a real exchange snapshot:
3,540 orders, 5 products, 8 time steps, 17 March 2020.

- The simulator is exposed as an **MCP server**, so any MCP client, including
  Claude Code, can read the book and trade on it.
- **Every order is checked against hard risk limits** inside the server before
  it reaches the exchange, whichever agent or client sent it.
- An **evaluation harness** replays the snapshot and scores each run on PnL,
  risk-limit breaches and failed tool calls. It runs in Docker and in CI on Linux.

New to the code? Read **[docs/LEARNING.md](docs/LEARNING.md)**. It walks through
every layer and why it's built that way.

## See it working

- **[Replay dashboard](https://lecrupus.github.io/agentic-trading-desk/):** every trader's run, step by step.
  It shows the order books, orders, risk-gate rejections with reasons, fills, and PnL against holding.
  When an agent run exists, it also shows the analyst → risk → execution hand-offs.
  CI rebuilds it after every push to `main`.
- **Live playground:** trade by hand against the replayed market and watch the risk gate block bad orders.
  The same server exposes MCP over HTTP at `/mcp`, so you can point Claude at it.
  Deploy your own copy (free plan):

  [![Deploy to Render](https://render.com/images/deploy-to-render-button.svg)](https://render.com/deploy?repo=https://github.com/Lecrupus/agentic-trading-desk)

  Or run it locally:

  ```bash
  uv run python -m trading_desk.web
  ```

  Then open http://localhost:7860.

## Architecture

```
            orchestrator ── analyst  (read-only tools)
                 │      ├── risk     (read + check_order)
                 │      └── execution (place/cancel only)
                 │   Claude Agent SDK · per-agent tools · PreToolUse role hook
                 ▼
  Claude Code ─► MCP server (Python, stdio) ── risk gate ── journal (JSONL)
                 │   text command in, JSON line out
                 ▼
        engine_server (C++17): order book · matching engine · wallet · clock
```

## Quick start

You need a C++17 compiler (`g++`), [uv](https://docs.astral.sh/uv/), and Python 3.11+.

```bash
make test          # Windows (MSYS2): mingw32-make test
```

This builds the engine, then runs the 34 C++ tests and 53 Python tests.

Replay the market with the scripted baseline traders (free, no API key):

```bash
uv run python -m trading_desk.evals --check
```

| Run | PnL (USDT) | Orders | Fills | Failed calls | Risk breaches |
|---|---:|---:|---:|---:|---:|
| hold | +0.00 | 0 | 0 | 0 | 0 |
| taker | −0.33 | 7 | 7 | 0 | 0 |
| momentum | −0.24 | 6 | 6 | 0 | 0 |
| reckless | +0.00 | 0 | 0 | 8 | 14 |

PnL is measured against holding the starting wallet at final prices, so holding scores exactly 0.

Run the agent desk. This calls Claude and needs the `claude` CLI plus
`ANTHROPIC_API_KEY` or a Claude login. `--max-budget-usd` caps the cost:

```bash
uv run python -m trading_desk.evals --agent --baselines hold --max-budget-usd 5
```

Or use Docker:

```bash
docker build -t trading-desk .
```

```bash
docker run --rm trading-desk --check
```

Or trade by hand in **Claude Code**: open this folder (`.mcp.json` registers
the server) and ask *"run a trading session"*.

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
| `reset_market` | Back to step 0 with the starting wallet (disabled for agents) |

## Layout

```
engine/                  C++ engine
  include/exchange.hpp     OrderBook, Wallet, matching engine, Exchange
  src/engine_server.cpp    stdin/stdout line protocol
  tests/                   C++ unit tests
  data/                    market snapshot
src/trading_desk/        Python package
  engine.py                process bridge to the C++ engine
  mcp_server.py            MCP server + journal
  risk.py                  pre-trade risk gate
  market.py                USDT prices, mark-to-market
  agents.py                orchestrator + analyst/risk/execution sub-agents
  evals.py                 replay + scoring harness, baseline strategies
  web.py                   live playground + MCP over HTTP
  site.py                  builds the replay dashboard
  static/                  playground.html, dashboard.html
.claude/skills/          Agent Skills: read-order-book, place-safe-order, trading-session
tests/                   Python tests
Dockerfile               engine build stage + Python runtime (eval harness)
deploy/playground/       Dockerfile for the live playground
render.yaml              Render blueprint for the playground
.github/workflows/       ci.yml (tests, evals, Docker, agent eval on demand), deploy.yml (Pages)
docs/                    PLAN.md, LEARNING.md
```

Built on my earlier [Cryptocurrency Trading Platform](https://github.com/Lecrupus/Cryptocurrency-Trading-Platform-) simulator.
