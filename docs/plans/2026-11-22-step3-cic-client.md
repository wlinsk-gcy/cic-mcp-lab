# Step 3：CIC 侧 MCP Client、任务监听与测试连接 实施计划

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** 实现 CIC 平台侧与第三方 MCP Server 交互的全部能力：
- Agent 入驻数据（registry）；
- 入口 Tool 契约校验；
- 基于 `mcp` SDK 的 Provider 客户端（tasks 扩展）；
- 任务监听组件 TaskWatcher：提交、订阅推送、补查、轮询兜底、TTL、按 taskId 只结算一次；
- 测试连接 `cic-probe`；
- 手工调用命令 `cic-call`。

**Architecture:**
- `cic/registry.py`：读取 `data/agents.yaml`。
- `cic/contract.py`：纯函数校验 inputSchema 与 CallToolResult。
- `cic/mcp_client.py`：`ProviderClient` 包装 SDK `Client`，配合自定义 `TasksClientExtension`，负责声明 tasks 能力、接收任务句柄、分发 `notifications/tasks` 推送、打开 `taskIds` 订阅。
- `cic/ledger.py`：账本。
- `cic/task_watcher.py`：每个任务一个 asyncio 协程，按设计文档 §6.8.2 跟踪到终态，结果放入 `asyncio.Queue`。
- `cic/probe.py` / `cic/call.py`：两个命令行入口。

实验阶段全部状态存在内存中；生产环境的形态见设计文档 §13。

**Tech Stack:** Python 3.12、uv、`mcp` 2.3.0（`Client`、`ClientExtension`、`ResultClaim`、`NotificationBinding`、`ClientSession.send_request`、`streamable_http_client`）、`httpx2`（SDK 自带的 HTTP 库）、anyio、PyYAML、pytest + pytest-asyncio。

**已验证的前提（2026-11-21 spike，代码在会话目录 `files/spike_tasks/client.py`）：**
- `Client(transport, extensions=[ext], message_handler=ext.on_message)`：`ext` 是 `ClientExtension` 子类，`identifier = "io.modelcontextprotocol/tasks"`，SDK 会在每个请求的 `_meta` 中声明该能力。
- 必须提供 `ResultClaim(result_type="task", model=CreateTaskResult, resolve=...)`，否则收到 `resultType: "task"` 时会报 ValidationError。`session.call_tool(..., allow_claimed=True)` 直接返回 `CreateTaskResult`。
- `notifications/tasks` 通过 `NotificationBinding` 接收。订阅确认是核心通知，会进入 `message_handler`；确认里没有能对应到具体订阅的 id，所以同一时间只能有一个订阅在等待确认。
- 自定义 `Request` 子类声明 `name_param = "taskId"` 后，`session.send_request` 会自动带上 `Mcp-Name` 请求头。
- `subscriptions/listen` 需要自己用 `session.send_request(SubscriptionsListenRequest(...), SubscriptionsListenResult)` 在 task group 中发送；取消 task group 即关闭订阅。
- SDK 使用 `httpx2`；`create_mcp_http_client` 不接受 `trust_env`，所以直接构造 `httpx2.AsyncClient(trust_env=False, timeout=Timeout(30, read=300))`，避免本地请求走 Windows 系统代理（设计文档 §12）。
- 连接时如果 key 错误或服务器不可达，SDK 抛出 `ExceptionGroup`；`MCPError` 有 `.code` 和 `.message`。

**约定：**
- 所有命令都在 `D:\develop\accentrixProject\cic-mcp-lab` 下执行。
- 每次提交附带 `Co-authored-by: Copilot <223556219+Copilot@users.noreply.github.com>`。
- `docs/` 已被 gitignore，本计划不提交；`img.png`、`test_result.md`、`step2_test_result.md` 属于用户，不要动。
- 除 Task 8 的手工检查外，不调用真实模型；测试使用 `tests/helpers.py` 中的 FakeRuntime。
- **同任务约束**：`ProviderClient` 和 `TaskWatcher` 内部有 anyio 的 cancel scope，必须在同一个 asyncio task 里进入和退出（CLI 和测试中天然满足）。

---

### Task 1: 测试辅助模块抽取 + Provider 支持「不走任务」

**Files:**
- Create: `tests/helpers.py`、`tests/conftest.py`
- Modify: `tests/test_provider_server.py`、`src/cic_mcp_lab/provider/server.py`

**目的：** 第 3 步的测试也需要启动模拟 Provider，所以把 `FakeRuntime`、`running_server` 和 `start_server` fixture 移到公共位置。同时加两项能力：
- `FakeRuntime` 支持 `mcp-error`，模拟 JSON-RPC 错误，使任务进入 failed；
- `build_app(tasks_enabled=False)` 加上 `provider-server --no-tasks`，用来验证「直接返回结果」的 Provider。

**Step 1: `tests/helpers.py`**

```python
"""Shared test helpers: a fake agent runtime and a local uvicorn server runner."""
from __future__ import annotations

import asyncio
import socket
import threading
import time
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from typing import Any

import uvicorn
from mcp.shared.exceptions import MCPError

from cic_mcp_lab.provider.agent import AgentResult, UnknownConversationError

TEST_API_KEY = "sk-test-provider-a"


class FakeRuntime:
    """Echo agent.

    'sleep=N;text' waits N seconds; 'unknown-conversation' raises a caller error;
    'mcp-error' raises a JSON-RPC error, which ends a task as failed.
    """

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
        if message == "mcp-error":
            raise MCPError(-32603, "provider backend unavailable")
        return AgentResult(conversation_id or "conv_test", "input_required", f"echo: {message}", 42)


def unused_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@contextmanager
def running_server(app: Any) -> Iterator[str]:
    port = unused_port()
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
```

**Step 2: `tests/conftest.py`**

```python
from contextlib import ExitStack

import pytest

from cic_mcp_lab.provider.server import build_app
from tests.helpers import TEST_API_KEY, FakeRuntime, running_server


@pytest.fixture
def start_server():
    """Start provider-a's MCP server with a fake agent runtime; returns (url, runtime)."""
    with ExitStack() as stack:

        def start(**overrides):
            runtime = FakeRuntime()
            app = build_app(provider_id="provider-a", api_key=TEST_API_KEY, runtime=runtime, **overrides)
            return stack.enter_context(running_server(app)), runtime

        yield start
```

**Step 3: 修改 `tests/test_provider_server.py`**

- 删除文件中的 `FakeRuntime`、`running_server`、`start_server` fixture 和 `KEY = ...`，改为 `from tests.helpers import TEST_API_KEY as KEY`；fixture 由 conftest 提供，测试函数无需改动。
- 删除因此不再使用的 import（如 `asyncio`、`socket`、`threading`、`Sequence`、`ExitStack`、`contextmanager`、`uvicorn`、`AgentResult`、`UnknownConversationError`、`build_app`），保留实际仍在使用的。
- 在文件末尾追加两个测试（先写，运行确认失败）：

