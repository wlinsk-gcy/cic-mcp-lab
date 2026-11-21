"""Runtime of the mock third-party agents: per-conversation memory, ask-back, credit."""
from __future__ import annotations

import asyncio
import mimetypes
import uuid
from dataclasses import dataclass, field
from typing import Callable, Literal, Protocol, Sequence
from urllib.parse import urlparse

from agentscope.agent import Agent, ReActConfig
from agentscope.message import DataBlock, Msg, TextBlock, URLSource, UserMsg
from agentscope.model import ChatModelBase
from agentscope.state import AgentState
from pydantic import BaseModel, Field

from .catalog import AgentSpec
from .credit import credit_from_usage

Status = Literal["input_required", "completed"]


class AgentReply(BaseModel):
    status: Status = Field(description="input_required：需要用户补充信息；completed：已交付结果")
    reply: str = Field(description="直接展示给用户的回复内容")


@dataclass(frozen=True)
class AgentResult:
    conversation_id: str
    status: Status
    reply: str
    credit_usage: int


class UnknownAgentError(LookupError):
    pass


class UnknownConversationError(LookupError):
    pass


class ImagesNotSupportedError(ValueError):
    pass


class InvalidImageUrlError(ValueError):
    pass


class AgentOutputError(RuntimeError):
    """The agent finished without a structured reply (e.g. interrupted or out of iterations)."""


class ReplyingAgent(Protocol):
    async def reply(self, inputs: Msg, structured_schema: type[BaseModel] | None = None) -> Msg: ...

    def checkpoint(self) -> object: ...

    def restore(self, snapshot: object) -> None: ...


AgentFactory = Callable[[AgentSpec], ReplyingAgent]

_RESTORABLE_STATE_FIELDS = (
    "summary",
    "context",
    "reply_context",
    "tool_context",
    "tasks_context",
    "middle_context",
)


class RollbackAgent:
    """agentscope Agent whose memory can be restored to before a failed turn."""

    def __init__(self, agent: Agent) -> None:
        self._agent = agent

    async def reply(self, inputs: Msg, structured_schema: type[BaseModel] | None = None) -> Msg:
        return await self._agent.reply(inputs, structured_schema=structured_schema)

    def checkpoint(self) -> AgentState:
        return self._agent.state.model_copy(deep=True)

    def restore(self, snapshot: AgentState) -> None:
        # Restore field by field: the agent's permission engine is bound to the live state object.
        for name in _RESTORABLE_STATE_FIELDS:
            setattr(self._agent.state, name, getattr(snapshot, name))


def image_media_type(url: str) -> str:
    media_type, _ = mimetypes.guess_type(urlparse(url).path)
    return media_type if media_type and media_type.startswith("image/") else "image/jpeg"


def build_user_msg(message: str, image_urls: Sequence[str]) -> Msg:
    blocks: list[TextBlock | DataBlock] = [TextBlock(text=message)]
    blocks += [
        DataBlock(source=URLSource(url=url, media_type=image_media_type(url)))
        for url in image_urls
    ]
    return UserMsg(name="user", content=blocks)


@dataclass
class _Conversation:
    agent_id: str
    agent: ReplyingAgent
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)


class ProviderAgentRuntime:
    """Each conversation_id owns one agent instance, so its memory carries across turns."""

    def __init__(self, specs: dict[str, AgentSpec], agent_factory: AgentFactory) -> None:
        self._specs = specs
        self._factory = agent_factory
        self._conversations: dict[str, _Conversation] = {}

    async def ask(
        self,
        agent_id: str,
        message: str,
        image_urls: Sequence[str] = (),
        conversation_id: str | None = None,
    ) -> AgentResult:
        spec = self._specs.get(agent_id)
        if spec is None:
            raise UnknownAgentError(f"Unknown agent: {agent_id}")
        if image_urls and not spec.supports_image:
            raise ImagesNotSupportedError(f"Agent {agent_id} does not accept images")
        insecure = [url for url in image_urls if urlparse(url).scheme != "https"]
        if insecure:
            # Non-https URLs (notably file://) would make the model formatter read local files.
            raise InvalidImageUrlError(f"Image URLs must use https: {', '.join(insecure)}")

        if conversation_id is None:
            conversation = _Conversation(agent_id, self._factory(spec))
            conversation_id = f"conv_{uuid.uuid4().hex}"
            is_new = True
        else:
            conversation = self._conversations.get(conversation_id)
            if conversation is None or conversation.agent_id != agent_id:
                raise UnknownConversationError(f"Conversation not found or expired: {conversation_id}")
            is_new = False

        async with conversation.lock:
            snapshot = conversation.agent.checkpoint()
            try:
                msg = await conversation.agent.reply(
                    build_user_msg(message, image_urls),
                    structured_schema=AgentReply,
                )
                if msg.structured_output is None:
                    raise AgentOutputError(
                        f"Agent produced no structured reply (finished_reason={msg.finished_reason})"
                    )
                parsed = AgentReply.model_validate(msg.structured_output)
                credit_usage = credit_from_usage(msg.usage)
            except BaseException:
                # A failed turn is not billed and must not stay in memory, so the user can retry.
                conversation.agent.restore(snapshot)
                raise
        if is_new:
            # Register only after a successful first turn.
            self._conversations[conversation_id] = conversation
        return AgentResult(conversation_id, parsed.status, parsed.reply, credit_usage)


def make_agent_factory(model: ChatModelBase) -> AgentFactory:
    def factory(spec: AgentSpec) -> RollbackAgent:
        return RollbackAgent(
            Agent(
                name=spec.agent_id,
                system_prompt=spec.system_prompt,
                model=model,
                # Surface cancellation/timeouts as CancelledError instead of an empty reply.
                react_config=ReActConfig(interruption_raise_cancelled_error=True),
            )
        )

    return factory
