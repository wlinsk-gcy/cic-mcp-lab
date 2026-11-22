# Step 5：推荐效果评测（V1 / V2 / V3）实施计划

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** 用一套约 40 条的测试问题，量化三种推荐方案的效果，为 SA&D 文档提供数据：
- V1：只用画像，等同于删除全部示例问题；
- V2：画像 + 示例问题；
- V3：V2 + Rerank。

**指标：** Hit@1、Hit@2、MRR（整体与分类别）、得分分布（期望 Agent 的得分 vs 无关问题的最高分）、阈值扫描（拦截率 / 误拦截率）与建议阈值、延迟、逐条错例。

V4（查询改写）依赖 Step 6 的意图识别，届时用同一套评测补跑（决策 29）。

**Architecture:**
- `cic/recommend/rerank.py`：`DashScopeReranker`，调用百炼 OpenAI 兼容接口 `POST /compatible-api/v1/reranks`。
- `cic/recommend/profile.py`：画像文本与 Rerank 文档；`profile_text` 从 `onboard.py` 移到这里。
- `Recommender` 的改动：
  - 新增 `rank()`，返回通过注册表回查的全部候选（不截断、不过滤阈值），供评测使用；
  - 新增 `kinds` 参数（V1 只检索 `kind=profile` 的点）和可选的 `reranker` 参数；
  - `Recommendation` 同时保留 `dense_score` 和 `rerank_score`，`score` 优先取 Rerank 分。
- `cic/eval_metrics.py`：测试集加载与纯函数指标。
- `cic/eval.py`：评测执行器 + Markdown 报告 + `cic-eval` 命令。

每条问题只 Embedding 一次，三个方案共用同一个向量，以保证对比公平；V3 额外调用一次 Rerank。报告写入 `reports/`（gitignore）。

**Tech Stack:** Python 3.12、uv、openai SDK（`AsyncOpenAI.post(..., cast_to=object)` 调用 rerank）、qdrant-client 1.20（`query_points_groups`）、PyYAML、pytest + pytest-asyncio。

**已验证的前提（2026-11-22 实测）：**
- 百炼 rerank：在 chat 接口所在 host 上请求 `POST https://<host>/compatible-api/v1/reranks`，body 为 `{model, query, documents, top_n?, instruct?}`。返回 `{"object":"list","results":[{"index":0,"relevance_score":0.45},...],"usage":{"total_tokens":132}}`，按相关度排序，`index` 指向输入文档。`qwen3-rerank` 处理 3 个文档耗时约 0.33s。官方文档注明 `instruct` 用英文书写，默认为问答检索。
- `AsyncOpenAI(base_url=".../compatible-api/v1").post("/reranks", body=..., cast_to=object)` 返回 dict。
- Step 4 的索引中有 15 个 Agent，每个 Agent 有 1 个画像点和 3～4 个示例问题点；`search_agents` 用 `query_points_groups` 按 agent 分组。

**约定：**
- 所有命令都在 `D:\develop\accentrixProject\cic-mcp-lab` 下执行。
- 每次提交附带 `Co-authored-by: Copilot <223556219+Copilot@users.noreply.github.com>`。
- `docs/` 不提交；`img.png`、`step1_test_result.md` ～ `step4_test_result.md` 属于用户，不要动；不读取、不打印 `.env`；测试中不调用真实模型。
- 一律用 `uv run --no-sync ...`，因为用户可能开着 provider-server，导致自动同步失败。新增的 `cic-eval` 由用户执行 `uv sync` 后安装。

---

### Task 1: Rerank 相关配置

**Files:**
- Modify: `src/cic_mcp_lab/config.py`
- Test: `tests/test_config.py`（追加）

**要点：** 三个可选配置：
- `RERANK_MODEL`：默认 `qwen3-rerank`；
- `RERANK_ENABLED`：默认 false，接受 `true/false/1/0/yes/no/on/off`，不区分大小写；
- `RECOMMEND_MIN_SCORE_RERANK`：默认不设阈值。

格式错误时抛 `ConfigError`，报错信息包含变量名。

**Step 1: 追加失败的测试** — `tests/test_config.py`：

```python
def test_rerank_settings_have_defaults():
    settings = load_settings(BASE_ENV)

    assert (settings.rerank_model, settings.rerank_enabled, settings.recommend_min_score_rerank) == (
        "qwen3-rerank",
        False,
        None,
    )


def test_rerank_settings_are_parsed():
    settings = load_settings(
        {**BASE_ENV, "RERANK_MODEL": "qwen3.7-text-rerank", "RERANK_ENABLED": "True", "RECOMMEND_MIN_SCORE_RERANK": "0.4"}
    )

    assert (settings.rerank_model, settings.rerank_enabled, settings.recommend_min_score_rerank) == (
        "qwen3.7-text-rerank",
        True,
        0.4,
    )


def test_malformed_rerank_switch_is_reported():
    with pytest.raises(ConfigError, match="RERANK_ENABLED"):
        load_settings({**BASE_ENV, "RERANK_ENABLED": "maybe"})
```

**Step 2:** `uv run --no-sync pytest tests/test_config.py -q` → 新测试失败

**Step 3: 实现** — `config.py`：

- `Settings` 末尾追加：

```python
    rerank_model: str = "qwen3-rerank"
    rerank_enabled: bool = False
    recommend_min_score_rerank: float | None = None  # chosen by the step 5 evaluation; unset = no threshold
```

- 模块级（`ConfigError` 之后）新增：

```python
def boolean(raw: str) -> bool:
    value = raw.strip().lower()
    if value in {"true", "1", "yes", "on"}:
        return True
    if value in {"false", "0", "no", "off"}:
        return False
    raise ValueError(raw)
```

- `Settings(...)` 调用中追加：

```python
        rerank_model=env.get("RERANK_MODEL", "").strip() or "qwen3-rerank",
        rerank_enabled=optional("RERANK_ENABLED", boolean, False),
        recommend_min_score_rerank=optional("RECOMMEND_MIN_SCORE_RERANK", float, None),
```

**Step 4:** `uv run --no-sync pytest tests/test_config.py -q` → 全部通过；`uv run --no-sync pytest -q` → `142 passed`

**Step 5: Commit**

```powershell
git add src/cic_mcp_lab/config.py tests/test_config.py
git commit -m "feat: rerank settings (model, switch, threshold)" -m "Co-authored-by: Copilot <223556219+Copilot@users.noreply.github.com>"
```

