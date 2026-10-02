"""The trading desk: an orchestrator agent and three sub-agents, built on the Claude Agent SDK.

    orchestrator ── runs the session one time step at a time; the only one who advances time
      ├─ analyst    read-only market tools → proposes trades
      ├─ risk       risk tools + check_order → approves, resizes or rejects each proposal
      └─ execution  the only agent allowed to place or cancel orders

Every agent reaches the exchange through the same MCP server (trading_desk.mcp_server),
launched over stdio exactly as Claude Code would launch it. Three layers keep roles apart:

1. Each sub-agent's `tools` list: the analyst is never even shown place_order.
2. A PreToolUse hook (`role_decision`): state-changing tools are denied unless the
   right agent is calling, whatever the tool lists say.
3. The MCP server's risk gate: limits hold even if both of the above were bypassed.

Run one session:  uv run python -m trading_desk.agents
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from trading_desk.engine import REPO_ROOT
from trading_desk.risk import RiskLimits

SERVER = "trading-desk"
DEFAULT_MODEL = "claude-opus-5-5"


def mcp_tool(name: str) -> str:
    """The name Claude Code gives an MCP tool: mcp__<server>__<tool>."""
    return f"mcp__{SERVER}__{name}"


MARKET_READ = [mcp_tool(t) for t in (
    "list_products", "get_market_time", "get_order_book", "get_wallet", "get_portfolio",
    "get_risk_limits", "list_open_orders",
)]
CHECK_ORDER = mcp_tool("check_order")
PLACE_ORDER = mcp_tool("place_order")
CANCEL_ORDER = mcp_tool("cancel_order")
ADVANCE_TIME = mcp_tool("advance_time")
RESET_MARKET = mcp_tool("reset_market")
DELEGATE = "Agent"  # Claude Code's built-in tool for handing a task to a sub-agent

ANALYST_TOOLS = MARKET_READ
RISK_TOOLS = MARKET_READ + [CHECK_ORDER]
EXECUTION_TOOLS = [mcp_tool("get_order_book"), mcp_tool("list_open_orders"), CHECK_ORDER, PLACE_ORDER, CANCEL_ORDER]
ORCHESTRATOR_TOOLS = [DELEGATE, mcp_tool("get_market_time"), mcp_tool("get_portfolio"), ADVANCE_TIME]

# State-changing tools and who may call them. None means the orchestrator
# itself (the main thread has no agent_type).
WRITE_PERMISSIONS: dict[str, set[str | None]] = {
    PLACE_ORDER: {"execution"},
    CANCEL_ORDER: {"execution"},
    ADVANCE_TIME: {None},
    RESET_MARKET: set(),  # nobody: a run must not rewind its own market
}


def role_decision(tool_name: str, agent_type: str | None) -> tuple[bool, str]:
    """Whether `agent_type` may call `tool_name`. Pure, so it can be unit-tested."""
    allowed = WRITE_PERMISSIONS.get(tool_name)
    if allowed is None:
        return True, ""
    if agent_type in allowed:
        return True, ""
    who = agent_type or "orchestrator"
    if not allowed:
        return False, f"{tool_name} is disabled for every agent on this desk"
    owners = ", ".join(a or "orchestrator" for a in sorted(allowed, key=str))
    return False, f"{who} may not call {tool_name}; only {owners} may"


async def enforce_roles(input_data: dict[str, Any], tool_use_id: str | None, context: Any) -> dict[str, Any]:
    """PreToolUse hook: deny state-changing tools to the wrong agent."""
    ok, reason = role_decision(input_data["tool_name"], input_data.get("agent_type"))
    if ok:
        return {}
    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": reason,
        }
    }


# ---------------------------------------------------------------------------
# Prompts
# ---------------------------------------------------------------------------

ORCHESTRATOR_PROMPT = """\
You run a small crypto trading desk on a simulated exchange that replays a real
order-book snapshot. The session has a fixed number of time steps. Your job is to
finish the session with a higher portfolio value (in USDT) than simply holding
the starting wallet would give, without breaking risk limits.

You coordinate three specialists through the Agent tool:
- analyst: studies the order books and proposes trades (it cannot trade).
- risk: reviews proposals against the hard limits and the wallet; approves,
  resizes or rejects each one.
- execution: places exactly the orders risk approved, and reports order ids.

