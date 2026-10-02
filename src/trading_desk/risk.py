"""Pre-trade risk checks.

The risk gate is plain code, not a model. It sits inside the MCP server's
place_order tool, so it doesn't matter which agent (or human) is calling: an
order that breaks a limit never reaches the exchange.

The risk *agent* (Phase 4) reviews trades with judgement; this gate is the
guarantee behind it.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

from trading_desk.engine import Engine
from trading_desk.market import CASH, mid_price, usdt_prices


@dataclass(frozen=True)
class RiskLimits:
    # Largest single order, valued in USDT.
    max_order_notional_usdt: float = 10_000.0
    # Largest holding of any one non-cash currency (balance + pending buys), in USDT.
    max_position_usdt: float = 75_000.0
    # How many unfilled orders may be open at once.
    max_open_orders: int = 5
    # How far a limit price may sit from the mid price ("fat finger" check).
    max_price_deviation: float = 0.02


@dataclass
class RiskDecision:
    approved: bool
    side: str
    product: str
    price: float
    amount: float
    notional_usdt: float | None = None
    position_after_usdt: float | None = None
    reasons: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class RiskGate:
    def __init__(self, engine: Engine, limits: RiskLimits | None = None):
        self.engine = engine
        self.limits = limits or RiskLimits()
        self.breaches: list[dict[str, Any]] = []

    def check(self, side: str, product: str, price: float, amount: float) -> RiskDecision:
        """Checks an order against every limit. Never touches the exchange."""
        d = RiskDecision(approved=True, side=side, product=product, price=price, amount=amount)
        lim = self.limits

        if side not in ("bid", "ask"):
            d.reasons.append(f"side must be bid or ask, not {side!r}")
        if not price > 0 or not amount > 0:
            d.reasons.append("price and amount must be positive")
        if product not in self.engine.products():
            d.reasons.append(f"unknown product {product}")
        if d.reasons:
            d.approved = False
            return d

        base, quote = product.split("/")
        prices = usdt_prices(self.engine)

        if quote not in prices:
            d.reasons.append(f"cannot value {quote} in USDT right now")
        else:
            d.notional_usdt = amount * price * prices[quote]
            if d.notional_usdt > lim.max_order_notional_usdt:
                d.reasons.append(
                    f"order notional {d.notional_usdt:,.2f} USDT exceeds limit {lim.max_order_notional_usdt:,.2f}"
                )

        mid = mid_price(self.engine.book(product, depth=1))
        if mid is not None and abs(price - mid) / mid > lim.max_price_deviation:
            d.reasons.append(
                f"price {price:g} is {abs(price - mid) / mid:.1%} from mid {mid:g} "
                f"(limit {lim.max_price_deviation:.0%})"
            )

        open_orders = self.engine.orders()
        if len(open_orders) >= lim.max_open_orders:
            d.reasons.append(f"already {len(open_orders)} open orders (limit {lim.max_open_orders})")

        # Position limit on whatever currency this order would *receive*.
        receives = base if side == "bid" else quote
        if receives != CASH:
            if receives not in prices:
                d.reasons.append(f"cannot value {receives} in USDT right now")
            else:
                received = amount if side == "bid" else amount * price
                pending = sum(_receives(o, receives) for o in open_orders)
                held = self.engine.wallet()["balances"].get(receives, 0.0)
                d.position_after_usdt = (held + pending + received) * prices[receives]
                if d.position_after_usdt > lim.max_position_usdt:
                    d.reasons.append(
                        f"{receives} position would be {d.position_after_usdt:,.2f} USDT "
                        f"(limit {lim.max_position_usdt:,.2f})"
                    )

        d.approved = not d.reasons
        return d

    def record_breach(self, decision: RiskDecision) -> None:
        self.breaches.append(decision.to_dict())


def _receives(order: dict[str, Any], currency: str) -> float:
    base, quote = order["product"].split("/")
    if order["side"] == "bid" and base == currency:
        return order["amount"]
    if order["side"] == "ask" and quote == currency:
        return order["amount"] * order["price"]
    return 0.0