---

### Task 2: Rerank 工具 + 画像文本模块

**Files:**
- Create: `src/cic_mcp_lab/cic/recommend/profile.py`、`src/cic_mcp_lab/cic/recommend/rerank.py`
- Modify: `src/cic_mcp_lab/cic/onboard.py`（`profile_text` 改为从 `recommend/profile.py` 导入）
- Test: `tests/test_cic_rerank.py`

**要点：**
- 设计文档 §6.4 第 4 步：Rerank 的 document = 画像文本 + 示例问题。
- `profile_text` 移到 `recommend/profile.py`，onboard 和 Rerank 共用；`onboard.py` 继续 `from .recommend.profile import profile_text`，因此 `from cic_mcp_lab.cic.onboard import profile_text` 仍然可用，现有测试不用改。
- Rerank 的 base URL 由 chat 的 base URL 推导：把 `/compatible-mode/` 换成 `/compatible-api/`，host 不变。
- 返回的分数按输入文档顺序对齐；有文档缺分数时报错；文档列表为空时不发请求。
- `instruct` 使用英文（官方要求），针对「为用户请求找能完成它的助手」的场景。

**Step 1: 写失败的测试** — `tests/test_cic_rerank.py`：

```python
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
```

**Step 2:** `uv run --no-sync pytest tests/test_cic_rerank.py -q` → FAIL（`ModuleNotFoundError`）

**Step 3: 实现**

`src/cic_mcp_lab/cic/recommend/profile.py`：

```python
"""Texts that represent an agent for recall and rerank (design §4.5, §6.4)."""
from __future__ import annotations

from ..registry import AgentRecord


def profile_text(record: AgentRecord) -> str:
    """One paragraph per agent from the Portal's basic fields (design §4.5)."""
    return "\n".join(
        [
            f"名称：{record.display_name}",
            f"分类：{', '.join(record.category)}",
            f"标签：{', '.join(record.tags)}",
            f"描述：{record.description}",
        ]
    )


def rerank_document(record: AgentRecord) -> str:
    """What the reranker reads for an agent: the profile plus its example questions (design §6.4)."""
    return f"{profile_text(record)}\n示例问题：{'；'.join(record.examples)}"
```

`src/cic_mcp_lab/cic/recommend/rerank.py`：

```python
"""Rerank tool (design §6.4 step 4): qwen3-rerank via Bailian's OpenAI-compatible /reranks endpoint."""
from __future__ import annotations

from collections.abc import Sequence
from typing import Any, Protocol

from openai import AsyncOpenAI

from ...config import Settings

# Bailian asks for English instructions; the default instruction targets question answering, not routing.
INSTRUCT = "Given a user's request, retrieve the descriptions of assistants that can fulfil it."


class Reranker(Protocol):
    async def rerank(self, query: str, documents: Sequence[str]) -> list[float]: ...


class DashScopeReranker:
    def __init__(self, client: Any, model: str, *, instruct: str = INSTRUCT) -> None:
        self._client = client  # an openai.AsyncOpenAI pointed at .../compatible-api/v1
        self.model = model
        self._instruct = instruct

    async def rerank(self, query: str, documents: Sequence[str]) -> list[float]:
        """A relevance score for every document, in the documents' order."""
        if not documents:
            return []
        response = await self._client.post(
            "/reranks",
            body={"model": self.model, "query": query, "documents": list(documents), "instruct": self._instruct},
            cast_to=object,
        )
        scores = {int(item["index"]): float(item["relevance_score"]) for item in response.get("results") or []}
        missing = [index for index in range(len(documents)) if index not in scores]
        if missing:
            raise RuntimeError(f"Rerank returned no score for documents {missing}")
        return [scores[index] for index in range(len(documents))]


def rerank_base_url(chat_base_url: str) -> str:
    """Bailian serves /reranks under /compatible-api/v1 on the host that serves /compatible-mode/v1."""
    if "/compatible-mode/" not in chat_base_url:
        raise ValueError(f"Cannot derive the rerank endpoint from {chat_base_url!r}")
    return chat_base_url.replace("/compatible-mode/", "/compatible-api/")


def make_reranker(settings: Settings) -> DashScopeReranker:
    client = AsyncOpenAI(api_key=settings.dashscope_api_key, base_url=rerank_base_url(settings.dashscope_base_url))
    return DashScopeReranker(client, settings.rerank_model)
```

`src/cic_mcp_lab/cic/onboard.py`：删除其中的 `profile_text` 函数定义，改为在 import 区加 `from .recommend.profile import profile_text`（其余代码不变）。

**Step 4:** `uv run --no-sync pytest tests/test_cic_rerank.py tests/test_cic_onboard.py -q` → 全部通过；`uv run --no-sync pytest -q` → `146 passed`

**Step 5: Commit**

```powershell
git add src/cic_mcp_lab/cic/recommend/profile.py src/cic_mcp_lab/cic/recommend/rerank.py src/cic_mcp_lab/cic/onboard.py tests/test_cic_rerank.py
git commit -m "feat: qwen3-rerank tool; share agent profile texts between onboarding and rerank" -m "Co-authored-by: Copilot <223556219+Copilot@users.noreply.github.com>"
```

---

### Task 3: 向量库按 kind 过滤 + Recommender 支持 `rank()` / V1 / Rerank

**Files:**
- Modify: `src/cic_mcp_lab/cic/recommend/vector_store.py`、`src/cic_mcp_lab/cic/recommend/recommender.py`、`src/cic_mcp_lab/cic/recommend/cli.py`
- Test: `tests/test_cic_vector_store.py`、`tests/test_cic_recommender.py`（追加）

**要点：**
- `search_agents(..., kinds=None)`：`kinds=("profile",)` 时只在画像点里分组召回，即 V1。
- `Recommendation`：
  - 字段改为 `dense_score` + 可选的 `rerank_score`；
  - `score` 改为属性，有 Rerank 分时取 Rerank 分；
  - 阈值和排序都按 `score`。
- `Recommender`：
  - 新增 `kinds`、`reranker` 参数；
  - 新增 `rank(query, *, with_image, include_mock, vector=None)`：返回通过注册表回查的全部候选（按 `score` 倒序）；`vector` 由调用方传入时跳过 Embedding（评测时多个方案共用同一个向量）；
  - `recommend()` = `rank()` → 阈值 → top_k。
