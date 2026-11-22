# Step 2：MCP Server + tasks 扩展（服务端）实施计划

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** 为每个模拟 Provider 启动一个 MCP Server：每个 Agent 对应一个入口 Tool，API Key 鉴权；实现 MCP tasks 扩展的服务端部分：`tools/call` 立即返回任务，支持 `tasks/get`、`tasks/cancel` 和 `subscriptions/listen` 的 taskIds 推送，任务按 TTL 过期。另提供 `provider-check` 命令，在协议层面手工验证。

**Architecture:**
- `provider/tasks.py`：纯逻辑的任务存储 `TaskStore`，负责生命周期、归属、TTL 和推送分发，不依赖 MCP SDK，便于单测。
- `provider/mcp_tasks.py`：MCP 适配层。`Extension` 负责拦截 `tools/call` 并注册 `tasks/get`、`tasks/cancel`；`ServerMiddleware` 负责处理带 `taskIds` 的 `subscriptions/listen`。
- `provider/auth.py`：用 SDK 自带的 `TokenVerifier` 实现 API Key 校验。
- `provider/server.py`：组装上述部分，再加上 `ProviderAgentRuntime`。
- `cic_mcp_lab/mcpwire.py`：极简的原始 JSON-RPC 客户端，供集成测试和 `provider-check` 在协议层面验证。它不经过任何 SDK 客户端代码，相当于一个独立的一致性检查器。

对应设计文档 §3.2、§4.3、§5、§6.8、§11 的第 2 步。

**Tech Stack:** Python 3.12、uv、`mcp` 2.3.0（`MCPServer`、`Extension`、`MethodBinding`、server middleware、`TokenVerifier`/`AuthSettings`）、`mcp_types` 2.3.0、starlette、uvicorn、anyio、httpx（测试与 `provider-check` 使用）、pytest + pytest-asyncio。

**已验证的前提（2026-11-21 spike，代码在会话目录 `files/spike_tasks/`）：**
- **立即返回任务：** `Extension.intercept_tool_call` 返回 `{"resultType": "task", ...}` 字典即可，SDK 不校验也不改写非核心的 `resultType`。把 `call_next(ctx)` 放进脱离请求的 `asyncio.create_task` 中执行，SDK 的参数校验和「工具抛异常 → `isError: true`」转换照常生效；取消该 asyncio task 会向工具函数传入 `CancelledError`。
- **必须自己判断客户端能力和协议版本：** 声明了 tasks 扩展的旧协议（2025-11-25）客户端如果收到任务，会报 "Handler returned an invalid result"。判断依据：`ctx.protocol_version in MODERN_PROTOCOL_VERSIONS`，并且 `ctx.session.client_capabilities.extensions` 中包含 `io.modelcontextprotocol/tasks`。
- **注册新方法：** 用 `MethodBinding(method="tasks/get", params_type=TaskIdParams, handler=...)`，handler 签名为 `async (ctx, params) -> dict`；抛出 `MCPError(code, msg)` 时返回 JSON-RPC 错误。SDK 不校验 `Mcp-Name` 请求头，需要自己从 `ctx.request.headers` 中检查。
- **推送：** SDK 内置的 listen 会丢掉未知的过滤字段，所以用 server middleware 拦截带 `taskIds` 的 `subscriptions/listen`，手工发送订阅确认和 `notifications/tasks`。middleware 被 SDK 标为 provisional（接口可能变动）；不带 `taskIds` 的订阅请求照常交给内置处理。
- **鉴权：** `MCPServer(token_verifier=..., auth=AuthSettings(issuer_url="https://auth.invalid", resource_server_url=None))`。无 key、错 key、`Basic` 认证、`server/discover`、`tools/list` 均返回 401，响应头为 `WWW-Authenticate: Bearer ...`；handler 中可用 `get_access_token().client_id` 识别调用方。
- **工具抛异常：** 普通异常对外只显示 "Error executing tool X"；抛 `ToolError(msg)` 时会带上 msg。在任务模式下，两种情况都是任务 `completed` 且 `result.isError: true`。
- **长连接空闲：** 服务端每 15 秒发送一次 SSE `: ping`；客户端读超时必须大于 15 秒。实测 70 秒的任务推送正常到达。