```python
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
```

Run: `uv run pytest tests/test_provider_server.py -q`
Expected: 新增的 `test_server_without_tasks_extension_answers_directly` 失败（`build_app` 不接受 `tasks_enabled`）。`test_json_rpc_errors_fail_the_task` 应该直接通过。**如果它失败**，说明 SDK 把工具抛出的 `MCPError` 包装成了 `isError` 结果，而不是 JSON-RPC 错误。这时报告实际看到的任务内容并停下，不要修改断言。

**Step 4: 修改 `src/cic_mcp_lab/provider/server.py`**

- `build_app` 新增关键字参数 `tasks_enabled: bool = True`。为 `True` 时与现在相同；为 `False` 时 `extensions=[]`、`middleware=[]`，Provider 对所有 `tools/call` 直接返回结果：

```python
        extensions=[TasksExtension(store, task_tools=set(specs))] if tasks_enabled else [],
        middleware=[TasksListenMiddleware(store)] if tasks_enabled else [],
```

- `main()` 新增参数 `parser.add_argument("--no-tasks", action="store_true", help="不支持 tasks 扩展，直接返回结果（模拟同步 Provider）")`，并传入 `tasks_enabled=not args.no_tasks`。

**Step 5: 验证并提交**

Run: `uv run pytest -q` → `50 passed`

```powershell
git add tests/helpers.py tests/conftest.py tests/test_provider_server.py src/cic_mcp_lab/provider/server.py
git commit -m "test: share provider test helpers; allow providers without the tasks extension" -m "Co-authored-by: Copilot <223556219+Copilot@users.noreply.github.com>"
```

---

### Task 2: Agent 入驻数据 `cic/registry.py` + `data/agents.yaml`

**Files:**
- Create: `data/agents.yaml`、`src/cic_mcp_lab/cic/registry.py`
- Test: `tests/test_cic_registry.py`

**Step 1: 写失败的测试** — `tests/test_cic_registry.py`：

```python
import pytest

from cic_mcp_lab.cic.registry import RegistryError, api_key_for, load_registry

ENTRY = (
    "  - {{agent_id: {agent_id}, provider_id: p, display_name: n, description: d, "
    'mcp_url: "http://h/mcp", api_key_env: K, entry_tool: {tool}, price_per_credit: 0.0001}}\n'
)


def write_registry(tmp_path, *entries):
    path = tmp_path / "agents.yaml"
    path.write_text("agents:\n" + "".join(entries), encoding="utf-8")
    return path


def test_onboarded_agents_point_at_their_provider_servers():
    registry = load_registry()

    assert {agent_id: (r.mcp_url, r.api_key_env, r.entry_tool) for agent_id, r in registry.items()} == {
        "trip_planner": ("http://127.0.0.1:8001/mcp", "PROVIDER_A_API_KEY", "trip_planner"),
        "product_copy": ("http://127.0.0.1:8001/mcp", "PROVIDER_A_API_KEY", "product_copy"),
        "weekly_report": ("http://127.0.0.1:8002/mcp", "PROVIDER_B_API_KEY", "weekly_report"),
    }


def test_one_endpoint_cannot_be_registered_twice(tmp_path):
    path = write_registry(tmp_path, ENTRY.format(agent_id="a", tool="t"), ENTRY.format(agent_id="b", tool="t"))

    with pytest.raises(RegistryError, match="already registered"):
        load_registry(path)


def test_missing_fields_are_reported(tmp_path):
    path = write_registry(tmp_path, "  - {agent_id: a}\n")

    with pytest.raises(RegistryError, match="mcp_url"):
        load_registry(path)


def test_api_key_comes_from_the_environment():
    record = load_registry()["weekly_report"]

    assert api_key_for(record, {"PROVIDER_B_API_KEY": " sk-b "}) == "sk-b"
    with pytest.raises(RegistryError, match="PROVIDER_B_API_KEY"):
        api_key_for(record, {})
```

**Step 2:** `uv run pytest tests/test_cic_registry.py -q` → FAIL（`ModuleNotFoundError`）

**Step 3: 实现**

`data/agents.yaml`：

```yaml
# Agents CIC has onboarded through the Provider Portal (simulated).
# API keys live in .env; only the variable name is stored here.
agents:
  - agent_id: trip_planner
    provider_id: provider-a
    display_name: 旅行规划
    description: 根据目的地、出行日期和预算，为用户规划逐日旅行行程。
    mcp_url: "http://127.0.0.1:8001/mcp"
    api_key_env: PROVIDER_A_API_KEY
    entry_tool: trip_planner
    price_per_credit: 0.0001
  - agent_id: product_copy
    provider_id: provider-a
    display_name: 商品文案
    description: 根据商品图片和描述，为电商平台撰写营销文案。
    mcp_url: "http://127.0.0.1:8001/mcp"
    api_key_env: PROVIDER_A_API_KEY
    entry_tool: product_copy
    price_per_credit: 0.0001
  - agent_id: weekly_report
    provider_id: provider-b
    display_name: 周报撰写
    description: 根据本周工作内容，生成结构化的工作周报。
    mcp_url: "http://127.0.0.1:8002/mcp"
    api_key_env: PROVIDER_B_API_KEY
    entry_tool: weekly_report
    price_per_credit: 0.0001
```

`src/cic_mcp_lab/cic/registry.py`：

```python
"""Agents CIC has onboarded (data/agents.yaml): where each agent's MCP server is and how to call it."""
from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass, fields
from pathlib import Path

import yaml

from ..config import PROJECT_ROOT

DEFAULT_REGISTRY_PATH = PROJECT_ROOT / "data" / "agents.yaml"


class RegistryError(ValueError):
    pass


@dataclass(frozen=True)
class AgentRecord:
    agent_id: str
    provider_id: str
    display_name: str
    description: str
    mcp_url: str
    api_key_env: str  # env var holding the provider API key; keys never live in the registry file
    entry_tool: str
    price_per_credit: float  # HKD per Credit


_FIELDS = tuple(field.name for field in fields(AgentRecord))


def load_registry(path: Path = DEFAULT_REGISTRY_PATH) -> dict[str, AgentRecord]:
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    records: dict[str, AgentRecord] = {}
    endpoints: set[tuple[str, str]] = set()
    for index, raw in enumerate(data.get("agents") or []):
        missing = [name for name in _FIELDS if name not in raw]
        if missing:
            raise RegistryError(f"agents[{index}] is missing: {', '.join(missing)}")
        values = {name: raw[name] for name in _FIELDS}
        values["price_per_credit"] = float(values["price_per_credit"])
        record = AgentRecord(**values)
        if record.agent_id in records:
            raise RegistryError(f"Duplicate agent_id {record.agent_id!r}")
        endpoint = (record.mcp_url, record.entry_tool)
        if endpoint in endpoints:
            raise RegistryError(f"{record.agent_id}: tool {record.entry_tool!r} at {record.mcp_url} is already registered")
        endpoints.add(endpoint)
        records[record.agent_id] = record
    return records


def api_key_for(record: AgentRecord, env: Mapping[str, str] = os.environ) -> str:
    key = env.get(record.api_key_env, "").strip()
    if not key:
        raise RegistryError(f"{record.agent_id}: environment variable {record.api_key_env} is not set")
    return key
```

