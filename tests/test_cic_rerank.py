import pytest

from cic_mcp_lab.cic.recommend.profile import profile_text, rerank_document
from cic_mcp_lab.cic.recommend.rerank import INSTRUCT, DashScopeReranker, rerank_base_url
from cic_mcp_lab.cic.registry import AgentRecord

RECORD = AgentRecord(
    "trip", "provider-a", "旅行规划", "规划行程", None, None, None, 0.0001,
    category=("出行",), tags=("行程",), examples=("去日本玩 5 天", "云南一周路线"),
)


class FakeClient:
    def __init__(self, results):
        self.results = results
        self.calls = []

    async def post(self, path, *, body, cast_to):
        self.calls.append((path, body))
        return {"object": "list", "results": self.results}


def test_rerank_document_is_the_profile_plus_examples():
    assert rerank_document(RECORD) == profile_text(RECORD) + "\n示例问题：去日本玩 5 天；云南一周路线"


async def test_scores_come_back_in_document_order():
    client = FakeClient([{"index": 1, "relevance_score": 0.9}, {"index": 0, "relevance_score": 0.2}])
    reranker = DashScopeReranker(client, "qwen3-rerank")

    assert await reranker.rerank("q", ["a", "b"]) == [0.2, 0.9]
    path, body = client.calls[0]
    assert path == "/reranks"
    assert body == {"model": "qwen3-rerank", "query": "q", "documents": ["a", "b"], "instruct": INSTRUCT}


async def test_missing_scores_are_an_error_and_nothing_to_rank_makes_no_request():
    client = FakeClient([{"index": 0, "relevance_score": 0.2}])
    reranker = DashScopeReranker(client, "qwen3-rerank")

    with pytest.raises(RuntimeError, match="1"):
        await reranker.rerank("q", ["a", "b"])
    assert await reranker.rerank("q", []) == []
    assert len(client.calls) == 1


def test_rerank_endpoint_shares_the_host_of_the_chat_endpoint():
    assert rerank_base_url("https://ws.cn-beijing.maas.aliyuncs.com/compatible-mode/v1") == (
        "https://ws.cn-beijing.maas.aliyuncs.com/compatible-api/v1"
    )
    with pytest.raises(ValueError, match="rerank"):
        rerank_base_url("https://example.com/v1")