**约定：**
- 所有命令都在 `D:\develop\accentrixProject\cic-mcp-lab` 下执行。
- 通过管道传中文前，先执行 `$OutputEncoding = New-Object System.Text.UTF8Encoding $false; $env:PYTHONUTF8='1'`。
- 每次提交附带 `Co-authored-by: Copilot <223556219+Copilot@users.noreply.github.com>`。
- `docs/` 已被 gitignore，本计划不提交。
- 除 Task 6 的手工检查外，不调用真实模型；测试中使用假的 runtime。

---

### Task 1: 任务存储 `provider/tasks.py`

**Files:**
- Create: `src/cic_mcp_lab/provider/tasks.py`
- Test: `tests/test_provider_tasks.py`

**Step 1: 写失败的测试**

```python
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
```

**Step 2: 运行，确认失败**

Run: `uv run pytest tests/test_provider_tasks.py -q`
Expected: FAIL，`ModuleNotFoundError: No module named 'cic_mcp_lab.provider.tasks'`

**Step 3: 最小实现**

```python
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
            if not record.terminal:
                self._transition(record, "cancelled", status_message="Cancelled by server")
            raise
        except TaskFailure as exc:
            self._transition(record, "failed", error=exc.error)
        except Exception as exc:  # safety net: tool exceptions normally arrive as isError results
            self._transition(record, "failed", error={"code": _INTERNAL_ERROR, "message": str(exc)})
        else:
            if not record.terminal:  # cancelled while the work was finishing
                self._transition(record, "completed", result=result)

    def _transition(self, record: TaskRecord, status: TaskStatus, **changes: Any) -> None:
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
                del self._tasks[task_id]
                self._listeners.pop(task_id, None)
                if record.runner is not None and not record.runner.done():
                    record.runner.cancel()
```

**Step 4: 运行，确认通过**

Run: `uv run pytest tests/test_provider_tasks.py -q` → `7 passed`；`uv run pytest -q` → `31 passed`

**Step 5: Commit**

```powershell
git add src/cic_mcp_lab/provider/tasks.py tests/test_provider_tasks.py
git commit -m "feat: add server-side task store for the MCP tasks extension" -m "Co-authored-by: Copilot <223556219+Copilot@users.noreply.github.com>"
```

---

### Task 2: 协议层客户端 `mcpwire.py`

**Files:**
- Create: `src/cic_mcp_lab/mcpwire.py`

这是集成测试和 `provider-check` 的基础设施，由 Task 3 和 Task 4 的集成测试间接覆盖，本身不单独写测试。

**Step 1: 实现**