**Step 4:** `uv run pytest tests/test_cic_registry.py -q` → `4 passed`；`uv run pytest -q` → `54 passed`

**Step 5: Commit**

```powershell
git add data/agents.yaml src/cic_mcp_lab/cic/registry.py tests/test_cic_registry.py
git commit -m "feat: add the onboarded agent registry" -m "Co-authored-by: Copilot <223556219+Copilot@users.noreply.github.com>"
```

---

### Task 3: 入口 Tool 契约 `cic/contract.py`

**Files:**
- Create: `src/cic_mcp_lab/cic/contract.py`
- Test: `tests/test_cic_contract.py`

**Step 1: 写失败的测试** — `tests/test_cic_contract.py`：

```python
import pytest

from cic_mcp_lab.cic.contract import (
    AgentCallFailed,
    ContractViolation,
    check_task_ttl,
    parse_agent_reply,
    validate_entry_tool,
)

MESSAGE = {"type": "string"}
NULLABLE_STRING = {"anyOf": [{"type": "string"}, {"type": "null"}], "default": None}
IMAGES = {"anyOf": [{"type": "array", "items": {"type": "string"}}, {"type": "null"}], "default": None}
_OMIT = object()


def tool(properties, required=("message",)):
    return {
        "name": "entry",
        "description": "d",
        "inputSchema": {"type": "object", "properties": properties, "required": list(required)},
    }


def call_result(*, credit=42, structured=None, is_error=False, text="ok"):
    return {
        "content": [] if text is None else [{"type": "text", "text": text}],
        "structuredContent": structured or {"conversation_id": "conv_1", "status": "completed", "reply": "ok"},
        "isError": is_error,
        "_meta": {} if credit is _OMIT else {"com.cic/credit_usage": credit},
    }


def test_entry_tool_accepts_plain_and_nullable_optional_fields():
    plain = validate_entry_tool(tool({"message": MESSAGE, "conversation_id": {"type": "string"}}))
    nullable = validate_entry_tool(
        tool({"message": MESSAGE, "conversation_id": NULLABLE_STRING, "image_urls": IMAGES})
    )

    assert (plain.supports_image, nullable.supports_image) == (False, True)


@pytest.mark.parametrize(
    ("schema", "reason"),
    [
        (tool({"query": MESSAGE}, required=("query",)), "message"),
        (tool({"message": MESSAGE}), "conversation_id"),
        (
            tool({"message": MESSAGE, "conversation_id": NULLABLE_STRING, "topic": MESSAGE}, required=("message", "topic")),
            "topic",
        ),
        (tool({"message": MESSAGE, "conversation_id": NULLABLE_STRING, "image_urls": MESSAGE}), "image_urls"),
    ],
)
def test_entry_tool_contract_violations(schema, reason):
    with pytest.raises(ContractViolation, match=reason):
        validate_entry_tool(schema)


def test_agent_reply_is_parsed_and_zero_credit_is_valid():
    reply = parse_agent_reply(call_result(credit=0))

    assert (reply.conversation_id, reply.status, reply.reply, reply.credit_usage) == ("conv_1", "completed", "ok", 0)


def test_tool_errors_carry_the_provider_message():
    with pytest.raises(AgentCallFailed, match="conv_x expired"):
        parse_agent_reply(call_result(is_error=True, text="conv_x expired"))


@pytest.mark.parametrize("credit", [_OMIT, None, "", "12", -1, True, float("nan")])
def test_missing_or_invalid_credit_usage_violates_the_contract(credit):
    with pytest.raises(ContractViolation, match="credit_usage"):
        parse_agent_reply(call_result(credit=credit))


@pytest.mark.parametrize(
    "structured",
    [
        {"status": "completed", "reply": "ok"},
        {"conversation_id": "conv_1", "status": "done", "reply": "ok"},
        {"conversation_id": "conv_1", "status": "completed"},
    ],
)
def test_incomplete_structured_content_violates_the_contract(structured):
    with pytest.raises(ContractViolation, match="structuredContent"):
        parse_agent_reply(call_result(structured=structured))


def test_result_without_text_content_violates_the_contract():
    with pytest.raises(ContractViolation, match="content"):
        parse_agent_reply(call_result(text=None))


def test_short_task_ttl_is_flagged():
    assert check_task_ttl(None) is None
    assert check_task_ttl(3_600_000) is None
    assert "ttlMs" in check_task_ttl(60_000)
```

**Step 2:** `uv run pytest tests/test_cic_contract.py -q` → FAIL（`ModuleNotFoundError`）

**Step 3: 实现** — `src/cic_mcp_lab/cic/contract.py`：