- `cic-recommend` 新增 `--rerank`（也可以设置 `RERANK_ENABLED=true`）：使用 Rerank 时阈值取 `RECOMMEND_MIN_SCORE_RERANK`，否则取 `RECOMMEND_MIN_SCORE_DENSE`。

**Step 1: 追加失败的测试**

`tests/test_cic_vector_store.py`：

```python
def test_agent_search_can_be_limited_to_profiles(store):
    assert store.search_agents([0.9, 0.1, 0], 5)[0].kind == "example"
    [hit] = store.search_agents([0.9, 0.1, 0], 5, kinds=("profile",))

    assert (hit.agent_id, hit.kind, hit.text) == ("trip", "profile", "旅行规划")
```

`tests/test_cic_recommender.py`：

```python
class FakeReranker:
    """Scores a document by the agent name in it (the test records' display names are upper-cased ids)."""

    def __init__(self, scores):
        self.scores = scores
        self.queries = []

    async def rerank(self, query, documents):
        self.queries.append(query)
        return [next(score for name, score in self.scores.items() if f"名称：{name}\n" in doc) for doc in documents]


async def test_rank_keeps_every_candidate_for_the_evaluation(store):
    ranked = await recommender(store, [1, 0, 0], top_k=1, min_score=0.9).rank("q")

    assert ids(ranked) == ["trip", "copy", "report"]


async def test_profile_only_variant_ignores_example_questions(store):
    best = (await recommender(store, [0.95, 0.05, 0], kinds=("profile",)).rank("q"))[0]

    assert (best.agent_id, best.matched_kind) == ("trip", "profile")
    assert best.score == pytest.approx(0.9986, abs=1e-3)


async def test_reranker_reorders_and_the_threshold_applies_to_its_score(store):
    reranker = FakeReranker({"TRIP": 0.5, "COPY": 0.1, "REPORT": 0.9})
    recs = await recommender(store, [1, 0, 0], top_k=3, min_score=0.3, reranker=reranker).recommend("北海道")

    assert ids(recs) == ["report", "trip"]
    assert (recs[0].rerank_score, round(recs[0].dense_score, 2)) == (0.9, 0.29)
    assert reranker.queries == ["北海道"]
```

**Step 2:** `uv run --no-sync pytest tests/test_cic_vector_store.py tests/test_cic_recommender.py -q` → 新测试失败

**Step 3: 实现**

`vector_store.py`：
- import 增加 `MatchAny`；
- `_searchable(callable_only, require_image)` 改为 `_searchable(callable_only, require_image, kinds=None)`，`kinds` 非空时追加 `FieldCondition(key="kind", match=MatchAny(any=list(kinds)))`；
- `search_agents` 增加关键字参数 `kinds: Sequence[Kind] | None = None` 并传给 `_searchable`；docstring 补充「`kinds=("profile",)` 时只看画像，即删除示例问题后的效果（V1）」。
- `search`（点级别检索）不变。

`recommender.py`（整体替换）：

```python
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
```

`recommend/cli.py`：
- `main()` 增加 `parser.add_argument("--rerank", action="store_true", help="用 Rerank 精排（也可设置 RERANK_ENABLED=true）")`；
- `_recommend` 中：`use_rerank = args.rerank or settings.rerank_enabled`；构造 `Recommender` 时传 `reranker=make_reranker(settings) if use_rerank else None`，`min_score=settings.recommend_min_score_rerank if use_rerank else settings.recommend_min_score_dense`；从 `.rerank` 导入 `make_reranker`；
- 输出：标题改为 `f"为你找到以下 Agent（按匹配度排序{'，Rerank' if use_rerank else ''}，{elapsed:.2f}s）："`；每行的匹配度在有 Rerank 分时写成 `f"匹配度 {pick.score:.2f}（向量 {pick.dense_score:.2f}）"`，否则保持 `f"匹配度 {pick.score:.2f}"`。

**Step 4:** `uv run --no-sync pytest tests/test_cic_vector_store.py tests/test_cic_recommender.py -q` → 全部通过；`uv run --no-sync pytest -q` → `150 passed`；`uv run --no-sync python -c "import sys; sys.argv=['cic-recommend','--help']; from cic_mcp_lab.cic.recommend.cli import main; main()"` 输出中有 `--rerank`。

**Step 5: Commit**

```powershell
git add src/cic_mcp_lab/cic/recommend/vector_store.py src/cic_mcp_lab/cic/recommend/recommender.py src/cic_mcp_lab/cic/recommend/cli.py tests/test_cic_vector_store.py tests/test_cic_recommender.py
git commit -m "feat: recommender rank(), profile-only variant and optional rerank" -m "Co-authored-by: Copilot <223556219+Copilot@users.noreply.github.com>"
```

---

### Task 4: 测试集 `data/eval_queries.yaml` + 加载

**Files:**
- Create: `data/eval_queries.yaml`、`src/cic_mcp_lab/cic/eval_metrics.py`（本 Task 只写测试集加载部分）
- Test: `tests/test_cic_eval_set.py`

**要点（设计文档 §7）：** 共 40 条：常规 20、易混淆 10、依赖上下文 5、无合适 Agent 5。
- 每条问题标注期望排第 1 的 Agent；「无合适 Agent」类的期望为 null。
- 3 条附带图片（`image: true`），只在支持图片的 Agent 中检索。
- 「依赖上下文」类带 `history`（前几轮对话）。V1～V3 只用最后一句检索，用来量化「不做查询改写」的损失；V4 会用到 `history`。
- 测试问题不得与任何已登记的示例问题原文相同，避免数据泄漏；用测试保证这一点。
- 15 个 Agent 都至少作为期望出现一次。

**Step 1: 写失败的测试** — `tests/test_cic_eval_set.py`：

```python
from collections import Counter

import pytest

from cic_mcp_lab.cic.eval_metrics import load_eval_queries
from cic_mcp_lab.cic.registry import load_registry

IMAGE_CAPABLE = {"product_copy", "poster_design", "product_cutout"}


def test_query_set_has_the_planned_mix():
    items = load_eval_queries()

    assert Counter(item.category for item in items) == {"regular": 20, "confusable": 10, "context": 5, "none": 5}
    assert all(item.history for item in items if item.category == "context")
    assert sum(item.image for item in items) == 3


def test_every_agent_is_expected_somewhere_and_image_queries_fit_image_agents():
    registry = load_registry()
    items = load_eval_queries()

    assert {item.expect for item in items if item.expect} == set(registry)
    assert all(item.expect in IMAGE_CAPABLE for item in items if item.image)


def test_no_query_repeats_a_registered_example_question():
    examples = {example for record in load_registry().values() for example in record.examples}

    assert [item.query for item in load_eval_queries() if item.query in examples] == []


def test_expect_must_be_null_exactly_for_no_fit_queries(tmp_path):
    path = tmp_path / "queries.yaml"
    path.write_text("queries:\n  - {query: 讲个笑话, expect: trip_planner, category: none}\n", encoding="utf-8")

    with pytest.raises(ValueError, match="expect"):
        load_eval_queries(path)
```

