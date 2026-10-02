import json

import pytest

from trading_desk.engine import DEFAULT_DATA
from trading_desk.evals import Score, check, markdown_report, run_baseline, score_journal
from trading_desk.risk import RiskLimits

pytestmark = pytest.mark.anyio


async def run(name, tmp_path):
    return await run_baseline(name, DEFAULT_DATA, tmp_path / f"{name}.jsonl", RiskLimits())


async def test_holding_scores_exactly_zero(tmp_path):
    s = await run("hold", tmp_path)
    assert s.completed and s.steps_completed == 8
    assert s.pnl_usdt == pytest.approx(0, abs=1e-6)
    assert s.orders_placed == s.fills == s.risk_breaches == s.failed_tool_calls == 0


async def test_crossing_the_spread_costs_money(tmp_path):
    s = await run("taker", tmp_path)
    assert s.fills > 0
    assert s.pnl_usdt < 0


async def test_reckless_mistakes_are_counted(tmp_path):
    s = await run("reckless", tmp_path)
    assert s.risk_breaches > 0
    assert s.failed_tool_calls > 0
    assert s.orders_placed == 0  # the gate stopped every one


async def test_baselines_pass_the_checks(tmp_path):
    scores = [await run(n, tmp_path) for n in ("hold", "taker", "momentum", "reckless")]
    assert check(scores, max_agent_breaches=0, max_agent_failed_calls=3) == []
    assert "| hold |" in markdown_report(scores, [])


def test_pnl_ignores_market_drift(tmp_path):
    # Wallet unchanged, but BTC moved from 5000 to 6000: not the trader's doing.
    path = tmp_path / "j.jsonl"
    snap = {"event": "snapshot", "balances": {"BTC": 1, "USDT": 100}, "stale_prices": []}
    path.write_text("\n".join(json.dumps(e) for e in [
        {**snap, "step": 0, "done": False, "prices_usdt": {"BTC": 5000, "USDT": 1}, "value_usdt": 5100},
        {**snap, "step": 8, "done": True, "prices_usdt": {"BTC": 6000, "USDT": 1}, "value_usdt": 6100},
    ]))
    s = score_journal(path, "x", "y")
    assert s.pnl_usdt == 0
    assert s.end_value_usdt - s.start_value_usdt == 1000


def test_agent_limits_are_enforced():
    agent = Score("agent:claude-opus-5-5", "s", True, 8, 0, 0, 0, 1, 1, 30, 4, 1, [])
    problems = check([agent], max_agent_breaches=0, max_agent_failed_calls=3)
    assert len(problems) == 2
