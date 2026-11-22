"""Recommender: recall agents by vector, re-check the registry, optional rerank, threshold, top_k (design §6.4)."""
from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace

from ..registry import AgentRecord
from .embedding import Embedder
from .profile import rerank_document
from .rerank import Reranker
from .vector_store import Kind, QdrantVectorStore


@dataclass(frozen=True)
class Recommendation:
    agent_id: str
    display_name: str
    provider_id: str
    callable: bool
    dense_score: float
    matched_kind: Kind
    matched_text: str  # the profile or example question that matched best: the "why" shown to the user
    rerank_score: float | None = None

    @property
    def score(self) -> float:
        """What ranking and the threshold use: the rerank score when the candidates were reranked."""
        return self.dense_score if self.rerank_score is None else self.rerank_score


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
        reranker: Reranker | None = None,
        kinds: Sequence[Kind] | None = None,  # ("profile",) = recommend without example questions (V1)
    ) -> None:
        self._embedder = embedder
        self._store = store
        self._registry = registry  # read on every call so delisting takes effect at once (design §13.10)
        self._recall_top_n = recall_top_n
        self._top_k = top_k
        self._min_score = min_score
        self._reranker = reranker
        self._kinds = kinds

    async def recommend(
        self, query: str, *, with_image: bool = False, include_mock: bool = False
    ) -> list[Recommendation]:
        picks = await self.rank(query, with_image=with_image, include_mock=include_mock)
        if self._min_score is not None:
            picks = [pick for pick in picks if pick.score >= self._min_score]
        return picks[: self._top_k]

    async def rank(
        self,
        query: str,
        *,
        with_image: bool = False,
        include_mock: bool = False,
        vector: Sequence[float] | None = None,
    ) -> list[Recommendation]:
        """Every recalled agent that passes the registry check, best first, before threshold and top_k."""
        if vector is None:
            [vector] = await self._embedder.embed([query])
        hits = self._store.search_agents(
            vector,
            self._recall_top_n,
            callable_only=not include_mock,
            require_image=with_image,
            kinds=self._kinds,
        )
        registry = self._registry()  # the index can lag behind the registry, so confirm every candidate
        picks: list[Recommendation] = []
        records: list[AgentRecord] = []
        for hit in hits:
            record = registry.get(hit.agent_id)
            if record is None or not record.published or (not include_mock and not record.callable):
                continue
            picks.append(
                Recommendation(
                    hit.agent_id, record.display_name, record.provider_id, record.callable, hit.score, hit.kind, hit.text
                )
            )
            records.append(record)
        if self._reranker is not None and picks:
            scores = await self._reranker.rerank(query, [rerank_document(record) for record in records])
            reranked = (replace(pick, rerank_score=score) for pick, score in zip(picks, scores, strict=True))
            picks = sorted(reranked, key=lambda pick: pick.score, reverse=True)
        return picks
