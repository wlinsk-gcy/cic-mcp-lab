"""MCP Server for one mock provider: one entry tool per agent, tasks extension, API-key auth."""
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
from .mcp_tasks import TasksExtension, TasksListenMiddleware
from .tasks import TaskStore

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
