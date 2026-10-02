---
name: read-order-book
description: How to read the trading-desk order book - best bid/ask, spread, mid price, depth, and cross rates between ETH/BTC, ETH/USDT and BTC/USDT. Use before proposing or sizing any trade on the trading-desk MCP server.
---

# Reading the order book

`get_order_book(product, depth)` returns the book **at the current time step**:

```json
{"product": "ETH/USDT", "best_bid": 117.056, "best_ask": 117.329, "spread": 0.273,
 "bids": [[117.056, 11.15], ...], "asks": [[117.329, 53.58], ...]}
```

Each level is `[price, amount]`. Bids are sorted highest first, asks lowest first.

## The numbers that matter

| Quantity | Formula | Meaning |
|---|---|---|
| Mid | `(best_bid + best_ask) / 2` | Fair value right now. Risk limits measure price deviation from it |
| Spread | `best_ask - best_bid` | What a round trip costs you. Compare it to the expected edge |
| Spread (bps) | `spread / mid * 10_000` | The spread made comparable across products |
| Depth at best | amount at level 0 | How much fills at the best price; beyond that, you walk the book |

## How fills work here

- A **bid** fills against asks priced **at or below** your limit, at **the ask's price**.
- An **ask** fills against bids priced **at or above** your limit.
- Matching happens only on `advance_time`, against **this step's** book. Unfilled
  orders expire. Nothing rests into the next step.
- So a bid below `best_ask` will not fill. To trade, you cross the spread.

## Cross rates (where an edge can come from)

Three books describe the same two exchange rates:

```
implied ETH/BTC = mid(ETH/USDT) / mid(BTC/USDT)
gap             = mid(ETH/BTC) / implied - 1
```

A gap only pays if it beats the **sum of the spreads** you would cross to capture
it, across every leg. On this snapshot gaps are usually smaller than that, and
doing nothing is often the right call. The same applies to DOGE through DOGE/BTC
and DOGE/USDT.

## Checklist before proposing

1. Both sides of the book exist (`best_bid` and `best_ask` are not null).
2. Spread in bps is small relative to your expected edge.
3. The size is within depth at the best price, and within `max_order_notional_usdt`.
4. The limit price is within `max_price_deviation` of the mid.