```python
"""Minimal raw JSON-RPC client for MCP 2026-07-28 over Streamable HTTP (wire-level checks)."""
from __future__ import annotations

import itertools
import json
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any

import httpx

PROTOCOL_VERSION = "2026-07-28"
TASKS_EXTENSION = "io.modelcontextprotocol/tasks"


@dataclass(frozen=True)
class WireResponse:
    status_code: int
    headers: httpx.Headers
    body: dict[str, Any] | None

    @property
    def result(self) -> dict[str, Any]:
        if not self.body or "result" not in self.body:
            raise RuntimeError(f"No JSON-RPC result (HTTP {self.status_code}): {self.body}")
        return self.body["result"]

    @property
    def error(self) -> dict[str, Any] | None:
        return (self.body or {}).get("error")


class WireClient:
    def __init__(self, url: str, api_key: str | None) -> None:
        self._url = url
        self._api_key = api_key
        # The server pings listen streams every 15 s, so the read timeout must be well above that.
        self._http = httpx.Client(timeout=httpx.Timeout(30.0, read=300.0))
        self._ids = itertools.count(1)

    def __enter__(self) -> WireClient:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self._http.close()

    def request(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        *,
        tasks: bool = True,
        name: str | None = None,
    ) -> WireResponse:
        resp = self._http.post(
            self._url,
            json=self._payload(method, params or {}, tasks),
            headers=self._headers(method, name),
        )
        return WireResponse(resp.status_code, resp.headers, _parse_body(resp))

    def call_tool(self, tool: str, arguments: dict[str, Any], *, tasks: bool = True) -> WireResponse:
        return self.request("tools/call", {"name": tool, "arguments": arguments}, tasks=tasks, name=tool)

    def get_task(self, task_id: str) -> WireResponse:
        return self.request("tasks/get", {"taskId": task_id}, name=task_id)

    def cancel_task(self, task_id: str) -> WireResponse:
        return self.request("tasks/cancel", {"taskId": task_id}, name=task_id)

    @contextmanager
    def listen_tasks(self, task_ids: list[str]) -> Iterator[Iterator[dict[str, Any]]]:
        """Open a `subscriptions/listen` stream for task ids; yields an iterator of JSON-RPC messages."""
        method = "subscriptions/listen"
        payload = self._payload(method, {"notifications": {"taskIds": task_ids}}, True)
        with self._http.stream("POST", self._url, json=payload, headers=self._headers(method, None)) as resp:
            resp.raise_for_status()
            yield (json.loads(line[5:]) for line in resp.iter_lines() if line.startswith("data:"))

    def _headers(self, method: str, name: str | None) -> dict[str, str]:
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            "MCP-Protocol-Version": PROTOCOL_VERSION,
            "Mcp-Method": method,
        }
        if self._api_key is not None:
            headers["Authorization"] = f"Bearer {self._api_key}"
        if name is not None:
            headers["Mcp-Name"] = name
        return headers

    def _payload(self, method: str, params: dict[str, Any], tasks: bool) -> dict[str, Any]:
        capabilities = {"extensions": {TASKS_EXTENSION: {}}} if tasks else {}
        meta = {
            "io.modelcontextprotocol/protocolVersion": PROTOCOL_VERSION,
            "io.modelcontextprotocol/clientCapabilities": capabilities,
            "io.modelcontextprotocol/clientInfo": {"name": "cic-mcp-lab-wire", "version": "0"},
        }
        return {"jsonrpc": "2.0", "id": next(self._ids), "method": method, "params": {**params, "_meta": meta}}


def _parse_body(resp: httpx.Response) -> dict[str, Any] | None:
    content_type = resp.headers.get("content-type", "")
    if content_type.startswith("text/event-stream"):
        frames = [json.loads(line[5:]) for line in resp.text.splitlines() if line.startswith("data:")]
        responses = [frame for frame in frames if "id" in frame and ("result" in frame or "error" in frame)]
        return responses[-1] if responses else None
    if content_type.startswith("application/json"):
        return resp.json()
    return None
```

**Step 2: 导入检查**

Run: `uv run python -c "import cic_mcp_lab.mcpwire"` → 无输出、无报错

**Step 3: Commit**

```powershell
git add src/cic_mcp_lab/mcpwire.py
git commit -m "feat: add raw MCP wire client for protocol checks" -m "Co-authored-by: Copilot <223556219+Copilot@users.noreply.github.com>"
```

---

### Task 3: 鉴权 + 入口 Tool（暂不含任务）

**Files:**
- Create: `src/cic_mcp_lab/provider/auth.py`
- Create: `src/cic_mcp_lab/provider/server.py`
- Test: `tests/test_provider_server.py`

**Step 1: 写失败的测试** — `tests/test_provider_server.py`：

```python
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
```

**Step 2: 运行，确认失败**

Run: `uv run pytest tests/test_provider_server.py -q`
Expected: FAIL，`ModuleNotFoundError: No module named 'cic_mcp_lab.provider.server'`

**Step 3: 实现**

`src/cic_mcp_lab/provider/auth.py`：

