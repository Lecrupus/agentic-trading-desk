"""Builds the replay dashboard: a static site that shows what each trader did, step by step.

It reads an evaluation's journals (and, if present, an agent run's transcript),
replays the market to record every step's order books, and writes one JSON file
next to the page. CI publishes the result to GitHub Pages.

  uv run python -m trading_desk.evals --out runs/site
  uv run python -m trading_desk.site --evals runs/site --out _site [--agent runs/agent]
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import time
from importlib import resources
from pathlib import Path
from typing import Any

from trading_desk.engine import DEFAULT_DATA, Engine
from trading_desk.evals import read_journal
from trading_desk.market import PriceTracker, mark_to_market

BOOK_DEPTH = 6
TEXT_LIMIT = 1500
READS = {"list_products", "get_market_time", "get_order_book", "get_wallet", "get_portfolio",
         "get_risk_limits", "list_open_orders"}
ADVANCE = "mcp__trading-desk__advance_time"


def market_timeline(data: Path) -> list[dict[str, Any]]:
    """The order books and USDT prices at every step, from a fresh replay."""
    steps = []
    with Engine(data=data) as engine:
        tracker = PriceTracker(engine)
        total = engine.time()["total_steps"]
        for _ in range(total):
            t = engine.time()
            books = {}
            for p in engine.products():
                b = engine.book(p, BOOK_DEPTH)
                books[p] = {k: b[k] for k in ("best_bid", "best_ask", "spread", "bids", "asks")}
            steps.append({"step": t["step"], "time": t["time"], "books": books,
                          "prices_usdt": tracker.prices(), "stale_prices": tracker.stale})
            engine.step()
    return steps


def run_steps(entries: list[dict[str, Any]], total_steps: int) -> tuple[list[dict[str, Any]], list[float]]:
    """Groups a journal by time step, and computes PnL vs holding after each step."""
    steps: list[dict[str, Any]] = [{"step": i, "events": [], "reads": 0} for i in range(total_steps)]
    snapshots = [e for e in entries if e["event"] == "snapshot"]
    start = snapshots[0]
    pnl = [0.0]
    current = 0
    for e in entries:
        if e["event"] == "snapshot":
            if e["reason"] == "advance_time":
                prices = e["prices_usdt"]
                pnl.append(mark_to_market(e["balances"], prices) - mark_to_market(start["balances"], prices))
            continue
        if current >= total_steps:
            continue
        bucket = steps[current]
        if e["tool"] in READS and e["status"] == "ok":
            bucket["reads"] += 1
        else:
            bucket["events"].append({k: e.get(k) for k in ("tool", "args", "status", "result", "error")})
        if e["tool"] == "advance_time" and e["status"] == "ok":
            current += 1
    return steps, pnl


def _text_of(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(c.get("text", "") for c in content if isinstance(c, dict))
    return ""


def _clip(s: str) -> str:
    s = s.strip()
    return s if len(s) <= TEXT_LIMIT else s[:TEXT_LIMIT] + " …"


def agent_handoffs(transcript: Path, total_steps: int) -> list[list[dict[str, Any]]]:
    """Per step: the orchestrator's notes and each hand-off to a sub-agent with its reply.

    The transcript holds the SDK's messages as JSON. Tool-use blocks are
    recognised by their keys: a block with name/input is a tool call, one with
    tool_use_id is its result.
    """
    per_step: list[list[dict[str, Any]]] = [[] for _ in range(total_steps)]
    pending: dict[str, dict[str, Any]] = {}
    step = 0
    for line in transcript.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        msg = json.loads(line)
        if msg.get("parent_tool_use_id"):  # inside a sub-agent: its own tool calls aren't shown here
            continue
        for block in msg.get("content") or []:
            if not isinstance(block, dict):
                continue
            here = per_step[min(step, total_steps - 1)]
            if msg["type"] == "AssistantMessage" and "name" in block and "input" in block:
                if block["name"] in ("Agent", "Task"):  # the delegation tool was called Task in older CLIs
                    item = {"kind": "handoff", "agent": block["input"].get("subagent_type", "?"),
                            "task": _clip(block["input"].get("prompt", "")), "reply": None, "error": False}
                    pending[block["id"]] = item
                    here.append(item)
                elif block["name"] == ADVANCE:
                    here.append({"kind": "advance"})
                    pending[block["id"]] = {"kind": "advance_result"}
            elif msg["type"] == "AssistantMessage" and "text" in block and block["text"].strip():
                here.append({"kind": "note", "text": _clip(block["text"])})
            elif msg["type"] == "UserMessage" and "tool_use_id" in block:
                item = pending.pop(block["tool_use_id"], None)
                if item is None:
                    continue
                if item["kind"] == "handoff":
                    item["reply"] = _clip(_text_of(block.get("content")))
                    item["error"] = bool(block.get("is_error"))
                elif item["kind"] == "advance_result" and not block.get("is_error"):
                    step += 1
    return per_step


def build(evals_dir: Path, out: Path, agent_dir: Path | None = None, data: Path = DEFAULT_DATA) -> dict[str, Any]:
    market = market_timeline(data)
    total = len(market)
    report = json.loads((evals_dir / "report.json").read_text(encoding="utf-8"))
    scores = [(s, evals_dir) for s in report["scores"] if not s["name"].startswith("agent:")]
    if agent_dir and (agent_dir / "report.json").exists():
        agent_report = json.loads((agent_dir / "report.json").read_text(encoding="utf-8"))
        scores += [(s, agent_dir) for s in agent_report["scores"] if s["name"].startswith("agent:")]

    runs = []
    for score, where in scores:
        is_agent = score["name"].startswith("agent:")
        stem = f"{score['scenario']}.{'agent' if is_agent else score['name']}"
        journal = where / f"{stem}.jsonl"
        steps, pnl = run_steps(read_journal(journal), total)
        run = {"name": score["name"], "agent": is_agent, "score": score, "steps": steps, "pnl": pnl}
        transcript = where / f"{stem}.transcript.jsonl"
        if is_agent and transcript.exists():
            for bucket, items in zip(steps, agent_handoffs(transcript, total)):
                bucket["handoffs"] = items
        runs.append(run)

    site = {"generated": time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime()), "scenario": data.stem,
            "playground_url": os.environ.get("PLAYGROUND_URL") or None, "market": market, "runs": runs}
    (out / "data").mkdir(parents=True, exist_ok=True)
    (out / "data" / "site.json").write_text(json.dumps(site, separators=(",", ":")), encoding="utf-8")
    page = resources.files("trading_desk").joinpath("static/dashboard.html")
    with resources.as_file(page) as src:
        shutil.copyfile(src, out / "index.html")
    (out / ".nojekyll").write_text("")
    return site


def main() -> None:
    parser = argparse.ArgumentParser(description="Build the replay dashboard.")
    parser.add_argument("--evals", type=Path, required=True, help="output folder of trading_desk.evals (baselines)")
    parser.add_argument("--agent", type=Path, help="output folder of an agent eval run, if any")
    parser.add_argument("--out", type=Path, default=Path("_site"))
    args = parser.parse_args()
    site = build(args.evals, args.out, args.agent)
    print(f"Built {args.out / 'index.html'} with {len(site['runs'])} runs over {len(site['market'])} steps")


if __name__ == "__main__":
    main()
