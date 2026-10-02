"""The live playground: a web UI and an MCP endpoint over HTTP, in one server.

    GET  /            the playground page (trade by hand, watch the risk gate)
    /api/...          JSON API behind the page; each browser gets its own exchange
    /mcp              the MCP server over Streamable HTTP, for Claude and other clients
                      (one shared sandbox market; anyone connected sees the same state)

Every action goes through DeskService, the same code the stdio MCP server and
the agents use, so the playground shows the real risk gate, not a copy of it.

Run locally:  uv run python -m trading_desk.web      then open http://localhost:7860
Environment:
  PORT            port to listen on (default 7860; hosts such as Render set it)
  PUBLIC_HOSTS    comma-separated host names to accept on /mcp (DNS-rebinding
                  protection), e.g. "agentic-trading-desk.onrender.com". On Render,
                  RENDER_EXTERNAL_HOSTNAME is used when this is not set.
  MAX_SESSIONS    playground exchanges kept alive at once (default 25)
"""

from __future__ import annotations

import os
import secrets
import threading
import time
from dataclasses import asdict
from importlib import resources
from typing import Any

from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, Response

from trading_desk.engine import Engine
from trading_desk.market import mark_to_market
from trading_desk.mcp_server import DeskError, DeskService, Journal, build_server_for

BOOK_DEPTH = 8
LOG_LIMIT = 200


class Session:
    def __init__(self) -> None:
        self.engine = Engine().start()
        self.desk = DeskService(self.engine, journal=Journal(keep=True))
        self.last_used = time.monotonic()

    def close(self) -> None:
        self.engine.close()


class SessionPool:
    """One exchange (one C++ process) per browser, oldest evicted first."""

    def __init__(self, max_sessions: int = 25, idle_seconds: float = 3600):
        self.max_sessions = max_sessions
        self.idle_seconds = idle_seconds
        self.sessions: dict[str, Session] = {}
        self.lock = threading.Lock()

    def get(self, sid: str | None) -> tuple[str, Session]:
        with self.lock:
            now = time.monotonic()
            for old in [k for k, s in self.sessions.items() if now - s.last_used > self.idle_seconds]:
                self.sessions.pop(old).close()
            if sid and sid in self.sessions:
                session = self.sessions[sid]
            else:
                if len(self.sessions) >= self.max_sessions:
                    oldest = min(self.sessions, key=lambda k: self.sessions[k].last_used)
                    self.sessions.pop(oldest).close()
                sid = secrets.token_urlsafe(12)
                session = self.sessions[sid] = Session()
            session.last_used = now
            return sid, session

    def close_all(self) -> None:
        with self.lock:
            for s in self.sessions.values():
                s.close()
            self.sessions.clear()


def state(desk: DeskService) -> dict[str, Any]:
    """Everything the page draws. Reads the engine directly, so it isn't journalled as activity."""
    engine = desk.engine
    prices = desk.tracker.prices()
    wallet = engine.wallet()
    entries = desk.journal.entries or []
    start_balances = wallet["balances"]
    for e in reversed(entries):  # the most recent start or reset is the baseline
        if e["event"] == "snapshot" and e["reason"] in ("start", "reset_market"):
            start_balances = e["balances"]
            break
    value = mark_to_market(wallet["balances"], prices)
    hold_value = mark_to_market(start_balances, prices)
    activity = [e for e in entries if e["event"] == "tool"][-LOG_LIMIT:]
    return {
        "time": engine.time(),
        "books": {p: engine.book(p, BOOK_DEPTH) for p in engine.products()},
        "wallet": wallet,
        "prices_usdt": prices,
        "stale_prices": desk.tracker.stale,
        "value_usdt": value,
        "pnl_vs_hold_usdt": value - hold_value,
        "orders": engine.orders(),
        "limits": {**asdict(desk.gate.limits), "orders_blocked": len(desk.gate.breaches)},
        "activity": activity,
    }


def create_app(pool: SessionPool | None = None, shared_desk: DeskService | None = None):
    pool = pool or SessionPool(max_sessions=int(os.environ.get("MAX_SESSIONS", "25")))
    shared_desk = shared_desk or DeskService(Engine().start())
    mcp = build_server_for(shared_desk)
    page = resources.files("trading_desk").joinpath("static/playground.html").read_text(encoding="utf-8")

    def respond(request: Request, sid: str, session: Session, extra: dict[str, Any] | None = None,
                status: int = 200) -> Response:
        body = {"session": sid, "state": state(session.desk), **(extra or {})}
        return JSONResponse(body, status_code=status)

    async def body_of(request: Request) -> dict[str, Any]:
        try:
            data = await request.json()
        except ValueError:
            return {}
        return data if isinstance(data, dict) else {}

    def action(fn):
        async def handler(request: Request) -> Response:
            sid, session = pool.get(request.headers.get("x-desk-session"))
            data = await body_of(request)
            try:
                result = fn(session.desk, data)
            except DeskError as e:
                return respond(request, sid, session, {"error": str(e), "kind": e.kind}, status=400)
            except (KeyError, TypeError, ValueError) as e:
                return respond(request, sid, session, {"error": f"bad request: {e}", "kind": "error"}, status=400)
            return respond(request, sid, session, {"result": result})
        return handler

    def order_args(data: dict[str, Any]) -> tuple[str, str, float, float]:
        return str(data["side"]), str(data["product"]), float(data["price"]), float(data["amount"])

    @mcp.custom_route("/", methods=["GET"])
    async def index(request: Request) -> Response:
        return HTMLResponse(page)

    @mcp.custom_route("/healthz", methods=["GET"])
    async def health(request: Request) -> Response:
        return JSONResponse({"ok": True, "sessions": len(pool.sessions)})

    mcp.custom_route("/api/state", methods=["GET"])(action(lambda desk, d: None))
    mcp.custom_route("/api/check", methods=["POST"])(action(lambda desk, d: desk.check(*order_args(d))))
    mcp.custom_route("/api/order", methods=["POST"])(action(lambda desk, d: desk.place(*order_args(d))))
    mcp.custom_route("/api/cancel", methods=["POST"])(action(lambda desk, d: desk.cancel(int(d["order_id"]))))
    mcp.custom_route("/api/step", methods=["POST"])(action(lambda desk, d: desk.advance()))
    mcp.custom_route("/api/reset", methods=["POST"])(action(lambda desk, d: desk.reset()))

    raw_hosts = os.environ.get("PUBLIC_HOSTS") or os.environ.get("RENDER_EXTERNAL_HOSTNAME", "")
    hosts = [h.strip() for h in raw_hosts.split(",") if h.strip()]
    if hosts:
        from mcp.server.transport_security import TransportSecuritySettings
        security = TransportSecuritySettings(
            enable_dns_rebinding_protection=True,
            allowed_hosts=hosts + [f"{h}:*" for h in hosts] + ["localhost:*", "127.0.0.1:*"],
            allowed_origins=[f"https://{h}" for h in hosts] + ["http://localhost:*", "http://127.0.0.1:*"],
        )
        return mcp.streamable_http_app(stateless_http=True, json_response=True, transport_security=security,
                                       host="0.0.0.0")
    return mcp.streamable_http_app(stateless_http=True, json_response=True, host="0.0.0.0")


def main() -> None:
    import uvicorn

    uvicorn.run(create_app(), host="0.0.0.0", port=int(os.environ.get("PORT", "7860")), log_level="info")


if __name__ == "__main__":
    main()
