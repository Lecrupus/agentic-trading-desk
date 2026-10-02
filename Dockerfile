# Two stages: compile and test the C++ engine, then ship it with the Python side.

# ---- 1. engine: build and run the C++ tests (a failing test fails the build) ----
FROM gcc:14 AS engine
WORKDIR /src
COPY Makefile ./
COPY engine/ engine/
# Link libstdc++ statically so the binary runs on the slimmer Python image,
# whatever libstdc++ version that image ships.
RUN make engine build/test_exchange LDFLAGS="-static-libstdc++ -static-libgcc" \
 && ./build/test_exchange

# ---- 2. runtime: Python + MCP server + agents + evals ----
FROM python:3.12-slim
COPY --from=ghcr.io/astral-sh/uv:0.11 /uv /uvx /bin/
WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy PYTHONUNBUFFERED=1

# Dependencies first, so editing source doesn't reinstall them.
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-dev --no-install-project

COPY src/ src/
COPY engine/data/ engine/data/
COPY .claude/ .claude/
COPY --from=engine /src/build/engine_server build/engine_server
RUN uv sync --frozen --no-dev

# Default: replay the snapshot with the baseline traders and enforce the checks.
# Agent run:  docker run -e ANTHROPIC_API_KEY=... trading-desk --agent --baselines hold
ENTRYPOINT ["uv", "run", "--frozen", "--no-dev", "python", "-m", "trading_desk.evals"]
CMD ["--check"]
