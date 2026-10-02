# Build plan

Each phase ends with something that runs and is tested, and gets its own
chapter in [LEARNING.md](LEARNING.md).

## Phase 1 — Make the C++ simulator drivable ✅

The original simulator was a menu loop for a human. An agent needs a program it
can call.

- [x] Extract `OrderBookEntry`, `CSVReader`, `Wallet`, `OrderBook` into `engine/include/exchange.hpp`
- [x] New `Exchange` class: owns book + wallet + clock; reserves funds for open orders; orders live one time step
- [x] `engine_server`: line protocol (one text command in → one JSON line out)
- [x] 34 C++ unit tests (`engine/tests/test_exchange.cpp`)

## Phase 2 — Expose the engine over MCP ✅

- [x] `trading_desk.engine.Engine`: owns the C++ process, validates arguments, turns replies into dicts
- [x] `trading_desk.mcp_server`: 9 MCP tools (book, wallet, place/cancel, advance time, …)
- [x] `.mcp.json` so Claude Code picks the server up automatically
- [x] Python tests: engine bridge + an in-process MCP client

## Phase 3 — Risk limits as code

The guarantee "every trade is checked against position limits before it reaches
the exchange" must not depend on a model remembering to check.

- [ ] `trading_desk.risk`: max order notional, max position per currency, max open orders (valued in USDT)
- [ ] Enforce inside `place_order`, so *every* MCP client goes through it
- [ ] Count blocked orders (these become the "risk-limit breaches" metric)
- [ ] `get_risk_limits` / `check_order` tools so agents can pre-check

## Phase 4 — The agents (Claude Agent SDK)

- [ ] Orchestrator agent: runs the trading loop one time step at a time
- [ ] Analyst sub-agent: read-only tools; returns a structured trade idea
- [ ] Risk sub-agent: reviews a proposal against limits and the wallet; approve / resize / reject
- [ ] Execution sub-agent: the only one allowed to call `place_order`
- [ ] Tool permissions per agent (`allowed_tools`), so the analyst *cannot* trade

## Phase 5 — Agent Skills

- [ ] `.claude/skills/read-order-book` — how to read depth, spread and mid price
- [ ] `.claude/skills/place-safe-order` — the pre-trade checklist
- [ ] Usable from any Claude Code session in this repo, not only our agents

## Phase 6 — Evaluation harness

- [ ] Replay the snapshot from step 0 to close, one run per agent configuration
- [ ] Score: PnL (marked to market in USDT), risk-limit breaches, failed tool calls
- [ ] Deterministic baseline strategies (do-nothing, scripted) so CI runs without an API key
- [ ] JSON + Markdown report per run

## Phase 7 — Docker and CI

- [ ] `Dockerfile`: build the engine, install Python deps, run tests
- [ ] GitHub Actions on Linux: C++ tests, Python tests, baseline evals
- [ ] Optional agent eval job when an `ANTHROPIC_API_KEY` secret is set
