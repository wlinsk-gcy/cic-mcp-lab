"""MCP Server for one mock provider: one entry tool per agent, tasks extension, API-key auth."""
from __future__ import annotations

import argparse
import asyncio
import logging
import os
from collections.abc import Sequence
from typing import Annotated, Any, Protocol

import uvicorn
from mcp.server.auth.settings import AuthSettings
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp_types import CallToolResult, TextContent
from pydantic import Field
from starlette.types import ASGIApp

from ..config import load_settings
from ..llm import make_chat_model
from .agent import (
    AgentResult,
    ImagesNotSupportedError,
    InvalidImageUrlError,
    ProviderAgentRuntime,
    UnknownConversationError,
    make_agent_factory,
)
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
    tasks_enabled: bool = True,
) -> ASGIApp:
    specs = provider_specs(provider_id)
    store = TaskStore(ttl_ms=ttl_ms, poll_interval_ms=poll_interval_ms, push_enabled=push_enabled)
    mcp = MCPServer(
        f"cic-lab-{provider_id}",
        extensions=[TasksExtension(store, task_tools=set(specs))] if tasks_enabled else [],
        middleware=[TasksListenMiddleware(store)] if tasks_enabled else [],
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


_API_KEY_ENV = {"A": "PROVIDER_A_API_KEY", "B": "PROVIDER_B_API_KEY"}


def main() -> None:
    parser = argparse.ArgumentParser(description="启动模拟 Provider 的 MCP Server")
    parser.add_argument("--provider", choices=sorted(_API_KEY_ENV), required=True)
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--delay", type=float, default=0.0, help="每轮调用 Agent 前额外等待的秒数（模拟长任务）")
    parser.add_argument("--no-push", action="store_true", help="不接受 taskIds 订阅（模拟不支持推送的 Provider）")
    parser.add_argument("--no-tasks", action="store_true", help="不支持 tasks 扩展，直接返回结果（模拟同步 Provider）")
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
        tasks_enabled=not args.no_tasks,
    )
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    # listen streams stay open until the client leaves; don't let them block shutdown
    uvicorn.run(app, host="127.0.0.1", port=args.port, log_level="info", timeout_graceful_shutdown=3)
