---
name: trading-session
description: Run or inspect a trading session on the trading-desk MCP server by hand from Claude Code - step through the market, trade, and score the result. Use when the user asks to trade, replay the market, or check how a session went.
---

# Running a session by hand

The `trading-desk` MCP server (from `.mcp.json`) replays 8 time steps of a real
order-book snapshot from 17 March 2020.

## Loop

1. `get_market_time`. Stop when `done` is true.
2. Inspect books with `get_order_book` (see the read-order-book skill).
3. Optionally trade, following the place-safe-order skill.
4. `advance_time`, then report fills and expiries.

When the session is over, `get_portfolio` gives the value in USDT.
`reset_market` rewinds to step 0.

## Scoring a run like the eval harness does

PnL is measured against **holding the starting wallet**, valued at the final
prices. That way market drift doesn't count as skill:

```
pnl = value(final_wallet, final_prices) - value(starting_wallet, final_prices)
```

The full harness runs with `uv run python -m trading_desk.evals`.
