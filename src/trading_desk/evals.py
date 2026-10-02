"""Evaluation harness: replay a market snapshot and score whoever traded it.

Each run starts a fresh exchange at step 0 and plays to the close. The trader is
either a scripted baseline (no model, runs in CI for free) or the agent desk.
Both reach the exchange through the same MCP server, which writes a journal;
scores come from that journal, not from the trader's own account.

Metrics
  pnl_usdt            final wallet minus *the starting wallet*, both at final prices.
                      Holding still scores exactly 0, so market drift is not skill.
  risk_breaches       place_order calls the risk gate rejected
  failed_tool_calls   tool calls the server answered with an error (bad input, no funds)
  fills, orders       activity

Usage:
  uv run python -m trading_desk.evals                      # baselines only
  uv run python -m trading_desk.evals --agent              # plus the agent desk (needs Claude)
  uv run python -m trading_desk.evals --check              # fail on broken invariants (CI)
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable

from mcp import Client

from trading_desk.engine import DEFAULT_DATA, REPO_ROOT, Engine
from trading_desk.market import mark_to_market
from trading_desk.mcp_server import Journal, build_server
from trading_desk.risk import RiskLimits

# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------


@dataclass
class Score:
    name: str
    scenario: str
    completed: bool
    steps_completed: int
    pnl_usdt: float
    start_value_usdt: float
    end_value_usdt: float
    orders_placed: int
    fills: int
    tool_calls: int
    failed_tool_calls: int
    risk_breaches: int
    stale_prices_at_end: list[str]
    extra: dict[str, Any] = field(default_factory=dict)


def read_journal(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def score_journal(path: Path, name: str, scenario: str) -> Score:
    entries = read_journal(path)
    snapshots = [e for e in entries if e["event"] == "snapshot"]
    if not snapshots:
        raise ValueError(f"{path} has no wallet snapshots")
    start, end = snapshots[0], snapshots[-1]
    tools = [e for e in entries if e["event"] == "tool"]

    final_prices = end["prices_usdt"]
    end_value = mark_to_market(end["balances"], final_prices)
    hold_value = mark_to_market(start["balances"], final_prices)

    return Score(
        name=name,
        scenario=scenario,
        completed=bool(end["done"]),
        steps_completed=int(end["step"]),
        pnl_usdt=end_value - hold_value,
        start_value_usdt=start["value_usdt"],
        end_value_usdt=end_value,
        orders_placed=sum(1 for e in tools if e["tool"] == "place_order" and e["status"] == "ok"),
        fills=sum(len(e["result"]["fills"]) for e in tools if e["tool"] == "advance_time" and e["status"] == "ok"),
        tool_calls=len(tools),
        failed_tool_calls=sum(1 for e in tools if e["status"] == "error"),
        risk_breaches=sum(1 for e in tools if e["status"] == "risk_rejected"),
        stale_prices_at_end=end.get("stale_prices", []),
    )


# ---------------------------------------------------------------------------
# Baseline strategies (no model)
# ---------------------------------------------------------------------------


class Desk:
    """A tiny wrapper so strategies read like the agents' tool calls."""

    def __init__(self, client: Client):
        self.client = client

    async def call(self, tool: str, **args: Any) -> tuple[bool, Any]:
        result = await self.client.call_tool(tool, args)
        if result.is_error:
            return False, result.content[0].text if result.content else ""
        data = result.structured_content
        return True, data.get("result", data) if isinstance(data, dict) and set(data) == {"result"} else data

    async def done(self) -> bool:
        return (await self.call("get_market_time"))[1]["done"]

    async def book(self, product: str) -> dict[str, Any]:
        return (await self.call("get_order_book", product=product, depth=5))[1]


async def hold(desk: Desk) -> None:
    """Never trades. Its PnL is 0 by definition: the reference point for everything else."""
    while not await desk.done():
        await desk.call("advance_time")


async def taker(desk: Desk) -> None:
    """Buys 0.5 ETH at the ask and sells it back at the bid on alternate steps. Pays the spread."""
    buy = True
    while not await desk.done():
        b = await desk.book("ETH/USDT")
        if b["best_bid"] and b["best_ask"]:
            side, price = ("bid", b["best_ask"]) if buy else ("ask", b["best_bid"])
            ok, _ = await desk.call("check_order", side=side, product="ETH/USDT", price=price, amount=0.5)
            if ok:
                await desk.call("place_order", side=side, product="ETH/USDT", price=price, amount=0.5)
                buy = not buy
        await desk.call("advance_time")