```python
"""CIC entry-tool contract (design §4.3, §5): checks for tool schemas and agent results."""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Literal

CREDIT_USAGE_META_KEY = "com.cic/credit_usage"
MIN_TASK_TTL_MS = 3_600_000
_STATUSES = ("input_required", "completed")


class ContractViolation(ValueError):
    """The provider's tool or result does not follow the CIC entry-tool contract."""


class AgentCallFailed(RuntimeError):
    """The provider reported a tool error (`isError: true`); the turn is not billed."""


@dataclass(frozen=True)
class EntryTool:
    name: str
    description: str
    supports_image: bool


@dataclass(frozen=True)
class AgentReply:
    conversation_id: str
    status: Literal["input_required", "completed"]
    reply: str
    credit_usage: float


def _variants(schema: Any) -> list[dict[str, Any]]:
    """Non-null alternatives of a property schema; pydantic renders `T | None` as `anyOf [T, null]`."""
    if not isinstance(schema, dict):
        return []
    options = schema.get("anyOf", [schema])
    return [option for option in options if isinstance(option, dict) and option.get("type") != "null"]


def _is_string(schema: Any) -> bool:
    variants = _variants(schema)
    return len(variants) == 1 and variants[0].get("type") == "string"


def _is_string_array(schema: Any) -> bool:
    variants = _variants(schema)
    return len(variants) == 1 and variants[0].get("type") == "array" and _is_string(variants[0].get("items"))


def validate_entry_tool(tool: dict[str, Any]) -> EntryTool:
    name = str(tool.get("name", ""))
    schema = tool.get("inputSchema")
    if not isinstance(schema, dict) or schema.get("type") != "object":
        raise ContractViolation(f"{name}: inputSchema must be an object schema")
    properties = schema.get("properties") or {}
    required = set(schema.get("required") or [])
    if "message" not in required or not _is_string(properties.get("message")):
        raise ContractViolation(f"{name}: 'message' must be a required string parameter")
    if "conversation_id" not in properties or not _is_string(properties["conversation_id"]):
        raise ContractViolation(f"{name}: 'conversation_id' must be an optional string parameter")
    extra_required = sorted(required - {"message"})
    if extra_required:
        raise ContractViolation(f"{name}: only 'message' may be required, got {', '.join(extra_required)}")
    supports_image = "image_urls" in properties
    if supports_image and not _is_string_array(properties["image_urls"]):
        raise ContractViolation(f"{name}: 'image_urls' must be an optional array of strings")
    return EntryTool(name, str(tool.get("description") or ""), supports_image)


def parse_agent_reply(result: dict[str, Any]) -> AgentReply:
    """Validate a CallToolResult (wire JSON) from an entry tool and extract the billable reply."""
    texts = [
        item.get("text", "")
        for item in result.get("content") or []
        if isinstance(item, dict) and item.get("type") == "text"
    ]
    if result.get("isError"):
        raise AgentCallFailed("; ".join(text for text in texts if text) or "The agent reported an error")
    if not texts:
        raise ContractViolation("content must contain a text block with the reply")
    structured = result.get("structuredContent")
    if not isinstance(structured, dict):
        raise ContractViolation("structuredContent is missing")
    conversation_id = structured.get("conversation_id")
    status = structured.get("status")
    reply = structured.get("reply")
    if not isinstance(conversation_id, str) or not conversation_id:
        raise ContractViolation("structuredContent.conversation_id must be a non-empty string")
    if status not in _STATUSES:
        raise ContractViolation(f"structuredContent.status must be one of {_STATUSES}, got {status!r}")
    if not isinstance(reply, str):
        raise ContractViolation("structuredContent.reply must be a string")
    credit = (result.get("_meta") or {}).get(CREDIT_USAGE_META_KEY)
    if isinstance(credit, bool) or not isinstance(credit, int | float) or not math.isfinite(credit) or credit < 0:
        raise ContractViolation(f"{CREDIT_USAGE_META_KEY} must be a number >= 0, got {credit!r}")
    return AgentReply(conversation_id, status, reply, credit)


def check_task_ttl(ttl_ms: int | None) -> str | None:
    """Warning text when a task's ttlMs is below the contract minimum (recorded, not enforced)."""
    if ttl_ms is not None and ttl_ms < MIN_TASK_TTL_MS:
        return f"ttlMs={ttl_ms} is below the contract minimum of {MIN_TASK_TTL_MS} (1 hour)"
    return None
```

**Step 4:** `uv run pytest tests/test_cic_contract.py -q` → `19 passed`；`uv run pytest -q` → `73 passed`

**Step 5: Commit**

```powershell
git add src/cic_mcp_lab/cic/contract.py tests/test_cic_contract.py
git commit -m "feat: validate the CIC entry-tool contract" -m "Co-authored-by: Copilot <223556219+Copilot@users.noreply.github.com>"
```

---

### Task 4: Provider 客户端 `cic/mcp_client.py`

**Files:**
- Create: `src/cic_mcp_lab/cic/mcp_client.py`
- Test: `tests/test_cic_mcp_client.py`

**要点：**
- `ProviderClient` 是 SDK `Client` 的薄封装，提供以下能力：
  - 声明 tasks 能力；
  - `submit()` 调用入口 Tool：Provider 走任务时返回 `CreateTaskResult`，直接返回结果时返回 CallToolResult 的 wire dict；
  - `get_task()` / `cancel_task()`；
  - `listen()` 打开 `taskIds` 订阅，返回 Provider 确认接受的 taskId 集合和推送事件流。
- 订阅确认是核心通知，里面没有订阅 id，无法对应到具体订阅，所以 `TasksClientExtension` 用锁保证同一时间只有一个订阅在等待确认。
- 结果和工具都转成 wire 格式的 dict（camelCase），交给 `contract.py` 的纯函数处理。

**Step 1: 写失败的测试** — `tests/test_cic_mcp_client.py`：

```python
import anyio
import pytest

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


async def test_provider_without_tasks_answers_directly(start_server):
    url, _ = start_server(tasks_enabled=False)
    async with ProviderClient(url, KEY) as client:
        assert not client.supports_tasks
        result = await client.submit("trip_planner", {"message": "hi"})

    assert parse_agent_reply(result).reply == "echo: hi"
```

**Step 2:** `uv run pytest tests/test_cic_mcp_client.py -q` → FAIL（`ModuleNotFoundError`）

**Step 3: 实现** — `src/cic_mcp_lab/cic/mcp_client.py`：

```python
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
                    yield TaskSubscription(accepted, recv)
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
        except Exception as exc:
            await stack.aclose()
            raise ProviderUnavailable(f"{self.url}: {describe_error(exc)}") from exc
        self._stack = stack
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        stack, self._stack, self._client = self._stack, None, None
        if stack is not None:
            await stack.__aexit__(*exc_info)

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
```

> 注意：
> - 如果 `ClientSession`、`IncomingMessage` 等名字的导入位置和上面不同，以 spike（`files/spike_tasks/client.py`）中可用的导入为准。
> - 如果 `Client.list_tools(cursor=...)` 的分页参数名不同，按 SDK 签名调整。
> - 如果 401 时 `ProviderUnavailable` 的报错文本里看不到 401 或 Unauthorized，在汇报中写明实际文本，不要为此改动 SDK 的行为。

**Step 4:** `uv run pytest tests/test_cic_mcp_client.py -q` → `6 passed`；`uv run pytest -q` → `79 passed`

**Step 5: Commit**

```powershell
git add src/cic_mcp_lab/cic/mcp_client.py tests/test_cic_mcp_client.py
git commit -m "feat: add the CIC provider client with task submission and push subscriptions" -m "Co-authored-by: Copilot <223556219+Copilot@users.noreply.github.com>"
```

---

### Task 5: 账本 `cic/ledger.py` + 任务监听 `cic/task_watcher.py`

