"""Factories for Bailian (DashScope) model clients."""
from agentscope.credential import DashScopeCredential
from agentscope.model import DashScopeChatModel
from openai import AsyncOpenAI

from .config import Settings


def make_chat_model(settings: Settings, model_name: str) -> DashScopeChatModel:
    """Non-streaming qwen chat model with thinking disabled (cheaper, faster)."""
    return DashScopeChatModel(
        credential=DashScopeCredential(
            api_key=settings.dashscope_api_key,
            base_url=settings.dashscope_base_url,
        ),
        model=model_name,
        parameters=DashScopeChatModel.Parameters(thinking_enable=False),
        stream=False,
    )


def make_openai_client(settings: Settings) -> AsyncOpenAI:
    """OpenAI-compatible client for Bailian embeddings (and rerank later)."""
    return AsyncOpenAI(
        api_key=settings.dashscope_api_key,
        base_url=settings.dashscope_base_url,
    )
