---
name: place-safe-order
description: Pre-trade checklist for the trading-desk MCP server - verify funds, run check_order, size within risk limits, then place. Use whenever reviewing, resizing or placing an order with place_order.
---

# Placing an order safely

Every `place_order` passes through a hard risk gate. A rejected order counts as a
**risk-limit breach** in the desk's evaluation, even though it never reaches the
exchange. So check first and place once.

## Steps

1. **Limits.** Call `get_risk_limits` once per session:
   - `max_order_notional_usdt`: order value in USDT (amount x price x quote's USDT price)
   - `max_position_usdt`: holding of the currency you *receive*, plus pending buys
   - `max_open_orders`
   - `max_price_deviation`: limit price vs mid
2. **Funds.** Call `get_wallet` and use `available`, not `balances`. Open orders
   reserve funds.
   - A bid on `BASE/QUOTE` needs `amount x price` of QUOTE.
   - An ask needs `amount` of BASE.
3. **Dry run.** Call `check_order(side, product, price, amount)`.
   - `approved: true`: go to step 4.
   - `approved: false`: read `reasons`. Shrink the amount (or move the price
     toward the mid) and check again. Dry runs never count as breaches.
4. **Place** with the exact numbers that passed `check_order`.
5. **Record** the `order_id`. Fills arrive only after `advance_time`.

## Sizing rule of thumb

```
max_amount ~= 0.9 x max_order_notional_usdt / (price x usdt_price_of_quote)
```

The 0.9 leaves headroom for the price moving between the check and the order.

## Never

- Retry a rejected order with slightly different numbers in a loop.
- Place an order that `check_order` rejected.
- Cancel and re-place to get around `max_open_orders`.
