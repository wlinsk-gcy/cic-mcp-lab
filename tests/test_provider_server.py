import sys
import time

import httpx
import pytest
from starlette.testclient import TestClient

from cic_mcp_lab import config
from cic_mcp_lab.mcpwire import PROTOCOL_VERSION, TASKS_EXTENSION, WireClient
from cic_mcp_lab.provider import server
from tests.helpers import TEST_API_KEY as KEY


@pytest.mark.parametrize(
    ("provider", "port", "public_ip", "expected_tools"),
    [
        ("A", 8001, None, {"trip_planner", "product_copy"}),
        ("A", 8881, "192.168.218.99", {"trip_planner", "product_copy"}),
        ("B", 8882, "192.168.218.91", {"weekly_report"}),
    ],
)
def test_cli_serves_its_catalog_with_only_provider_settings(
    monkeypatch, tmp_path, provider, port, public_ip, expected_tools
):
    monkeypatch.setattr(config, "PROJECT_ROOT", tmp_path)
    for name in ("ROUTER_MODEL", "EMBEDDING_MODEL", "OSS_BUCKET", "PROVIDER_A_API_KEY", "PROVIDER_B_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("DASHSCOPE_BASE_URL", "https://dashscope.invalid/compatible-mode/v1")
    monkeypatch.setenv("DASHSCOPE_API_KEY", "test-model-key")
    monkeypatch.setenv("PROVIDER_MODEL", "qwen4-max")
    monkeypatch.setenv(f"PROVIDER_{provider}_API_KEY", KEY)
    argv = ["provider-server", "--provider", provider, "--port", str(port)]
    if public_ip:
        argv.extend(("--public-ip", public_ip))
    if provider == "B":
        argv.append("--no-push")
    monkeypatch.setattr(sys, "argv", argv)

    def verify_http(app, *, host, port, **kwargs):
        assert host == ("0.0.0.0" if public_ip else "127.0.0.1")
        loopback_host = f"127.0.0.1:{port}"
        expected_host = f"{public_ip}:{port}" if public_ip else loopback_host
        with TestClient(app, base_url=f"http://{expected_host}") as client:

            def request(method, **headers):
                return client.post(
                    "/mcp",
                    headers={
                        "Accept": "application/json, text/event-stream",
                        "MCP-Protocol-Version": PROTOCOL_VERSION,
                        "Mcp-Method": method,
                        **headers,
                    },
                    json={
                        "jsonrpc": "2.0",
                        "id": 1,
                        "method": method,
                        "params": {
                            "_meta": {
                                "io.modelcontextprotocol/protocolVersion": PROTOCOL_VERSION,
                                "io.modelcontextprotocol/clientInfo": {"name": "test", "version": "0"},
                                "io.modelcontextprotocol/clientCapabilities": {"extensions": {TASKS_EXTENSION: {}}},
                            }
                        },
                    },
                )

            def result(response):
                assert response.status_code == 200, response.text
                return response.json()["result"]

            auth = {"Authorization": f"Bearer {KEY}"}
            assert request("tools/list").status_code == 401
            assert request("tools/list", Authorization="Bearer wrong-key").status_code == 401
            assert request("tools/list", Host=f"unexpected.invalid:{port}", **auth).status_code == 421
            wrong_host = f"{public_ip}:{port + 1}" if public_ip else f"192.168.218.99:{port}"
            assert request("tools/list", Host=wrong_host, **auth).status_code == 421
            assert request("tools/list", Origin="http://unexpected.invalid", **auth).status_code == 403
            for allowed_host in {expected_host, loopback_host}:
                tools = result(request("tools/list", Host=allowed_host, Origin=f"http://{allowed_host}", **auth))["tools"]
                assert {tool["name"] for tool in tools} == expected_tools
            capabilities = result(request("server/discover", **auth))["capabilities"]
            assert TASKS_EXTENSION in capabilities["extensions"]

    monkeypatch.setattr(server.uvicorn, "run", verify_http)
    server.main()


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

        with client.listen_tasks([task_id, "unknown-task"], timeout=10) as messages:
            ack = next(messages)
            assert ack["method"] == "notifications/subscriptions/acknowledged"
            assert ack["params"]["notifications"]["taskIds"] == [task_id]
            pushed = None
            for message in messages:
                if message.get("method") == "notifications/tasks" and message["params"]["status"] == "completed":
                    pushed = message["params"]
                    break
            assert pushed is not None, "stream ended without a completed push"
        polled = client.get_task(task_id).result

    assert pushed["result"]["_meta"]["com.cic/credit_usage"] == 42
    assert pushed["result"]["structuredContent"]["reply"] == "echo: hello"
    assert polled["status"] == "completed"
    assert polled["result"] == pushed["result"]


def test_server_without_push_acknowledges_no_tasks_but_polling_works(start_server):
    url, _ = start_server("provider-b", push_enabled=False, poll_interval_ms=100)
    with WireClient(url, KEY) as client:
        task_id = client.call_tool("weekly_report", {"message": "hello"}).result["taskId"]
        with client.listen_tasks([task_id], timeout=10) as messages:
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
        with client.listen_tasks(task_ids, timeout=20) as messages:
            assert len(next(messages)["params"]["notifications"]["taskIds"]) == 300
            pushed = {next(messages)["params"]["taskId"] for _ in range(300)}

    assert pushed == set(task_ids)


def test_listen_timeout_stops_a_stream_that_never_delivers(start_server):
    url, _ = start_server()
    with WireClient(url, KEY) as client:
        task_id = client.call_tool("trip_planner", {"message": "sleep=30;long"}).result["taskId"]
        started = time.monotonic()
        with pytest.raises((httpx.ReadError, httpx.StreamClosed)):
            with client.listen_tasks([task_id], timeout=1) as messages:
                next(messages)  # ack
                next(messages)  # no push for 30 s: the timeout must end the read
        assert time.monotonic() - started < 5
        client.cancel_task(task_id)


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


def test_server_without_tasks_extension_answers_directly(start_server):
    url, _ = start_server(tasks_enabled=False)
    with WireClient(url, KEY) as client:
        capabilities = client.request("server/discover").result["capabilities"]
        result = client.call_tool("trip_planner", {"message": "hello"}).result

    assert TASKS_EXTENSION not in (capabilities.get("extensions") or {})
    assert result["resultType"] == "complete"
    assert result["_meta"]["com.cic/credit_usage"] == 42


def test_json_rpc_errors_fail_the_task(start_server):
    url, _ = start_server(poll_interval_ms=100)
    with WireClient(url, KEY) as client:
        task_id = client.call_tool("trip_planner", {"message": "mcp-error"}).result["taskId"]
        task = wait_for_terminal(client, task_id)

    assert task["status"] == "failed"
    assert "provider backend unavailable" in task["error"]["message"]
