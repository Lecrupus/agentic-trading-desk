import pytest

from trading_desk.engine import DEFAULT_BINARY, Engine


@pytest.fixture
def engine():
    if not DEFAULT_BINARY.exists():
        pytest.fail(f"build the engine first (make engine): {DEFAULT_BINARY} is missing")
    with Engine() as e:
        yield e


@pytest.fixture
def anyio_backend():
    # MCP is built on anyio; its pytest plugin runs the async tests.
    return "asyncio"
