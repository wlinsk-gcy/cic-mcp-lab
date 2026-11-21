"""Runtime of the mock third-party agents: per-conversation memory, ask-back, credit."""
from __future__ import annotations

import mimetypes
import uuid
from dataclasses import dataclass
from typing import Callable, Literal, Protocol, Sequence
from urllib.parse import urlparse

from agentscope.agent import Agent
from agentscope.message import DataBlock, Msg, TextBlock, URLSource, UserMsg
from agentscope.model import ChatModelBase
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


class ReplyingAgent(Protocol):
    async def reply(self, inputs: Msg, structured_schema: type[BaseModel] | None = None) -> Msg: ...


AgentFactory = Callable[[AgentSpec], ReplyingAgent]


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

        if conversation_id is None:
            conversation = _Conversation(agent_id, self._factory(spec))
            conversation_id = f"conv_{uuid.uuid4().hex}"
            is_new = True
        else:
            conversation = self._conversations.get(conversation_id)
            if conversation is None or conversation.agent_id != agent_id:
                raise UnknownConversationError(f"Conversation not found or expired: {conversation_id}")
            is_new = False

        msg = await conversation.agent.reply(
            build_user_msg(message, image_urls),
            structured_schema=AgentReply,
        )
        parsed = AgentReply.model_validate(msg.structured_output)
        credit_usage = credit_from_usage(msg.usage)
        if is_new:
            # Register only after a successful first turn.
            self._conversations[conversation_id] = conversation
        return AgentResult(conversation_id, parsed.status, parsed.reply, credit_usage)


def make_agent_factory(model: ChatModelBase) -> AgentFactory:
    def factory(spec: AgentSpec) -> Agent:
        return Agent(name=spec.agent_id, system_prompt=spec.system_prompt, model=model)

    return factory
