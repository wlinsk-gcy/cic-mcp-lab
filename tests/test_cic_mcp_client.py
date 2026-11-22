import anyio
import pytest
from mcp.shared.exceptions import MCPError

from cic_mcp_lab.cic.contract import parse_agent_reply, validate_entry_tool
from cic_mcp_lab.cic.mcp_client import CreateTaskResult, ProviderClient, ProviderUnavailable
from tests.helpers import TEST_API_KEY as KEY


async def test_connects_and_lists_entry_tools(start_server):
    url, _ = start_server()
    async with ProviderClient(url, KEY) as client:
        tools = await client.list_tools()

        assert client.protocol_version == "2026-07-28"
        assert client.supports_tasks
    assert sorted(tool["name"] for tool in tools) == ["product_copy", "trip_planner"]
    assert all(validate_entry_tool(tool) for tool in tools)


async def test_wrong_api_key_means_provider_unavailable(start_server):
    url, _ = start_server()
    with pytest.raises(ProviderUnavailable):
        async with ProviderClient(url, "sk-wrong"):
            pass


async def test_submitted_task_result_is_pushed(start_server):
    url, _ = start_server()
    async with ProviderClient(url, KEY) as client:
        handle = await client.submit("trip_planner", {"message": "sleep=0.3;hi"})
        assert isinstance(handle, CreateTaskResult)
        async with client.listen([handle.task_id]) as subscription:
            assert subscription.accepted == {handle.task_id}
            with anyio.fail_after(10):
                async for event in subscription.events:
                    if event.status == "completed":
                        break

    assert parse_agent_reply(event.result).credit_usage == 42


async def test_cancelled_task_reads_back_as_cancelled(start_server):
    url, runtime = start_server()
    async with ProviderClient(url, KEY) as client:
        handle = await client.submit("trip_planner", {"message": "sleep=30;slow"})
        await client.cancel_task(handle.task_id)
        snapshot = await client.get_task(handle.task_id)

    assert snapshot.status == "cancelled"
    assert runtime.cancelled.wait(timeout=5)


async def test_provider_without_push_accepts_no_subscription(start_server):
    url, _ = start_server(push_enabled=False)
    async with ProviderClient(url, KEY) as client:
        handle = await client.submit("trip_planner", {"message": "sleep=1;hi"})
        async with client.listen([handle.task_id]) as subscription:
            assert subscription.accepted == frozenset()


async def test_errors_inside_client_and_listen_blocks_propagate_unwrapped(start_server):
    url, _ = start_server()
    with pytest.raises(MCPError):
        async with ProviderClient(url, KEY) as client:
            handle = await client.submit("trip_planner", {"message": "sleep=1;hi"})
            async with client.listen([handle.task_id]):
                await client.get_task("no-such-task")
    with pytest.raises(ValueError, match="body"):
        async with ProviderClient(url, KEY):
            raise ValueError("body")


async def test_provider_without_tasks_answers_directly(start_server):
    url, _ = start_server(tasks_enabled=False)
    async with ProviderClient(url, KEY) as client:
        assert not client.supports_tasks
        result = await client.submit("trip_planner", {"message": "hi"})

    assert parse_agent_reply(result).reply == "echo: hi"
