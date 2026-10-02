"""The desk's wiring, checked without calling a model."""

import pytest

from trading_desk.agents import (
    ADVANCE_TIME, ANALYST_TOOLS, CANCEL_ORDER, DELEGATE, EXECUTION_TOOLS, PLACE_ORDER, RESET_MARKET,
    RISK_TOOLS, DeskConfig, build_options, enforce_roles, mcp_tool, role_decision,
)


@pytest.mark.parametrize("tool, agent, allowed", [
    (PLACE_ORDER, "execution", True),
    (PLACE_ORDER, "analyst", False),
    (PLACE_ORDER, "risk", False),
    (PLACE_ORDER, None, False),          # the orchestrator must delegate, not trade
    (CANCEL_ORDER, "execution", True),
    (ADVANCE_TIME, None, True),
    (ADVANCE_TIME, "execution", False),  # only the orchestrator moves the clock
    (RESET_MARKET, None, False),
    (mcp_tool("get_order_book"), "analyst", True),
    (mcp_tool("check_order"), "risk", True),
])
def test_role_decision(tool, agent, allowed):
    ok, reason = role_decision(tool, agent)
    assert ok is allowed
    assert bool(reason) is not allowed


def test_only_execution_is_shown_trading_tools():
    assert PLACE_ORDER in EXECUTION_TOOLS
    assert PLACE_ORDER not in ANALYST_TOOLS and PLACE_ORDER not in RISK_TOOLS
    assert ADVANCE_TIME not in ANALYST_TOOLS + RISK_TOOLS + EXECUTION_TOOLS


@pytest.mark.anyio
async def test_hook_denies_with_a_reason():
    out = await enforce_roles({"tool_name": PLACE_ORDER, "agent_type": "analyst"}, "t1", None)
    assert out["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert "only execution may" in out["hookSpecificOutput"]["permissionDecisionReason"]
    assert await enforce_roles({"tool_name": PLACE_ORDER, "agent_type": "execution"}, "t2", None) == {}


def test_options(tmp_path):
    opts = build_options(DeskConfig(journal=tmp_path / "j.jsonl"))
    assert opts.model == "claude-opus-5-5"
    assert opts.tools == [DELEGATE]
    assert opts.permission_mode == "dontAsk"
    assert RESET_MARKET in opts.disallowed_tools
    assert set(opts.agents) == {"analyst", "risk", "execution"}
    assert opts.agents["analyst"].tools == ANALYST_TOOLS

    server = opts.mcp_servers["trading-desk"]
    assert server["args"] == ["-m", "trading_desk.mcp_server"]
    assert server["env"]["TRADING_JOURNAL"].endswith("j.jsonl")
    assert '"max_open_orders": 5' in server["env"]["TRADING_RISK_LIMITS"]
    assert opts.hooks["PreToolUse"][0].hooks == [enforce_roles]