```python
"""API-key authentication for the provider MCP servers (SDK TokenVerifier)."""
from __future__ import annotations

import hmac

from mcp.server.auth.provider import AccessToken


class ApiKeyVerifier:
    """Accepts `Authorization: Bearer <key>` for a static key → caller-id table."""

    def __init__(self, keys: dict[str, str]) -> None:
        self._keys = keys

    async def verify_token(self, token: str) -> AccessToken | None:
        for key, client_id in self._keys.items():
            if hmac.compare_digest(key.encode(), token.encode()):
                return AccessToken(token=token, client_id=client_id, scopes=[])
        return None
```

`src/cic_mcp_lab/provider/server.py`：

```python
"""MCP Server for one mock provider: one entry tool per agent, API-key auth."""
from __future__ import annotations

import asyncio
from collections.abc import Sequence
from typing import Annotated, Any, Protocol

from mcp.server.auth.settings import AuthSettings
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp_types import CallToolResult, TextContent
from pydantic import Field
from starlette.types import ASGIApp

from .agent import AgentResult, ImagesNotSupportedError, InvalidImageUrlError, UnknownConversationError
from .auth import ApiKeyVerifier
from .catalog import CATALOG, AgentSpec

CREDIT_USAGE_META_KEY = "com.cic/credit_usage"
CIC_CALLER_ID = "cic"
# Errors the caller can act on; their message is returned instead of the SDK's generic text.
_CALLER_ERRORS = (UnknownConversationError, ImagesNotSupportedError, InvalidImageUrlError)

Message = Annotated[str, Field(description="用户本轮输入的原话")]
ConversationId = Annotated[
    str | None, Field(description="开启新会话时省略；继续会话时传入上次返回的 conversation_id")
]
ImageUrls = Annotated[list[str] | None, Field(description="图片的 https 公网地址")]


class AgentRuntime(Protocol):
    async def ask(
        self,
        agent_id: str,
        message: str,
        image_urls: Sequence[str] = (),
        conversation_id: str | None = None,
    ) -> AgentResult: ...


def provider_specs(provider_id: str) -> dict[str, AgentSpec]:
    specs = {spec.agent_id: spec for spec in CATALOG.values() if spec.provider_id == provider_id}
    if not specs:
        raise ValueError(f"No agents for provider {provider_id}")
    return specs


def to_call_tool_result(result: AgentResult) -> CallToolResult:
    return CallToolResult(
        content=[TextContent(type="text", text=result.reply)],
        structured_content={
            "conversation_id": result.conversation_id,
            "status": result.status,
            "reply": result.reply,
        },
        meta={CREDIT_USAGE_META_KEY: result.credit_usage},
    )


def _make_tool(spec: AgentSpec, runtime: AgentRuntime, delay_seconds: float) -> Any:
    async def run(message: str, image_urls: list[str] | None, conversation_id: str | None) -> CallToolResult:
        if delay_seconds:
            await asyncio.sleep(delay_seconds)  # simulates a long-running agent
        try:
            result = await runtime.ask(spec.agent_id, message, image_urls or (), conversation_id)
        except _CALLER_ERRORS as exc:
            raise ToolError(str(exc)) from exc
        return to_call_tool_result(result)

    if spec.supports_image:

        async def tool(
            message: Message, image_urls: ImageUrls = None, conversation_id: ConversationId = None
        ) -> CallToolResult:
            return await run(message, image_urls, conversation_id)

    else:

        async def tool(message: Message, conversation_id: ConversationId = None) -> CallToolResult:
            return await run(message, None, conversation_id)

    return tool


def build_app(
    *,
    provider_id: str,
    api_key: str,
    runtime: AgentRuntime,
    delay_seconds: float = 0.0,
) -> ASGIApp:
    specs = provider_specs(provider_id)
    mcp = MCPServer(
        f"cic-lab-{provider_id}",
        token_verifier=ApiKeyVerifier({api_key: CIC_CALLER_ID}),
        # API-key auth only: AuthSettings requires an issuer URL, which is never contacted.
        auth=AuthSettings(issuer_url="https://auth.invalid", resource_server_url=None),
    )
    for spec in specs.values():
        mcp.add_tool(
            _make_tool(spec, runtime, delay_seconds),
            name=spec.agent_id,
            title=spec.display_name,
            description=spec.description,
        )
    return mcp.streamable_http_app(host="127.0.0.1")
```

