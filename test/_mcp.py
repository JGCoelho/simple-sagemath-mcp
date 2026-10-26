"""One MCP client for the tests in this folder.

The kernel starts on the first call and stays for the whole file. Each test file
pays the kernel start time only one time.

Do not import this module from server.py.
"""

import asyncio
import atexit
import concurrent.futures
import contextlib
import dataclasses
import threading
from pathlib import Path

from fastmcp import Client

SERVER = str(Path(__file__).resolve().parent.parent / "server.py")

_loop = asyncio.new_event_loop()
threading.Thread(target=_loop.run_forever, daemon=True).start()
_client = None
_stack = None


def _start() -> None:
    global _client, _stack
    _stack = contextlib.AsyncExitStack()
    _client = asyncio.run_coroutine_threadsafe(
        _stack.enter_async_context(Client(Path(SERVER))), _loop).result(120)


def unwrap(result) -> dict:
    """Turn a client result's structured data into a plain dict.

    FastMCP validates the tool's structured output against its output schema and
    hands back a generated dataclass; the tests assert on the plain dict.
    """
    data = result.data
    if dataclasses.is_dataclass(data):
        return dataclasses.asdict(data)
    model_dump = getattr(data, "model_dump", None)
    return model_dump() if model_dump is not None else data


def call_async(name: str, **args) -> concurrent.futures.Future:
    """Start a tool call and return its future, without waiting for it.

    Use it to keep a computation running while another call (interrupt,
    restart) is made. The future resolves to the tool's dict.
    """
    if _client is None:
        _start()
    return asyncio.run_coroutine_threadsafe(_client.call_tool(name, args), _loop)


def call(name: str, **args):
    """Call a tool, wait for it, and return the dict of the tool."""
    return unwrap(call_async(name, **args).result(300))


@atexit.register
def _stop() -> None:
    if _stack is not None:
        asyncio.run_coroutine_threadsafe(_stack.aclose(), _loop).result(60)
        _loop.call_soon_threadsafe(_loop.stop)
