import pytest
from starlette.testclient import TestClient

from trading_desk.web import SessionPool, create_app


@pytest.fixture
def pool():
    p = SessionPool(max_sessions=2)
    yield p
    p.close_all()


@pytest.fixture
def http(pool):
    with TestClient(create_app(pool=pool)) as c:
        yield c


def post(http, path, body, sid):
    return http.post(path, json=body, headers={"x-desk-session": sid})


def test_page_and_health(http):
    assert "Trading Desk Playground" in http.get("/").text
    assert http.get("/healthz").json()["ok"] is True


def test_session_trade_and_step(http):
    first = http.get("/api/state").json()
    sid, book = first["session"], first["state"]["books"]["ETH/USDT"]
    assert first["state"]["time"]["step"] == 0

    r = post(http, "/api/order", {"side": "bid", "product": "ETH/USDT", "price": book["best_ask"], "amount": 1}, sid)
    assert r.status_code == 200 and r.json()["result"]["order_id"] == 1

    state = post(http, "/api/step", {}, sid).json()["state"]
    assert state["wallet"]["balances"]["ETH"] == 101
    assert state["time"]["step"] == 1
    kinds = [(e["tool"], e["status"]) for e in state["activity"]]
    assert kinds == [("place_order", "ok"), ("advance_time", "ok")]  # reads are not logged as activity


def test_risk_gate_blocks_in_the_playground(http):
    sid = http.get("/api/state").json()["session"]
    r = post(http, "/api/order", {"side": "bid", "product": "ETH/USDT", "price": 117.3, "amount": 100}, sid)
    assert r.status_code == 400
    assert r.json()["kind"] == "risk_rejected"
    assert r.json()["state"]["limits"]["orders_blocked"] == 1


def test_sessions_are_isolated_and_bounded(http, pool):
    a = http.get("/api/state").json()["session"]
    b = http.get("/api/state").json()["session"]
    post(http, "/api/step", {}, a)
    assert http.get("/api/state", headers={"x-desk-session": b}).json()["state"]["time"]["step"] == 0
    http.get("/api/state")  # a third browser evicts the least recently used
    assert len(pool.sessions) == 2


def test_bad_input_is_a_400_not_a_crash(http):
    sid = http.get("/api/state").json()["session"]
    assert post(http, "/api/order", {"side": "bid"}, sid).status_code == 400
    assert post(http, "/api/order", {"side": "bid", "product": "ETH/USDT\nreset", "price": 1, "amount": 1}, sid).status_code == 400