**Files:**
- Create: `src/cic_mcp_lab/cic/ledger.py`、`src/cic_mcp_lab/cic/task_watcher.py`
- Test: `tests/test_cic_ledger.py`、`tests/test_cic_task_watcher.py`

**要点（对应设计文档 §6.8.2）：**
- `submit()` 只等到 Provider 接受任务就返回 `Submission`（拿到 taskId）。最终结果作为 `TaskOutcome` 放入 `watcher.outcomes`。
- 每次提交对应一个 asyncio task，各自持有一个 `ProviderClient`（实验阶段简单起见；生产环境见 §13）。
- 跟踪流程：
  1. 订阅推送；
  2. 订阅被接受后补查一次 `tasks/get`；
  3. 等推送，期间每隔 `backstop_poll_seconds` 秒查一次作为兜底；
  4. 订阅未被接受、失败或推送流断开时改为按 `pollIntervalMs` 轮询；
  5. 截止时间 = 提交时刻 + ttlMs。
- 失败分类：
  - 超过截止时间收到 `-32602`（任务不存在）→ `expired`；截止前收到 → `lost`；
  - 轮询连续 3 次出现其他错误 → `lost`；
  - 意外异常 → `internal`。
- 只有 `completed` 且通过契约校验的结果才结算，以 taskId 为结算键，账本保证幂等；直接返回结果的 Provider 用 submission_id 作结算键。
- 同一会话同时只允许一轮在途，否则抛 `ConversationBusy`（设计文档 §6.6）。在途标记在结果入队之前释放。
- `SubmitFailed`：未知 Agent、缺少 key、连不上或 401、调用被拒绝。这些情况都不计费。

**Step 1: 写失败的测试**

`tests/test_cic_ledger.py`：

```python
from decimal import Decimal

from cic_mcp_lab.cic.ledger import Ledger, LedgerEntry


def entry(key, credit=42):
    return LedgerEntry(key, "provider-a", "trip_planner", "conv_1", credit, 0.0001)


def test_each_task_is_settled_once():
    ledger = Ledger()

    assert ledger.settle(entry("task_1"))
    assert not ledger.settle(entry("task_1", credit=99))
    assert ledger.settle(entry("task_2", credit=8))
    assert ledger.total_credit() == 50
    assert ledger.total_hkd() == Decimal("0.005")


def test_hkd_is_computed_exactly():
    assert entry("t", credit=1234).hkd == Decimal("0.1234")
```

`tests/test_cic_task_watcher.py`：

```python
from contextlib import asynccontextmanager
from decimal import Decimal

import anyio
import pytest
from mcp.shared.exceptions import MCPError

from cic_mcp_lab.cic.ledger import Ledger
from cic_mcp_lab.cic.mcp_client import CreateTaskResult, TaskSnapshot, TaskSubscription
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
```

**Step 2:** `uv run pytest tests/test_cic_ledger.py tests/test_cic_task_watcher.py -q` → FAIL（`ModuleNotFoundError`）

**Step 3: 实现**

`src/cic_mcp_lab/cic/ledger.py`：

```python
"""In-memory credit ledger: each billable turn is settled once, keyed by its taskId (design §5)."""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal


@dataclass(frozen=True)
class LedgerEntry:
    settlement_key: str  # taskId; the submission id for providers that answer directly
    provider_id: str
    agent_id: str
    conversation_id: str
    credit_usage: float
    price_per_credit: float  # HKD per Credit

    @property
    def hkd(self) -> Decimal:
        return Decimal(str(self.credit_usage)) * Decimal(str(self.price_per_credit))


class Ledger:
    def __init__(self) -> None:
        self._entries: dict[str, LedgerEntry] = {}

    def settle(self, entry: LedgerEntry) -> bool:
        """Record a billable turn; False when its settlement key was already recorded."""
        if entry.settlement_key in self._entries:
            return False
        self._entries[entry.settlement_key] = entry
        return True

    @property
    def entries(self) -> tuple[LedgerEntry, ...]:
        return tuple(self._entries.values())

    def total_credit(self) -> float:
        return sum(entry.credit_usage for entry in self._entries.values())

    def total_hkd(self) -> Decimal:
        return sum((entry.hkd for entry in self._entries.values()), Decimal(0))
```

`src/cic_mcp_lab/cic/task_watcher.py`：

```python
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
```

> 注意：
> - `test_failed_turns_are_not_billed[unknown-conversation]`：FakeRuntime 抛出 `UnknownConversationError`，server 端会转成 `ToolError` → `isError` 的 completed 任务 → `agent_error`。
> - `[mcp-error]`：JSON-RPC 错误 → failed 任务 → `task_failed`（已在 Task 1 中验证）。
> - 如果 `ScriptedClient` 中用字段名构造 `CreateTaskResult` / `TaskSnapshot` 失败，说明模型不接受按字段名赋值，改用 `model_validate({...camelCase...}, by_name=False)`。

**Step 4:** `uv run pytest tests/test_cic_ledger.py tests/test_cic_task_watcher.py -q` → `13 passed`；`uv run pytest -q` → `92 passed`

**Step 5: Commit**

```powershell
git add src/cic_mcp_lab/cic/ledger.py src/cic_mcp_lab/cic/task_watcher.py tests/test_cic_ledger.py tests/test_cic_task_watcher.py
git commit -m "feat: follow provider tasks to completion and settle credit once per task" -m "Co-authored-by: Copilot <223556219+Copilot@users.noreply.github.com>"
```

---

### Task 6: 测试连接 `cic/probe.py` + `cic-probe`

**Files:**
- Create: `src/cic_mcp_lab/cic/probe.py`
- Modify: `pyproject.toml`（`[project.scripts]` 增加 `cic-probe = "cic_mcp_lab.cic.probe:main"`）
- Test: `tests/test_cic_probe.py`

**要点（设计文档 §4.2）：** 四个步骤按顺序执行，遇到第一个失败就停止。
1. `discover`：协议版本必须是 `2026-07-28`，且声明了 `tools` 能力；同时记录是否声明 tasks 扩展，仅作展示。
2. `auth`：不带 key 调用 `tools/list`，必须返回 HTTP 401，否则阻断并提示「MCP Server 未启用鉴权」。
3. `tools`：至少有 1 个 tool；tool 名字符集为 `A-Za-z0-9_.-`；`inputSchema` 必须是 object schema。
4. `entry_tool`：登记的入口 Tool 存在，且通过 §4.3 契约校验。

报告附带 `tested_at`，以及 `fingerprint = sha256([url, sha256(key), entry_tool])`。url、key、入口 Tool 任一项改变，测试结果即失效。

**Step 1: 写失败的测试** — `tests/test_cic_probe.py`：

