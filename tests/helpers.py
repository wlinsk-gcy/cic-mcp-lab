"""Shared test helpers: a fake agent runtime and a local uvicorn server runner."""
from __future__ import annotations

import asyncio
import socket
import threading
import time
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from typing import Any

import uvicorn
from mcp.shared.exceptions import MCPError

from cic_mcp_lab.provider.agent import AgentResult, UnknownConversationError

TEST_API_KEY = "sk-test-provider-a"


class FakeRuntime:
    """Echo agent.

    'sleep=N;text' waits N seconds; 'unknown-conversation' raises a caller error;
    'mcp-error' raises a JSON-RPC error, which ends a task as failed.
    """

    def __init__(self) -> None:
        self.cancelled = threading.Event()

    async def ask(
        self,
        agent_id: str,
        message: str,
        image_urls: Sequence[str] = (),
        conversation_id: str | None = None,
    ) -> AgentResult:
        delay = 0.0
        if message.startswith("sleep="):
            head, _, message = message.partition(";")
            delay = float(head.removeprefix("sleep="))
        try:
            await asyncio.sleep(delay)
        except asyncio.CancelledError:
            self.cancelled.set()
            raise
        if message == "unknown-conversation":
            raise UnknownConversationError(f"Conversation not found or expired: {conversation_id}")
        if message == "mcp-error":
            raise MCPError(-32603, "provider backend unavailable")
        return AgentResult(conversation_id or "conv_test", "input_required", f"echo: {message}", 42)


def unused_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@contextmanager
def running_server(app: Any) -> Iterator[str]:
    port = unused_port()
    server = uvicorn.Server(
        uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning", timeout_graceful_shutdown=1)
    )
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not server.started:
        if time.monotonic() > deadline:
            raise RuntimeError("server did not start")
        time.sleep(0.05)
    try:
        yield f"http://127.0.0.1:{port}/mcp"
    finally:
        server.should_exit = True
        thread.join(timeout=10)