For each time step, in order:
1. Call get_market_time. If done is true, go to the wrap-up.
2. Ask the analyst for trade ideas for this step. Pass along anything useful
   from earlier steps (fills, expired orders).
3. If it proposed trades, send them to risk. Only risk-approved orders (with
   risk's final size) go to execution. If nothing is approved, place nothing.
4. Call advance_time to run the matching engine, and note fills and expiries.

Doing nothing in a step is a valid decision; trading costs the spread.

Wrap-up: call get_portfolio and finish with a short report: trades made, fills,
anything risk rejected, and the final portfolio value.
"""

ANALYST_PROMPT = """\
You are the desk's market analyst. You can read the market but cannot trade.

Look at the order books (get_order_book; depth 5 is usually enough), the wallet
and the risk limits, then propose at most 2 trades for the current time step.

Things worth checking:
- Spreads: crossing a wide spread loses money immediately.
- Cross rates: ETH/BTC should be close to (ETH/USDT) / (BTC/USDT), and likewise
  for DOGE. A large gap after costs is an opportunity.
- Size: keep each order well inside max_order_notional_usdt and the price
  within max_price_deviation of the mid.

Orders fill only against the book at this step, at the ask price for bids. An
order priced off the book simply expires.

Reply with JSON only, in this shape:
{"step": <int>, "ideas": [{"side": "bid"|"ask", "product": "ETH/USDT",
  "price": <float>, "amount": <float>, "rationale": "<one sentence>"}]}
Use "ideas": [] when nothing is worth the spread.
"""

RISK_PROMPT = """\
You are the desk's risk officer. You review trade proposals; you cannot trade.

For each proposal:
1. Run check_order with its exact side, product, price and amount.
2. Look at the wallet and open orders. Is the trade sensible: does it fit the
   rationale, and is the size proportionate?
3. Decide: approve as is, resize (smaller amount, re-run check_order on the new
   size), or reject.

Never approve an order that check_order rejects.

Reply with JSON only:
{"decisions": [{"side": ..., "product": ..., "price": ..., "amount": <final>,
  "decision": "approve"|"resize"|"reject", "reason": "<one sentence>"}]}
"""

EXECUTION_PROMPT = """\
You are the desk's execution trader. You place exactly the orders you are
given (approved or resized), with the exact side, product, price and amount.
Do not change them, and do not add orders of your own.

Place each one with place_order. If one is rejected, do not retry it with
different numbers: report the error.

Reply with JSON only:
{"placed": [{"order_id": <int>, "side": ..., "product": ..., "price": ...,
  "amount": ...}], "errors": ["<message>", ...]}
"""


@dataclass
class DeskConfig:
    model: str = DEFAULT_MODEL
    subagent_model: str = "inherit"
    max_turns: int = 200
    max_budget_usd: float | None = 5.0
    limits: RiskLimits = field(default_factory=RiskLimits)
    journal: Path | None = None
    transcript: Path | None = None
    cli_path: str | None = None


def build_options(cfg: DeskConfig):
    """Builds the Agent SDK options for one desk session."""
    from claude_agent_sdk import AgentDefinition, ClaudeAgentOptions, HookMatcher

    server_env = {"TRADING_RISK_LIMITS": json.dumps(asdict(cfg.limits))}
    if cfg.journal:
        server_env["TRADING_JOURNAL"] = str(cfg.journal.resolve())

    agents = {
        "analyst": AgentDefinition(
            description="Market analyst. Reads order books and proposes trades as JSON. Cannot trade.",
            prompt=ANALYST_PROMPT, tools=ANALYST_TOOLS, model=cfg.subagent_model,
            skills=["read-order-book"],
        ),
        "risk": AgentDefinition(
            description="Risk officer. Reviews proposed trades against limits; approves, resizes or rejects.",
            prompt=RISK_PROMPT, tools=RISK_TOOLS, model=cfg.subagent_model,
            skills=["place-safe-order"],
        ),
        "execution": AgentDefinition(
            description="Execution trader. Places exactly the risk-approved orders and reports order ids.",
            prompt=EXECUTION_PROMPT, tools=EXECUTION_TOOLS, model=cfg.subagent_model,
            skills=["place-safe-order"],
        ),
    }

    every_tool = sorted(set(ORCHESTRATOR_TOOLS + ANALYST_TOOLS + RISK_TOOLS + EXECUTION_TOOLS))
    return ClaudeAgentOptions(
        model=cfg.model,
        system_prompt=ORCHESTRATOR_PROMPT,
        tools=[DELEGATE],               # no built-in file/shell tools at all
        allowed_tools=every_tool,       # pre-approved; anything else is refused (dontAsk)
        disallowed_tools=[RESET_MARKET],
        permission_mode="dontAsk",
        mcp_servers={SERVER: {
            "type": "stdio",
            "command": sys.executable,
            "args": ["-m", "trading_desk.mcp_server"],
            "env": server_env,
        }},
        strict_mcp_config=True,         # ignore .mcp.json and user-level servers
        setting_sources=["project"],    # loads .claude/skills from this repo
        agents=agents,
        hooks={"PreToolUse": [HookMatcher(matcher=None, hooks=[enforce_roles])]},
        max_turns=cfg.max_turns,
        max_budget_usd=cfg.max_budget_usd,
        cwd=str(REPO_ROOT),
        cli_path=cfg.cli_path,
    )


@dataclass
class DeskResult:
    ok: bool
    final_report: str
    num_turns: int
    cost_usd: float | None
    tool_calls: int
    tool_errors: int
    role_denials: int
    stop_reason: str | None
    errors: list[str]


async def run_desk(cfg: DeskConfig, prompt: str = "Run the trading session from the current step until the market closes.") -> DeskResult:
    from claude_agent_sdk import AssistantMessage, ResultMessage, ToolResultBlock, ToolUseBlock, UserMessage, query

    tool_calls = tool_errors = 0
    result: ResultMessage | None = None
    transcript = cfg.transcript.open("w", encoding="utf-8") if cfg.transcript else None

    try:
        async for message in query(prompt=prompt, options=build_options(cfg)):
            if isinstance(message, AssistantMessage):
                for block in message.content:
                    if isinstance(block, ToolUseBlock):
                        tool_calls += 1
            elif isinstance(message, UserMessage) and isinstance(message.content, list):
                for block in message.content:
                    if isinstance(block, ToolResultBlock) and block.is_error:
                        tool_errors += 1
            elif isinstance(message, ResultMessage):
                result = message
            if transcript:
                transcript.write(json.dumps(_to_jsonable(message), default=str) + "\n")
    finally:
        if transcript:
            transcript.close()

    if result is None:
        return DeskResult(False, "", 0, None, tool_calls, tool_errors, 0, None, ["no result message"])
    return DeskResult(
        ok=not result.is_error,
        final_report=result.result or "",
        num_turns=result.num_turns,
        cost_usd=result.total_cost_usd,
        tool_calls=tool_calls,
        tool_errors=tool_errors,
        role_denials=len(result.permission_denials or []),
        stop_reason=result.terminal_reason or result.stop_reason,
        errors=list(result.errors or []),
    )


def _to_jsonable(message: Any) -> dict[str, Any]:
    try:
        body = asdict(message)
    except TypeError:
        body = {"repr": repr(message)}
    return {"type": type(message).__name__, **body}


def main() -> None:
    parser = argparse.ArgumentParser(description="Run one trading-desk session.")
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--subagent-model", default="inherit", help='"inherit", "sonnet", "opus", "haiku" or a model id')
    parser.add_argument("--max-budget-usd", type=float, default=5.0)
    parser.add_argument("--journal", type=Path, default=REPO_ROOT / "runs" / "desk.jsonl")
    parser.add_argument("--cli-path", default=os.environ.get("CLAUDE_CLI_PATH"))
    args = parser.parse_args()

    args.journal.parent.mkdir(parents=True, exist_ok=True)
    args.journal.unlink(missing_ok=True)
    cfg = DeskConfig(
        model=args.model, subagent_model=args.subagent_model, max_budget_usd=args.max_budget_usd,
        journal=args.journal, transcript=args.journal.with_suffix(".transcript.jsonl"), cli_path=args.cli_path,
    )
    result = asyncio.run(run_desk(cfg))
    print(result.final_report)
    print(json.dumps({k: v for k, v in asdict(result).items() if k != "final_report"}, indent=2), file=sys.stderr)


if __name__ == "__main__":
    main()