```python
from mcp.server.auth.settings import AuthSettings
from mcp.server.mcpserver import MCPServer

from cic_mcp_lab.cic.probe import STEPS, connection_fingerprint, probe
from cic_mcp_lab.cic.registry import AgentRecord
from cic_mcp_lab.provider.auth import ApiKeyVerifier
from tests.helpers import TEST_API_KEY as KEY
from tests.helpers import running_server, unused_port


def record_for(url, entry_tool="trip_planner"):
    return AgentRecord("agent", "provider-x", "n", "d", url, "K", entry_tool, 0.0001)


def inline_server(*, auth):
    """A server whose only tool ignores the CIC contract (its parameter is 'query', not 'message')."""
    options = (
        {
            "token_verifier": ApiKeyVerifier({KEY: "cic"}),
            "auth": AuthSettings(issuer_url="https://auth.invalid", resource_server_url=None),
        }
        if auth
        else {}
    )
    mcp = MCPServer("inline", **options)

    async def entry(query: str) -> str:
        return query

    mcp.add_tool(entry, name="entry")
    return running_server(mcp.streamable_http_app(host="127.0.0.1"))


def step_results(report):
    return [(step.name, step.ok) for step in report.steps]


async def test_compliant_agent_passes_every_step(start_server):
    url, _ = start_server()
    report = await probe(record_for(url), KEY)

    assert report.ok
    assert [step.name for step in report.steps] == list(STEPS)
    assert report.supports_tasks
    assert report.entry_tool.supports_image is False
    assert report.fingerprint == connection_fingerprint(url, KEY, "trip_planner")


def test_fingerprint_covers_url_key_and_entry_tool():
    base = connection_fingerprint("http://h/mcp", "sk-1", "t")

    assert base == connection_fingerprint("http://h/mcp", "sk-1", "t")
    assert len({base, connection_fingerprint("http://x/mcp", "sk-1", "t"),
                connection_fingerprint("http://h/mcp", "sk-2", "t"),
                connection_fingerprint("http://h/mcp", "sk-1", "u")}) == 4
    assert "sk-1" not in base


async def test_unreachable_server_fails_discover():
    report = await probe(record_for(f"http://127.0.0.1:{unused_port()}/mcp"), KEY)

    assert step_results(report) == [("discover", False)]


async def test_server_without_auth_is_blocked():
    with inline_server(auth=False) as url:
        report = await probe(record_for(url, entry_tool="entry"), KEY)

    assert step_results(report) == [("discover", True), ("auth", False)]
    assert "HTTP 200" in report.steps[-1].detail


async def test_non_compliant_entry_tool_is_rejected():
    with inline_server(auth=True) as url:
        report = await probe(record_for(url, entry_tool="entry"), KEY)

    assert step_results(report)[-1] == ("entry_tool", False)
    assert "message" in report.steps[-1].detail
    assert not report.ok


async def test_unlisted_entry_tool_is_rejected(start_server):
    url, _ = start_server()
    report = await probe(record_for(url, entry_tool="nope"), KEY)

    assert step_results(report)[-1] == ("entry_tool", False)
    assert "nope" in report.steps[-1].detail
```

**Step 2:** `uv run pytest tests/test_cic_probe.py -q` → FAIL（`ModuleNotFoundError`）

**Step 3: 实现** — `src/cic_mcp_lab/cic/probe.py`：

```python
"""Test connection for an onboarded agent (design §4.2), the prototype of the Provider Portal check."""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import re
import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import anyio

from ..config import load_settings
from ..mcpwire import PROTOCOL_VERSION, WireClient
from .contract import ContractViolation, EntryTool, validate_entry_tool
from .mcp_client import ProviderClient, describe_error
from .registry import AgentRecord, RegistryError, api_key_for, load_registry

STEPS = ("discover", "auth", "tools", "entry_tool")
TOOL_NAME = re.compile(r"[A-Za-z0-9_.-]{1,128}")


@dataclass(frozen=True)
class ProbeStep:
    name: str
    ok: bool
    detail: str


@dataclass(frozen=True)
class ProbeReport:
    agent_id: str
    steps: tuple[ProbeStep, ...]  # stops at the first failing step
    supports_tasks: bool | None
    entry_tool: EntryTool | None
    fingerprint: str
    tested_at: str

    @property
    def ok(self) -> bool:
        return len(self.steps) == len(STEPS) and all(step.ok for step in self.steps)


def connection_fingerprint(url: str, api_key: str, entry_tool: str) -> str:
    """What a passing test vouches for; changing the URL, key or entry tool invalidates it."""
    key_digest = hashlib.sha256(api_key.encode()).hexdigest()
    return hashlib.sha256(json.dumps([url, key_digest, entry_tool]).encode()).hexdigest()


def _yes(flag: bool) -> str:
    return "yes" if flag else "no"


def _anonymous_status(url: str) -> int:
    with WireClient(url, None) as wire:
        return wire.request("tools/list").status_code


def _tool_problem(tool: dict[str, Any]) -> str | None:
    name = tool.get("name")
    if not isinstance(name, str) or not TOOL_NAME.fullmatch(name):
        return f"invalid tool name {name!r}"
    schema = tool.get("inputSchema")
    if not isinstance(schema, dict) or schema.get("type") != "object":
        return f"{name}: inputSchema must be an object schema"
    return None


async def probe(record: AgentRecord, api_key: str) -> ProbeReport:
    tested_at = datetime.now(UTC).isoformat(timespec="seconds")
    steps: list[ProbeStep] = []
    supports_tasks: bool | None = None
    entry: EntryTool | None = None

    async def run() -> None:
        nonlocal supports_tasks, entry
        async with ProviderClient(record.mcp_url, api_key) as client:
            has_tools = client.server_capabilities.tools is not None
            supports_tasks = client.supports_tasks
            detail = f"protocol {client.protocol_version}; tools: {_yes(has_tools)}; tasks extension: {_yes(supports_tasks)}"
            steps.append(ProbeStep("discover", client.protocol_version == PROTOCOL_VERSION and has_tools, detail))
            if not steps[-1].ok:
                return

            status = await anyio.to_thread.run_sync(_anonymous_status, record.mcp_url)
            detail = f"anonymous tools/list -> HTTP {status}"
            steps.append(ProbeStep("auth", status == 401, detail if status == 401 else f"{detail}; MCP Server 未启用鉴权"))
            if not steps[-1].ok:
                return

            tools = await client.list_tools()
            problems = [problem for tool in tools if (problem := _tool_problem(tool))]
            if not tools:
                problems.append("the server lists no tools")
            names = ", ".join(sorted(str(tool.get("name")) for tool in tools))
            steps.append(ProbeStep("tools", not problems, "; ".join(problems) or f"{len(tools)} tools: {names}"))
            if problems:
                return

            tool = next((tool for tool in tools if tool.get("name") == record.entry_tool), None)
            if tool is None:
                steps.append(ProbeStep("entry_tool", False, f"entry tool {record.entry_tool!r} is not listed"))
                return
            try:
                entry = validate_entry_tool(tool)
            except ContractViolation as exc:
                steps.append(ProbeStep("entry_tool", False, str(exc)))
                return
            steps.append(ProbeStep("entry_tool", True, f"{entry.name}; images: {_yes(entry.supports_image)}"))

    try:
        await run()
    except Exception as exc:
        if len(steps) < len(STEPS):  # the step that was running when it failed
            steps.append(ProbeStep(STEPS[len(steps)], False, describe_error(exc)))
    return ProbeReport(
        record.agent_id,
        tuple(steps),
        supports_tasks,
        entry,
        connection_fingerprint(record.mcp_url, api_key, record.entry_tool),
        tested_at,
    )


def _print_report(record: AgentRecord, report: ProbeReport) -> None:
    print(f"[{record.agent_id}] {record.mcp_url} · 入口 Tool {record.entry_tool}")
    for step in report.steps:
        print(f"  {'✓' if step.ok else '✗'} {step.name:<10} {step.detail}")
    verdict = "通过" if report.ok else "未通过，阻断提交"
    print(f"  => {verdict} · tested_at={report.tested_at} · fingerprint={report.fingerprint[:16]}…")


async def _probe_all(registry: dict[str, AgentRecord], agent_ids: list[str]) -> bool:
    all_ok = True
    for agent_id in agent_ids:
        record = registry[agent_id]
        try:
            api_key = api_key_for(record)
        except RegistryError as exc:
            print(f"[{agent_id}] ✗ {exc}")
            all_ok = False
            continue
        report = await probe(record, api_key)
        _print_report(record, report)
        all_ok = all_ok and report.ok
    return all_ok


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description="对已入驻的 Agent 执行测试连接（设计文档 §4.2）")
    target = parser.add_mutually_exclusive_group(required=True)
    target.add_argument("agent_id", nargs="?", help="data/agents.yaml 中的 agent_id")
    target.add_argument("--all", action="store_true", help="测试全部已入驻的 Agent")
    args = parser.parse_args()
    load_settings()  # loads .env into the process environment
    registry = load_registry()
    agent_ids = sorted(registry) if args.all else [args.agent_id]
    unknown = [agent_id for agent_id in agent_ids if agent_id not in registry]
    if unknown:
        raise SystemExit(f"Unknown agent: {', '.join(unknown)}")
    sys.exit(0 if asyncio.run(_probe_all(registry, agent_ids)) else 1)
```