**Step 4: 运行，确认通过**

Run: `uv run pytest tests/test_provider_server.py -q` → `4 passed`；`uv run pytest -q` → `35 passed`

如果 `schema["required"]` 断言失败，原因是 SDK 生成的 schema 与预期不同（例如 `Annotated` 的写法不被支持）。这时应报告实际生成的 schema，不要修改断言来迁就实现。

**Step 5: Commit**

```powershell
git add src/cic_mcp_lab/provider/auth.py src/cic_mcp_lab/provider/server.py tests/test_provider_server.py
git commit -m "feat: serve provider agents as MCP tools behind API-key auth" -m "Co-authored-by: Copilot <223556219+Copilot@users.noreply.github.com>"
```

---

### Task 4: tasks 扩展接入 MCP（任务、查询、取消、推送）

**Files:**
- Create: `src/cic_mcp_lab/provider/mcp_tasks.py`
- Modify: `src/cic_mcp_lab/provider/server.py`（`build_app`）
- Modify: `tests/test_provider_server.py`（追加测试）

**Step 1: 追加失败的测试**（加在 `tests/test_provider_server.py` 末尾，并把 `from cic_mcp_lab.mcpwire import WireClient` 改为 `from cic_mcp_lab.mcpwire import TASKS_EXTENSION, WireClient`）：

```python
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
            pushed = next(
                message["params"]
                for message in messages
                if message.get("method") == "notifications/tasks" and message["params"]["status"] == "completed"
            )
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
        assert client.cancel_task(task_id).error["code"] == -32602

    assert runtime.cancelled.wait(timeout=5)


def test_task_requests_must_name_the_task_in_the_mcp_name_header(start_server):
    url, _ = start_server()
    with WireClient(url, KEY) as client:
        task_id = client.call_tool("trip_planner", {"message": "hello"}).result["taskId"]
        resp = client.request("tasks/get", {"taskId": task_id})

    assert resp.error["code"] == -32020


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
```

**Step 2: 运行，确认失败**

Run: `uv run pytest tests/test_provider_server.py -q`
Expected：新增的 7 个测试失败（`build_app` 不接受 `poll_interval_ms` / `push_enabled` / `ttl_ms` 参数，`server/discover` 中也没有 tasks 扩展）；Task 3 的 4 个测试仍然通过。

**Step 3: 实现**

`src/cic_mcp_lab/provider/mcp_tasks.py`：

```python
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
        _check_mcp_name(ctx, params.task_id)
        return {"resultType": "complete", **self._find(params.task_id).detailed()}

    async def _tasks_cancel(self, ctx: ServerRequestContext[Any, Any], params: TaskIdParams) -> dict[str, Any]:
        _check_mcp_name(ctx, params.task_id)
        self._find(params.task_id)
        try:
            self._store.cancel(params.task_id, current_owner())
        except TaskAlreadyFinishedError as exc:
            raise MCPError(INVALID_PARAMS, str(exc)) from None
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

        params = SubscriptionsListenRequestParams.model_validate(ctx.params, by_name=False)
        requested = [
            task_id
            for task_id in (params.notifications.model_extra or {}).get("taskIds") or []
            if isinstance(task_id, str)
        ]
        owner = current_owner()
        meta = {SUBSCRIPTION_ID_META_KEY: ctx.request_id}
        send, recv = anyio.create_memory_object_stream[dict[str, Any]](256)
        accepted, unsubscribe = self._store.subscribe(owner, requested, send)
        try:
            await ctx.session.send_notification(
                SubscriptionsAcknowledgedNotification(
                    params=SubscriptionsAcknowledgedNotificationParams(
                        notifications=SubscriptionFilter(taskIds=accepted), _meta=meta
                    )
                ),
                related_request_id=ctx.request_id,
            )
            # Tasks that finished before the listen arrived: push their final state right away.
            for task_id in accepted:
                record = self._store.find(task_id, owner)
                if record is not None and record.terminal:
                    send.send_nowait(record.detailed())
            async for snapshot in recv:
                await ctx.session.send_notification(
                    TasksNotification(params={**snapshot, "_meta": meta}),  # type: ignore[arg-type]
                    related_request_id=ctx.request_id,
                )
        finally:
            unsubscribe()
            send.close()
            recv.close()
        # A short-circuiting middleware owns the whole result envelope.
        return {"resultType": "complete", "_meta": meta}
```