async def momentum(desk: Desk) -> None:
    """Buys 1 ETH after the ETH/USDT mid rises, sells 1 after it falls. Checks risk first."""
    last_mid = None
    while not await desk.done():
        b = await desk.book("ETH/USDT")
        if b["best_bid"] and b["best_ask"]:
            mid = (b["best_bid"] + b["best_ask"]) / 2
            if last_mid is not None and mid != last_mid:
                side, price = ("bid", b["best_ask"]) if mid > last_mid else ("ask", b["best_bid"])
                ok, check = await desk.call("check_order", side=side, product="ETH/USDT", price=price, amount=1.0)
                if ok and check["approved"]:
                    await desk.call("place_order", side=side, product="ETH/USDT", price=price, amount=1.0)
            last_mid = mid
        await desk.call("advance_time")


async def reckless(desk: Desk) -> None:
    """Ignores the risk tools and makes classic mistakes. Proves the metrics catch them."""
    while not await desk.done():
        b = await desk.book("BTC/USDT")
        if b["best_ask"]:
            await desk.call("place_order", side="bid", product="BTC/USDT", price=b["best_ask"], amount=5)  # ~27k notional
            await desk.call("place_order", side="bid", product="BTC/USDT", price=b["best_ask"] * 1.5, amount=0.01)  # fat finger
        await desk.call("get_order_book", product="XRP/USDT")  # not a product
        await desk.call("advance_time")


BASELINES: dict[str, Callable[[Desk], Awaitable[None]]] = {
    "hold": hold, "taker": taker, "momentum": momentum, "reckless": reckless,
}


async def run_baseline(name: str, data: Path, journal: Path, limits: RiskLimits) -> Score:
    journal.unlink(missing_ok=True)
    with Engine(data=data) as engine:
        async with Client(build_server(engine, limits, Journal(journal))) as client:
            await BASELINES[name](Desk(client))
    return score_journal(journal, name, data.stem)


# ---------------------------------------------------------------------------
# The agent desk
# ---------------------------------------------------------------------------


async def run_agent(data: Path, journal: Path, limits: RiskLimits, model: str, subagent_model: str,
                    max_budget_usd: float, cli_path: str | None) -> Score:
    from trading_desk.agents import DeskConfig, run_desk

    journal.unlink(missing_ok=True)
    os.environ["TRADING_ENGINE_DATA"] = str(data.resolve())  # inherited by the MCP server process
    cfg = DeskConfig(model=model, subagent_model=subagent_model, max_budget_usd=max_budget_usd, limits=limits,
                     journal=journal, transcript=journal.with_suffix(".transcript.jsonl"), cli_path=cli_path)
    started = time.monotonic()
    result = await run_desk(cfg)
    if not journal.exists():
        raise RuntimeError(f"agent run wrote no journal; errors: {result.errors}")
    score = score_journal(journal, f"agent:{model}", data.stem)
    score.extra = {
        "cost_usd": result.cost_usd, "turns": result.num_turns, "seconds": round(time.monotonic() - started, 1),
        "sdk_tool_calls": result.tool_calls, "sdk_tool_errors": result.tool_errors,
        "role_denials": result.role_denials, "stop_reason": result.stop_reason, "ok": result.ok,
    }
    return score


# ---------------------------------------------------------------------------
# Checks and reports
# ---------------------------------------------------------------------------


