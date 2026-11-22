import asyncio
import socket
import threading
import time
from collections.abc import Sequence
from contextlib import ExitStack, contextmanager

import pytest
import uvicorn

from cic_mcp_lab.mcpwire import TASKS_EXTENSION, WireClient
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


def wait_for_terminal(client: WireClient, task_id: str, timeout: float = 5.0) -> dict:
    deadline = time.monotonic() + timeout
    while True:
        task = client.get_task(task_id).result
        if task["status"] in {"completed", "failed", "cancelled"}:
            return task
        assert time.monotonic() < deadline, f"task still {task['status']}"
        time.sleep(task["pollIntervalMs"] / 1000)


def test_discover_advertises_the_tasks_extension(start_server):
    url, _ = start_server()
    with WireClient(url, KEY) as client:
        capabilities = client.request("server/discover").result["capabilities"]

    assert TASKS_EXTENSION in capabilities["extensions"]


def test_task_is_returned_immediately_and_its_result_is_pushed(start_server):
    url, _ = start_server(poll_interval_ms=100)
    with WireClient(url, KEY) as client:
        started = time.monotonic()
        created = client.call_tool("trip_planner", {"message": "sleep=1;hello"}).result
        assert time.monotonic() - started < 0.5
        assert (created["resultType"], created["status"]) == ("task", "working")
        task_id = created["taskId"]

        with client.listen_tasks([task_id, "unknown-task"]) as messages:
            ack = next(messages)
            assert ack["method"] == "notifications/subscriptions/acknowledged"
            assert ack["params"]["notifications"]["taskIds"] == [task_id]
            deadline = time.monotonic() + 10
            pushed = None
            for message in messages:
                if message.get("method") == "notifications/tasks" and message["params"]["status"] == "completed":
                    pushed = message["params"]
                    break
                assert time.monotonic() < deadline, "no completed push within 10 s"
            assert pushed is not None
        polled = client.get_task(task_id).result

    assert pushed["result"]["_meta"]["com.cic/credit_usage"] == 42
    assert pushed["result"]["structuredContent"]["reply"] == "echo: hello"
    assert polled["status"] == "completed"
    assert polled["result"] == pushed["result"]


def test_server_without_push_acknowledges_no_tasks_but_polling_works(start_server):
    url, _ = start_server(push_enabled=False, poll_interval_ms=100)
    with WireClient(url, KEY) as client:
        task_id = client.call_tool("trip_planner", {"message": "hello"}).result["taskId"]
        with client.listen_tasks([task_id]) as messages:
            assert next(messages)["params"]["notifications"].get("taskIds", []) == []
        task = wait_for_terminal(client, task_id)

    assert task["result"]["_meta"]["com.cic/credit_usage"] == 42


def test_cancel_stops_the_running_agent(start_server):
    url, runtime = start_server()
    with WireClient(url, KEY) as client:
        task_id = client.call_tool("trip_planner", {"message": "sleep=30;long"}).result["taskId"]
        assert client.cancel_task(task_id).error is None
        assert client.get_task(task_id).result["status"] == "cancelled"
        assert client.cancel_task(task_id).error is None  # cancelling a finished task is acknowledged
        assert client.cancel_task("unknown-task").error["code"] == -32602

    assert runtime.cancelled.wait(timeout=5)


def test_task_requests_must_name_the_task_in_the_mcp_name_header(start_server):
    url, _ = start_server()
    with WireClient(url, KEY) as client:
        task_id = client.call_tool("trip_planner", {"message": "hello"}).result["taskId"]
        resp = client.request("tasks/get", {"taskId": task_id})

    assert resp.error["code"] == -32020


def test_task_methods_require_the_client_to_declare_the_extension(start_server):
    url, _ = start_server()
    with WireClient(url, KEY) as client:
        task_id = client.call_tool("trip_planner", {"message": "hello"}).result["taskId"]
        responses = [
            client.request("tasks/get", {"taskId": task_id}, tasks=False, name=task_id),
            client.request("tasks/cancel", {"taskId": task_id}, tasks=False, name=task_id),
            client.request("subscriptions/listen", {"notifications": {"taskIds": [task_id]}}, tasks=False),
        ]

    for resp in responses:
        assert resp.error["code"] == -32021
        assert resp.error["data"]["requiredCapabilities"]["extensions"] == {TASKS_EXTENSION: {}}


def test_listen_delivers_every_already_finished_task(start_server):
    url, _ = start_server(poll_interval_ms=50)
    with WireClient(url, KEY) as client:
        task_ids = [client.call_tool("trip_planner", {"message": f"m{i}"}).result["taskId"] for i in range(300)]
        for task_id in task_ids:
            wait_for_terminal(client, task_id)
        with client.listen_tasks(task_ids) as messages:
            assert len(next(messages)["params"]["notifications"]["taskIds"]) == 300
            pushed = {next(messages)["params"]["taskId"] for _ in range(300)}

    assert pushed == set(task_ids)


def test_agent_errors_complete_the_task_with_is_error(start_server):
    url, _ = start_server(poll_interval_ms=100)
    with WireClient(url, KEY) as client:
        task_id = client.call_tool(
            "trip_planner", {"message": "unknown-conversation", "conversation_id": "conv_x"}
        ).result["taskId"]
        task = wait_for_terminal(client, task_id)

    assert task["status"] == "completed"
    assert task["result"]["isError"] is True
    assert "conv_x" in task["result"]["content"][0]["text"]


def test_tasks_are_forgotten_after_their_ttl(start_server):
    url, _ = start_server(ttl_ms=300)
    with WireClient(url, KEY) as client:
        task_id = client.call_tool("trip_planner", {"message": "hello"}).result["taskId"]
        time.sleep(0.5)
        resp = client.get_task(task_id)

    assert resp.error["code"] == -32602
