"""Recommender: recall by vector, keep each agent's best match, re-check the registry, threshold, top_k (design §6.4)."""
from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass

from ..registry import AgentRecord
from .embedding import Embedder
from .vector_store import Kind, QdrantVectorStore


@dataclass(frozen=True)
class Recommendation:
    agent_id: str
    display_name: str
    provider_id: str
    callable: bool
    score: float
    matched_kind: Kind
    matched_text: str  # the profile or example question that matched best: the "why" shown to the user


class Recommender:
    def __init__(
        self,
        embedder: Embedder,
        store: QdrantVectorStore,
        registry: Callable[[], Mapping[str, AgentRecord]],
        *,
        recall_top_n: int = 20,  # how many agents (not points) to recall before the registry check and threshold
        top_k: int = 2,
        min_score: float | None = None,
    ) -> None:
        self._embedder = embedder
        self._store = store
        self._registry = registry  # read on every call so delisting takes effect at once (design §13.10)
        self._recall_top_n = recall_top_n
        self._top_k = top_k
        self._min_score = min_score

    async def recommend(
        self, query: str, *, with_image: bool = False, include_mock: bool = False
    ) -> list[Recommendation]:
        [vector] = await self._embedder.embed([query])
        # One hit per agent (its best match), best agents first.
        hits = self._store.search_agents(
            vector, self._recall_top_n, callable_only=not include_mock, require_image=with_image
        )

        registry = self._registry()  # the index can lag behind the registry, so confirm every candidate
        picks: list[Recommendation] = []
        for hit in hits:
            record = registry.get(hit.agent_id)
            if record is None or not record.published or (not include_mock and not record.callable):
                continue
            if self._min_score is not None and hit.score < self._min_score:
                continue
            picks.append(
                Recommendation(
                    hit.agent_id, record.display_name, record.provider_id, record.callable, hit.score, hit.kind, hit.text
                )
            )
        return picks[: self._top_k]
