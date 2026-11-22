"""Embedding tool shared by onboarding and recommendation (design §6.4)."""
from __future__ import annotations

from collections.abc import Sequence
from typing import Any, Protocol

from ...config import Settings
from ...llm import make_openai_client

MAX_BATCH = 10  # text-embedding-v4 accepts at most 10 inputs per request on the OpenAI-compatible API


class Embedder(Protocol):
    model: str
    dim: int

    async def embed(self, texts: Sequence[str]) -> list[list[float]]: ...


class DashScopeEmbedder:
    def __init__(self, client: Any, model: str, dim: int) -> None:
        self._client = client  # an openai.AsyncOpenAI pointed at Bailian
        self.model = model
        self.dim = dim

    async def embed(self, texts: Sequence[str]) -> list[list[float]]:
        vectors: list[list[float]] = []
        for start in range(0, len(texts), MAX_BATCH):
            batch = list(texts[start : start + MAX_BATCH])
            response = await self._client.embeddings.create(
                model=self.model, input=batch, dimensions=self.dim, encoding_format="float"
            )
            ordered = sorted(response.data, key=lambda item: item.index)
            if len(ordered) != len(batch):
                raise RuntimeError(f"Embedding returned {len(ordered)} vectors for {len(batch)} texts")
            vectors.extend(list(item.embedding) for item in ordered)
        return vectors


def make_embedder(settings: Settings) -> DashScopeEmbedder:
    return DashScopeEmbedder(make_openai_client(settings), settings.embedding_model, settings.embedding_dim)