`src/cic_mcp_lab/provider/server.py`：在 import 区加入

```python
from .mcp_tasks import TasksExtension, TasksListenMiddleware
from .tasks import TaskStore
```

并把 `build_app` 整体替换为：

```python
def build_app(
    *,
    provider_id: str,
    api_key: str,
    runtime: AgentRuntime,
    delay_seconds: float = 0.0,
    push_enabled: bool = True,
    ttl_ms: int = 3_600_000,
    poll_interval_ms: int = 2_000,
) -> ASGIApp:
    specs = provider_specs(provider_id)
    store = TaskStore(ttl_ms=ttl_ms, poll_interval_ms=poll_interval_ms, push_enabled=push_enabled)
    mcp = MCPServer(
        f"cic-lab-{provider_id}",
        extensions=[TasksExtension(store, task_tools=set(specs))],
        middleware=[TasksListenMiddleware(store)],
        token_verifier=ApiKeyVerifier({api_key: CIC_CALLER_ID}),
        # API-key auth only: AuthSettings requires an issuer URL, which is never contacted.
        auth=AuthSettings(issuer_url="https://auth.invalid", resource_server_url=None),
    )
    for spec in specs.values():
        mcp.add_tool(
            _make_tool(spec, runtime, delay_seconds),
            name=spec.agent_id,
            title=spec.display_name,
            description=spec.description,
        )
    return mcp.streamable_http_app(host="127.0.0.1")
```

同时把模块 docstring 改为 `"""MCP Server for one mock provider: one entry tool per agent, tasks extension, API-key auth."""`。

**Step 4: 运行，确认通过**

Run: `uv run pytest tests/test_provider_server.py -q` → `11 passed`；`uv run pytest -q` → `42 passed`。整个测试文件应在 15 秒内跑完，不能卡住。

**Step 5: Commit**

```powershell
git add src/cic_mcp_lab/provider/mcp_tasks.py src/cic_mcp_lab/provider/server.py tests/test_provider_server.py
git commit -m "feat: implement the MCP tasks extension on provider servers" -m "Co-authored-by: Copilot <223556219+Copilot@users.noreply.github.com>"
```

---

### Task 5: 命令行入口 `provider-server` 与 `provider-check`

**Files:**
- Modify: `src/cic_mcp_lab/provider/server.py`（追加 `main`）
- Create: `src/cic_mcp_lab/provider/check.py`
- Modify: `pyproject.toml`、`.env.example`、`README.md`

这两个命令是需要真实网络的手工验证入口，不写单元测试；协议行为已由 Task 4 的集成测试覆盖。

**Step 1: `server.py` 追加 `main`**

在 import 区加入 `import argparse`、`import logging`、`import os`、`import uvicorn`，以及：

```python
from ..config import load_settings
from ..llm import make_chat_model
from .agent import ProviderAgentRuntime, make_agent_factory
```

文件末尾追加：

