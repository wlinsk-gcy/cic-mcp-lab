import asyncio

import anyio
import pytest

from cic_mcp_lab.provider.tasks import (
    TaskAlreadyFinishedError,
    TaskFailure,
    TaskStore,
    UnknownTaskError,
)

RESULT = {"content": [], "structuredContent": {"reply": "ok"}, "_meta": {"com.cic/credit_usage": 7}}


def make_store(**overrides) -> TaskStore:
    return TaskStore(**({"ttl_ms": 60_000, "poll_interval_ms": 500} | overrides))


async def finished():
    return RESULT


async def test_task_starts_working_and_completes_with_the_work_result():
    store = make_store()
    record = store.create("cic", finished)
    assert record.task_fields()["status"] == "working"

    await record.runner

    detail = store.get(record.task_id, "cic").detailed()
    assert (detail["status"], detail["result"]) == ("completed", RESULT)
    assert (detail["ttlMs"], detail["pollIntervalMs"]) == (60_000, 500)


async def test_tasks_are_only_visible_to_their_owner():
    store = make_store()
    record = store.create("cic", finished)

    with pytest.raises(UnknownTaskError):
        store.get(record.task_id, "someone-else")
    await record.runner


async def test_cancel_stops_the_work_and_cannot_be_repeated():
    store = make_store()
    started, stopped = asyncio.Event(), asyncio.Event()

    async def hang():
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            stopped.set()

    record = store.create("cic", hang)
    await started.wait()
    store.cancel(record.task_id, "cic")

    with pytest.raises(asyncio.CancelledError):
        await record.runner
    assert stopped.is_set()
    assert store.get(record.task_id, "cic").status == "cancelled"
    with pytest.raises(TaskAlreadyFinishedError):
        store.cancel(record.task_id, "cic")


@pytest.mark.parametrize("error", [TaskFailure({"code": -32800, "message": "cancelled"}), RuntimeError("cleanup failed")])
async def test_cancelled_task_stays_cancelled_even_if_the_work_raises_while_unwinding(error):
    store = make_store()
    started = asyncio.Event()

    async def raise_on_cancel():
        started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            raise error

    record = store.create("cic", raise_on_cancel)
    send, recv = anyio.create_memory_object_stream[dict](8)
    store.subscribe("cic", [record.task_id], send)
    await started.wait()

    store.cancel(record.task_id, "cic")
    await asyncio.gather(record.runner, return_exceptions=True)

    assert store.get(record.task_id, "cic").status == "cancelled"
    assert [recv.receive_nowait()["status"]] == ["cancelled"]
    with pytest.raises(anyio.WouldBlock):
        recv.receive_nowait()
    send.close()
    recv.close()


async def test_work_failure_marks_the_task_failed_with_the_error():
    store = make_store()
    error = {"code": -32603, "message": "boom"}

    async def fail():
        raise TaskFailure(error)

    record = store.create("cic", fail)
    await record.runner

    detail = store.get(record.task_id, "cic").detailed()
    assert (detail["status"], detail["error"]) == ("failed", error)
    assert "result" not in detail


async def test_tasks_expire_after_their_ttl():
    now = [100.0]
    store = make_store(ttl_ms=1_000, clock=lambda: now[0])
    record = store.create("cic", finished)
    await record.runner

    now[0] += 1.0

    with pytest.raises(UnknownTaskError):
        store.get(record.task_id, "cic")


async def test_subscribers_are_told_when_a_running_task_expires():
    now = [100.0]
    store = make_store(ttl_ms=1_000, clock=lambda: now[0])

    async def hang():
        await asyncio.Event().wait()

    record = store.create("cic", hang)
    send, recv = anyio.create_memory_object_stream[dict](8)
    store.subscribe("cic", [record.task_id], send)

    now[0] += 1.0
    assert store.find(record.task_id, "cic") is None
    await asyncio.gather(record.runner, return_exceptions=True)

    assert recv.receive_nowait()["status"] == "cancelled"
    send.close()
    recv.close()


async def test_subscribers_receive_the_final_snapshot():
    store = make_store()
    release = asyncio.Event()

    async def wait_then_finish():
        await release.wait()
        return RESULT

    record = store.create("cic", wait_then_finish)
    send, recv = anyio.create_memory_object_stream[dict](8)
    accepted, unsubscribe = store.subscribe("cic", [record.task_id, "unknown"], send)
    assert accepted == [record.task_id]

    release.set()
    await record.runner

    assert recv.receive_nowait() == store.get(record.task_id, "cic").detailed()
    unsubscribe()


async def test_push_disabled_accepts_no_subscriptions():
    store = make_store(push_enabled=False)
    record = store.create("cic", finished)
    send, _ = anyio.create_memory_object_stream[dict](8)

    accepted, _ = store.subscribe("cic", [record.task_id], send)

    assert accepted == []
    await record.runner
