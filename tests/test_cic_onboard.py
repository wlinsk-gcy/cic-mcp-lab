from dataclasses import replace

import pytest
from qdrant_client import QdrantClient

from cic_mcp_lab.cic.contract import EntryTool
from cic_mcp_lab.cic.onboard import onboard, profile_text
from cic_mcp_lab.cic.probe import STEPS, ProbeReport, ProbeStep
from cic_mcp_lab.cic.recommend.vector_store import QdrantVectorStore
from cic_mcp_lab.cic.registry import AgentRecord

ENV = {"K": "sk-test"}
MOCK = AgentRecord(
    "hotel", "provider-mock", "酒店推荐", "按预算推荐酒店", None, None, None, 0.0001,
    category=("出行",), tags=("酒店", "住宿"), examples=("e1", "e2", "e3"), supports_image=True,
)
REAL = AgentRecord(
    "trip", "provider-a", "旅行规划", "规划行程", "http://h/mcp", "K", "trip", 0.0001,
    category=("出行", "生活服务"), tags=("行程",), examples=("e1", "e2", "e3", "e4"),
)


class CountingEmbedder:
    model, dim = "fake", 3

    def __init__(self):
        self.texts = []

    async def embed(self, texts):
        self.texts.extend(texts)
        return [[1.0, float(i), 0.0] for i, _ in enumerate(texts)]


class NoEmbedding:
    model, dim = "fake", 3

    async def embed(self, texts):
        raise AssertionError("embedding should not run")


def passing_probe(supports_image):
    async def prober(record, api_key):
        assert api_key == "sk-test"
        steps = tuple(ProbeStep(name, True, "") for name in STEPS)
        return ProbeReport(record.agent_id, steps, True, EntryTool(record.entry_tool, "d", supports_image), "fp", "now")

    return prober


async def failing_probe(record, api_key):
    steps = (ProbeStep("discover", True, ""), ProbeStep("auth", False, "anonymous tools/list -> HTTP 200"))
    return ProbeReport(record.agent_id, steps, True, None, "fp", "now")


async def no_probe(record, api_key):
    raise AssertionError("test connection should not run")


@pytest.fixture
def store():
    store = QdrantVectorStore(QdrantClient(":memory:"), embedding_model="fake", dim=3)
    yield store
    store.close()


def test_profile_text_combines_the_portal_fields():
    assert profile_text(REAL) == "名称：旅行规划\n分类：出行, 生活服务\n标签：行程\n描述：规划行程"


async def test_mock_agent_is_indexed_without_a_test_connection(store):
    embedder = CountingEmbedder()
    result = await onboard(MOCK, embedder=embedder, store=store, prober=no_probe, env=ENV)

    assert (result.action, result.points) == ("indexed", 4)
    assert embedder.texts == [profile_text(MOCK), "e1", "e2", "e3"]
    hits = store.search([1, 0, 0], 10, callable_only=False, require_image=True)
    assert sorted({(hit.agent_id, hit.kind) for hit in hits}) == [("hotel", "example"), ("hotel", "profile")]
    assert store.search([1, 0, 0], 10) == []  # mock agents are never callable


async def test_callable_agent_takes_image_support_from_its_entry_tool(store):
    result = await onboard(REAL, embedder=CountingEmbedder(), store=store, prober=passing_probe(True), env=ENV)

    assert (result.action, result.points) == ("indexed", 5)
    assert {hit.agent_id for hit in store.search([1, 0, 0], 10, require_image=True)} == {"trip"}


async def test_failed_test_connection_blocks_and_keeps_the_approved_version(store):
    await onboard(REAL, embedder=CountingEmbedder(), store=store, prober=passing_probe(False), env=ENV)
    edited = replace(REAL, examples=("x1", "x2", "x3"))
    result = await onboard(edited, embedder=NoEmbedding(), store=store, prober=failing_probe, env=ENV)

    assert result.action == "blocked"
    assert "auth" in result.detail
    assert len(store.search([1, 0, 0], 10)) == 5


async def test_missing_api_key_blocks(store):
    result = await onboard(REAL, embedder=NoEmbedding(), store=store, prober=no_probe, env={})

    assert result.action == "blocked"
    assert "environment variable K" in result.detail


async def test_delisting_only_syncs_the_status(store):
    await onboard(REAL, embedder=CountingEmbedder(), store=store, prober=passing_probe(False), env=ENV)
    result = await onboard(replace(REAL, status="delisted"), embedder=NoEmbedding(), store=store, prober=no_probe, env=ENV)

    assert (result.action, result.points) == ("status_synced", 5)
    assert store.search([1, 0, 0], 10) == []
