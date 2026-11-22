"""TaskWatcher: submits turns to provider agents and follows each task to its final state (design §6.8.2).

Lab form: one asyncio task and one ProviderClient per submission; outcomes are put on `outcomes` and
billable replies are settled in the Ledger once per taskId. Production runs this as a service (design §13).
"""
from __future__ import annotations

import asyncio
import itertools
import os
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Literal

import anyio
from anyio.streams.memory import MemoryObjectReceiveStream
from mcp.shared.exceptions import MCPError
from mcp_types import INVALID_PARAMS

from .contract import AgentCallFailed, AgentReply, ContractViolation, check_task_ttl, parse_agent_reply
from .ledger import Ledger, LedgerEntry
from .mcp_client import (
    TERMINAL_STATUSES,
    CreateTaskResult,
    ListenFailed,
    ProviderClient,
    TaskNotificationParams,
    TaskSnapshot,
    TaskState,
    describe_error,
)
from .registry import AgentRecord, RegistryError, api_key_for

Delivery = Literal["direct", "push", "recheck", "poll"]
Failure = Literal["agent_error", "contract", "task_failed", "task_cancelled", "expired", "lost", "internal"]
DEFAULT_POLL_INTERVAL_MS = 2_000
MAX_POLL_ERRORS = 3


class SubmitFailed(RuntimeError):
    """The turn never reached the agent (unknown agent, missing key, provider down or call rejected); not billed."""


class ConversationBusy(RuntimeError):
    """The previous turn of this conversation is still running; a provider never gets two at once."""


@dataclass(frozen=True)
class Submission:
    submission_id: str
    agent_id: str
    task_id: str | None  # None when the provider answers directly instead of creating a task
    warnings: tuple[str, ...] = ()


@dataclass(frozen=True)
class TaskOutcome:
    submission_id: str
    agent_id: str
    task_id: str | None
    delivery: Delivery | None  # how the final state reached CIC; None when it never did
    reply: AgentReply | None
    failure: Failure | None
    detail: str
    notes: tuple[str, ...]  # how the watch went, e.g. falling back from push to polling
    elapsed_seconds: float

    @property
    def ok(self) -> bool:
        return self.failure is None


@dataclass(frozen=True)
class _Verdict:
    delivery: Delivery | None
    reply: AgentReply | None = None
    failure: Failure | None = None
    detail: str = ""


class _TaskGone(Exception):
    def __init__(self, failure: Failure, detail: str) -> None:
        super().__init__(detail)
        self.failure = failure
        self.detail = detail


def _remaining(seconds: float, deadline: float | None) -> float:
    if deadline is None:
        return seconds
    return max(0.0, min(seconds, deadline - time.monotonic()))


def _check_deadline(deadline: float | None) -> None:
    if deadline is not None and time.monotonic() >= deadline:
        raise _TaskGone("expired", "ttlMs elapsed before the task reached a final state")


