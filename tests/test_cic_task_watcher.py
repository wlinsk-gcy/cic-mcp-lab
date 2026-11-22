from contextlib import asynccontextmanager
from decimal import Decimal

import anyio
import pytest
from mcp.shared.exceptions import MCPError

from cic_mcp_lab.cic.ledger import Ledger
from cic_mcp_lab.cic.mcp_client import CreateTaskResult, ProviderUnavailable, TaskSnapshot, TaskSubscription
from cic_mcp_lab.cic.registry import AgentRecord
from cic_mcp_lab.cic.task_watcher import ConversationBusy, SubmitFailed, TaskWatcher
from tests.helpers import TEST_API_KEY, unused_port

ENV = {"TEST_KEY": TEST_API_KEY}
NOW = "2026-11-22T00:00:00.000Z"
DONE = {
    "content": [{"type": "text", "text": "done"}],
    "structuredContent": {"conversation_id": "conv_9", "status": "completed", "reply": "done"},
    "_meta": {"com.cic/credit_usage": 7},
}


def record_for(url, agent_id="trip_planner"):
    return AgentRecord(agent_id, "provider-a", "n", "d", url, "TEST_KEY", agent_id, 0.0001)


async def next_outcome(watcher, timeout=10):
    with anyio.fail_after(timeout):
        return await watcher.outcomes.get()


async def test_pushed_reply_is_settled_once_per_task(start_server):
    url, _ = start_server()
    ledger = Ledger()
    async with TaskWatcher({"trip_planner": record_for(url)}, ledger, env=ENV) as watcher:
        submission = await watcher.submit("trip_planner", "sleep=0.5;hi")
        outcome = await next_outcome(watcher)

    assert (outcome.ok, outcome.delivery, outcome.task_id) == (True, "push", submission.task_id)
    assert outcome.reply.reply == "echo: hi"
    assert [entry.settlement_key for entry in ledger.entries] == [submission.task_id]
    assert ledger.total_hkd() == Decimal("0.0042")


async def test_provider_without_push_is_polled(start_server):
    url, _ = start_server(push_enabled=False, poll_interval_ms=100)
    async with TaskWatcher({"trip_planner": record_for(url)}, Ledger(), env=ENV) as watcher:
        await watcher.submit("trip_planner", "hi")
        outcome = await next_outcome(watcher)

    assert (outcome.ok, outcome.delivery) == (True, "poll")
    assert any("push" in note for note in outcome.notes)


async def test_short_task_ttl_is_flagged_on_submission(start_server):
    url, _ = start_server(ttl_ms=60_000)
    async with TaskWatcher({"trip_planner": record_for(url)}, Ledger(), env=ENV) as watcher:
        submission = await watcher.submit("trip_planner", "hi")

    assert any("ttlMs" in warning for warning in submission.warnings)


async def test_provider_without_tasks_answers_directly(start_server):
    url, _ = start_server(tasks_enabled=False)
    ledger = Ledger()
    async with TaskWatcher({"trip_planner": record_for(url)}, ledger, env=ENV) as watcher:
        submission = await watcher.submit("trip_planner", "hi")
        outcome = await next_outcome(watcher)

    assert submission.task_id is None
    assert (outcome.ok, outcome.delivery) == (True, "direct")
    assert [entry.settlement_key for entry in ledger.entries] == [submission.submission_id]


@pytest.mark.parametrize(("message", "failure"), [("unknown-conversation", "agent_error"), ("mcp-error", "task_failed")])
async def test_failed_turns_are_not_billed(start_server, message, failure):
    url, _ = start_server()
    ledger = Ledger()
    async with TaskWatcher({"trip_planner": record_for(url)}, ledger, env=ENV) as watcher:
        await watcher.submit("trip_planner", message, conversation_id="conv_1")
        outcome = await next_outcome(watcher)

    assert outcome.failure == failure
    assert ledger.entries == ()


async def test_turns_of_one_conversation_never_overlap(start_server):
    url, _ = start_server()
    async with TaskWatcher({"trip_planner": record_for(url)}, Ledger(), env=ENV) as watcher:
        await watcher.submit("trip_planner", "sleep=0.5;first", conversation_id="conv_1")
        with pytest.raises(ConversationBusy):
            await watcher.submit("trip_planner", "second", conversation_id="conv_1")
        await next_outcome(watcher)
        await watcher.submit("trip_planner", "third", conversation_id="conv_1")
        outcome = await next_outcome(watcher)

    assert outcome.reply.reply == "echo: third"


async def test_a_finished_watch_never_frees_the_next_turns_busy_mark(start_server):
    url, _ = start_server()
    async with TaskWatcher({"trip_planner": record_for(url)}, Ledger(), env=ENV) as watcher:
        await watcher.submit("trip_planner", "first", conversation_id="conv_1")
        await next_outcome(watcher)
        await watcher.submit("trip_planner", "sleep=2;second", conversation_id="conv_1")
        await anyio.sleep(0.5)  # the first watch has finished tearing down by now
        with pytest.raises(ConversationBusy):
            await watcher.submit("trip_planner", "third", conversation_id="conv_1")


