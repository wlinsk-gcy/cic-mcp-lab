from types import SimpleNamespace

from cic_mcp_lab.cic.recommend.embedding import MAX_BATCH, DashScopeEmbedder


class FakeEmbeddings:
    def __init__(self):
        self.calls = []

    async def create(self, *, model, input, dimensions, encoding_format):
        self.calls.append((model, list(input), dimensions))
        data = [SimpleNamespace(index=i, embedding=[float(len(text)), float(dimensions)]) for i, text in enumerate(input)]
        return SimpleNamespace(data=list(reversed(data)))  # the API does not promise any order


async def test_texts_are_embedded_in_batches_and_keep_their_order():
    fake = FakeEmbeddings()
    embedder = DashScopeEmbedder(SimpleNamespace(embeddings=fake), "text-embedding-v4", 1024)
    texts = ["x" * (i + 1) for i in range(23)]

    vectors = await embedder.embed(texts)

    assert [len(batch) for _, batch, _ in fake.calls] == [MAX_BATCH, MAX_BATCH, 3]
    assert {(model, dim) for model, _, dim in fake.calls} == {("text-embedding-v4", 1024)}
    assert [vector[0] for vector in vectors] == [float(i + 1) for i in range(23)]


async def test_nothing_to_embed_makes_no_request():
    fake = FakeEmbeddings()
    embedder = DashScopeEmbedder(SimpleNamespace(embeddings=fake), "text-embedding-v4", 1024)

    assert await embedder.embed([]) == []
    assert fake.calls == []
