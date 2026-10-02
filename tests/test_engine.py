import pytest

from trading_desk.engine import EngineError


def test_ready_line_reports_the_snapshot(engine):
    assert engine.ready["orders_loaded"] == 3540
    assert engine.ready["total_steps"] == 8


def test_products(engine):
    assert engine.products() == ["BTC/USDT", "DOGE/BTC", "DOGE/USDT", "ETH/BTC", "ETH/USDT"]


def test_book_is_sorted_best_first(engine):
    book = engine.book("ETH/USDT", depth=5)
    bids = [p for p, _ in book["bids"]]
    asks = [p for p, _ in book["asks"]]
    assert bids == sorted(bids, reverse=True)
    assert asks == sorted(asks)
    assert book["best_bid"] < book["best_ask"]


def test_marketable_bid_fills_at_the_ask(engine):
    best_ask = engine.book("ETH/USDT")["best_ask"]
    usdt_before = engine.wallet()["balances"]["USDT"]

    order = engine.place("bid", "ETH/USDT", best_ask + 1, 1)
    assert engine.wallet()["available"]["USDT"] < usdt_before  # funds reserved

    result = engine.step()
    assert result["fills"] == [
        {"order_id": order["order_id"], "product": "ETH/USDT", "side": "bid", "price": best_ask, "amount": 1}
    ]
    assert engine.wallet()["balances"]["ETH"] == 101
    assert result["step"] == 1


def test_resting_order_expires_after_one_step(engine):
    order = engine.place("bid", "ETH/USDT", 1, 1)
    assert engine.step()["expired"] == [order["order_id"]]
    assert engine.orders() == []


def test_engine_errors_are_raised(engine):
    with pytest.raises(EngineError, match="insufficient USDT"):
        engine.place("bid", "ETH/USDT", 1_000_000, 1)


def test_injection_is_refused(engine):
    with pytest.raises(ValueError):
        engine.book("ETH/USDT 5\nreset")
    with pytest.raises(ValueError):
        engine.place("bid", "ETH/USDT step", 1, 1)


def test_market_closes_after_last_step(engine):
    for _ in range(8):
        engine.step()
    assert engine.time()["done"] is True
    with pytest.raises(EngineError, match="market closed"):
        engine.place("bid", "ETH/USDT", 1, 1)
    assert engine.reset()["step"] == 0