> 注意：
> - 如果 MCPServer 在无鉴权模式下的 `streamable_http_app(host=...)` 签名不同，按 SDK 调整 `inline_server`。
> - `test_unreachable_server_fails_discover` 依赖 `ProviderClient` 把连接失败包装为 `ProviderUnavailable`，并由 `run()` 外层的 `except Exception` 记为 discover 失败。

**Step 4:** `uv run pytest tests/test_cic_probe.py -q` → `6 passed`；`uv run pytest -q` → `98 passed`

**Step 5: Commit**

```powershell
git add src/cic_mcp_lab/cic/probe.py tests/test_cic_probe.py pyproject.toml
git commit -m "feat: add cic-probe, the onboarding test connection" -m "Co-authored-by: Copilot <223556219+Copilot@users.noreply.github.com>"
```

---

### Task 7: 手工调用 `cic-call` + 文档

**Files:**
- Create: `src/cic_mcp_lab/cic/call.py`
- Modify: `pyproject.toml`（增加 `cic-call = "cic_mcp_lab.cic.call:main"`）、`README.md`、`.env.example`

**要点：** `cic-call` 是 Step 3 手工检查点使用的命令。它通过 TaskWatcher 发送一轮消息，然后依次打印：
- 被接受的耗时和 taskId；
- 结果经哪条路径送达（直接返回 / 推送 / 订阅后补查 / 轮询）；
- 回复内容；
- credit_usage 与 HKD；
- 失败原因（不计费）。

`--image` 接受本地路径（先上传 OSS）或 http(s) 地址。这是纯 CLI 胶水代码，逻辑已由前面各 Task 的测试覆盖，因此不另写单测。

**Step 1: 实现** — `src/cic_mcp_lab/cic/call.py`：

```python
"""cic-call: send one turn to an onboarded agent over MCP and wait for its outcome (step 3 checkpoint)."""
from __future__ import annotations

import argparse
import asyncio
import os
import sys
import time
from pathlib import Path

from ..config import Settings, load_settings
from .ledger import Ledger
from .oss import OssUploader
from .registry import load_registry
from .task_watcher import SubmitFailed, TaskWatcher

DELIVERY_LABELS = {"direct": "直接返回", "push": "推送", "recheck": "订阅后补查", "poll": "轮询"}


async def _image_urls(images: list[str], settings: Settings) -> list[str]:
    urls: list[str] = []
    uploader: OssUploader | None = None
    for image in images:
        if image.startswith(("http://", "https://")):
            urls.append(image)
            continue
        uploader = uploader or OssUploader(settings)
        url = await asyncio.to_thread(uploader.upload_file, Path(image))
        print(f"[image] 已上传 {image} -> {url}")
        urls.append(url)
    return urls


async def _call(args: argparse.Namespace) -> bool:
    settings = load_settings()
    registry = load_registry()
    if args.agent not in registry:
        print(f"[error] 未入驻的 Agent：{args.agent}（可选：{', '.join(sorted(registry))}）")
        return False
    try:
        image_urls = await _image_urls(args.image or [], settings)
    except Exception as exc:
        print(f"[error] 图片上传失败：{type(exc).__name__}: {exc}")
        return False

    ledger = Ledger()
    backstop = float(os.environ.get("TASK_BACKSTOP_POLL_SECONDS", "30"))
    started = time.monotonic()
    async with TaskWatcher(registry, ledger, backstop_poll_seconds=backstop) as watcher:
        try:
            submission = await watcher.submit(args.agent, args.message, image_urls, args.conversation_id)
        except SubmitFailed as exc:
            print(f"[submit] 提交失败（不计费）：{exc}")
            return False
        task = submission.task_id or "无（Provider 不走任务，等待直接返回）"
        print(f"[submit] {time.monotonic() - started:.2f}s 被接受 · taskId={task}")
        for warning in submission.warnings:
            print(f"[warn] {warning}")
        outcome = await watcher.outcomes.get()

    for note in outcome.notes:
        print(f"[note] {note}")
    via = DELIVERY_LABELS.get(outcome.delivery or "", "未拿到最终状态")
    print(f"[result] 经{via} · 从提交起 {outcome.elapsed_seconds:.1f}s")
    if not outcome.ok:
        print(f"[failed] {outcome.failure}: {outcome.detail}（不计费）")
        return False
    reply = outcome.reply
    print(f"[agent] conversation_id={reply.conversation_id} · status={reply.status}")
    print(reply.reply)
    entry = ledger.entries[0]
    print(
        f"[credit] credit_usage={reply.credit_usage} × {entry.price_per_credit} HKD/Credit = {entry.hkd} HKD"
        f" · 结算键={entry.settlement_key}"
    )
    if reply.status == "input_required":
        print(f"[next] Agent 需要更多信息；继续本会话请加 --conversation-id {reply.conversation_id}")
    return True


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8")  # agent replies may contain emoji; gbk pipes would crash
    parser = argparse.ArgumentParser(description="经 MCP 向已入驻的 Agent 发送一轮消息，并等待最终结果")
    parser.add_argument("--agent", required=True, help="data/agents.yaml 中的 agent_id")
    parser.add_argument("--message", required=True, help="用户本轮输入的原话")
    parser.add_argument("--conversation-id", help="继续会话时传入上一轮返回的 conversation_id")
    parser.add_argument("--image", action="append", help="本地图片路径（上传 OSS）或 https 地址，可重复")
    args = parser.parse_args()
    try:
        ok = asyncio.run(_call(args))
    except KeyboardInterrupt:
        ok = False
    sys.exit(0 if ok else 1)
```

