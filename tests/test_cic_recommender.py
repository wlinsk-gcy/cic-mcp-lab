import pytest
from qdrant_client import QdrantClient

from cic_mcp_lab.cic.recommend.recommender import Recommender
from cic_mcp_lab.cic.recommend.vector_store import AgentPoint, QdrantVectorStore
from cic_mcp_lab.cic.registry import AgentRecord


class FixedEmbedder:
    model, dim = "fake", 3

    def __init__(self, vector):
        self.vector = vector

    async def embed(self, texts):
        return [list(self.vector) for _ in texts]


def record(agent_id, *, callable=True, status="published"):
    endpoint = ("http://h/mcp", "K", agent_id) if callable else (None, None, None)
    return AgentRecord(agent_id, "p", agent_id.upper(), "d", *endpoint, 0.0001, status=status)


def point(agent_id, kind, text, vector, *, callable=True, supports_image=False):
    return AgentPoint(agent_id, "p", kind, text, "published", supports_image, callable, vector)


@pytest.fixture
def store():
    store = QdrantVectorStore(QdrantClient(":memory:"), embedding_model="fake", dim=3)
    store.replace_agent("trip", [point("trip", "profile", "旅行规划", [1, 0, 0]), point("trip", "example", "规划日本行程", [0.95, 0.05, 0])])
    store.replace_agent("report", [point("report", "profile", "周报撰写", [0.3, 1, 0])])
    store.replace_agent("hotel", [point("hotel", "profile", "酒店推荐", [0.9, 0.3, 0], callable=False)])
    store.replace_agent("copy", [point("copy", "profile", "商品文案", [0.5, 0, 0.9], supports_image=True)])
    yield store
    store.close()


REGISTRY = {
    "trip": record("trip"),
    "report": record("report"),
    "hotel": record("hotel", callable=False),
    "copy": record("copy"),
}


def recommender(store, query, registry=REGISTRY, **options):
    return Recommender(FixedEmbedder(query), store, lambda: registry, **options)


def ids(recommendations):
    return [r.agent_id for r in recommendations]


async def test_each_agent_is_listed_once_with_its_best_match(store):
    recs = await recommender(store, [0.95, 0.05, 0], top_k=3).recommend("q")

    assert ids(recs) == ["trip", "copy", "report"]
    assert (recs[0].matched_kind, recs[0].matched_text, recs[0].display_name) == ("example", "规划日本行程", "TRIP")
    assert recs[0].score == pytest.approx(1.0)


async def test_mock_agents_only_when_asked(store):
    assert "hotel" not in ids(await recommender(store, [1, 0, 0], top_k=4).recommend("q"))
    recs = await recommender(store, [1, 0, 0], top_k=2).recommend("q", include_mock=True)

    assert ids(recs) == ["trip", "hotel"]
    assert recs[1].callable is False


async def test_attached_image_limits_to_image_capable_agents(store):
    assert ids(await recommender(store, [1, 0, 0], top_k=4).recommend("q", with_image=True)) == ["copy"]


async def test_threshold_and_top_k(store):
    recs = await recommender(store, [1, 0, 0], top_k=5, min_score=0.6).recommend("q")

    assert ids(recs) == ["trip"]


async def test_registry_is_rechecked_because_the_index_can_lag(store):
    delisted = {**REGISTRY, "trip": record("trip", status="delisted")}
    del delisted["report"]

    assert ids(await recommender(store, [1, 0, 0], delisted, top_k=4).recommend("q")) == ["copy"]
