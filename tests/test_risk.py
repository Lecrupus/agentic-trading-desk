import pytest

from trading_desk.market import mark_to_market, usdt_prices
from trading_desk.risk import RiskGate, RiskLimits


@pytest.fixture
def gate(engine):
    return RiskGate(engine)


def best_ask(engine, product):
    return engine.book(product, 1)["best_ask"]


def test_every_currency_gets_a_usdt_price(engine):
    prices = usdt_prices(engine)
    assert set(prices) == {"USDT", "BTC", "ETH", "DOGE"}
    assert 5_000 < prices["BTC"] < 6_000
    assert mark_to_market({"BTC": 2, "USDT": 10}, prices) == pytest.approx(2 * prices["BTC"] + 10)


def test_small_order_is_approved(engine, gate):
    d = gate.check("bid", "ETH/USDT", best_ask(engine, "ETH/USDT"), 1)
    assert d.approved, d.reasons
    assert 100 < d.notional_usdt < 130


def test_order_notional_limit(engine, gate):
    d = gate.check("bid", "ETH/USDT", best_ask(engine, "ETH/USDT"), 100)  # about 11,700 USDT
    assert not d.approved
    assert "notional" in d.reasons[0]


def test_notional_is_valued_in_usdt_for_cross_products(engine, gate):
    d = gate.check("bid", "ETH/BTC", best_ask(engine, "ETH/BTC"), 1)
    assert 100 < d.notional_usdt < 130  # 1 ETH is about 117 USDT whatever it is quoted in


def test_fat_finger_price_is_rejected(engine, gate):
    d = gate.check("bid", "ETH/USDT", best_ask(engine, "ETH/USDT") * 1.10, 1)
    assert not d.approved
    assert "from mid" in d.reasons[0]


def test_position_limit_counts_pending_orders(engine, gate):
    price = best_ask(engine, "BTC/USDT")
    for _ in range(2):  # 10 BTC held + 2 x 1.8 pending is about 72,800 USDT
        assert gate.check("bid", "BTC/USDT", price, 1.8).approved
        engine.place("bid", "BTC/USDT", price, 1.8)
    d = gate.check("bid", "BTC/USDT", price, 1.8)
    assert not d.approved
    assert "BTC position" in d.reasons[0]


def test_selling_btc_for_usdt_is_never_a_position_breach(engine, gate):
    d = gate.check("ask", "BTC/USDT", engine.book("BTC/USDT", 1)["best_bid"], 1.5)
    assert d.approved, d.reasons


def test_open_order_limit(engine):
    gate = RiskGate(engine, RiskLimits(max_open_orders=2))
    price = best_ask(engine, "ETH/USDT")
    for _ in range(2):
        engine.place("bid", "ETH/USDT", price, 0.1)
    assert "open orders" in gate.check("bid", "ETH/USDT", price, 0.1).reasons[0]


def test_bad_input_is_rejected_before_any_pricing(gate):
    assert not gate.check("bid", "XRP/USDT", 1, 1).approved
    assert not gate.check("bid", "ETH/USDT", -1, 1).approved
