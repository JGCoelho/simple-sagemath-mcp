"""One MCP client for the tests in this folder.

The kernel starts on the first call and stays for the whole file. Each test file
pays the kernel start time only one time.

`Server` starts a second process with its own argv (for a flag the shared client
does not pass, such as `--directory`). It has its own kernel and namespace.

A test that starts a job must reach the job's end on every path, pass or fail:
use `settle(job_id)` in a `finally`. A job left holding the kernel makes every
later call `kernel_busy`.

Do not import this module from server.py.
"""

import asyncio
import atexit
import concurrent.futures
import contextlib
import dataclasses
import threading
import time
from pathlib import Path

from fastmcp import Client
from fastmcp.client.transports import PythonStdioTransport

SERVER = str(Path(__file__).resolve().parent.parent / "server.py")

_loop = asyncio.new_event_loop()
threading.Thread(target=_loop.run_forever, daemon=True).start()
_client = None
_stack = None
_servers: list["Server"] = []


def _start() -> None:
    global _client, _stack
    _stack = contextlib.AsyncExitStack()
    _client = asyncio.run_coroutine_threadsafe(
        _stack.enter_async_context(Client(PythonStdioTransport(SERVER))),
        _loop).result(120)


class Server:
    """A second server process with its own argv, its own kernel and namespace.

    Call `close()` when done; the module closes any left open at exit.
    """

    def __init__(self, *args: str):
        self._stack = contextlib.AsyncExitStack()
        transport = PythonStdioTransport(SERVER, args=list(args))
        self._client = asyncio.run_coroutine_threadsafe(
            self._stack.enter_async_context(Client(transport)), _loop).result(120)
        _servers.append(self)

    def call(self, name: str, **args) -> dict:
        future = asyncio.run_coroutine_threadsafe(
            self._client.call_tool(name, args), _loop)
        return unwrap(future.result(300))

    def tool_names(self) -> list[str]:
        future = asyncio.run_coroutine_threadsafe(self._client.list_tools(), _loop)
        return [tool.name for tool in future.result(120)]

    def close(self) -> None:
        if self in _servers:
            _servers.remove(self)
        asyncio.run_coroutine_threadsafe(self._stack.aclose(), _loop).result(60)


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


def tool_names() -> list[str]:
    """The names of the shared client's tools, in tools/list order."""
    if _client is None:
        _start()
    future = asyncio.run_coroutine_threadsafe(_client.list_tools(), _loop)
    return [tool.name for tool in future.result(120)]


def call_async(name: str, **args) -> concurrent.futures.Future:
    """Start a tool call and return its future, without waiting for it.

    Use it to keep a computation running while another call (interrupt,
    restart) is made. The future resolves to the tool's dict.
    """
    if _client is None:
        _start()
    return asyncio.run_coroutine_threadsafe(_client.call_tool(name, args), _loop)


def settle_call(fut: concurrent.futures.Future, limit: float = 120.0) -> dict:
    """Make sure a call started with `call_async` has returned; return its dict.

    For a `finally` block, when an assertion has failed. The call returns when its
    job ends, so an interrupt is sent until it does, as `settle` does for a job id.
    Do not use it on the success path.
    """
    deadline = time.monotonic() + limit
    while not fut.done():
        if time.monotonic() > deadline:
            raise TimeoutError(f"the call is still running after {limit:g} s")
        call("interrupt")
        time.sleep(0.2)
    return unwrap(fut.result())


def call(name: str, **args):
    """Call a tool, wait for it, and return the dict of the tool."""
    return unwrap(call_async(name, **args).result(300))


def call_raw(name: str, **args) -> dict:
    """Call a tool and return the payload exactly as the server sent it.

    `unwrap` goes through the client's generated type, which fills every absent
    optional field with None. This returns the wire dict, so a test can check
    which keys the server actually sent.
    """
    return call_async(name, **args).result(300).structured_content


def wait_job(job_id: str, limit: float = 120.0) -> dict:
    """Poll `job_status` until the job is no longer running; return that reply."""
    deadline = time.monotonic() + limit
    while True:
        reply = call("job_status", job_id=job_id)
        if reply.get("status") != "running":
            return reply
        if time.monotonic() > deadline:
            raise TimeoutError(f"job {job_id} still running after {limit:g} s")
        time.sleep(0.2)


def settle(job_id: str, limit: float = 120.0) -> dict:
    """Make sure a job has ended, for a `finally` block; return its last reply.

    A running job is interrupted, and the interrupt is sent again until the job
    ends: the job may not have reached the kernel yet, and `interrupt` does
    nothing on an idle kernel, so repeating it is safe. Safe on a finished job.
    """
    deadline = time.monotonic() + limit
    while True:
        reply = call("job_status", job_id=job_id)
        if reply.get("status") != "running":
            return reply
        if time.monotonic() > deadline:
            raise TimeoutError(f"job {job_id} still running after {limit:g} s")
        call("interrupt")
        time.sleep(0.2)


@atexit.register
def _stop() -> None:
    for server in list(_servers):
        try:
            server.close()
        except Exception:
            pass
    if _stack is not None:
        asyncio.run_coroutine_threadsafe(_stack.aclose(), _loop).result(60)
        _loop.call_soon_threadsafe(_loop.stop)
