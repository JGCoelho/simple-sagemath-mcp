"""One MCP client for the tests in this folder.

The kernel starts on the first call and stays for the whole file. Each test file
pays the kernel start time only one time.

Do not import this module from server.py.
"""

import asyncio
import atexit
import contextlib
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
        _stack.enter_async_context(Client(SERVER)), _loop).result(120)


def call(name: str, **args):
    """Call a tool. Raise on error. Return the dict of the tool."""
    if _client is None:
        _start()
    fut = asyncio.run_coroutine_threadsafe(_client.call_tool(name, args), _loop)
    return fut.result(300).data


def call_error(name: str, **args) -> str | None:
    """Call a tool that must fail. Return the error text, or None if it did not fail."""
    try:
        call(name, **args)
    except Exception as e:
        return str(e)
    return None


@atexit.register
def _stop() -> None:
    if _stack is not None:
        asyncio.run_coroutine_threadsafe(_stack.aclose(), _loop).result(60)
        _loop.call_soon_threadsafe(_loop.stop)
