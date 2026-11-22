"""Vector store for agent profiles and example questions (design §4.5); Qdrant local mode in the lab.

Production would swap in pgvector behind the same methods.
"""
from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from qdrant_client import QdrantClient
from qdrant_client.models import (
    Distance,
    FieldCondition,
    Filter,
    FilterSelector,
    MatchAny,
    MatchValue,
    PointStruct,
    ScoredPoint,
    VectorParams,
)

COLLECTION = "agents"
Kind = Literal["profile", "example"]


@dataclass(frozen=True)
class AgentPoint:
    agent_id: str
    provider_id: str
    kind: Kind
    text: str
    status: str
    supports_image: bool
    callable: bool
    vector: list[float]


@dataclass(frozen=True)
class SearchHit:
    agent_id: str
    kind: Kind
    text: str
    score: float


def _match(key: str, value: object) -> FieldCondition:
    return FieldCondition(key=key, match=MatchValue(value=value))


def _agent(agent_id: str) -> Filter:
    return Filter(must=[_match("agent_id", agent_id)])


def _searchable(callable_only: bool, require_image: bool, kinds: Sequence[Kind] | None = None) -> Filter:
    must = [_match("status", "published")]
    if callable_only:
        must.append(_match("callable", True))
    if require_image:
        must.append(_match("supports_image", True))
    if kinds:
        must.append(FieldCondition(key="kind", match=MatchAny(any=list(kinds))))
    return Filter(must=must)


def _hit(point: ScoredPoint) -> SearchHit:
    payload = point.payload or {}
    return SearchHit(payload["agent_id"], payload["kind"], payload["text"], point.score)


class QdrantVectorStore:
    """Only `published` points are searchable; delisting flips the payload status (design §13.10)."""

    def __init__(self, client: QdrantClient, *, embedding_model: str, dim: int) -> None:
        self._client = client
        self._embedding_model = embedding_model
        self._dim = dim
        if not client.collection_exists(COLLECTION):
            client.create_collection(COLLECTION, vectors_config=VectorParams(size=dim, distance=Distance.COSINE))
            return
        size = client.get_collection(COLLECTION).config.params.vectors.size
        if size != dim:
            raise ValueError(
                f"Collection {COLLECTION!r} holds {size}-dim vectors but {dim} are configured; "
                "delete the local store and re-onboard every agent"
            )

    @classmethod
    def open(cls, path: Path, *, embedding_model: str, dim: int) -> QdrantVectorStore:
        """The on-disk local store; only one process may have it open at a time."""
        return cls(QdrantClient(path=str(path)), embedding_model=embedding_model, dim=dim)

    def close(self) -> None:
        self._client.close()

    def replace_agent(self, agent_id: str, points: Sequence[AgentPoint]) -> None:
        """Re-onboarding replaces every point of the agent (design §4.5)."""
        self.delete_agent(agent_id)
        counters: dict[str, int] = {}
        structs: list[PointStruct] = []
        for point in points:
            if point.agent_id != agent_id:
                raise ValueError(f"Point for {point.agent_id!r} passed with agent {agent_id!r}")
            index = counters.get(point.kind, 0)
            counters[point.kind] = index + 1
            structs.append(
                PointStruct(
                    id=str(uuid.uuid5(uuid.NAMESPACE_URL, f"{agent_id}/{point.kind}/{index}")),
                    vector=list(point.vector),
                    payload={
                        "agent_id": point.agent_id,
                        "provider_id": point.provider_id,
                        "kind": point.kind,
                        "text": point.text,
                        "status": point.status,
                        "supports_image": point.supports_image,
                        "callable": point.callable,
                        "embedding_model": self._embedding_model,
                        "dim": self._dim,
                    },
                )
            )
        if structs:
            self._client.upsert(COLLECTION, points=structs)

    def delete_agent(self, agent_id: str) -> None:
        self._client.delete(COLLECTION, points_selector=FilterSelector(filter=_agent(agent_id)))

    def set_status(self, agent_id: str, status: str) -> int:
        """Change the listing status of every point of the agent; returns how many points it has."""
        count = self._client.count(COLLECTION, count_filter=_agent(agent_id), exact=True).count
        if count:
            self._client.set_payload(COLLECTION, payload={"status": status}, points=_agent(agent_id))
        return count

    def agent_ids(self) -> set[str]:
        ids: set[str] = set()
        offset = None
        while True:
            points, offset = self._client.scroll(COLLECTION, limit=256, offset=offset, with_payload=["agent_id"])
            ids.update(str(point.payload["agent_id"]) for point in points if point.payload)
            if offset is None:
                return ids

    def search(
        self,
        vector: Sequence[float],
        top_n: int,
        *,
        callable_only: bool = True,
        require_image: bool = False,
    ) -> list[SearchHit]:
        """The best `top_n` points; one agent may fill several of them."""
        result = self._client.query_points(
            COLLECTION,
            query=list(vector),
            query_filter=_searchable(callable_only, require_image),
            limit=top_n,
            with_payload=True,
        )
        return [_hit(point) for point in result.points if point.payload]

    def search_agents(
        self,
        vector: Sequence[float],
        top_agents: int,
        *,
        callable_only: bool = True,
        require_image: bool = False,
        kinds: Sequence[Kind] | None = None,
    ) -> list[SearchHit]:
        """Each agent's single best point, best agents first, for up to `top_agents` agents.

        `kinds=("profile",)` looks at profiles only: recommendation as if the example questions were deleted (V1).
        """
        result = self._client.query_points_groups(
            COLLECTION,
            query=list(vector),
            group_by="agent_id",
            group_size=1,
            limit=top_agents,
            query_filter=_searchable(callable_only, require_image, kinds),
            with_payload=True,
        )
        return [_hit(group.hits[0]) for group in result.groups if group.hits and group.hits[0].payload]