```python
_API_KEY_ENV = {"A": "PROVIDER_A_API_KEY", "B": "PROVIDER_B_API_KEY"}


def main() -> None:
    parser = argparse.ArgumentParser(description="启动模拟 Provider 的 MCP Server")
    parser.add_argument("--provider", choices=sorted(_API_KEY_ENV), required=True)
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--delay", type=float, default=0.0, help="每轮调用 Agent 前额外等待的秒数（模拟长任务）")
    parser.add_argument("--no-push", action="store_true", help="不接受 taskIds 订阅（模拟不支持推送的 Provider）")
    args = parser.parse_args()

    settings = load_settings()
    key_env = _API_KEY_ENV[args.provider]
    api_key = os.environ.get(key_env, "").strip()
    if not api_key:
        raise SystemExit(f"Missing {key_env} in .env")
    provider_id = f"provider-{args.provider.lower()}"
    runtime = ProviderAgentRuntime(
        provider_specs(provider_id),
        make_agent_factory(make_chat_model(settings, settings.provider_model)),
    )
    app = build_app(
        provider_id=provider_id,
        api_key=api_key,
        runtime=runtime,
        delay_seconds=args.delay,
        push_enabled=not args.no_push,
        ttl_ms=int(os.environ.get("PROVIDER_TASK_TTL_MS", "3600000")),
        poll_interval_ms=int(os.environ.get("PROVIDER_POLL_INTERVAL_MS", "2000")),
    )
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    uvicorn.run(app, host="127.0.0.1", port=args.port, log_level="info")
```

**Step 2: `src/cic_mcp_lab/provider/check.py`**

```python
"""Wire-level manual check of one provider MCP server (step 2 checkpoint)."""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from typing import Any

from ..config import load_settings
from ..mcpwire import TASKS_EXTENSION, WireClient
from .tasks import TERMINAL_STATUSES


def _wait_for_task(client: WireClient, task_id: str) -> dict[str, Any]:
    with client.listen_tasks([task_id]) as messages:
        accepted = next(messages)["params"]["notifications"].get("taskIds", [])
        print(f"[listen] 订阅确认 taskIds={accepted}")
        if task_id in accepted:
            for message in messages:
                params = message.get("params", {})
                if message.get("method") == "notifications/tasks" and params.get("status") in TERMINAL_STATUSES:
                    print("[push] 收到任务结束推送")
                    return params
    print("[poll] 未通过推送拿到结果，改为轮询 tasks/get")
    while True:
        task = client.get_task(task_id).result
        if task["status"] in TERMINAL_STATUSES:
            return task
        time.sleep(task["pollIntervalMs"] / 1000)


def _run(args: argparse.Namespace, api_key: str) -> bool:
    url = f"http://127.0.0.1:{args.port}/mcp"
    with WireClient(url, None) as anonymous:
        status = anonymous.request("server/discover").status_code
    print(f"[auth] 不带 key：HTTP {status}")
    if status != 401:
        return False

    with WireClient(url, api_key) as client:
        discover = client.request("server/discover")
        if discover.status_code != 200:
            print(f"[auth] key 被拒绝：HTTP {discover.status_code}")
            return False
        extensions = discover.result["capabilities"].get("extensions", {})
        print(f"[discover] 声明 tasks 扩展：{TASKS_EXTENSION in extensions}")
        print(f"[tools] {[tool['name'] for tool in client.request('tools/list').result['tools']]}")

        arguments: dict[str, Any] = {"message": args.message}
        if args.conversation_id:
            arguments["conversation_id"] = args.conversation_id
        if args.image:
            arguments["image_urls"] = args.image
        started = time.monotonic()
        created = client.call_tool(args.agent, arguments)
        if created.error:
            print(f"[call] JSON-RPC error：{created.error}")
            return False
        result = created.result
        print(f"[call] {time.monotonic() - started:.2f}s 返回 resultType={result['resultType']}")
        if result["resultType"] == "task":
            print(f"[task] taskId={result['taskId']} pollIntervalMs={result['pollIntervalMs']} ttlMs={result['ttlMs']}")
            task = _wait_for_task(client, result["taskId"])
            print(f"[task] status={task['status']}，从提交起共 {time.monotonic() - started:.1f}s")
            if task["status"] != "completed":
                print(json.dumps(task, ensure_ascii=False, indent=2))
                return False
            again = client.get_task(result["taskId"]).result
            print(f"[task] tasks/get 与推送/轮询结果一致：{again.get('result') == task['result']}")
            tool_result = task["result"]
        else:
            tool_result = result

    print(json.dumps(tool_result.get("structuredContent"), ensure_ascii=False, indent=2))
    print(f"[credit] com.cic/credit_usage={tool_result.get('_meta', {}).get('com.cic/credit_usage')}")
    return not tool_result.get("isError", False)


def main() -> None:
    parser = argparse.ArgumentParser(description="在协议层面检查一个 Provider MCP Server")
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--key-env", required=True, help="保存 API Key 的环境变量名，如 PROVIDER_A_API_KEY")
    parser.add_argument("--agent", required=True)
    parser.add_argument("--message", required=True)
    parser.add_argument("--conversation-id")
    parser.add_argument("--image", action="append", help="https 图片地址，可重复")
    args = parser.parse_args()
    load_settings()  # loads .env into the process environment
    api_key = os.environ.get(args.key_env, "").strip()
    if not api_key:
        raise SystemExit(f"Missing {args.key_env} in .env")
    sys.exit(0 if _run(args, api_key) else 1)
```