def check(scores: list[Score], max_agent_breaches: int, max_agent_failed_calls: int) -> list[str]:
    """Invariants a healthy harness and desk must satisfy. Returns failures."""
    problems = []
    for s in scores:
        tag = f"{s.name} on {s.scenario}"
        if not s.completed:
            problems.append(f"{tag}: did not reach the market close (step {s.steps_completed})")
        if s.name == "hold" and (abs(s.pnl_usdt) > 1e-6 or s.orders_placed):
            problems.append(f"{tag}: holding must place no orders and score PnL 0 (got {s.pnl_usdt})")
        if s.name in ("hold", "taker", "momentum") and (s.risk_breaches or s.failed_tool_calls):
            problems.append(f"{tag}: careful baselines must not breach or fail calls")
        if s.name == "reckless" and (s.risk_breaches == 0 or s.failed_tool_calls == 0):
            problems.append(f"{tag}: the harness failed to count reckless mistakes")
        if s.name.startswith("agent:"):
            if s.risk_breaches > max_agent_breaches:
                problems.append(f"{tag}: {s.risk_breaches} risk breaches (max {max_agent_breaches})")
            if s.failed_tool_calls > max_agent_failed_calls:
                problems.append(f"{tag}: {s.failed_tool_calls} failed tool calls (max {max_agent_failed_calls})")
    return problems


def markdown_report(scores: list[Score], problems: list[str]) -> str:
    lines = [
        "# Evaluation report", "",
        "PnL is measured against holding the starting wallet, both valued at final prices.", "",
        "| Run | Scenario | Done | PnL (USDT) | Orders | Fills | Tool calls | Failed | Risk breaches | Cost |",
        "|---|---|---|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for s in scores:
        cost = s.extra.get("cost_usd")
        lines.append(
            f"| {s.name} | {s.scenario} | {'yes' if s.completed else 'no'} | {s.pnl_usdt:+,.2f} | {s.orders_placed} | "
            f"{s.fills} | {s.tool_calls} | {s.failed_tool_calls} | {s.risk_breaches} | "
            f"{'' if cost is None else f'${cost:.2f}'} |"
        )
    stale = sorted({c for s in scores for c in s.stale_prices_at_end})
    if stale:
        lines += ["", f"Final prices for {', '.join(stale)} were carried forward from the last step that quoted them."]
    lines += ["", "## Checks", ""] + ([f"- FAIL: {p}" for p in problems] or ["- all passed"])
    return "\n".join(lines) + "\n"


async def evaluate(args: argparse.Namespace) -> int:
    out: Path = args.out or REPO_ROOT / "runs" / time.strftime("eval-%Y%m%d-%H%M%S")
    out.mkdir(parents=True, exist_ok=True)
    limits = RiskLimits()
    scores: list[Score] = []

    for data in args.data:
        for name in args.baselines:
            scores.append(await run_baseline(name, data, out / f"{data.stem}.{name}.jsonl", limits))
        if args.agent:
            scores.append(await run_agent(data, out / f"{data.stem}.agent.jsonl", limits, args.model,
                                          args.subagent_model, args.max_budget_usd, args.cli_path))

    problems = check(scores, args.max_agent_breaches, args.max_agent_failed_calls)
    (out / "report.json").write_text(json.dumps({"scores": [asdict(s) for s in scores], "problems": problems}, indent=2))
    report = markdown_report(scores, problems)
    (out / "report.md").write_text(report, encoding="utf-8")
    print(report)
    print(f"Journals and reports in {out}", file=sys.stderr)
    return 1 if (args.check and problems) else 0


def main() -> None:
    # Rejected tool calls are expected here (the reckless baseline makes them on
    # purpose) and are already counted from the journal; don't log each one.
    logging.getLogger("mcp").setLevel(logging.CRITICAL)
    parser = argparse.ArgumentParser(description="Replay market snapshots and score traders.")
    parser.add_argument("--data", type=Path, nargs="+", default=[DEFAULT_DATA], help="snapshot CSV file(s) to replay")
    parser.add_argument("--baselines", nargs="*", default=list(BASELINES), choices=list(BASELINES))
    parser.add_argument("--agent", action="store_true", help="also run the agent desk (calls Claude; costs money)")
    parser.add_argument("--model", default="claude-opus-5-5")
    parser.add_argument("--subagent-model", default="inherit")
    parser.add_argument("--max-budget-usd", type=float, default=5.0, help="per agent run")
    parser.add_argument("--cli-path", default=os.environ.get("CLAUDE_CLI_PATH"))
    parser.add_argument("--max-agent-breaches", type=int, default=0)
    parser.add_argument("--max-agent-failed-calls", type=int, default=3)
    parser.add_argument("--out", type=Path)
    parser.add_argument("--check", action="store_true", help="exit 1 if any check fails")
    sys.exit(asyncio.run(evaluate(parser.parse_args())))


if __name__ == "__main__":
    main()