**Step 2:** `uv run --no-sync pytest tests/test_cic_eval_set.py -q` → FAIL（`ModuleNotFoundError`）

**Step 3: 实现**

`src/cic_mcp_lab/cic/eval_metrics.py`（本 Task 写到 `load_eval_queries` 为止，Task 5 继续追加）：

```python
"""Recommendation evaluation (design §7): the query set and pure metric functions."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import yaml

from ..config import PROJECT_ROOT

DEFAULT_QUERIES_PATH = PROJECT_ROOT / "data" / "eval_queries.yaml"
CATEGORIES = ("regular", "confusable", "context", "none")
CATEGORY_LABELS = {"regular": "常规", "confusable": "易混淆", "context": "依赖上下文", "none": "无合适 Agent"}
Category = Literal["regular", "confusable", "context", "none"]


@dataclass(frozen=True)
class EvalQuery:
    query: str
    expect: str | None  # the agent that should come first; None = no onboarded agent fits
    category: Category
    image: bool = False  # the user attached an image, so only image-capable agents qualify
    history: tuple[str, ...] = ()  # earlier turns; V1-V3 search with the last message only (rewriting is V4)


def load_eval_queries(path: Path = DEFAULT_QUERIES_PATH) -> list[EvalQuery]:
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    items: list[EvalQuery] = []
    for index, raw in enumerate(data.get("queries") or []):
        where = f"queries[{index}]"
        category = raw.get("category")
        if category not in CATEGORIES:
            raise ValueError(f"{where}: category must be one of {CATEGORIES}, got {category!r}")
        expect = raw.get("expect")
        if (expect is None) != (category == "none"):
            raise ValueError(f"{where}: expect must be null exactly when the category is 'none'")
        query = str(raw.get("query") or "").strip()
        if not query:
            raise ValueError(f"{where}: query is empty")
        history = tuple(str(turn) for turn in raw.get("history") or ())
        items.append(EvalQuery(query, expect, category, bool(raw.get("image", False)), history))
    return items
```

`data/eval_queries.yaml`：

```yaml
# Recommendation test set (design §7). Never copy an agent's registered example question here (data leakage).
# expect: the agent that should come first; null = no onboarded agent fits (category none).
# image: true = the user attached an image, so only image-capable agents qualify.
# history: earlier turns; V1-V3 search with the last message only, V4 (step 6) will rewrite with the history.
queries:
  # ---- regular: clear requests ----
  - {query: 五一想带孩子去西安玩三天，帮我做个攻略, expect: trip_planner, category: regular}
  - {query: 去新加坡出差顺便玩两天，帮我排一下每天去哪, expect: trip_planner, category: regular}
  - {query: 帮我给这款蓝牙耳机写一段电商详情页介绍, expect: product_copy, category: regular}
  - {query: 新款防晒霜要上架了，帮忙写几句吸引人的商品描述, expect: product_copy, category: regular}
  - {query: 这周主要在做数据迁移和性能优化，帮我整理成周报, expect: weekly_report, category: regular}
  - {query: 帮我把这周写的代码和修的 bug 汇总成工作周报, expect: weekly_report, category: regular}
  - {query: 杭州西湖边有没有安静一点的酒店推荐, expect: hotel_booking, category: regular}
  - {query: 去泰国旅游要不要提前办签证, expect: visa_consult, category: regular}
  - {query: 第一次办日本签证，流程是怎样的, expect: visa_consult, category: regular}
  - {query: 下周五深圳飞成都，哪个航班便宜, expect: flight_deals, category: regular}
  - {query: 帮我把需求评审会的讨论整理成纪要，列出待办, expect: meeting_minutes, category: regular}
  - {query: 给合作方写一封邮件，说明项目要延期一周, expect: email_writer, category: regular}
  - {query: 用英文给海外客户写一封新品推广邮件, expect: email_writer, category: regular}
  - {query: 季度业务复盘要做演示文稿，先帮我列个提纲, expect: ppt_outline, category: regular}
  - {query: 帮店铺的周年庆活动设计一张宣传海报, expect: poster_design, category: regular}
  - {query: 这张衣服照片背景太乱了，帮我抠成透明底, expect: product_cutout, category: regular, image: true}
  - {query: 明晚直播卖洗发水，帮我准备一套讲解话术, expect: livestream_script, category: regular}
  - {query: 被公司无故辞退了，能要多少赔偿, expect: legal_consult, category: regular}
  - {query: 想练出腹肌，每周应该怎么安排训练, expect: fitness_coach, category: regular}
  - {query: 家里有排骨和玉米，晚饭做什么好, expect: recipe_helper, category: regular}

  # ---- confusable: must be told apart from a look-alike agent ----
  - {query: 去大阪玩 4 天，住哪个区域方便, expect: hotel_booking, category: confusable}
  - {query: 帮我安排一下去巴黎的十日游，每天去哪些景点, expect: trip_planner, category: confusable}
  - {query: 下周去东京，往返机票什么时候买最划算, expect: flight_deals, category: confusable}
  - {query: 去美国读书要准备什么签证材料, expect: visa_consult, category: confusable}
  - {query: 把今天的部门例会记录整理一下，标出每个人的任务, expect: meeting_minutes, category: confusable}
  - {query: 把本周三个项目的进展写成给老板的汇报, expect: weekly_report, category: confusable}
  - {query: 把这份周报内容做成几页汇报用的幻灯片, expect: ppt_outline, category: confusable}
  - {query: 给这款口红的照片配一段小红书文案, expect: product_copy, category: confusable, image: true}
  - {query: 用这张商品图做一张双十二的促销主图, expect: poster_design, category: confusable, image: true}
  - {query: 直播间要推一款空气炸锅，开场怎么说, expect: livestream_script, category: confusable}

  # ---- context: the last message alone is ambiguous ----
  - query: 帮我整理成给领导看的版本
    expect: weekly_report
    category: context
    history: [用户：这周我主要做了接口联调和需求评审，还修了几个线上问题]
  - query: 帮我整理一下要点
    expect: meeting_minutes
    category: context
    history: [用户：我们刚开完产品评审会，讨论了上线时间和分工]
  - query: 那住的地方呢
    expect: hotel_booking
    category: context
    history: [用户：国庆想去日本玩五天]
  - query: 帮我写点能吸引人下单的话
    expect: product_copy
    category: context
    history: [用户：我在淘宝开了家手工皂店，最近上了新品]
  - query: 需要提前办什么手续吗
    expect: visa_consult
    category: context
    history: [用户：下个月要去英国参加学术会议]

  # ---- none: no onboarded agent fits ----
  - {query: 今天北京天气怎么样, expect: null, category: none}
  - {query: 帮我算一下 123 乘以 456 等于多少, expect: null, category: none}
  - {query: 给我讲个笑话, expect: null, category: none}
  - {query: Python 里怎么反转一个列表, expect: null, category: none}
  - {query: 最近有什么好看的电影推荐, expect: null, category: none}
```