**Step 3: 配置与文档**

- `pyproject.toml` 的 `[project.scripts]` 追加：

```toml
provider-server = "cic_mcp_lab.provider.server:main"
provider-check = "cic_mcp_lab.provider.check:main"
```

- `.env.example` 末尾追加：

```dotenv
# PROVIDER_TASK_TTL_MS=3600000
# PROVIDER_POLL_INTERVAL_MS=2000
```

- `README.md` 的命令表在 `provider-agent` 行之后插入两行：

```markdown
| `uv run provider-server --provider <A\|B> --port <端口> [--delay 秒] [--no-push]` | 启动模拟 Provider 的 MCP Server（API Key 鉴权，tasks 扩展） |
| `uv run provider-check --port <端口> --key-env <变量名> --agent <id> --message <文本>` | 在协议层面检查一个 Provider MCP Server：鉴权、任务、推送或轮询、credit_usage |
```

**Step 4: 验证（不调用模型）**

- `uv run provider-server --help`、`uv run provider-check --help` 正常显示帮助；
- `uv run pytest -q` → `42 passed`。

**Step 5: Commit**

```powershell
git add src/cic_mcp_lab/provider/server.py src/cic_mcp_lab/provider/check.py pyproject.toml .env.example README.md
git commit -m "feat: add provider-server and provider-check commands" -m "Co-authored-by: Copilot <223556219+Copilot@users.noreply.github.com>"
```

---

### Task 6: 手工检查点（真实模型，由用户执行）

终端 1：`uv run provider-server --provider A --port 8001`
终端 2：`uv run provider-server --provider B --port 8002 --no-push`
终端 3 依次执行：

1. `uv run provider-check --port 8001 --key-env PROVIDER_A_API_KEY --agent trip_planner --message "帮我规划旅行"`
   - 期望：不带 key 时返回 401；声明了 tasks 扩展；tools 为 `trip_planner`、`product_copy`；`tools/call` 在 1 秒内返回 task；订阅确认包含该 taskId；收到推送；`status=input_required`；有 credit_usage。
2. 复制上一步的 conversation_id，追加 `--conversation-id <id> --message "去广州，明天出发玩2天，预算2000"`
   - 期望：同一会话返回 `completed`，说明第三方上下文是连续的。
3. `uv run provider-check --port 8002 --key-env PROVIDER_A_API_KEY --agent weekly_report --message 写周报`
   - 期望：`[auth] key 被拒绝：HTTP 401`，即 A 的 key 访问不了 B。
4. `uv run provider-check --port 8002 --key-env PROVIDER_B_API_KEY --agent weekly_report --message 写周报`
   - 期望：订阅确认 `taskIds=[]`，改为轮询，最终拿到结果和 credit_usage。
5. 终端 1 改为 `uv run provider-server --provider A --port 8001 --delay 90` 重启，再执行第 1 条
   - 期望：`tools/call` 仍在 1 秒内返回；约 90 秒后通过推送拿到结果（期间连接不断开）。

> ✅ **第 2 步检查点**：用户确认以上结果后，再编写第 3 步（CIC 侧 MCP Client + 任务监听 + `cic-probe`）的计划。
