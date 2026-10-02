"""Prices and portfolio value, in USDT.

Risk limits and PnL both need one common unit. Every currency is valued at the
mid price of its X/USDT book; if that book is empty, through a cross such as
DOGE/BTC x BTC/USDT.
"""

from __future__ import annotations

from typing import Any, Mapping

from trading_desk.engine import Engine

CASH = "USDT"


def mid_price(book: Mapping[str, Any]) -> float | None:
    bid, ask = book.get("best_bid"), book.get("best_ask")
    if bid is not None and ask is not None:
        return (bid + ask) / 2
    return bid if bid is not None else ask


def usdt_prices(engine: Engine) -> dict[str, float]:
    """USDT value of one unit of each currency at the current time step."""
    mids: dict[tuple[str, str], float] = {}
    for product in engine.products():
        base, quote = product.split("/")
        m = mid_price(engine.book(product, depth=1))
        if m is not None:
            mids[(base, quote)] = m

    prices = {CASH: 1.0}
    # Direct quotes first, then keep resolving crosses until nothing new is priced.
    changed = True
    while changed:
        changed = False
        for (base, quote), m in mids.items():
            if base not in prices and quote in prices:
                prices[base] = m * prices[quote]
                changed = True
    return prices


def mark_to_market(balances: Mapping[str, float], prices: Mapping[str, float]) -> float:
    """Total wallet value in USDT. Currencies with no price count as zero."""
    return sum(amount * prices.get(currency, 0.0) for currency, amount in balances.items())