async def test_submissions_that_never_reach_the_agent_fail(start_server):
    url, _ = start_server()
    registry = {
        "trip_planner": record_for(url),
        "offline": record_for(f"http://127.0.0.1:{unused_port()}/mcp", agent_id="offline"),
    }
    async with TaskWatcher(registry, Ledger(), env={"TEST_KEY": "sk-wrong"}) as watcher:
        for agent_id in ("nope", "trip_planner", "offline"):
            with pytest.raises(SubmitFailed):
                await watcher.submit(agent_id, "hi")
    async with TaskWatcher(registry, Ledger(), env={}) as watcher:
        with pytest.raises(SubmitFailed, match="TEST_KEY"):
            await watcher.submit("trip_planner", "hi")


class ScriptedClient:
    """Provider stand-in: accepts the push subscription but never pushes; tasks/get follows a script."""

    supports_tasks = True

    def __init__(self, script, *, ttl_ms=3_600_000):
        self._script = list(script)  # status strings or exceptions; the last entry repeats
        self._ttl_ms = ttl_ms

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc_info):
        pass

    async def submit(self, tool, arguments):
        return CreateTaskResult(
            task_id="task_1", status="working", created_at=NOW, last_updated_at=NOW, ttl_ms=self._ttl_ms, poll_interval_ms=10
        )

    async def get_task(self, task_id):
        step = self._script.pop(0) if len(self._script) > 1 else self._script[0]
        if isinstance(step, Exception):
            raise step
        result = DONE if step == "completed" else None
        return TaskSnapshot(task_id=task_id, status=step, created_at=NOW, last_updated_at=NOW, result=result)

    @asynccontextmanager
    async def listen(self, task_ids):
        send, recv = anyio.create_memory_object_stream(1)
        with send, recv:  # the send side stays open: no push ever arrives and the stream never ends
            yield TaskSubscription(frozenset(task_ids), recv)


def scripted_watcher(client, ledger=None):
    return TaskWatcher(
        {"trip_planner": record_for("http://scripted/mcp")},
        ledger or Ledger(),
        env=ENV,
        client_factory=lambda url, key: client,
        backstop_poll_seconds=0.05,
    )


async def test_backstop_poll_recovers_a_missed_push():
    ledger = Ledger()
    async with scripted_watcher(ScriptedClient(["working", "working", "completed"]), ledger) as watcher:
        await watcher.submit("trip_planner", "hi")
        outcome = await next_outcome(watcher)

    assert (outcome.ok, outcome.delivery, outcome.reply.credit_usage) == (True, "poll", 7)
    assert ledger.total_credit() == 7


@pytest.mark.parametrize(
    ("script", "ttl_ms", "failure"),
    [
        ([MCPError(-32602, "Unknown taskId")], 3_600_000, "lost"),
        (["working"], 100, "expired"),
    ],
)
async def test_tasks_that_vanish_or_outlive_their_ttl(script, ttl_ms, failure):
    async with scripted_watcher(ScriptedClient(script, ttl_ms=ttl_ms)) as watcher:
        await watcher.submit("trip_planner", "hi")
        outcome = await next_outcome(watcher)

    assert outcome.failure == failure


async def test_closing_the_watcher_fails_pending_submissions():
    class Hanging(ScriptedClient):
        async def submit(self, tool, arguments):
            await anyio.sleep_forever()

    watcher = scripted_watcher(Hanging(["working"]))
    async with anyio.create_task_group() as tg:
        async def close_soon():
            await anyio.sleep(0.2)
            await watcher.aclose()

        tg.start_soon(close_soon)
        with anyio.fail_after(5), pytest.raises(SubmitFailed):
            await watcher.submit("trip_planner", "hi")


async def test_a_failed_recheck_falls_back_to_push_and_backstop_polls():
    script = [MCPError(-32603, "transient"), "working", "completed"]
    async with scripted_watcher(ScriptedClient(script)) as watcher:
        await watcher.submit("trip_planner", "hi")
        outcome = await next_outcome(watcher)

    assert (outcome.ok, outcome.delivery) == (True, "poll")
    assert any("re-check failed" in note for note in outcome.notes)


class DroppedPushClient(ScriptedClient):
    """The push stream breaks mid-watch, the way a dropped connection surfaces from the SDK."""

    @asynccontextmanager
    async def listen(self, task_ids):
        send, recv = anyio.create_memory_object_stream(1)
        recv.close()
        with send:
            yield TaskSubscription(frozenset(task_ids), recv)


class Unreachable:
    async def __aenter__(self):
        raise ProviderUnavailable("http://scripted/mcp: ConnectError")

    async def __aexit__(self, *exc_info):
        pass


@pytest.mark.parametrize(("reconnects", "failure"), [(["completed"], None), ([], "lost")])
async def test_watch_reconnects_and_polls_after_the_connection_breaks(reconnects, failure):
    clients = iter([DroppedPushClient(["working"]), *(ScriptedClient([status]) for status in reconnects)])
    watcher = TaskWatcher(
        {"trip_planner": record_for("http://scripted/mcp")},
        Ledger(),
        env=ENV,
        client_factory=lambda url, key: next(clients, Unreachable()),
        backstop_poll_seconds=0.05,
    )
    async with watcher:
        await watcher.submit("trip_planner", "hi")
        outcome = await next_outcome(watcher)

    assert outcome.failure == failure
    assert any("reconnecting" in note for note in outcome.notes)
