import pytest
from qdrant_client import QdrantClient

from cic_mcp_lab.cic.recommend.vector_store import AgentPoint, QdrantVectorStore


def make_store(dim=3):
    return QdrantVectorStore(QdrantClient(":memory:"), embedding_model="fake", dim=dim)


def point(agent_id, kind, text, vector, *, callable=True, supports_image=False, status="published"):
    return AgentPoint(agent_id, "p", kind, text, status, supports_image, callable, vector)


@pytest.fixture
def store():
    store = make_store()
    store.replace_agent("trip", [point("trip", "profile", "旅行规划", [1, 0, 0]), point("trip", "example", "去日本玩", [0.9, 0.1, 0])])
    store.replace_agent("hotel", [point("hotel", "profile", "酒店推荐", [0.8, 0.2, 0], callable=False)])
    store.replace_agent("poster", [point("poster", "profile", "海报设计", [0.6, 0, 0.8], callable=False, supports_image=True)])
    yield store
    store.close()


def agents(hits):
    return [hit.agent_id for hit in hits]


def test_search_returns_published_callable_points_best_first(store):
    hits = store.search([1, 0, 0], 10)

    assert [(hit.agent_id, hit.kind, hit.text) for hit in hits] == [("trip", "profile", "旅行规划"), ("trip", "example", "去日本玩")]
    assert hits[0].score == pytest.approx(1.0)


def test_mock_agents_and_image_filter(store):
    assert agents(store.search([1, 0, 0], 10, callable_only=False)) == ["trip", "trip", "hotel", "poster"]
    assert agents(store.search([1, 0, 0], 10, callable_only=False, require_image=True)) == ["poster"]


def test_reonboarding_replaces_every_point_of_the_agent(store):
    store.replace_agent("trip", [point("trip", "profile", "旅行规划 v2", [1, 0, 0])])

    assert [hit.text for hit in store.search([1, 0, 0], 10)] == ["旅行规划 v2"]
    assert store.agent_ids() == {"trip", "hotel", "poster"}


def test_delisted_points_are_not_searchable_until_published_again(store):
    assert store.set_status("trip", "delisted") == 2
    assert store.search([1, 0, 0], 10) == []
    assert store.set_status("trip", "published") == 2
    assert agents(store.search([1, 0, 0], 10)) == ["trip", "trip"]
    assert store.set_status("unknown", "delisted") == 0


def test_deleted_agents_disappear(store):
    store.delete_agent("hotel")

    assert store.agent_ids() == {"trip", "poster"}


def test_existing_collection_with_another_dimension_is_rejected():
    client = QdrantClient(":memory:")
    QdrantVectorStore(client, embedding_model="fake", dim=3)

    with pytest.raises(ValueError, match="re-onboard"):
        QdrantVectorStore(client, embedding_model="fake", dim=4)