**Step 2: `pyproject.toml`**：`[project.scripts]` 末尾追加（`cic-probe` 已在 Task 6 中加入）

```toml
cic-call = "cic_mcp_lab.cic.call:main"
```

**Step 3: `.env.example`** 末尾追加：

```
# TASK_BACKSTOP_POLL_SECONDS=30
```

**Step 4: `README.md`**：
- `provider-server` 一行改为 `uv run provider-server --provider <A\|B> --port <端口> [--delay 秒] [--no-push] [--no-tasks]`，说明末尾补充「`--no-tasks` 模拟不支持 tasks 扩展、直接返回结果的 Provider」。
- 在 `provider-check` 一行之后、`pytest` 一行之前插入两行：

```markdown
| `uv run cic-probe <agent_id> \| --all` | 对 `data/agents.yaml` 中已入驻的 Agent 执行测试连接：discover → 鉴权生效检查 → tools/list → 入口 Tool 契约校验 |
| `uv run cic-call --agent <id> --message <文本> [--conversation-id <id>] [--image <路径或URL>]` | 经 MCP 调用已入驻的 Agent：提交任务 → 推送 / 补查 / 轮询拿结果 → 校验契约并按 credit_usage 结算 |
```

**Step 5: 验证**

Run: `uv run pytest -q` → `98 passed`
Run: `uv run cic-call --help` → 正常打印帮助（不调用模型）
Run: `uv run cic-probe --help` → 正常打印帮助

**Step 6: Commit**

```powershell
git add src/cic_mcp_lab/cic/call.py pyproject.toml README.md .env.example
git commit -m "feat: add cic-call for manual MCP agent calls; document step 3 commands" -m "Co-authored-by: Copilot <223556219+Copilot@users.noreply.github.com>"
```

---

### Task 8: 手工检查点（由用户执行）

终端 1：`uv run provider-server --provider A --port 8001`
终端 2：`uv run provider-server --provider B --port 8002 --no-push`
终端 3 按顺序执行以下场景：

| # | 场景 | 命令 | 预期 |
| --- | --- | --- | --- |
| 1 | 测试连接 | `uv run cic-probe --all` | 3 个 Agent 四步全部 ✓。A 的两个 Agent 显示 `tasks extension: yes` |
| 2 | 推送路径 | `uv run cic-call --agent trip_planner --message "帮我规划一个东京 3 天的行程"` | 提交在 1 秒内被接受，拿到 taskId；结果「经推送」送达；打印 credit_usage 与 HKD；大概率是 `input_required` 并提示 `--conversation-id` |
| 3 | 同会话续聊 | 按场景 2 的提示，带 `--conversation-id <id>` 补充信息（如「下个月出发，预算 1 万」） | conversation_id 不变；Agent 能接上上一轮的上下文 |
| 4 | 不推送的 Provider | `uv run cic-call --agent weekly_report --message "帮我写本周周报：完成了 MCP 接入实验"` | 出现 `[note] provider did not accept the push subscription; polling`；结果「经轮询」送达 |
| 5 | 会话失效 | `uv run cic-call --agent trip_planner --message "继续" --conversation-id conv_nope` | `[failed] agent_error: ...`（不计费） |
| 6 | 图片输入 | `uv run cic-call --agent product_copy --message "为这款商品写一段电商文案" --image <本地图片路径>` | 先上传 OSS 并打印公网地址；回复内容与图片相符 |
| 7 | 长任务 | 终端 1 改为 `--delay 90` 重启 A，再执行场景 2 的命令 | 提交仍在 1 秒内被接受；约 90 秒后经推送拿到结果；期间命令不会超时退出 |
| 8 | 直接返回结果的 Provider | 终端 1 改为 `--no-tasks` 重启 A；执行 `uv run cic-probe trip_planner` 和场景 2 的命令 | probe 通过且显示 `tasks extension: no`；call 显示 `taskId=无`，结果「经直接返回」 |
| 9 | Provider 不可用 | 停掉终端 2；执行场景 4 的命令和 `uv run cic-probe weekly_report` | call：`[submit] 提交失败（不计费）`；probe：`✗ discover` |
| 10 | 短 TTL 告警（可选） | 终端 1 设置 `$env:PROVIDER_TASK_TTL_MS=60000` 后正常启动 A，再执行场景 2 的命令 | 出现 `[warn] ttlMs=60000 is below the contract minimum ...`；调用不受影响 |
| 11 | 任务进行中 Provider 宕机（可选） | 终端 1 以 `--delay 90` 启动 A，执行场景 2 的命令；拿到 taskId 后 Ctrl+C 停掉 A | 出现 `[note] push stream closed ...` 和/或 `[note] connection to the provider failed ...; reconnecting to poll`；重连连续失败后结果为 `[failed] lost: ...`（不计费） |

请把各场景的完整输出整理到 `step3_test_result.md`，我会逐条核对。

**以下内容不需要手工验证（已由单测覆盖）：**
- 不合规 Tool 被拦截；
- 订阅后补查与兜底轮询；
- 任务丢失 / 过期；
- JSON-RPC 错误导致任务 failed；
- 同一会话不允许并发；
- 同一任务只结算一次。

