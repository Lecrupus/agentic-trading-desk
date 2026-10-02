# The live playground + MCP-over-HTTP endpoint (trading_desk.web).
# Build from the repo root:  docker build -f deploy/playground/Dockerfile -t trading-desk-playground .
# Run:                       docker run -p 7860:7860 trading-desk-playground

FROM gcc:14 AS engine
WORKDIR /src
COPY Makefile ./
COPY engine/ engine/
RUN make engine build/test_exchange LDFLAGS="-static-libstdc++ -static-libgcc" \
 && ./build/test_exchange

FROM python:3.12-slim
COPY --from=ghcr.io/astral-sh/uv:0.11 /uv /bin/
WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy PYTHONUNBUFFERED=1 PORT=7860
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-dev --no-install-project
COPY src/ src/
COPY engine/data/ engine/data/
COPY --from=engine /src/build/engine_server build/engine_server
RUN uv sync --frozen --no-dev

# Run unprivileged; everything above is world-readable.
USER 1000
EXPOSE 7860
HEALTHCHECK CMD python -c "import urllib.request,os; urllib.request.urlopen(f'http://localhost:{os.environ[\"PORT\"]}/healthz')"
CMD ["/app/.venv/bin/python", "-m", "trading_desk.web"]