**Step 4:** `uv run --no-sync pytest tests/test_cic_eval_set.py -q` → `4 passed`；`uv run --no-sync pytest -q` → `154 passed`

**Step 5: Commit**

```powershell
git add data/eval_queries.yaml src/cic_mcp_lab/cic/eval_metrics.py tests/test_cic_eval_set.py
git commit -m "feat: recommendation test set (40 queries across four categories)" -m "Co-authored-by: Copilot <223556219+Copilot@users.noreply.github.com>"
```

---

### Task 5: 指标纯函数

**Files:**
- Modify: `src/cic_mcp_lab/cic/eval_metrics.py`（追加）
- Test: `tests/test_cic_eval_metrics.py`

**要点：**
- `Scored`：表示一条问题在某个方案下的排序结果（`(agent_id, score)` 按分数倒序）和耗时。
- `accuracy`：只统计有期望 Agent 的问题，计算 Hit@1、Hit@2、MRR。期望 Agent 不在排序中时，倒数排名记 0。
- `accuracy_by_category`：按类别分别计算。
- 阈值：
  - 一条问题被「拦截」= 它的最高分低于阈值；
  - **拦截率** = 无合适 Agent 类中被拦截的比例；
  - **误拦截率** = 有期望 Agent 的问题中被拦截的比例；
  - `best_threshold` 在相邻最高分的中点里，选出使（拦截率 − 误拦截率）最大的阈值；相同时取误拦截率更低的。
- `separation`：期望 Agent 排第 1 时得分的最低 / 平均值，与无合适 Agent 类最高分的最高 / 平均值，对应设计文档 §7.1 的「分差」。
- `latency`：耗时的平均值与 P95。

**Step 1: 写失败的测试** — `tests/test_cic_eval_metrics.py`：

```python
import pytest

from cic_mcp_lab.cic.eval_metrics import (
    EvalQuery,
    Scored,
    accuracy,
    accuracy_by_category,
    best_threshold,
    latency,
    separation,
    threshold_stats,
)


def scored(expect, ranking, category="regular", latency_ms=10.0):
    item = EvalQuery("q", expect, "none" if expect is None else category)
    return Scored(item, tuple(ranking), latency_ms)


RESULTS = [
    scored("a", [("a", 0.8), ("b", 0.5)]),  # hit at 1
    scored("a", [("b", 0.7), ("a", 0.6)], "confusable"),  # hit at 2
    scored("a", [("b", 0.4), ("c", 0.3)], "confusable"),  # expected agent not ranked
    scored(None, [("b", 0.45)]),
    scored(None, [("c", 0.2)], latency_ms=30.0),
]


def test_accuracy_counts_only_queries_with_an_expected_agent():
    overall = accuracy(RESULTS)
    by_category = accuracy_by_category(RESULTS)

    assert (overall.n, overall.hit1, overall.hit2) == (3, pytest.approx(1 / 3), pytest.approx(2 / 3))
    assert overall.mrr == pytest.approx((1 + 0.5) / 3)
    assert set(by_category) == {"regular", "confusable"}
    assert (by_category["confusable"].n, by_category["confusable"].hit2) == (2, 0.5)


def test_queries_below_the_threshold_are_intercepted():
    stats = threshold_stats(RESULTS, 0.5)

    assert (stats.intercept_rate, stats.false_intercept_rate) == (1.0, pytest.approx(1 / 3))


def test_best_threshold_maximises_intercepted_minus_wrongly_intercepted():
    best = best_threshold(RESULTS)

    assert best.threshold == pytest.approx(0.575)  # halfway between 0.45 (no fit) and 0.7
    assert (best.intercept_rate, best.false_intercept_rate) == (1.0, pytest.approx(1 / 3))


def test_score_separation_and_latency():
    gap = separation(RESULTS)

    assert (gap.expected_min, gap.expected_mean, gap.none_max) == (0.8, 0.8, 0.45)
    assert gap.none_mean == pytest.approx(0.325)
    assert latency(RESULTS) == (pytest.approx(14.0), 30.0)
```

> 说明：
> - `best_threshold` 的候选阈值：最高分去重排序后为 `0.2, 0.4, 0.45, 0.7, 0.8`，候选取相邻两值的中点 `0.3, 0.425, 0.575, 0.75`，再加上低于最小值和高于最大值各一个。各候选的（拦截率 − 误拦截率）分别为 0.3→0.5、0.425→0.167、0.575→0.667、0.75→0.333，所以选 0.575。
> - P95 用最近秩法：`sorted(values)[ceil(0.95 * n) - 1]`。n=5 时取第 5 个值，即 30.0。

**Step 2:** `uv run --no-sync pytest tests/test_cic_eval_metrics.py -q` → FAIL（`ImportError`）

**Step 3: 实现** — 在 `eval_metrics.py` 末尾追加（import 区补充 `import math`、`from collections.abc import Callable, Sequence`）：

