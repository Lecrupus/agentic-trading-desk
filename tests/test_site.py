import json

import pytest

from trading_desk.engine import DEFAULT_DATA
from trading_desk.evals import run_baseline
from trading_desk.risk import RiskLimits
from trading_desk.site import agent_handoffs, build, market_timeline, run_steps


def test_market_timeline_has_every_step():
    market = market_timeline(DEFAULT_DATA)
    assert len(market) == 8
    assert market[0]["books"]["ETH/USDT"]["best_ask"] > market[0]["books"]["ETH/USDT"]["best_bid"]
    assert market[7]["stale_prices"] == ["BTC", "DOGE", "ETH"]  # the snapshot's last step has no USDT books


def test_run_steps_groups_by_step_and_tracks_pnl():
    snap = lambda reason, step, eth: {"event": "snapshot", "reason": reason, "step": step,
                                      "balances": {"ETH": eth, "USDT": 0}, "prices_usdt": {"ETH": 100, "USDT": 1}}
    entries = [
        snap("start", 0, 1),
        {"event": "tool", "tool": "get_order_book", "status": "ok"},
        {"event": "tool", "tool": "place_order", "status": "risk_rejected", "args": {}, "error": "too big"},
        {"event": "tool", "tool": "advance_time", "status": "ok", "result": {"fills": [], "expired": []}},
        snap("advance_time", 1, 2),
    ]
    steps, pnl = run_steps(entries, 2)
    assert steps[0]["reads"] == 1
    assert [e["status"] for e in steps[0]["events"]] == ["risk_rejected", "ok"]
    assert steps[1]["events"] == []
    assert pnl == [0.0, 100.0]


def test_agent_handoffs_follow_the_orchestrator(tmp_path):
    lines = [
        {"type": "AssistantMessage", "parent_tool_use_id": None, "content": [
            {"text": "Step 1: asking the analyst."},
            {"id": "a1", "name": "Agent", "input": {"subagent_type": "analyst", "prompt": "Ideas for step 1?"}}]},
        {"type": "AssistantMessage", "parent_tool_use_id": "a1", "content": [  # inside the sub-agent: hidden
            {"id": "x", "name": "mcp__trading-desk__get_order_book", "input": {}}]},
        {"type": "UserMessage", "parent_tool_use_id": None, "content": [
            {"tool_use_id": "a1", "content": [{"type": "text", "text": '{"ideas": []}'}], "is_error": False}]},
        {"type": "AssistantMessage", "parent_tool_use_id": None, "content": [
            {"id": "t1", "name": "mcp__trading-desk__advance_time", "input": {}}]},
        {"type": "UserMessage", "parent_tool_use_id": None, "content": [{"tool_use_id": "t1", "content": "{}", "is_error": False}]},
        {"type": "AssistantMessage", "parent_tool_use_id": None, "content": [
            {"id": "a2", "name": "Agent", "input": {"subagent_type": "risk", "prompt": "Review"}}]},
    ]
    path = tmp_path / "t.jsonl"
    path.write_text("\n".join(json.dumps(l) for l in lines))
    steps = agent_handoffs(path, 3)
    kinds = [[i["kind"] for i in s] for s in steps]
    assert kinds == [["note", "handoff", "advance"], ["handoff"], []]
    assert steps[0][1]["agent"] == "analyst" and steps[0][1]["reply"] == '{"ideas": []}'
    assert steps[1][0]["reply"] is None  # no reply recorded yet


@pytest.mark.anyio
async def test_build_writes_a_complete_site(tmp_path):
    evals = tmp_path / "evals"
    evals.mkdir()
    scores = [await run_baseline(n, DEFAULT_DATA, evals / f"20200317.{n}.jsonl", RiskLimits()) for n in ("hold", "taker")]
    (evals / "report.json").write_text(json.dumps({"scores": [s.__dict__ for s in scores], "problems": []}))

    site = build(evals, tmp_path / "site")
    assert (tmp_path / "site" / "index.html").exists()
    data = json.loads((tmp_path / "site" / "data" / "site.json").read_text())
    assert [r["name"] for r in data["runs"]] == ["hold", "taker"]
    assert len(data["runs"][1]["pnl"]) == 9  # start + 8 steps
    assert data["runs"][1]["pnl"][-1] == pytest.approx(scores[1].pnl_usdt)
    assert site["scenario"] == "20200317"
