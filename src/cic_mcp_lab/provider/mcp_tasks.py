"""MCP glue for the tasks extension (io.modelcontextprotocol/tasks) on top of TaskStore."""
from __future__ import annotations

from typing import Any, Literal

import anyio
from mcp.server.auth.middleware.auth_context import get_access_token
from mcp.server.context import CallNext, HandlerResult, ServerRequestContext
from mcp.server.mcpserver import Extension, MethodBinding
from mcp.shared.exceptions import MCPError
from mcp.shared.inbound import decode_header_value
from mcp.shared.subscriptions import SUBSCRIPTION_ID_META_KEY
from mcp_types import (
    HEADER_MISMATCH,
    INVALID_PARAMS,
    MISSING_REQUIRED_CLIENT_CAPABILITY,
    CallToolRequestParams,
    Notification,
    RequestParams,
    SubscriptionFilter,
    SubscriptionsAcknowledgedNotification,
    SubscriptionsAcknowledgedNotificationParams,
    SubscriptionsListenRequestParams,
)
from mcp_types.version import MODERN_PROTOCOL_VERSIONS
from pydantic import BaseModel

from .tasks import TaskAlreadyFinishedError, TaskFailure, TaskRecord, TaskStore, UnknownTaskError

TASKS_EXTENSION = "io.modelcontextprotocol/tasks"
_ANONYMOUS = "anonymous"


class TaskIdParams(RequestParams):
    task_id: str  # wire name: taskId


class TasksNotification(Notification[dict[str, Any], Literal["notifications/tasks"]]):
    method: Literal["notifications/tasks"] = "notifications/tasks"
    params: dict[str, Any]


def current_owner() -> str:
    token = get_access_token()
    return token.client_id if token is not None else _ANONYMOUS


def client_declared_tasks(ctx: ServerRequestContext[Any, Any]) -> bool:
    # Handshake-era (2025-11-25) clients validate tools/call results as CallToolResult: never send them a task.
    if ctx.protocol_version not in MODERN_PROTOCOL_VERSIONS:
        return False
    capabilities = ctx.session.client_capabilities
    return bool(capabilities and capabilities.extensions and TASKS_EXTENSION in capabilities.extensions)


def _require_tasks_extension(ctx: ServerRequestContext[Any, Any]) -> None:
    if not client_declared_tasks(ctx):
        raise MCPError(
            MISSING_REQUIRED_CLIENT_CAPABILITY,
            f"Client did not declare required extension {TASKS_EXTENSION!r}",
            data={"requiredCapabilities": {"extensions": {TASKS_EXTENSION: {}}}},
        )


def _check_mcp_name(ctx: ServerRequestContext[Any, Any], task_id: str) -> None:
    request = ctx.request
    if request is None:
        return
    raw = request.headers.get("mcp-name")
    if raw is None or decode_header_value(raw) != task_id:
        raise MCPError(HEADER_MISMATCH, "Mcp-Name header must equal params.taskId")


def _dump(result: HandlerResult) -> dict[str, Any]:
    if isinstance(result, BaseModel):
        return result.model_dump(by_alias=True, mode="json", exclude_none=True)
    return dict(result or {})


class TasksExtension(Extension):
    identifier = TASKS_EXTENSION

    def __init__(self, store: TaskStore, task_tools: set[str]) -> None:
        self._store = store
        self._task_tools = frozenset(task_tools)

    def settings(self) -> dict[str, Any]:
        return {}

    def methods(self) -> list[MethodBinding]:
        return [
            MethodBinding(method="tasks/get", params_type=TaskIdParams, handler=self._tasks_get),
            MethodBinding(method="tasks/cancel", params_type=TaskIdParams, handler=self._tasks_cancel),
        ]

    async def intercept_tool_call(
        self, params: CallToolRequestParams, ctx: ServerRequestContext[Any, Any], call_next: CallNext
    ) -> HandlerResult:
        if params.name not in self._task_tools or not client_declared_tasks(ctx):
            return await call_next(ctx)

        async def work() -> dict[str, Any]:
            try:
                return _dump(await call_next(ctx))
            except MCPError as exc:
                raise TaskFailure(exc.error.model_dump(mode="json", exclude_none=True)) from exc

        record = self._store.create(current_owner(), work)
        return {"resultType": "task", **record.task_fields()}

    async def _tasks_get(self, ctx: ServerRequestContext[Any, Any], params: TaskIdParams) -> dict[str, Any]:
        _require_tasks_extension(ctx)
        _check_mcp_name(ctx, params.task_id)
        return {"resultType": "complete", **self._find(params.task_id).detailed()}

    async def _tasks_cancel(self, ctx: ServerRequestContext[Any, Any], params: TaskIdParams) -> dict[str, Any]:
        _require_tasks_extension(ctx)
        _check_mcp_name(ctx, params.task_id)
        self._find(params.task_id)
        try:
            self._store.cancel(params.task_id, current_owner())
        except TaskAlreadyFinishedError:
            pass  # Cancellation is eventually consistent: a finished task is acknowledged and left unchanged.
        return {"resultType": "complete"}

    def _find(self, task_id: str) -> TaskRecord:
        try:
            return self._store.get(task_id, current_owner())
        except UnknownTaskError:
            raise MCPError(INVALID_PARAMS, f"Unknown taskId {task_id!r}") from None


class TasksListenMiddleware:
    """Serves `subscriptions/listen` requests whose filter carries `taskIds`.

    The SDK's built-in listen handler only knows the core filters; other listens fall through to it.
    """

    def __init__(self, store: TaskStore) -> None:
        self._store = store

    async def __call__(self, ctx: ServerRequestContext[Any, Any], call_next: CallNext) -> HandlerResult:
        if ctx.method != "subscriptions/listen" or ctx.request_id is None:
            return await call_next(ctx)
        raw_filter = (ctx.params or {}).get("notifications")
        if not isinstance(raw_filter, dict) or "taskIds" not in raw_filter:
            return await call_next(ctx)
        _require_tasks_extension(ctx)

        params = SubscriptionsListenRequestParams.model_validate(ctx.params, by_name=False)
        requested = [
            task_id
            for task_id in (params.notifications.model_extra or {}).get("taskIds") or []
            if isinstance(task_id, str)
        ]
        owner = current_owner()
        meta = {SUBSCRIPTION_ID_META_KEY: ctx.request_id}

        async def push(snapshot: dict[str, Any]) -> None:
            await ctx.session.send_notification(
                TasksNotification(params={**snapshot, "_meta": meta}),  # type: ignore[arg-type]
                related_request_id=ctx.request_id,
            )

        send, recv = anyio.create_memory_object_stream[dict[str, Any]](256)
        accepted, unsubscribe = self._store.subscribe(owner, requested, send)
        # Snapshot before any await: tasks finishing later are pushed by the store, so each arrives exactly once.
        already_finished = [
            record.detailed()
            for task_id in accepted
            if (record := self._store.find(task_id, owner)) is not None and record.terminal
        ]
        try:
            await ctx.session.send_notification(
                SubscriptionsAcknowledgedNotification(
                    params=SubscriptionsAcknowledgedNotificationParams(
                        notifications=SubscriptionFilter(taskIds=accepted), _meta=meta
                    )
                ),
                related_request_id=ctx.request_id,
            )
            # Sent directly rather than queued: there can be more of them than the stream buffer holds.
            for snapshot in already_finished:
                await push(snapshot)
            async for snapshot in recv:
                await push(snapshot)
        finally:
            unsubscribe()
            send.close()
            recv.close()
        # A short-circuiting middleware owns the whole result envelope.
        return {"resultType": "complete", "_meta": meta}