class TaskWatcher:
    """Submits agent turns and reports each turn's final outcome on `outcomes`.

    Use it as an async context manager; leaving stops the watches (providers keep running the tasks).
    """

    def __init__(
        self,
        registry: Mapping[str, AgentRecord],
        ledger: Ledger,
        *,
        client_factory: Callable[[str, str], Any] = ProviderClient,
        env: Mapping[str, str] = os.environ,
        backstop_poll_seconds: float = 30.0,
    ) -> None:
        self._registry = registry
        self._ledger = ledger
        self._client_factory = client_factory
        self._env = env
        self._backstop_seconds = backstop_poll_seconds
        self.outcomes: asyncio.Queue[TaskOutcome] = asyncio.Queue()
        self._ids = itertools.count(1)
        self._watches: set[asyncio.Task[None]] = set()
        self._busy: set[tuple[str, str]] = set()

    async def __aenter__(self) -> TaskWatcher:
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        watches = list(self._watches)
        for watch in watches:
            watch.cancel()
        await asyncio.gather(*watches, return_exceptions=True)

    async def submit(
        self,
        agent_id: str,
        message: str,
        image_urls: Sequence[str] = (),
        conversation_id: str | None = None,
    ) -> Submission:
        """Send one turn; returns once the provider has accepted it. The outcome arrives on `outcomes`."""
        record = self._registry.get(agent_id)
        if record is None:
            raise SubmitFailed(f"Unknown agent {agent_id!r}")
        try:
            api_key = api_key_for(record, self._env)
        except RegistryError as exc:
            raise SubmitFailed(str(exc)) from exc
        busy_key = (agent_id, conversation_id) if conversation_id else None
        if busy_key is not None:
            if busy_key in self._busy:
                raise ConversationBusy(f"{agent_id}: conversation {conversation_id} is still working on the previous turn")
            self._busy.add(busy_key)
        arguments: dict[str, Any] = {"message": message}
        if conversation_id:
            arguments["conversation_id"] = conversation_id
        if image_urls:
            arguments["image_urls"] = list(image_urls)

        submission_id = f"sub_{next(self._ids)}"
        accepted: asyncio.Future[Submission] = asyncio.get_running_loop().create_future()
        watch = asyncio.create_task(
            self._watch(submission_id, record, api_key, arguments, busy_key, accepted),
            name=f"watch-{submission_id}",
        )
        self._watches.add(watch)

        def forget(task: asyncio.Task[None]) -> None:
            self._watches.discard(task)
            self._release(busy_key)

        watch.add_done_callback(forget)
        return await accepted

    def _release(self, busy_key: tuple[str, str] | None) -> None:
        if busy_key is not None:
            self._busy.discard(busy_key)

    async def _watch(
        self,
        submission_id: str,
        record: AgentRecord,
        api_key: str,
        arguments: dict[str, Any],
        busy_key: tuple[str, str] | None,
        accepted: asyncio.Future[Submission],
    ) -> None:
        started = time.monotonic()
        notes: list[str] = []
        task_id: str | None = None
        submitted = finished = False

        def accept(warnings: tuple[str, ...] = ()) -> None:
            nonlocal submitted
            submitted = True
            if not accepted.done():
                accepted.set_result(Submission(submission_id, record.agent_id, task_id, warnings))

        def reject(exc: BaseException) -> None:
            self._release(busy_key)
            if not accepted.done():
                accepted.set_exception(SubmitFailed(f"{record.agent_id}: {describe_error(exc)}"))

        def finish(verdict: _Verdict) -> None:
            nonlocal finished
            if finished:
                return
            finished = True
            self._release(busy_key)  # before the outcome is visible, so the next turn can go right away
            self.outcomes.put_nowait(
                TaskOutcome(
                    submission_id,
                    record.agent_id,
                    task_id,
                    verdict.delivery,
                    verdict.reply,
                    verdict.failure,
                    verdict.detail,
                    tuple(notes),
                    round(time.monotonic() - started, 2),
                )
            )

        try:
            async with self._client_factory(record.mcp_url, api_key) as client:
                if not client.supports_tasks:
                    # The call blocks until the agent answers: accept now and report the answer as the outcome.
                    notes.append("provider has no tasks extension; waiting for a direct answer")
                    accept()
                try:
                    handle = await client.submit(record.entry_tool, arguments)
                except Exception as exc:
                    if submitted:
                        finish(_Verdict(None, failure="agent_error", detail=describe_error(exc)))
                    else:
                        reject(exc)
                    return
                if not isinstance(handle, CreateTaskResult):
                    accept()
                    finish(self._settle(record, submission_id, handle, "direct"))
                    return
                task_id = handle.task_id
                warning = check_task_ttl(handle.ttl_ms)
                accept((warning,) if warning else ())
                deadline = None if handle.ttl_ms is None else started + handle.ttl_ms / 1000
                try:
                    state, delivery = await self._follow(client, handle, deadline, notes)
                except _TaskGone as gone:
                    finish(_Verdict(None, failure=gone.failure, detail=gone.detail))
                    return
                finish(self._conclude(record, state, delivery))
        except Exception as exc:
            if submitted:
                finish(_Verdict(None, failure="internal", detail=describe_error(exc)))
            else:
                reject(exc)
        finally:
            if not accepted.done():
                accepted.cancel()

    async def _follow(
        self, client: ProviderClient, handle: CreateTaskResult, deadline: float | None, notes: list[str]
    ) -> tuple[TaskState, Delivery]:
        """Push first, one re-check right after subscribing, then polling (design §6.8.2)."""
        task_id = handle.task_id
        try:
            async with client.listen([task_id]) as subscription:
                if task_id in subscription.accepted:
                    snapshot = await self._get(client, task_id, deadline)
                    if snapshot.status in TERMINAL_STATUSES:
                        return snapshot, "recheck"
                    pushed = await self._await_push(client, task_id, subscription.events, deadline, notes)
                    if pushed is not None:
                        return pushed
                else:
                    notes.append("provider did not accept the push subscription; polling")
        except ListenFailed as exc:
            notes.append(f"push subscription failed ({exc}); polling")
        interval = (handle.poll_interval_ms or DEFAULT_POLL_INTERVAL_MS) / 1000
        return await self._poll(client, task_id, deadline, interval), "poll"

    async def _await_push(
        self,
        client: ProviderClient,
        task_id: str,
        events: MemoryObjectReceiveStream[TaskNotificationParams],
        deadline: float | None,
        notes: list[str],
    ) -> tuple[TaskState, Delivery] | None:
        """Wait for a terminal push, polling every `backstop_poll_seconds` in case a push was lost."""
        while True:
            with anyio.move_on_after(_remaining(self._backstop_seconds, deadline)):
                try:
                    event = await events.receive()
                except anyio.EndOfStream:
                    notes.append("push stream closed by the provider; polling")
                    return None
                if event.status in TERMINAL_STATUSES:
                    return event, "push"
                continue
            try:
                snapshot = await self._get(client, task_id, deadline)
            except _TaskGone:
                raise
            except Exception as exc:
                notes.append(f"backstop poll failed ({describe_error(exc)}); polling")
                return None
            if snapshot.status in TERMINAL_STATUSES:
                return snapshot, "poll"
            _check_deadline(deadline)

    async def _poll(
        self, client: ProviderClient, task_id: str, deadline: float | None, interval: float
    ) -> TaskSnapshot:
        errors = 0
        while True:
            try:
                snapshot = await self._get(client, task_id, deadline)
            except _TaskGone:
                raise
            except Exception as exc:
                errors += 1
                if errors >= MAX_POLL_ERRORS:
                    raise _TaskGone("lost", f"tasks/get failed {errors} times: {describe_error(exc)}") from exc
            else:
                errors = 0
                if snapshot.status in TERMINAL_STATUSES:
                    return snapshot
                _check_deadline(deadline)
            await anyio.sleep(_remaining(interval, deadline))

    async def _get(self, client: ProviderClient, task_id: str, deadline: float | None) -> TaskSnapshot:
        try:
            return await client.get_task(task_id)
        except MCPError as exc:
            if exc.code != INVALID_PARAMS:
                raise
            expired = deadline is not None and time.monotonic() >= deadline
            raise _TaskGone("expired" if expired else "lost", describe_error(exc)) from exc

    def _conclude(self, record: AgentRecord, state: TaskState, delivery: Delivery) -> _Verdict:
        if state.status == "completed":
            return self._settle(record, state.task_id, state.result or {}, delivery)
        if state.status == "failed":
            error = state.error or {}
            detail = f"MCP error {error.get('code')}: {error.get('message', '')}"
            return _Verdict(delivery, failure="task_failed", detail=detail)
        return _Verdict(delivery, failure="task_cancelled", detail=state.status_message or "cancelled")

    def _settle(
        self, record: AgentRecord, settlement_key: str, result: dict[str, Any], delivery: Delivery
    ) -> _Verdict:
        """Validate a final CallToolResult; a valid reply is billed once per settlement key."""
        try:
            reply = parse_agent_reply(result)
        except AgentCallFailed as exc:
            return _Verdict(delivery, failure="agent_error", detail=str(exc))
        except ContractViolation as exc:
            return _Verdict(delivery, failure="contract", detail=str(exc))
        self._ledger.settle(
            LedgerEntry(
                settlement_key,
                record.provider_id,
                record.agent_id,
                reply.conversation_id,
                reply.credit_usage,
                record.price_per_credit,
            )
        )
        return _Verdict(delivery, reply=reply)
