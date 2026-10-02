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

## Phase 3 — Risk limits as code ✅

The guarantee "every trade is checked against position limits before it reaches
the exchange" must not depend on a model remembering to check.

- [x] `trading_desk.risk`: max order notional, max position per currency, max open orders, price deviation (valued in USDT)
- [x] Enforce inside `place_order`, so *every* MCP client goes through it
- [x] Count blocked orders (these become the "risk-limit breaches" metric)
- [x] `get_risk_limits` / `check_order` tools so agents can pre-check
- [x] Journal (JSONL) of every tool call and wallet snapshot, for scoring

## Phase 4 — The agents (Claude Agent SDK) ✅

- [x] Orchestrator agent: runs the trading loop one time step at a time
- [x] Analyst sub-agent: read-only tools; returns a structured trade idea
- [x] Risk sub-agent: reviews a proposal against limits and the wallet; approve / resize / reject
- [x] Execution sub-agent: the only one allowed to call `place_order`
- [x] Tool permissions per agent (`AgentDefinition.tools`), so the analyst *cannot* trade
- [x] `PreToolUse` hook enforcing who may place orders and advance time

## Phase 5 — Agent Skills ✅

- [x] `.claude/skills/read-order-book` — how to read depth, spread and mid price
- [x] `.claude/skills/place-safe-order` — the pre-trade checklist
- [x] `.claude/skills/trading-session` — running and scoring a session by hand
- [x] Usable from any Claude Code session in this repo, not only our agents

## Phase 6 — Evaluation harness ✅

- [x] Replay the snapshot from step 0 to close, one run per agent configuration
- [x] Score: PnL (marked to market in USDT), risk-limit breaches, failed tool calls
- [x] Deterministic baseline strategies (do-nothing, scripted) so CI runs without an API key
- [x] JSON + Markdown report per run
- [x] `--check` invariants with a non-zero exit code for CI

## Phase 7 — Docker and CI ✅

- [x] `Dockerfile`: build stage compiles the engine and runs the C++ tests; runtime stage runs the evals
- [x] GitHub Actions on Linux: C++ tests, Python tests, baseline evals, Docker build + run
- [x] Agent eval job, run on demand, using the `ANTHROPIC_API_KEY` secret

## Phase 8 — Show it working ✅

- [x] `DeskService`: one code path (validation, risk gate, journal) shared by MCP tools and the web API
- [x] Live playground (`trading_desk.web`): trade by hand, per-browser exchanges, MCP over Streamable HTTP at `/mcp`
- [x] Replay dashboard (`trading_desk.site`): scoreboard, PnL chart, step-by-step replay incl. agent hand-offs
- [x] GitHub Pages deploy after every green CI run; CI smoke-tests the playground container
- [x] Render blueprint + "Deploy to Render" button for the playground (Hugging Face Docker Spaces now require PRO)
