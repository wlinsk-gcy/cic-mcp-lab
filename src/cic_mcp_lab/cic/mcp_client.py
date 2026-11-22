"""CIC-side MCP client for provider servers: the mcp SDK `Client` plus the tasks extension (design §6.8)."""
from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from contextlib import AsyncExitStack, asynccontextmanager
from dataclasses import dataclass
from typing import Any, Literal

import anyio
import httpx2
from anyio.streams.memory import MemoryObjectReceiveStream, MemoryObjectSendStream
from mcp.client import ClaimContext, Client, ClientExtension, ClientSession, NotificationBinding, ResultClaim
from mcp.client.session import IncomingMessage
from mcp.client.streamable_http import streamable_http_client
from mcp.shared.exceptions import MCPError
from mcp_types import (
    CallToolResult,
    EmptyResult,
    NotificationParams,
    Request,
    RequestParams,
    Result,
    ServerCapabilities,
    SubscriptionFilter,
    SubscriptionsAcknowledgedNotification,
    SubscriptionsListenRequest,
    SubscriptionsListenRequestParams,
    SubscriptionsListenResult,
)
from pydantic import BaseModel

TASKS_EXTENSION = "io.modelcontextprotocol/tasks"
TERMINAL_STATUSES = frozenset({"completed", "failed", "cancelled"})


class ProviderUnavailable(RuntimeError):
    """Could not connect to the provider's MCP server (network, auth or protocol failure)."""


class ListenFailed(RuntimeError):
    """`subscriptions/listen` was rejected or not acknowledged; the caller falls back to polling."""


def describe_error(exc: BaseException) -> str:
    """Readable text for SDK failures; connection errors arrive wrapped in exception groups."""
    if isinstance(exc, BaseExceptionGroup):
        return "; ".join(describe_error(inner) for inner in exc.exceptions)
    if isinstance(exc, MCPError):
        return f"MCP error {exc.code}: {exc.message}"
    return f"{type(exc).__name__}: {exc}" if str(exc) else type(exc).__name__


def dump_model(model: BaseModel) -> dict[str, Any]:
    """Wire-format (camelCase) JSON of an SDK model."""
    return model.model_dump(by_alias=True, mode="json", exclude_none=True)


# Wire models of the tasks extension; the SDK does not ship them.


class CreateTaskResult(Result):
    result_type: Literal["task"] = "task"
    task_id: str
    status: str
    status_message: str | None = None
    created_at: str
    last_updated_at: str
    ttl_ms: int | None = None
    poll_interval_ms: int | None = None


class TaskSnapshot(Result):
    """`tasks/get` result: the task's current state, with `result` or `error` once finished."""

    task_id: str
    status: str
    status_message: str | None = None
    created_at: str
    last_updated_at: str
    ttl_ms: int | None = None
    poll_interval_ms: int | None = None
    result: dict[str, Any] | None = None
    error: dict[str, Any] | None = None


class TaskNotificationParams(NotificationParams):
    """`notifications/tasks` params: the same fields as TaskSnapshot."""

    task_id: str
    status: str
    status_message: str | None = None
    created_at: str
    last_updated_at: str
    ttl_ms: int | None = None
    poll_interval_ms: int | None = None
    result: dict[str, Any] | None = None
    error: dict[str, Any] | None = None


TaskState = TaskSnapshot | TaskNotificationParams


class TaskIdParams(RequestParams):
    task_id: str


class GetTaskRequest(Request[TaskIdParams, Literal["tasks/get"]]):
    method: Literal["tasks/get"] = "tasks/get"
    params: TaskIdParams
    name_param = "taskId"  # sends the Mcp-Name header the server requires


class CancelTaskRequest(Request[TaskIdParams, Literal["tasks/cancel"]]):
    method: Literal["tasks/cancel"] = "tasks/cancel"
    params: TaskIdParams
    name_param = "taskId"


@dataclass(frozen=True)
class TaskSubscription:
    accepted: frozenset[str]  # task ids the provider will push; empty when it does not push
    events: MemoryObjectReceiveStream[TaskNotificationParams]