```python
@dataclass(frozen=True)
class Scored:
    """One query ranked by one variant: (agent_id, score) pairs, best first."""

    item: EvalQuery
    ranking: tuple[tuple[str, float], ...]
    latency_ms: float

    @property
    def top_score(self) -> float:
        return self.ranking[0][1] if self.ranking else 0.0

    def rank_of_expected(self) -> int | None:
        for position, (agent_id, _) in enumerate(self.ranking, 1):
            if agent_id == self.item.expect:
                return position
        return None


@dataclass(frozen=True)
class Accuracy:
    n: int
    hit1: float
    hit2: float
    mrr: float


@dataclass(frozen=True)
class ThresholdStats:
    threshold: float
    intercept_rate: float  # no-fit queries whose best score falls below the threshold (correctly blocked)
    false_intercept_rate: float  # queries with an expected agent that get blocked as well


@dataclass(frozen=True)
class Separation:
    expected_min: float | None  # lowest / mean score of the expected agent where it ranked first
    expected_mean: float | None
    none_max: float | None  # highest / mean best score among no-fit queries
    none_mean: float | None


def _share(results: Sequence[Scored], predicate: Callable[[Scored], bool]) -> float:
    return sum(1 for result in results if predicate(result)) / len(results) if results else 0.0


def _positives(results: Sequence[Scored]) -> list[Scored]:
    return [result for result in results if result.item.expect is not None]


def _no_fit(results: Sequence[Scored]) -> list[Scored]:
    return [result for result in results if result.item.expect is None]


def accuracy(results: Sequence[Scored]) -> Accuracy:
    """Hit@1, Hit@2 and MRR over the queries that have an expected agent."""
    positives = _positives(results)
    if not positives:
        return Accuracy(0, 0.0, 0.0, 0.0)
    ranks = [result.rank_of_expected() for result in positives]
    n = len(positives)
    return Accuracy(
        n,
        sum(1 for rank in ranks if rank == 1) / n,
        sum(1 for rank in ranks if rank is not None and rank <= 2) / n,
        sum(1 / rank for rank in ranks if rank) / n,
    )


def accuracy_by_category(results: Sequence[Scored]) -> dict[str, Accuracy]:
    present = {result.item.category for result in _positives(results)}
    return {
        category: accuracy([result for result in results if result.item.category == category])
        for category in CATEGORIES
        if category in present
    }


def threshold_stats(results: Sequence[Scored], threshold: float) -> ThresholdStats:
    def blocked(result: Scored) -> bool:
        return result.top_score < threshold

    return ThresholdStats(threshold, _share(_no_fit(results), blocked), _share(_positives(results), blocked))


def best_threshold(results: Sequence[Scored]) -> ThresholdStats:
    """The cut that best separates no-fit queries from the rest: max(intercept − false intercept)."""
    scores = sorted({result.top_score for result in results})
    if not scores:
        return ThresholdStats(0.0, 0.0, 0.0)
    candidates = [scores[0] - 0.01, *((low + high) / 2 for low, high in zip(scores, scores[1:])), scores[-1] + 0.01]
    stats = [threshold_stats(results, candidate) for candidate in candidates]
    return max(stats, key=lambda s: (s.intercept_rate - s.false_intercept_rate, -s.false_intercept_rate))


def separation(results: Sequence[Scored]) -> Separation:
    expected = [result.top_score for result in _positives(results) if result.rank_of_expected() == 1]
    no_fit = [result.top_score for result in _no_fit(results)]
    return Separation(
        min(expected) if expected else None,
        sum(expected) / len(expected) if expected else None,
        max(no_fit) if no_fit else None,
        sum(no_fit) / len(no_fit) if no_fit else None,
    )


def latency(results: Sequence[Scored]) -> tuple[float, float]:
    """Mean and 95th-percentile (nearest rank) latency in milliseconds."""
    values = sorted(result.latency_ms for result in results)
    if not values:
        return 0.0, 0.0
    return sum(values) / len(values), values[math.ceil(0.95 * len(values)) - 1]
```

> `max(..., key=(J, -false))`：J 相同时取误拦截率更低的；仍相同时 `max` 返回第一个出现的，也就是阈值更低的。

**Step 4:** `uv run --no-sync pytest tests/test_cic_eval_metrics.py -q` → `4 passed`；`uv run --no-sync pytest -q` → `158 passed`

**Step 5: Commit**

```powershell
git add src/cic_mcp_lab/cic/eval_metrics.py tests/test_cic_eval_metrics.py
git commit -m "feat: evaluation metrics (hit@k, mrr, threshold sweep, score separation, latency)" -m "Co-authored-by: Copilot <223556219+Copilot@users.noreply.github.com>"
```

---

### Task 6: 评测执行器 + 报告 + `cic-eval`

**Files:**
- Create: `src/cic_mcp_lab/cic/eval.py`
- Modify: `pyproject.toml`（增加 `cic-eval = "cic_mcp_lab.cic.eval:main"`）、`README.md`、`.gitignore`（增加 `reports/`）
- Test: `tests/test_cic_eval.py`

**要点：**
- 三个方案共用同一个 Embedder、向量库和注册表：
  - V1 = `Recommender(kinds=("profile",))`
  - V2 = `Recommender()`
  - V3 = `Recommender(reranker=...)`
- 评测时 `recall_top_n=50`，即召回全部 Agent；`include_mock=True`；附带图片的问题设 `with_image=True`。
- 每条问题只 Embedding 一次，各方案共用这个向量。耗时 = Embedding 耗时 + 该方案的检索（+ Rerank）耗时。
- 报告（Markdown）包含：
  - 评测设置；
  - 总体指标表：Hit@1/2、MRR、分类别 Hit@1、分差、建议阈值、拦截率 / 误拦截率、平均 / P95 耗时；
  - 每个方案的阈值扫描（0.20～0.80，步长 0.05）；
  - 每个方案的错例（期望 Agent 未排第 1 的问题）及实际前 3 名；
  - 逐条结果对照表（每条问题在各方案下的期望 Agent 排名 / 最高分）。
- `cic-eval [--variants V1,V2,V3] [--output PATH]`：报告默认写入 `reports/step5-eval-<YYYYMMDD-HHMM>.md`，终端打印总体指标表和报告路径。期望 Agent 不在注册表或已下架时，在报告开头给出警告（这会拉低指标）。

**Step 1: 写失败的测试** — `tests/test_cic_eval.py`：

```python
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
```

**Step 2:** `uv run --no-sync pytest tests/test_cic_eval.py -q` → FAIL（`ModuleNotFoundError`）

**Step 3: 实现** — `src/cic_mcp_lab/cic/eval.py`：

