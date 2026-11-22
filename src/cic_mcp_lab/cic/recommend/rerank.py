"""Rerank tool (design §6.4 step 4): qwen3-rerank via Bailian's OpenAI-compatible /reranks endpoint."""
from __future__ import annotations

from collections.abc import Sequence
from typing import Any, Protocol

from openai import AsyncOpenAI

from ...config import Settings

# Bailian asks for English instructions; the default instruction targets question answering, not routing.
INSTRUCT = "Given a user's request, retrieve the descriptions of assistants that can fulfil it."


class Reranker(Protocol):
    async def rerank(self, query: str, documents: Sequence[str]) -> list[float]: ...


class DashScopeReranker:
    def __init__(self, client: Any, model: str, *, instruct: str = INSTRUCT) -> None:
        self._client = client  # an openai.AsyncOpenAI pointed at .../compatible-api/v1
        self.model = model
        self._instruct = instruct

    async def rerank(self, query: str, documents: Sequence[str]) -> list[float]:
        """A relevance score for every document, in the documents' order."""
        if not documents:
            return []
        response = await self._client.post(
            "/reranks",
            body={"model": self.model, "query": query, "documents": list(documents), "instruct": self._instruct},
            cast_to=object,
        )
        scores = {int(item["index"]): float(item["relevance_score"]) for item in response.get("results") or []}
        missing = [index for index in range(len(documents)) if index not in scores]
        if missing:
            raise RuntimeError(f"Rerank returned no score for documents {missing}")
        return [scores[index] for index in range(len(documents))]


def rerank_base_url(chat_base_url: str) -> str:
    """Bailian serves /reranks under /compatible-api/v1 on the host that serves /compatible-mode/v1."""
    if "/compatible-mode/" not in chat_base_url:
        raise ValueError(f"Cannot derive the rerank endpoint from {chat_base_url!r}")
    return chat_base_url.replace("/compatible-mode/", "/compatible-api/")


def make_reranker(settings: Settings) -> DashScopeReranker:
    client = AsyncOpenAI(api_key=settings.dashscope_api_key, base_url=rerank_base_url(settings.dashscope_base_url))
    return DashScopeReranker(client, settings.rerank_model)