class TasksClientExtension(ClientExtension):
    """Declares the tasks extension, claims task handles and fans `notifications/tasks` out to listeners."""

    identifier = TASKS_EXTENSION

    def __init__(self) -> None:
        self._listeners: dict[str, set[MemoryObjectSendStream[TaskNotificationParams]]] = {}
        # Acknowledgements carry no subscription id, so only one listen may await its ack at a time.
        self._ack_lock = anyio.Lock()
        self._pending_ack: tuple[anyio.Event, dict[str, Any]] | None = None

    def claims(self) -> list[ResultClaim[Any]]:
        return [ResultClaim(result_type="task", model=CreateTaskResult, resolve=self._resolve)]

    def notifications(self) -> list[NotificationBinding[Any]]:
        return [
            NotificationBinding(method="notifications/tasks", params_type=TaskNotificationParams, handler=self._on_task)
        ]

    async def on_message(self, message: IncomingMessage) -> None:
        """Client(message_handler=...): the listen acknowledgement is a core notification and lands here."""
        if isinstance(message, SubscriptionsAcknowledgedNotification) and self._pending_ack is not None:
            event, holder = self._pending_ack
            task_ids = (message.params.notifications.model_extra or {}).get("taskIds") or []
            holder["accepted"] = frozenset(task_id for task_id in task_ids if isinstance(task_id, str))
            event.set()

    async def _on_task(self, params: TaskNotificationParams) -> None:
        for stream in list(self._listeners.get(params.task_id, ())):
            try:
                stream.send_nowait(params)
            except (anyio.WouldBlock, anyio.ClosedResourceError, anyio.BrokenResourceError):
                pass  # dropped pushes are covered by the watcher's backstop poll

    async def _resolve(self, created: CreateTaskResult, ctx: ClaimContext) -> CallToolResult:
        raise RuntimeError("Task handles are tracked by TaskWatcher; use ProviderClient.submit()")

    @asynccontextmanager
    async def listen(
        self, session: ClientSession, task_ids: Sequence[str], *, ack_timeout: float
    ) -> AsyncIterator[TaskSubscription]:
        ids = list(dict.fromkeys(task_ids))
        send, recv = anyio.create_memory_object_stream[TaskNotificationParams](64)
        for task_id in ids:  # registered before the request, so pushes right after the ack are not missed
            self._listeners.setdefault(task_id, set()).add(send)
        request = SubscriptionsListenRequest(
            params=SubscriptionsListenRequestParams(notifications=SubscriptionFilter(taskIds=ids))
        )
        acked, holder = anyio.Event(), {}

        async def drive() -> None:
            try:
                # Returns only when the server ends the stream; cancelling it closes the subscription.
                await session.send_request(request, SubscriptionsListenResult)
            except Exception as exc:
                holder["error"] = exc
            finally:
                acked.set()
                send.close()  # ends the caller's `async for` over events

        accepted: frozenset[str] | None = None
        body_error: Exception | None = None
        try:
            async with anyio.create_task_group() as tg:
                async with self._ack_lock:
                    self._pending_ack = (acked, holder)
                    tg.start_soon(drive)
                    try:
                        with anyio.move_on_after(ack_timeout):
                            await acked.wait()
                    finally:
                        self._pending_ack = None
                accepted = holder.get("accepted")
                if accepted is not None:
                    try:
                        yield TaskSubscription(accepted, recv)
                    except Exception as exc:  # re-raised after the task group so callers see it unwrapped
                        body_error = exc
                tg.cancel_scope.cancel()
        finally:
            for task_id in ids:
                listeners = self._listeners.get(task_id)
                if listeners is not None:
                    listeners.discard(send)
                    if not listeners:
                        del self._listeners[task_id]
            send.close()
            recv.close()
        if body_error is not None:
            raise body_error
        if accepted is None:
            error = holder.get("error")
            raise ListenFailed(describe_error(error) if error else f"No acknowledgement within {ack_timeout}s")


class ProviderClient:
    """An authenticated connection to one provider MCP server.

    Enter and exit it in the same asyncio task (the SDK uses anyio cancel scopes).
    """

    def __init__(self, url: str, api_key: str, *, ack_timeout: float = 10.0) -> None:
        self.url = url
        self._api_key = api_key
        self._ack_timeout = ack_timeout
        self._extension = TasksClientExtension()
        self._stack: AsyncExitStack | None = None
        self._client: Client | None = None

    async def __aenter__(self) -> ProviderClient:
        stack = AsyncExitStack()
        try:
            # trust_env=False: never route provider calls through the Windows system proxy (design §12).
            http = await stack.enter_async_context(
                httpx2.AsyncClient(
                    headers={"Authorization": f"Bearer {self._api_key}"},
                    timeout=httpx2.Timeout(30.0, read=300.0),
                    trust_env=False,
                )
            )
            self._client = await stack.enter_async_context(
                Client(
                    streamable_http_client(self.url, http_client=http),
                    extensions=[self._extension],
                    message_handler=self._extension.on_message,
                )
            )
        except BaseException as exc:
            with anyio.CancelScope(shield=True):
                await stack.aclose()
            if isinstance(exc, Exception):
                raise ProviderUnavailable(f"{self.url}: {describe_error(exc)}") from exc
            raise
        self._stack = stack
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        stack, self._stack, self._client = self._stack, None, None
        if stack is not None:
            # Not forwarding exc_info: the SDK's task groups would wrap the caller's exception in groups.
            await stack.aclose()

    @property
    def _connected(self) -> Client:
        if self._client is None:
            raise RuntimeError("ProviderClient must be used as an async context manager")
        return self._client

    @property
    def protocol_version(self) -> str:
        return self._connected.protocol_version

    @property
    def server_capabilities(self) -> ServerCapabilities:
        return self._connected.server_capabilities

    @property
    def supports_tasks(self) -> bool:
        return TASKS_EXTENSION in (self.server_capabilities.extensions or {})

    async def list_tools(self) -> list[dict[str, Any]]:
        tools: list[dict[str, Any]] = []
        cursor: str | None = None
        while True:
            page = await self._connected.list_tools(cursor=cursor)
            tools.extend(dump_model(tool) for tool in page.tools)
            cursor = page.next_cursor
            if cursor is None:
                return tools

    async def submit(self, tool: str, arguments: dict[str, Any]) -> CreateTaskResult | dict[str, Any]:
        """Call an entry tool: a task handle, or the CallToolResult (wire JSON) when answered directly."""
        result = await self._connected.session.call_tool(tool, arguments, allow_claimed=True)
        return result if isinstance(result, CreateTaskResult) else dump_model(result)

    async def get_task(self, task_id: str) -> TaskSnapshot:
        return await self._connected.session.send_request(
            GetTaskRequest(params=TaskIdParams(task_id=task_id)), TaskSnapshot
        )

    async def cancel_task(self, task_id: str) -> None:
        await self._connected.session.send_request(
            CancelTaskRequest(params=TaskIdParams(task_id=task_id)), EmptyResult
        )

    @asynccontextmanager
    async def listen(self, task_ids: Sequence[str]) -> AsyncIterator[TaskSubscription]:
        async with self._extension.listen(
            self._connected.session, task_ids, ack_timeout=self._ack_timeout
        ) as subscription:
            yield subscription
