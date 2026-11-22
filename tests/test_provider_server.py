import asyncio
import socket
import threading
import time
from collections.abc import Sequence
from contextlib import ExitStack, contextmanager

import pytest
import uvicorn

from cic_mcp_lab.mcpwire import WireClient
from cic_mcp_lab.provider.agent import AgentResult, UnknownConversationError
from cic_mcp_lab.provider.server import build_app

KEY = "sk-test-provider-a"


class FakeRuntime:
    """Echo agent. 'sleep=N;text' waits N seconds; 'unknown-conversation' raises a caller error."""

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
        return AgentResult(conversation_id or "conv_test", "input_required", f"echo: {message}", 42)


@contextmanager
def running_server(app):
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
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


@pytest.fixture
def start_server():
    with ExitStack() as stack:

        def start(**overrides):
            runtime = FakeRuntime()
            app = build_app(provider_id="provider-a", api_key=KEY, runtime=runtime, **overrides)
            return stack.enter_context(running_server(app)), runtime

        yield start


def test_requests_without_a_valid_key_are_rejected(start_server):
    url, _ = start_server()
    for key in (None, "sk-wrong"):
        with WireClient(url, key) as client:
            resp = client.request("server/discover")
        assert resp.status_code == 401
        assert resp.headers["www-authenticate"].startswith("Bearer")


def test_each_provider_agent_is_one_tool_following_the_entry_contract(start_server):
    url, _ = start_server()
    with WireClient(url, KEY) as client:
        tools = {tool["name"]: tool["inputSchema"] for tool in client.request("tools/list").result["tools"]}

    assert set(tools) == {"trip_planner", "product_copy"}
    for schema in tools.values():
        assert schema["required"] == ["message"]
        assert "conversation_id" in schema["properties"]
    assert "image_urls" in tools["product_copy"]["properties"]
    assert "image_urls" not in tools["trip_planner"]["properties"]


def test_without_tasks_capability_the_result_is_returned_directly(start_server):
    url, _ = start_server()
    with WireClient(url, KEY) as client:
        result = client.call_tool("trip_planner", {"message": "hello"}, tasks=False).result

    assert result["resultType"] == "complete"
    assert result["structuredContent"] == {
        "conversation_id": "conv_test",
        "status": "input_required",
        "reply": "echo: hello",
    }
    assert result["content"][0]["text"] == "echo: hello"
    assert result["_meta"]["com.cic/credit_usage"] == 42


def test_caller_errors_are_reported_with_their_message(start_server):
    url, _ = start_server()
    with WireClient(url, KEY) as client:
        result = client.call_tool(
            "trip_planner", {"message": "unknown-conversation", "conversation_id": "conv_x"}, tasks=False
        ).result

    assert result["isError"] is True
    assert "conv_x" in result["content"][0]["text"]
