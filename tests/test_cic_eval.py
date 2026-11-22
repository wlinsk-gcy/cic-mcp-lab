from qdrant_client import QdrantClient

from cic_mcp_lab.cic.eval import build_recommenders, render_report, run_eval
from cic_mcp_lab.cic.eval_metrics import EvalQuery, accuracy
from cic_mcp_lab.cic.recommend.vector_store import AgentPoint, QdrantVectorStore
from cic_mcp_lab.cic.registry import AgentRecord

ITEMS = [EvalQuery("想去旅行", "trip", "regular"), EvalQuery("讲个笑话", None, "none")]
VECTORS = {"想去旅行": [0, 1, 0], "讲个笑话": [0, 0, 1]}


class LookupEmbedder:
    model, dim = "fake", 3

    def __init__(self):
        self.calls = 0

    async def embed(self, texts):
        self.calls += 1
        return [VECTORS[text] for text in texts]


class NameReranker:
    async def rerank(self, query, documents):
        return [0.9 if "名称：旅行规划\n" in document else 0.2 for document in documents]


def record(agent_id, name):
    return AgentRecord(agent_id, "p", name, "d", "http://h/mcp", "K", agent_id, 0.0001, examples=("e1", "e2", "e3"))


async def test_variants_share_one_embedding_and_are_reported_side_by_side():
    store = QdrantVectorStore(QdrantClient(":memory:"), embedding_model="fake", dim=3)
    # trip matches the query only through its example question; report's profile is the closer profile.
    store.replace_agent("trip", [
        AgentPoint("trip", "p", "profile", "旅行规划", "published", False, True, [1, 0, 0]),
        AgentPoint("trip", "p", "example", "出去玩", "published", False, True, [0, 1, 0]),
    ])
    store.replace_agent("report", [AgentPoint("report", "p", "profile", "周报撰写", "published", False, True, [0.5, 0.866, 0])])
    registry = {"trip": record("trip", "旅行规划"), "report": record("report", "周报撰写")}
    embedder = LookupEmbedder()

    recommenders = build_recommenders(embedder, store, lambda: registry, NameReranker(), ["V1", "V2", "V3"])
    results = await run_eval(ITEMS, recommenders, embedder)
    report = render_report(results, names={"trip": "旅行规划", "report": "周报撰写"}, header_lines=["测试"])
    store.close()

    assert embedder.calls == len(ITEMS)
    assert [accuracy(results[name]).hit1 for name in ("V1", "V2", "V3")] == [0.0, 1.0, 1.0]
    assert results["V3"][0].ranking[0] == ("trip", 0.9)
    assert "| V2 |" in report and "## 错例" in report and "想去旅行" in report
