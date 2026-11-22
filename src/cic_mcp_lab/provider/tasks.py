"""Server-side task state for the MCP tasks extension: lifecycle, ownership, TTL and push fan-out."""
from __future__ import annotations

import asyncio
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Literal

import anyio
from anyio.streams.memory import MemoryObjectSendStream

TaskStatus = Literal["working", "input_required", "completed", "failed", "cancelled"]
TERMINAL_STATUSES: frozenset[str] = frozenset({"completed", "failed", "cancelled"})
_INTERNAL_ERROR = -32603

Snapshot = dict[str, Any]
Work = Callable[[], Awaitable[dict[str, Any]]]


class UnknownTaskError(LookupError):
    """The task does not exist, has expired, or belongs to another caller."""


class TaskAlreadyFinishedError(ValueError):
    pass


class TaskFailure(Exception):
    """Raised by task work to end the task as `failed` with a JSON-RPC error object."""

    def __init__(self, error: dict[str, Any]) -> None:
        super().__init__(error.get("message", "task failed"))
        self.error = error


def _now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


@dataclass
class TaskRecord:
    task_id: str
    owner: str
    created_at: str
    last_updated_at: str
    ttl_ms: int
    poll_interval_ms: int
    expires_at: float
    status: TaskStatus = "working"
    status_message: str | None = None
    result: dict[str, Any] | None = None
    error: dict[str, Any] | None = None
    runner: asyncio.Task[None] | None = field(default=None, repr=False)

    @property
    def terminal(self) -> bool:
        return self.status in TERMINAL_STATUSES

    def task_fields(self) -> Snapshot:
        fields: Snapshot = {
            "taskId": self.task_id,
            "status": self.status,
            "createdAt": self.created_at,
            "lastUpdatedAt": self.last_updated_at,
            "ttlMs": self.ttl_ms,
            "pollIntervalMs": self.poll_interval_ms,
        }
        if self.status_message is not None:
            fields["statusMessage"] = self.status_message
        return fields

    def detailed(self) -> Snapshot:
        """The task as returned by tasks/get and notifications/tasks."""
        data = self.task_fields()
        if self.status == "completed" and self.result is not None:
            data["result"] = self.result
        if self.status == "failed" and self.error is not None:
            data["error"] = self.error
        return data


class TaskStore:
    def __init__(
        self,
        *,
        ttl_ms: int,
        poll_interval_ms: int,
        push_enabled: bool = True,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._ttl_ms = ttl_ms
        self._poll_interval_ms = poll_interval_ms
        self._push_enabled = push_enabled
        self._clock = clock
        self._tasks: dict[str, TaskRecord] = {}
        self._listeners: dict[str, set[MemoryObjectSendStream[Snapshot]]] = {}

    def create(self, owner: str, work: Work) -> TaskRecord:
        self._purge_expired()
        timestamp = _now_iso()
        record = TaskRecord(
            task_id=uuid.uuid4().hex,
            owner=owner,
            created_at=timestamp,
            last_updated_at=timestamp,
            ttl_ms=self._ttl_ms,
            poll_interval_ms=self._poll_interval_ms,
            expires_at=self._clock() + self._ttl_ms / 1000,
        )
        self._tasks[record.task_id] = record
        # A detached asyncio task: the work outlives the tools/call request that created it.
        record.runner = asyncio.create_task(self._run(record, work), name=f"mcp-task-{record.task_id}")
        return record

    def find(self, task_id: str, owner: str) -> TaskRecord | None:
        self._purge_expired()
        record = self._tasks.get(task_id)
        return record if record is not None and record.owner == owner else None

    def get(self, task_id: str, owner: str) -> TaskRecord:
        record = self.find(task_id, owner)
        if record is None:
            raise UnknownTaskError(task_id)
        return record

    def cancel(self, task_id: str, owner: str) -> TaskRecord:
        record = self.get(task_id, owner)
        if record.terminal:
            raise TaskAlreadyFinishedError(f"Task {task_id} is already {record.status}")
        self._transition(record, "cancelled", status_message="Cancelled by client")
        if record.runner is not None:
            record.runner.cancel()
        return record

    def subscribe(
        self, owner: str, task_ids: list[str], stream: MemoryObjectSendStream[Snapshot]
    ) -> tuple[list[str], Callable[[], None]]:
        """Register `stream` for the caller's known tasks; returns accepted ids and an unsubscribe callback."""
        accepted = (
            [task_id for task_id in dict.fromkeys(task_ids) if self.find(task_id, owner) is not None]
            if self._push_enabled
            else []
        )
        for task_id in accepted:
            self._listeners.setdefault(task_id, set()).add(stream)

        def unsubscribe() -> None:
            for task_id in accepted:
                listeners = self._listeners.get(task_id)
                if listeners is not None:
                    listeners.discard(stream)
                    if not listeners:
                        del self._listeners[task_id]

        return accepted, unsubscribe

    async def _run(self, record: TaskRecord, work: Work) -> None:
        try:
            result = await work()
        except asyncio.CancelledError:
            self._transition(record, "cancelled", status_message="Cancelled by server")
            raise
        except TaskFailure as exc:
            self._transition(record, "failed", error=exc.error)
        except Exception as exc:  # safety net: tool exceptions normally arrive as isError results
            self._transition(record, "failed", error={"code": _INTERNAL_ERROR, "message": str(exc)})
        else:
            self._transition(record, "completed", result=result)

    def _transition(self, record: TaskRecord, status: TaskStatus, **changes: Any) -> None:
        if record.terminal:
            return  # a finished task never changes state again
        record.status = status
        record.last_updated_at = _now_iso()
        for name, value in changes.items():
            setattr(record, name, value)
        snapshot = record.detailed()
        for stream in list(self._listeners.get(record.task_id, ())):
            try:
                stream.send_nowait(snapshot)
            except (anyio.WouldBlock, anyio.ClosedResourceError, anyio.BrokenResourceError):
                pass  # the subscriber falls back to polling tasks/get

    def _purge_expired(self) -> None:
        now = self._clock()
        for task_id, record in list(self._tasks.items()):
            if now >= record.expires_at:
                if not record.terminal:
                    self._transition(record, "cancelled", status_message="Expired: TTL elapsed")
                del self._tasks[task_id]
                self._listeners.pop(task_id, None)
                if record.runner is not None and not record.runner.done():
                    record.runner.cancel()