```python
"""cic-eval: compare recommendation variants on data/eval_queries.yaml and write a Markdown report (design §7)."""
from __future__ import annotations

import argparse
import asyncio
import sys
import time
from collections.abc import Callable, Mapping, Sequence
from datetime import datetime
from pathlib import Path

from ..config import PROJECT_ROOT, load_settings
from .eval_metrics import (
    CATEGORY_LABELS,
    EvalQuery,
    Scored,
    accuracy,
    accuracy_by_category,
    best_threshold,
    latency,
    load_eval_queries,
    separation,
    threshold_stats,
)
from .recommend.embedding import Embedder, make_embedder
from .recommend.recommender import Recommender
from .recommend.rerank import INSTRUCT, Reranker, make_reranker
from .recommend.vector_store import QdrantVectorStore
from .registry import AgentRecord, load_registry

VARIANTS = ("V1", "V2", "V3")
DESCRIPTIONS = {"V1": "只用画像（等同于删除全部示例问题）", "V2": "画像 + 示例问题", "V3": "画像 + 示例问题 + Rerank"}
SWEEP = tuple(round(0.20 + 0.05 * step, 2) for step in range(13))  # 0.20 … 0.80
EVAL_RECALL = 50  # recall every onboarded agent: the evaluation ranks the whole field


def build_recommenders(
    embedder: Embedder,
    store: QdrantVectorStore,
    registry: Callable[[], Mapping[str, AgentRecord]],
    reranker: Reranker | None,
    variants: Sequence[str],
) -> dict[str, Recommender]:
    options: dict[str, dict] = {"V1": {"kinds": ("profile",)}, "V2": {}, "V3": {"reranker": reranker}}
    if "V3" in variants and reranker is None:
        raise ValueError("V3 needs a reranker")
    return {
        name: Recommender(embedder, store, registry, recall_top_n=EVAL_RECALL, **options[name]) for name in variants
    }


async def run_eval(
    items: Sequence[EvalQuery], recommenders: Mapping[str, Recommender], embedder: Embedder
) -> dict[str, list[Scored]]:
    """Rank every query with every variant; each query is embedded once and shared by all variants."""
    results: dict[str, list[Scored]] = {name: [] for name in recommenders}
    for item in items:
        started = time.perf_counter()
        [vector] = await embedder.embed([item.query])
        embed_ms = (time.perf_counter() - started) * 1000
        for name, recommender in recommenders.items():
            started = time.perf_counter()
            ranking = await recommender.rank(item.query, vector=vector, with_image=item.image, include_mock=True)
            elapsed_ms = embed_ms + (time.perf_counter() - started) * 1000
            results[name].append(Scored(item, tuple((pick.agent_id, pick.score) for pick in ranking), elapsed_ms))
    return results


def _pct(value: float) -> str:
    return f"{value * 100:.1f}%"


def _num(value: float | None) -> str:
    return "—" if value is None else f"{value:.2f}"


def render_report(
    results: Mapping[str, Sequence[Scored]], *, names: Mapping[str, str], header_lines: Sequence[str]
) -> str:
    def name_of(agent_id: str | None) -> str:
        return "—" if agent_id is None else names.get(agent_id, agent_id)

    lines = ["# 推荐效果评测报告（Step 5）", "", *(f"- {line}" for line in header_lines), "", "## 总体指标", ""]
    lines += [
        "| 方案 | 说明 | Hit@1 | Hit@2 | MRR | 常规 Hit@1 | 易混淆 Hit@1 | 依赖上下文 Hit@1 "
        "| 期望 Agent 得分（最低 / 平均） | 无关问题最高分（最高 / 平均） | 建议阈值 | 拦截率 | 误拦截率 | 耗时（平均 / P95） |",
        "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for name, scored in results.items():
        overall, by_category = accuracy(scored), accuracy_by_category(scored)
        gap, best = separation(scored), best_threshold(scored)
        mean_ms, p95_ms = latency(scored)

        def hit1(category: str) -> str:
            return _pct(by_category[category].hit1) if category in by_category else "—"

        lines.append(
            f"| {name} | {DESCRIPTIONS[name]} | {_pct(overall.hit1)} | {_pct(overall.hit2)} | {overall.mrr:.3f} "
            f"| {hit1('regular')} | {hit1('confusable')} | {hit1('context')} "
            f"| {_num(gap.expected_min)} / {_num(gap.expected_mean)} | {_num(gap.none_max)} / {_num(gap.none_mean)} "
            f"| {best.threshold:.3f} | {_pct(best.intercept_rate)} | {_pct(best.false_intercept_rate)} "
            f"| {mean_ms:.0f} / {p95_ms:.0f} ms |"
        )
    lines += [
        "",
        "> - Hit@k / MRR 只统计有期望 Agent 的问题；拦截 = 最高分低于阈值；拦截率针对「无合适 Agent」类，误拦截率针对其余问题。",
        "> - 建议阈值取使（拦截率 − 误拦截率）最大的分数线。V3 的分数是 Rerank 分，与 V1 / V2 的向量分不可直接比较。",
        "",
        "## 阈值扫描",
        "",
    ]
    for name, scored in results.items():
        lines += [f"### {name}", "", "| 阈值 | 无合适 Agent 拦截率 | 正常问题误拦截率 |", "| --- | --- | --- |"]
        for threshold in SWEEP:
            stats = threshold_stats(scored, threshold)
            lines.append(f"| {threshold:.2f} | {_pct(stats.intercept_rate)} | {_pct(stats.false_intercept_rate)} |")
        lines.append("")

    lines += ["## 错例（期望 Agent 未排第 1）", ""]
    for name, scored in results.items():
        misses = [result for result in scored if result.item.expect is not None and result.rank_of_expected() != 1]
        lines += [f"### {name}（{len(misses)} 条）", ""]
        if not misses:
            lines += ["无", ""]
            continue
        lines += ["| 问题 | 类别 | 期望（排名） | 实际前 3 |", "| --- | --- | --- | --- |"]
        for result in misses:
            rank = result.rank_of_expected()
            top3 = "；".join(f"{name_of(agent_id)} {score:.2f}" for agent_id, score in result.ranking[:3])
            lines.append(
                f"| {result.item.query} | {CATEGORY_LABELS[result.item.category]} "
                f"| {name_of(result.item.expect)}（{rank or '未召回'}） | {top3} |"
            )
        lines.append("")

    variants = list(results)
    lines += [
        "## 逐条结果（期望 Agent 的排名 · 最高分）",
        "",
        f"| 问题 | 类别 | 期望 | {' | '.join(variants)} |",
        f"| --- | --- | --- | {' | '.join('---' for _ in variants)} |",
    ]
    for index, row in enumerate(next(iter(results.values()), [])):
        cells = []
        for name in variants:
            result = results[name][index]
            rank = result.rank_of_expected() if row.item.expect else None
            label = "—" if row.item.expect is None else (str(rank) if rank else "未召回")
            cells.append(f"{label} · {result.top_score:.2f}")
        lines.append(
            f"| {row.item.query} | {CATEGORY_LABELS[row.item.category]} | {name_of(row.item.expect)} | {' | '.join(cells)} |"
        )
    return "\n".join(lines) + "\n"


async def _evaluate(variants: list[str], output: Path | None) -> Path:
    settings = load_settings()
    registry = load_registry()
    items = load_eval_queries()
    unavailable = sorted({item.expect for item in items if item.expect and (item.expect not in registry or not registry[item.expect].published)})
    store = QdrantVectorStore.open(settings.qdrant_path, embedding_model=settings.embedding_model, dim=settings.embedding_dim)
    try:
        embedder = make_embedder(settings)
        reranker = make_reranker(settings) if "V3" in variants else None
        recommenders = build_recommenders(embedder, store, load_registry, reranker, variants)
        indexed = len(store.agent_ids())
        results = await run_eval(items, recommenders, embedder)
    finally:
        store.close()

    counts = {category: sum(1 for item in items if item.category == category) for category in CATEGORY_LABELS}
    header = [
        f"时间：{datetime.now():%Y-%m-%d %H:%M}",
        f"Agent：注册表 {len(registry)} 个（其中模拟 {sum(not r.callable for r in registry.values())} 个），索引中 {indexed} 个；全部参与排序",
        f"测试问题：{len(items)} 条（" + " / ".join(f"{CATEGORY_LABELS[c]} {n}" for c, n in counts.items()) + f"），其中 {sum(item.image for item in items)} 条附带图片，只在支持图片的 Agent 中检索",
        f"Embedding：{settings.embedding_model}（{settings.embedding_dim} 维，cosine）；Rerank：{settings.rerank_model}（instruct：{INSTRUCT}）",
        "依赖上下文类只用最后一句检索，没有做查询改写（V4 待 Step 6 补跑）",
        "每条问题只 Embedding 一次，各方案共用；耗时 = Embedding + 检索（+ Rerank）",
    ]
    if unavailable:
        header.insert(0, f"⚠ 以下期望 Agent 不在注册表或已下架，会拉低指标：{', '.join(unavailable)}")
    report = render_report(results, names={a: r.display_name for a, r in registry.items()}, header_lines=header)
    path = output or PROJECT_ROOT / "reports" / f"step5-eval-{datetime.now():%Y%m%d-%H%M}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(report, encoding="utf-8")
    print(report.split("## 阈值扫描")[0].rstrip())
    return path


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description="推荐效果评测：V1（只用画像）/ V2（画像 + 示例问题）/ V3（+ Rerank）")
    parser.add_argument("--variants", default=",".join(VARIANTS), help="逗号分隔，默认 V1,V2,V3")
    parser.add_argument("--output", type=Path, help="报告路径，默认 reports/step5-eval-<时间>.md")
    args = parser.parse_args()
    variants = [variant.strip().upper() for variant in args.variants.split(",") if variant.strip()]
    unknown = [variant for variant in variants if variant not in VARIANTS]
    if unknown or not variants:
        parser.error(f"未知方案：{', '.join(unknown) or '（空）'}；可选 {', '.join(VARIANTS)}")
    path = asyncio.run(_evaluate(variants, args.output))
    print(f"\n完整报告：{path}")
```

