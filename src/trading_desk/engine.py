"""Talks to the C++ engine (build/engine_server) over its stdin/stdout.

The engine speaks a line protocol: we write one command, it writes back one
JSON object. This module owns that process and turns the protocol into
ordinary Python method calls. Nothing above this layer knows C++ exists.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import threading
from pathlib import Path
from typing import Any, Literal

REPO_ROOT = Path(__file__).resolve().parents[2]
_EXE = ".exe" if sys.platform == "win32" else ""
DEFAULT_BINARY = REPO_ROOT / "build" / f"engine_server{_EXE}"
DEFAULT_DATA = REPO_ROOT / "engine" / "data" / "20200317.csv"

# Arguments end up inside a text command, so anything with a space or newline
# could smuggle in a second command. Only allow the shapes we expect.
_PRODUCT = re.compile(r"^[A-Z0-9]{2,10}/[A-Z0-9]{2,10}$")

Side = Literal["bid", "ask"]


class EngineError(RuntimeError):
    """The engine rejected a command (bad input, insufficient funds, ...)."""


class Engine:
    def __init__(self, binary: str | Path | None = None, data: str | Path | None = None):
        self.binary = Path(binary or os.environ.get("TRADING_ENGINE_BIN", DEFAULT_BINARY))
        self.data = Path(data or os.environ.get("TRADING_ENGINE_DATA", DEFAULT_DATA))
        self._proc: subprocess.Popen[str] | None = None
        self._lock = threading.Lock()
        self.ready: dict[str, Any] = {}

    # ---- process lifecycle -------------------------------------------------

    def start(self) -> "Engine":
        if self._proc is not None:
            return self
        if not self.binary.exists():
            raise FileNotFoundError(
                f"engine binary not found at {self.binary}. Build it first: see README (make engine)."
            )
        self._proc = subprocess.Popen(
            [str(self.binary), str(self.data)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            bufsize=1,  # line-buffered: each command goes out as soon as we write it
        )
        self.ready = self._read()
        if not self.ready.get("ok"):
            self.close()
            raise EngineError(self.ready.get("error", "engine failed to start"))
        return self

    def close(self) -> None:
        proc, self._proc = self._proc, None
        if proc is None:
            return
        try:
            if proc.stdin:
                proc.stdin.write("quit\n")
                proc.stdin.flush()
            proc.wait(timeout=5)
        except (OSError, subprocess.TimeoutExpired):
            proc.kill()

    def __enter__(self) -> "Engine":
        return self.start()

    def __exit__(self, *exc: object) -> None:
        self.close()

    # ---- protocol ----------------------------------------------------------

    def _read(self) -> dict[str, Any]:
        assert self._proc and self._proc.stdout
        line = self._proc.stdout.readline()
        if not line:
            err = self._proc.stderr.read() if self._proc.stderr else ""
            raise EngineError(f"engine exited unexpectedly. {err}".strip())
        return json.loads(line)

    def send(self, command: str) -> dict[str, Any]:
        """Sends one raw command and returns the engine's reply, raising on errors."""
        if "\n" in command or "\r" in command:
            raise ValueError("a command must be a single line")
        self.start()
        with self._lock:  # one request in flight at a time, or replies get mixed up
            assert self._proc and self._proc.stdin
            self._proc.stdin.write(command + "\n")
            self._proc.stdin.flush()
            reply = self._read()
        if not reply.get("ok"):
            raise EngineError(reply.get("error", "unknown engine error"))
        reply.pop("ok", None)
        return reply

    # ---- typed commands ----------------------------------------------------

    def products(self) -> list[str]:
        return self.send("products")["products"]

    def time(self) -> dict[str, Any]:
        return self.send("time")

    def book(self, product: str, depth: int = 10) -> dict[str, Any]:
        return self.send(f"book {_check_product(product)} {int(depth)}")

    def wallet(self) -> dict[str, Any]:
        return self.send("wallet")

    def place(self, side: Side, product: str, price: float, amount: float) -> dict[str, Any]:
        if side not in ("bid", "ask"):
            raise ValueError("side must be 'bid' or 'ask'")
        return self.send(f"place {side} {_check_product(product)} {float(price)!r} {float(amount)!r}")

    def orders(self) -> list[dict[str, Any]]:
        return self.send("orders")["orders"]

    def cancel(self, order_id: int) -> dict[str, Any]:
        return self.send(f"cancel {int(order_id)}")

    def step(self) -> dict[str, Any]:
        return self.send("step")

    def reset(self) -> dict[str, Any]:
        return self.send("reset")


def _check_product(product: str) -> str:
    if not _PRODUCT.match(product):
        raise ValueError(f"not a product symbol: {product!r} (expected e.g. ETH/USDT)")
    return product