> 长行可以按风格折行，不要改变逻辑。

**Step 4: 配套修改**
- `pyproject.toml`：`[project.scripts]` 末尾追加 `cic-eval = "cic_mcp_lab.cic.eval:main"`。
- `.gitignore`：在 `# Local runtime data` 段落中追加一行 `reports/`。
- `README.md`：在 `cic-recommend` 一行之后、`pytest` 一行之前插入：

```markdown
| `uv run cic-eval [--variants V1,V2,V3] [--output <路径>]` | 推荐效果评测：V1 只用画像（等同于删除示例问题）/ V2 画像 + 示例问题 / V3 + Rerank；输出 Hit@1/2、MRR、阈值扫描、错例，报告写入 `reports/` |
```

并把 `cic-recommend` 一行的命令改为 `` `uv run cic-recommend "<问题>" [--image] [--include-mock] [--top-k N] [--rerank]` ``。

**Step 5: 验证**
- `uv run --no-sync pytest tests/test_cic_eval.py -q` → `1 passed`；`uv run --no-sync pytest -q` → `159 passed`
- `uv run --no-sync python -c "import sys; sys.argv=['cic-eval','--help']; from cic_mcp_lab.cic.eval import main; main()"` → 打印帮助
- `uv lock --check` 通过

**Step 6: Commit**

```powershell
git add src/cic_mcp_lab/cic/eval.py tests/test_cic_eval.py pyproject.toml README.md .gitignore
git commit -m "feat: cic-eval compares recommendation variants and writes a markdown report" -m "Co-authored-by: Copilot <223556219+Copilot@users.noreply.github.com>"
```

---

### Task 7: 执行评测（由用户执行）

**准备：** 新增了命令。先停掉所有 provider-server（评测本身不需要 Provider），执行 `uv sync`。索引沿用 Step 4 的结果，15 个 Agent 应当都是 published；如果有疑问，先执行 `uv run cic-onboard --all`，这一步需要 A、B 在线。

| # | 命令 | 预期 |
| --- | --- | --- |
| 1 | `uv run cic-eval` | 约 40 次 Embedding + 40 次 Rerank，总计约 30 秒。终端打印总体指标表，`reports/` 下生成完整报告 |
| 2 | `uv run cic-recommend "下周想去北海道玩几天，帮我安排一下" --include-mock --top-k 5 --rerank` | 显示 Rerank 后的排序，每项写出「匹配度 x（向量 y）」 |

请把终端输出和报告文件路径发给我（或者把报告内容整理到 `step5_test_result.md`）。我会核对报告，并把结论（V1 vs V2 的差距、是否启用 Rerank、两个阈值的建议值）写入设计文档 §7 和决策记录，作为 SA&D 的数据来源。

