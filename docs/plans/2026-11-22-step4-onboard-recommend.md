# Step 4：入驻向量化 + 推荐器 + 上下架 实施计划

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** 实现 Agent 入驻与推荐的完整链路：
- 注册表 v2：在原有字段基础上增加分类、标签、示例问题和上下架状态，并加入 12 个仅有画像的模拟 Agent；
- 已下架 / 模拟 Agent 不可调用；
- Embedding 工具、Qdrant 向量库；
- 推荐器：向量召回 → 按 Agent 聚合 → 回查注册表 → 阈值 → top_k；
- `cic-onboard`：测试连接 → 画像 → Embedding → 写入索引；状态同步；
- `cic-recommend`：命令行查询推荐结果。

**Architecture:**
- `cic/registry.py` 是唯一的数据来源（实验用 yaml，生产环境用数据库，见设计文档 §13.10）。
- `cic/recommend/` 下各模块：
  - `embedding.py`：`DashScopeEmbedder`，OpenAI 兼容接口，每批最多 10 条；
  - `vector_store.py`：`QdrantVectorStore`，本地模式，只检索 `status=published` 的点；
  - `recommender.py`：聚合、注册表回查、阈值、top_k；
  - `cli.py`：`cic-recommend`。
- `cic/onboard.py`：
  - 可调用 Agent：先测试连接，不通过则阻断，索引保持不变；
  - 模拟 Agent：跳过测试连接；
  - 已下架 Agent：只同步 payload 中的状态，不测试连接、不重新 Embedding。
- 下架靠修改 yaml 的 `status` 字段（模拟运营后台操作）。推荐结果展示前、提交任务时都会实时读取注册表，所以即使索引还没同步，下架也立即生效。
- Rerank 放到 Step 5（与评测一起做）。阈值 `RECOMMEND_MIN_SCORE_DENSE` 默认不启用，取值由 Step 5 评测确定。

**Tech Stack:** Python 3.12、uv、qdrant-client 1.20（本地模式 / `:memory:`）、openai SDK（百炼 OpenAI 兼容接口 `text-embedding-v4`，1024 维）、PyYAML、pytest + pytest-asyncio。

**已验证的前提（2026-11-22 在 `.venv` 中用 `QdrantClient(":memory:")` 实测）：**
- `collection_exists` / `create_collection(VectorParams(size, COSINE))` / `get_collection(...).config.params.vectors.size` 均可用。
- `query_points(name, query=vec, query_filter=Filter(must=[FieldCondition(key, match=MatchValue(...))]), limit, with_payload=True).points` 返回带 `score` 和 `payload` 的点；cosine 分数即相似度。
- `set_payload(name, payload={...}, points=Filter(...))` 可按过滤条件批量改 payload。
- `delete(name, points_selector=FilterSelector(filter=...))` 可按过滤条件删除点。
- `count(name, count_filter=..., exact=True).count` 和 `scroll(name, limit, offset, with_payload=[...])` 均可用。
- 本地磁盘模式 `QdrantClient(path=...)` 同一时间只允许一个进程打开（设计文档 §12）。
- `text-embedding-v4` 在 OpenAI 兼容接口下每次最多 10 条输入（百炼文档）；`cic-smoke` 已验证可以返回 1024 维向量。

**约定：**
- 所有命令都在 `D:\develop\accentrixProject\cic-mcp-lab` 下执行。
- 每次提交附带 `Co-authored-by: Copilot <223556219+Copilot@users.noreply.github.com>`。
- `docs/` 不提交；`img.png`、`step1_test_result.md`、`step2_test_result.md`、`step3_test_result.md` 属于用户，不要动；不读取、不打印 `.env`。
- 测试中不调用真实模型：Embedding 用假实现，向量库用 `QdrantClient(":memory:")`。
- 用户可能开着 provider-server，导致 `uv run` 自动同步失败（`.venv\Scripts\provider-server.exe` 被占用）。实现期间一律用 `uv run --no-sync ...`。新增的 console script 由用户停掉 server 后执行 `uv sync` 安装。

---

### Task 1: 注册表 v2 + 15 个 Agent 的入驻数据

**Files:**
- Modify: `src/cic_mcp_lab/cic/registry.py`、`data/agents.yaml`
- Test: `tests/test_cic_registry.py`（重写）

**要点：**
- `AgentRecord` 前 8 个位置参数保持原顺序，因为 Step 3 的测试按位置构造。`mcp_url` / `api_key_env` / `entry_tool` 改为 `str | None`。
- 新增字段，均带默认值：`category`、`tags`、`examples`（tuple）、`status`（`published` | `delisted`，默认 `published`）、`supports_image`（默认 False，只对模拟 Agent 有意义）。
- 新增两个属性：`callable`（有 MCP 端点）、`published`。
- `load_registry` 的校验规则：
  - 必填字段；
  - 三个端点字段要么全给、要么全不给；
  - 示例问题 3～10 条（决策 11）；
  - `status` 取值合法；
  - 列表字段必须是非空字符串组成的列表；
  - 可调用 Agent 的 `(mcp_url, entry_tool)` 不能重复。
- 模拟 Agent 调用 `api_key_for` 时报错 `not callable`。

**Step 1: 重写测试** — `tests/test_cic_registry.py`：

```python
import pytest
import yaml

from cic_mcp_lab.cic.registry import RegistryError, api_key_for, load_registry

ENDPOINT = {"mcp_url": "http://h/mcp", "api_key_env": "K", "entry_tool": "t"}


def entry(agent_id="a", **fields):
    """A valid mock-agent entry; a field set to None is left out."""
    data = {
        "agent_id": agent_id,
        "provider_id": "p",
        "display_name": "n",
        "description": "d",
        "category": ["c"],
        "tags": ["t"],
        "examples": ["e1", "e2", "e3"],
        "price_per_credit": 0.0001,
        **fields,
    }
    return {key: value for key, value in data.items() if value is not None}


def write_registry(tmp_path, *entries):
    path = tmp_path / "agents.yaml"
    path.write_text(yaml.safe_dump({"agents": list(entries)}, allow_unicode=True), encoding="utf-8")
    return path


def test_onboarded_agents_point_at_their_provider_servers():
    registry = load_registry()

    assert {agent_id: (r.mcp_url, r.api_key_env, r.entry_tool) for agent_id, r in registry.items() if r.callable} == {
        "trip_planner": ("http://127.0.0.1:8001/mcp", "PROVIDER_A_API_KEY", "trip_planner"),
        "product_copy": ("http://127.0.0.1:8001/mcp", "PROVIDER_A_API_KEY", "product_copy"),
        "weekly_report": ("http://127.0.0.1:8002/mcp", "PROVIDER_B_API_KEY", "weekly_report"),
    }


def test_seed_registry_adds_mock_agents_for_recommendation():
    registry = load_registry()

    assert len(registry) == 15
    assert sum(not record.callable for record in registry.values()) == 12
    assert all(record.published for record in registry.values())
    assert {agent_id for agent_id, r in registry.items() if r.supports_image and not r.callable} == {
        "poster_design",
        "product_cutout",
    }


def test_one_endpoint_cannot_be_registered_twice(tmp_path):
    path = write_registry(tmp_path, entry("a", **ENDPOINT), entry("b", **ENDPOINT))

    with pytest.raises(RegistryError, match="already registered"):
        load_registry(path)


@pytest.mark.parametrize(
    ("fields", "reason"),
    [
        ({"examples": None}, "missing: examples"),
        ({"examples": ["e1", "e2"]}, "3-10"),
        ({"examples": [f"e{i}" for i in range(11)]}, "3-10"),
        ({"tags": "t"}, "tags"),
        ({"mcp_url": "http://h/mcp"}, "together"),
        ({"status": "draft"}, "status"),
    ],
)
def test_invalid_entries_are_rejected(tmp_path, fields, reason):
    with pytest.raises(RegistryError, match=reason):
        load_registry(write_registry(tmp_path, entry(**fields)))


def test_mock_agents_have_no_endpoint_or_key(tmp_path):
    record = load_registry(write_registry(tmp_path, entry(supports_image=True)))["a"]

    assert (record.callable, record.supports_image, record.status, record.examples) == (
        False,
        True,
        "published",
        ("e1", "e2", "e3"),
    )
    with pytest.raises(RegistryError, match="not callable"):
        api_key_for(record, {"K": "sk"})


def test_api_key_comes_from_the_environment():
    record = load_registry()["weekly_report"]

    assert api_key_for(record, {"PROVIDER_B_API_KEY": " sk-b "}) == "sk-b"
    with pytest.raises(RegistryError, match="PROVIDER_B_API_KEY"):
        api_key_for(record, {})
```

**Step 2:** `uv run --no-sync pytest tests/test_cic_registry.py -q` → 新增的用例失败（例如 `KeyError` / 断言失败 / 没有抛出 `RegistryError`）

**Step 3: 实现**

`src/cic_mcp_lab/cic/registry.py`（整体替换）：

```python
"""Agents CIC has onboarded (data/agents.yaml): profile, MCP endpoint and listing status.

The lab reads a YAML file; production keeps the registry in a database (design §13.10).
"""
from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

import yaml

from ..config import PROJECT_ROOT

DEFAULT_REGISTRY_PATH = PROJECT_ROOT / "data" / "agents.yaml"
STATUSES = ("published", "delisted")
MIN_EXAMPLES, MAX_EXAMPLES = 3, 10
_REQUIRED = ("agent_id", "provider_id", "display_name", "description", "category", "tags", "examples", "price_per_credit")
_ENDPOINT = ("mcp_url", "api_key_env", "entry_tool")

Status = Literal["published", "delisted"]


class RegistryError(ValueError):
    pass


@dataclass(frozen=True)
class AgentRecord:
    agent_id: str
    provider_id: str
    display_name: str
    description: str
    mcp_url: str | None  # None for mock agents, which exist only for recommendation tests
    api_key_env: str | None  # env var holding the provider API key; keys never live in the registry file
    entry_tool: str | None
    price_per_credit: float  # HKD per Credit
    category: tuple[str, ...] = ()
    tags: tuple[str, ...] = ()
    examples: tuple[str, ...] = ()  # how users would ask; each one is embedded for recall (design §4.5)
    status: Status = "published"
    supports_image: bool = False  # read for mock agents only; a callable agent's entry tool schema decides

    @property
    def callable(self) -> bool:
        return self.mcp_url is not None

    @property
    def published(self) -> bool:
        return self.status == "published"


def _strings(value: Any, name: str, where: str) -> tuple[str, ...]:
    if not isinstance(value, list) or not all(isinstance(item, str) and item.strip() for item in value):
        raise RegistryError(f"{where}: {name} must be a list of non-empty strings")
    return tuple(item.strip() for item in value)


def _record(raw: dict[str, Any], where: str) -> AgentRecord:
    missing = [name for name in _REQUIRED if name not in raw]
    if missing:
        raise RegistryError(f"{where} is missing: {', '.join(missing)}")
    given = [name for name in _ENDPOINT if raw.get(name)]
    if given and len(given) != len(_ENDPOINT):
        raise RegistryError(f"{where}: {', '.join(_ENDPOINT)} must be given together")
    examples = _strings(raw["examples"], "examples", where)
    if not MIN_EXAMPLES <= len(examples) <= MAX_EXAMPLES:
        raise RegistryError(f"{where}: examples must have {MIN_EXAMPLES}-{MAX_EXAMPLES} items, got {len(examples)}")
    status = raw.get("status", "published")
    if status not in STATUSES:
        raise RegistryError(f"{where}: status must be one of {STATUSES}, got {status!r}")
    return AgentRecord(
        agent_id=str(raw["agent_id"]),
        provider_id=str(raw["provider_id"]),
        display_name=str(raw["display_name"]),
        description=str(raw["description"]),
        mcp_url=raw.get("mcp_url") or None,
        api_key_env=raw.get("api_key_env") or None,
        entry_tool=raw.get("entry_tool") or None,
        price_per_credit=float(raw["price_per_credit"]),
        category=_strings(raw["category"], "category", where),
        tags=_strings(raw["tags"], "tags", where),
        examples=examples,
        status=status,
        supports_image=bool(raw.get("supports_image", False)),
    )


def load_registry(path: Path = DEFAULT_REGISTRY_PATH) -> dict[str, AgentRecord]:
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    records: dict[str, AgentRecord] = {}
    endpoints: set[tuple[str, str]] = set()
    for index, raw in enumerate(data.get("agents") or []):
        record = _record(raw, f"agents[{index}]")
        if record.agent_id in records:
            raise RegistryError(f"Duplicate agent_id {record.agent_id!r}")
        if record.callable:
            endpoint = (record.mcp_url, record.entry_tool)
            if endpoint in endpoints:
                raise RegistryError(
                    f"{record.agent_id}: tool {record.entry_tool!r} at {record.mcp_url} is already registered"
                )
            endpoints.add(endpoint)
        records[record.agent_id] = record
    return records


def api_key_for(record: AgentRecord, env: Mapping[str, str] = os.environ) -> str:
    if record.api_key_env is None:
        raise RegistryError(f"{record.agent_id} is not callable: it has no MCP endpoint")
    key = env.get(record.api_key_env, "").strip()
    if not key:
        raise RegistryError(f"{record.agent_id}: environment variable {record.api_key_env} is not set")
    return key
```

> `endpoints` 的类型是 `set[tuple[str, str]]`；`record.callable` 为真时 `mcp_url` 和 `entry_tool` 都不是 None。如果类型检查器报错，可以用 `assert` 收窄类型，不要改变逻辑。

`data/agents.yaml`（整体替换）：

```yaml
# Agents CIC has onboarded through the Provider Portal (simulated; production keeps them in a database, design §13.10).
# - Callable agents have mcp_url / api_key_env / entry_tool. API keys live in .env; only the variable name is here.
# - Mock agents have no MCP endpoint: they only exist so recommendation has look-alike agents to tell apart (design §7).
# - status: published | delisted. Editing it simulates the operations console; run cic-onboard to sync the index.
# - supports_image is read for mock agents only; a callable agent's entry tool schema decides (design §4.5).
agents:
  - agent_id: trip_planner
    provider_id: provider-a
    display_name: 旅行规划
    description: 根据目的地、出行日期和预算，为用户规划逐日旅行行程。
    category: [出行, 生活服务]
    tags: [行程规划, 自由行, 预算]
    examples:
      - 帮我规划一个 5 天的日本自由行
      - 国庆想去云南玩一周，路线怎么安排
      - 预算 8000 元带父母去成都玩 4 天，帮我排一下行程
      - 第一次去欧洲，两周时间怎么规划比较好
    mcp_url: "http://127.0.0.1:8001/mcp"
    api_key_env: PROVIDER_A_API_KEY
    entry_tool: trip_planner
    price_per_credit: 0.0001
  - agent_id: product_copy
    provider_id: provider-a
    display_name: 商品文案
    description: 根据商品图片和描述，为电商平台撰写营销文案。
    category: [电商, 营销]
    tags: [商品文案, 种草, 卖点提炼]
    examples:
      - 帮这款保温杯写一段淘宝详情页文案
      - 根据商品图片写一条小红书种草笔记
      - 给新款运动鞋提炼 5 个卖点
      - 帮我写一段抖音橱窗的商品介绍
    mcp_url: "http://127.0.0.1:8001/mcp"
    api_key_env: PROVIDER_A_API_KEY
    entry_tool: product_copy
    price_per_credit: 0.0001
  - agent_id: weekly_report
    provider_id: provider-b
    display_name: 周报撰写
    description: 根据本周工作内容，生成结构化的工作周报。
    category: [办公, 写作]
    tags: [周报, 工作总结, 汇报]
    examples:
      - 帮我把这周做的事整理成周报
      - 根据我列的工作要点生成一份结构化周报
      - 本周完成了需求评审和两个接口开发，帮我写周报
      - 写一份给领导看的本周工作总结
    mcp_url: "http://127.0.0.1:8002/mcp"
    api_key_env: PROVIDER_B_API_KEY
    entry_tool: weekly_report
    price_per_credit: 0.0001

  # ---- Mock agents (profile only, no MCP endpoint) ----
  - agent_id: hotel_booking
    provider_id: provider-mock
    display_name: 酒店推荐
    description: 按城市、商圈、预算和出行人群推荐合适的酒店，并对比价格与评价。
    category: [出行, 生活服务]
    tags: [酒店, 住宿, 比价]
    examples:
      - 东京新宿附近有什么性价比高的酒店
      - 帮我找三亚适合带小孩住的海边酒店
      - 下周去上海出差，推荐交通方便的商务酒店
    price_per_credit: 0.0001
  - agent_id: visa_consult
    provider_id: provider-mock
    display_name: 签证咨询
    description: 解答各国旅游、商务、留学签证的办理条件、材料清单和办理周期。
    category: [出行, 政务]
    tags: [签证, 材料清单, 出入境]
    examples:
      - 申请日本旅游签证需要准备哪些材料
      - 申根签证一般要提前多久办理
      - 自由职业者怎么申请美国签证
    price_per_credit: 0.0001
  - agent_id: flight_deals
    provider_id: provider-mock
    display_name: 机票比价
    description: 比较不同日期和航司的机票价格，帮用户找到便宜或合适的航班。
    category: [出行]
    tags: [机票, 特价, 航班]
    examples:
      - 下个月北京飞大阪哪天最便宜
      - 广州到伦敦有没有直飞航班
      - 春节回家的机票怎么买比较划算
    price_per_credit: 0.0001
  - agent_id: meeting_minutes
    provider_id: provider-mock
    display_name: 会议纪要
    description: 把会议记录或录音转写整理成会议纪要，提炼决议和待办事项。
    category: [办公, 写作]
    tags: [会议记录, 待办事项, 决议]
    examples:
      - 把这段会议录音的文字整理成会议纪要
      - 根据讨论内容列出会议决议和待办
      - 帮我把今天项目例会的要点整理一下
    price_per_credit: 0.0001
  - agent_id: email_writer
    provider_id: provider-mock
    display_name: 邮件写作
    description: 撰写和回复中英文商务邮件，语气得体、结构清晰。
    category: [办公, 写作]
    tags: [商务邮件, 英文邮件, 邮件回复]
    examples:
      - 帮我写一封催客户付款的邮件
      - 用英文回复供应商的询价
      - 写一封请假邮件给主管
    price_per_credit: 0.0001
  - agent_id: ppt_outline
    provider_id: provider-mock
    display_name: PPT 大纲
    description: 根据主题或材料生成演示文稿的结构大纲和每页要点。
    category: [办公]
    tags: [演示文稿, 大纲, 汇报]
    examples:
      - 帮我列一个年度总结汇报的 PPT 大纲
      - 产品发布会的演示文稿应该包含哪些页
      - 把这份方案整理成 10 页 PPT 的结构
    price_per_credit: 0.0001
  - agent_id: poster_design
    provider_id: provider-mock
    display_name: 海报设计
    description: 根据活动主题或商品图片生成宣传海报的设计方案与视觉稿。
    category: [电商, 设计]
    tags: [海报, 主视觉, 促销图]
    examples:
      - 给双十一大促设计一张主视觉海报
      - 根据这张产品图做一张宣传海报
      - 设计一张新品上市的朋友圈海报
    supports_image: true
    price_per_credit: 0.0001
  - agent_id: product_cutout
    provider_id: provider-mock
    display_name: 商品抠图
    description: 去除商品图片背景，生成白底图或透明底图，适用于电商主图。
    category: [电商, 设计]
    tags: [抠图, 白底图, 图片处理]
    examples:
      - 把这张商品图的背景去掉换成白底
      - 帮我把模特图里的衣服单独抠出来
      - 批量处理商品主图的背景
    supports_image: true
    price_per_credit: 0.0001
  - agent_id: livestream_script
    provider_id: provider-mock
    display_name: 直播话术
    description: 为直播带货撰写开场、讲品、促单等环节的话术与脚本。
    category: [电商, 营销]
    tags: [直播带货, 话术, 脚本]
    examples:
      - 帮我写一段直播间卖面膜的话术
      - 直播开场怎么暖场留人
      - 给零食专场写一份直播脚本
    price_per_credit: 0.0001
  - agent_id: legal_consult
    provider_id: provider-mock
    display_name: 法律咨询
    description: 解答劳动、租房、合同、消费维权等常见法律问题，给出处理建议。
    category: [法律, 生活服务]
    tags: [劳动纠纷, 合同, 维权]
    examples:
      - 公司拖欠工资应该怎么维权
      - 租房到期房东不退押金怎么办
      - 签劳动合同要注意哪些条款
    price_per_credit: 0.0001
  - agent_id: fitness_coach
    provider_id: provider-mock
    display_name: 健身计划
    description: 根据身体情况和目标制定减脂、增肌等训练与饮食计划。
    category: [健康, 运动]
    tags: [减脂, 增肌, 训练计划]
    examples:
      - 帮我制定一个三个月的减脂计划
      - 新手在家没有器械怎么练
      - 增肌期间一天应该吃多少蛋白质
    price_per_credit: 0.0001
  - agent_id: recipe_helper
    provider_id: provider-mock
    display_name: 家常菜谱
    description: 根据现有食材、口味和人数推荐家常菜做法与搭配。
    category: [美食, 生活服务]
    tags: [菜谱, 做饭, 营养搭配]
    examples:
      - 冰箱里有鸡蛋、番茄和土豆，能做什么菜
      - 红烧肉怎么做才不腻
      - 给一家三口安排一周的晚餐
    price_per_credit: 0.0001
```

**Step 4:** `uv run --no-sync pytest tests/test_cic_registry.py -q` → `11 passed`；`uv run --no-sync pytest -q` → 全部通过（112 passed：Step 3 的测试按位置构造 `AgentRecord`，不受影响）

**Step 5: Commit**

```powershell
git add src/cic_mcp_lab/cic/registry.py data/agents.yaml tests/test_cic_registry.py
git commit -m "feat: registry v2 with profiles, examples, listing status and mock agents" -m "Co-authored-by: Copilot <223556219+Copilot@users.noreply.github.com>"
```

---

### Task 2: 已下架 / 模拟 Agent 不可调用；`cic-probe --all` 只测可调用 Agent

**Files:**
- Modify: `src/cic_mcp_lab/cic/task_watcher.py`（`submit`）、`src/cic_mcp_lab/cic/probe.py`（`main`）
- Test: `tests/test_cic_task_watcher.py`（追加 2 个测试）

**要点（设计文档 §13.10）：**
- `submit()` 每次都查注册表：模拟 Agent → `SubmitFailed("... mock agent ...")`；已下架 → `SubmitFailed("... has been delisted")`。两种情况都不计费。
- 已提交的任务按提交时的快照继续跟踪和结算，下架不影响它。现有实现已经这样做（`_watch` 拿到的是提交时的 `record`），这里补一个测试锁定这一行为。
- `cic-probe --all` 只测可调用 Agent；对模拟 Agent 单独执行 probe 时给出明确提示。

**Step 1: 追加失败的测试** — `tests/test_cic_task_watcher.py`（顶部补充 `from dataclasses import replace`）：

```python
async def test_delisting_blocks_new_turns_but_running_tasks_still_settle(start_server):
    url, _ = start_server()
    registry = {"trip_planner": record_for(url)}
    ledger = Ledger()
    async with TaskWatcher(registry, ledger, env=ENV) as watcher:
        submission = await watcher.submit("trip_planner", "sleep=0.5;hi")
        registry["trip_planner"] = replace(registry["trip_planner"], status="delisted")
        with pytest.raises(SubmitFailed, match="delisted"):
            await watcher.submit("trip_planner", "again")
        outcome = await next_outcome(watcher)

    assert outcome.ok
    assert [entry.settlement_key for entry in ledger.entries] == [submission.task_id]


async def test_mock_agents_cannot_be_called():
    mock = AgentRecord("hotel_booking", "provider-mock", "n", "d", None, None, None, 0.0001)
    async with TaskWatcher({"hotel_booking": mock}, Ledger(), env=ENV) as watcher:
        with pytest.raises(SubmitFailed, match="mock agent"):
            await watcher.submit("hotel_booking", "hi")
```

**Step 2:** `uv run --no-sync pytest tests/test_cic_task_watcher.py -q` → 2 个新测试失败（第一个没有抛 `SubmitFailed`；第二个的报错信息不含 "mock agent"）

**Step 3: 实现**

`task_watcher.py` 的 `submit()`：在 `record is None` 检查之后、`api_key_for` 之前插入：

```python
        if not record.callable:
            raise SubmitFailed(f"{agent_id} is a mock agent without an MCP endpoint")
        if not record.published:
            raise SubmitFailed(f"{agent_id} has been delisted")
```

并把 `submit` 的 docstring 改为：`"""Send one turn; returns once the provider has accepted it. The outcome arrives on `outcomes`.\n\n        The registry is consulted on every call, so delisting takes effect at once; turns already\n        accepted keep the record they were submitted with (design §13.10).\n        """`（保持三引号多行格式）。

`probe.py` 的 `main()`：把

```python
    agent_ids = sorted(registry) if args.all else [args.agent_id]
    unknown = [agent_id for agent_id in agent_ids if agent_id not in registry]
    if unknown:
        raise SystemExit(f"Unknown agent: {', '.join(unknown)}")
```

改为

```python
    agent_ids = sorted(a for a, record in registry.items() if record.callable) if args.all else [args.agent_id]
    unknown = [agent_id for agent_id in agent_ids if agent_id not in registry]
    if unknown:
        raise SystemExit(f"Unknown agent: {', '.join(unknown)}")
    mocks = [agent_id for agent_id in agent_ids if not registry[agent_id].callable]
    if mocks:
        raise SystemExit(f"模拟 Agent 没有 MCP 端点，无需测试连接：{', '.join(mocks)}")
```

同时把 `--all` 的 help 改为 `"测试全部可调用的 Agent（跳过模拟 Agent）"`。

**Step 4:** `uv run --no-sync pytest tests/test_cic_task_watcher.py -q` → 全部通过；`uv run --no-sync pytest -q` → `114 passed`

**Step 5: Commit**

```powershell
git add src/cic_mcp_lab/cic/task_watcher.py src/cic_mcp_lab/cic/probe.py tests/test_cic_task_watcher.py
git commit -m "feat: refuse turns for delisted and mock agents; probe only callable agents" -m "Co-authored-by: Copilot <223556219+Copilot@users.noreply.github.com>"
```

---

### Task 3: 推荐相关配置

**Files:**
- Modify: `src/cic_mcp_lab/config.py`
- Test: `tests/test_config.py`（追加）

**要点：**
- 新增 4 个可选配置，均有默认值，不加入 `_REQUIRED`：
  - `QDRANT_PATH`：默认 `./data/qdrant`；相对路径按项目根目录解析，而不是按当前工作目录；
  - `RECALL_TOP_N`：默认 20；
  - `RECOMMEND_TOP_K`：默认 2；
  - `RECOMMEND_MIN_SCORE_DENSE`：默认不设阈值。
- 取值格式错误时抛 `ConfigError`，报错信息包含变量名。

**Step 1: 追加失败的测试** — `tests/test_config.py`（补充 `from cic_mcp_lab.config import PROJECT_ROOT`）：

```python
def test_recommendation_settings_have_defaults():
    settings = load_settings(BASE_ENV)

    assert settings.qdrant_path == PROJECT_ROOT / "data" / "qdrant"
    assert (settings.recall_top_n, settings.recommend_top_k, settings.recommend_min_score_dense) == (20, 2, None)


def test_recommendation_settings_are_parsed():
    settings = load_settings(
        {
            **BASE_ENV,
            "QDRANT_PATH": "./tmp/vectors",
            "RECALL_TOP_N": "30",
            "RECOMMEND_TOP_K": "5",
            "RECOMMEND_MIN_SCORE_DENSE": "0.45",
        }
    )

    assert settings.qdrant_path == PROJECT_ROOT / "tmp" / "vectors"
    assert (settings.recall_top_n, settings.recommend_top_k, settings.recommend_min_score_dense) == (30, 5, 0.45)


@pytest.mark.parametrize(("name", "value"), [("RECALL_TOP_N", "many"), ("RECOMMEND_MIN_SCORE_DENSE", "high")])
def test_malformed_recommendation_settings_are_reported(name, value):
    with pytest.raises(ConfigError, match=name):
        load_settings({**BASE_ENV, name: value})
```

**Step 2:** `uv run --no-sync pytest tests/test_config.py -q` → 新测试失败（`AttributeError`）

**Step 3: 实现** — `config.py`：

- `Settings` 末尾追加（放在所有无默认值字段之后）：

```python
    qdrant_path: Path = PROJECT_ROOT / "data" / "qdrant"
    recall_top_n: int = 20
    recommend_top_k: int = 2
    recommend_min_score_dense: float | None = None  # chosen by the step 5 evaluation; unset = no threshold
```

- `load_settings` 中，在 `return Settings(...)` 之前加：

```python
    def optional(name: str, parse: Callable[[str], T], default: T) -> T:
        raw = env.get(name, "").strip()
        if not raw:
            return default
        try:
            return parse(raw)
        except ValueError as exc:
            raise ConfigError(f"{name} is not a valid {parse.__name__}: {raw!r}") from exc

    qdrant_path = Path(env.get("QDRANT_PATH", "").strip() or "data/qdrant")
```

并在 `Settings(...)` 调用中追加：

```python
        qdrant_path=qdrant_path if qdrant_path.is_absolute() else PROJECT_ROOT / qdrant_path,
        recall_top_n=optional("RECALL_TOP_N", int, 20),
        recommend_top_k=optional("RECOMMEND_TOP_K", int, 2),
        recommend_min_score_dense=optional("RECOMMEND_MIN_SCORE_DENSE", float, None),
```

- 顶部 import 补充 `from collections.abc import Callable` 与 `from typing import TypeVar`，定义 `T = TypeVar("T")`。原有的 `from typing import Mapping` 保持不变。

**Step 4:** `uv run --no-sync pytest tests/test_config.py -q` → 全部通过；`uv run --no-sync pytest -q` → `118 passed`

**Step 5: Commit**

```powershell
git add src/cic_mcp_lab/config.py tests/test_config.py
git commit -m "feat: recommendation settings (qdrant path, recall/top-k, dense threshold)" -m "Co-authored-by: Copilot <223556219+Copilot@users.noreply.github.com>"
```

---

### Task 4: Embedding 工具 `cic/recommend/embedding.py`

**Files:**
- Create: `src/cic_mcp_lab/cic/recommend/__init__.py`（空文件）、`src/cic_mcp_lab/cic/recommend/embedding.py`
- Test: `tests/test_cic_embedding.py`

**要点：** 入驻和检索共用同一个 Embedder（设计文档 §6.4）。用百炼 OpenAI 兼容接口，每批最多 10 条，按返回的 `index` 排序，保证向量与输入一一对应。`Embedder` Protocol 让测试和后续评测可以替换实现。

**Step 1: 写失败的测试** — `tests/test_cic_embedding.py`：

```python
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
```

**Step 2:** `uv run --no-sync pytest tests/test_cic_embedding.py -q` → FAIL（`ModuleNotFoundError`）

**Step 3: 实现** — `src/cic_mcp_lab/cic/recommend/embedding.py`：

```python
"""Embedding tool shared by onboarding and recommendation (design §6.4)."""
from __future__ import annotations

from collections.abc import Sequence
from typing import Any, Protocol

from ...config import Settings
from ...llm import make_openai_client

MAX_BATCH = 10  # text-embedding-v4 accepts at most 10 inputs per request on the OpenAI-compatible API


class Embedder(Protocol):
    model: str
    dim: int

    async def embed(self, texts: Sequence[str]) -> list[list[float]]: ...


class DashScopeEmbedder:
    def __init__(self, client: Any, model: str, dim: int) -> None:
        self._client = client  # an openai.AsyncOpenAI pointed at Bailian
        self.model = model
        self.dim = dim

    async def embed(self, texts: Sequence[str]) -> list[list[float]]:
        vectors: list[list[float]] = []
        for start in range(0, len(texts), MAX_BATCH):
            batch = list(texts[start : start + MAX_BATCH])
            response = await self._client.embeddings.create(
                model=self.model, input=batch, dimensions=self.dim, encoding_format="float"
            )
            ordered = sorted(response.data, key=lambda item: item.index)
            if len(ordered) != len(batch):
                raise RuntimeError(f"Embedding returned {len(ordered)} vectors for {len(batch)} texts")
            vectors.extend(list(item.embedding) for item in ordered)
        return vectors


def make_embedder(settings: Settings) -> DashScopeEmbedder:
    return DashScopeEmbedder(make_openai_client(settings), settings.embedding_model, settings.embedding_dim)
```

**Step 4:** `uv run --no-sync pytest tests/test_cic_embedding.py -q` → `2 passed`；`uv run --no-sync pytest -q` → `120 passed`

**Step 5: Commit**

```powershell
git add src/cic_mcp_lab/cic/recommend/__init__.py src/cic_mcp_lab/cic/recommend/embedding.py tests/test_cic_embedding.py
git commit -m "feat: batched DashScope embedding tool" -m "Co-authored-by: Copilot <223556219+Copilot@users.noreply.github.com>"
```

---

### Task 5: 向量库 `cic/recommend/vector_store.py`

**Files:**
- Create: `src/cic_mcp_lab/cic/recommend/vector_store.py`
- Test: `tests/test_cic_vector_store.py`

**要点（设计文档 §4.5）：**
- 每个 Agent 写入 1 个 `profile` 点和 N 个 `example` 点。
- payload：`agent_id, provider_id, kind, text, status, supports_image, callable, embedding_model, dim`。
- 重新入驻时先删除该 Agent 的全部点，再整体写入。
- 下架只改 payload 中的 `status`。
- 检索固定只取 `status=published`，另有两个可选过滤：`callable=true`（默认开启）、`supports_image=true`（用户附带图片时）。
- 已有集合的维度与配置不一致时报错，提示需要重建索引；更换 Embedding 模型或维度后必须重新入驻。
- 点 id 用 `uuid5(agent_id/kind/序号)`，保证可重复写入。
- 实验阶段只有 Qdrant 一个实现；生产环境换成 pgvector 时，按同样的方法签名再写一个实现即可（设计文档 §4.5）。

**Step 1: 写失败的测试** — `tests/test_cic_vector_store.py`：

```python
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
```

**Step 2:** `uv run --no-sync pytest tests/test_cic_vector_store.py -q` → FAIL（`ModuleNotFoundError`）

**Step 3: 实现** — `src/cic_mcp_lab/cic/recommend/vector_store.py`：

```python
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
    MatchValue,
    PointStruct,
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
        must = [_match("status", "published")]
        if callable_only:
            must.append(_match("callable", True))
        if require_image:
            must.append(_match("supports_image", True))
        result = self._client.query_points(
            COLLECTION, query=list(vector), query_filter=Filter(must=must), limit=top_n, with_payload=True
        )
        return [
            SearchHit(point.payload["agent_id"], point.payload["kind"], point.payload["text"], point.score)
            for point in result.points
            if point.payload
        ]
```

**Step 4:** `uv run --no-sync pytest tests/test_cic_vector_store.py -q` → `6 passed`；`uv run --no-sync pytest -q` → `126 passed`

**Step 5: Commit**

```powershell
git add src/cic_mcp_lab/cic/recommend/vector_store.py tests/test_cic_vector_store.py
git commit -m "feat: qdrant vector store for agent profiles and example questions" -m "Co-authored-by: Copilot <223556219+Copilot@users.noreply.github.com>"
```

---

### Task 6: 推荐器 `cic/recommend/recommender.py`

**Files:**
- Create: `src/cic_mcp_lab/cic/recommend/recommender.py`
- Test: `tests/test_cic_recommender.py`

**要点（设计文档 §6.4，暂不含 Rerank）：**
1. 对 query 做 Embedding；
2. 召回 `recall_top_n` 个点；
3. 按 `agent_id` 聚合，取各点分数的最大值，命中的文本作为匹配依据；
4. **回查注册表**：不在注册表中、已下架、或（未开 `include_mock` 时）不可调用的 Agent 一律剔除。索引可能落后于注册表（§13.10）；
5. 阈值过滤，`min_score=None` 时不过滤；
6. 按分数倒序取 `top_k`。

注册表以函数形式传入（`registry: Callable[[], Mapping]`），每次推荐都重新读取，这样下架能立即生效。

**Step 1: 写失败的测试** — `tests/test_cic_recommender.py`：

```python
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
```

> 分数依据（cosine）：
> - query `[0.95,0.05,0]`：trip 的 example 约为 1.0；copy 约为 `0.475/(0.951*1.03)≈0.48`；report 约为 `(0.285+0.05)/(0.951*1.044)≈0.34`。
> - query `[1,0,0]`：trip = 1；hotel ≈ 0.95；copy ≈ 0.49；report ≈ 0.29。阈值 0.6 时只剩 trip。

**Step 2:** `uv run --no-sync pytest tests/test_cic_recommender.py -q` → FAIL（`ModuleNotFoundError`）

**Step 3: 实现** — `src/cic_mcp_lab/cic/recommend/recommender.py`：

```python
"""Recommender: recall by vector, keep each agent's best match, re-check the registry, threshold, top_k (design §6.4)."""
from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass

from ..registry import AgentRecord
from .embedding import Embedder
from .vector_store import Kind, QdrantVectorStore, SearchHit


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
        recall_top_n: int = 20,
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
        hits = self._store.search(
            vector, self._recall_top_n, callable_only=not include_mock, require_image=with_image
        )
        best: dict[str, SearchHit] = {}
        for hit in hits:
            if hit.agent_id not in best or hit.score > best[hit.agent_id].score:
                best[hit.agent_id] = hit

        registry = self._registry()  # the index can lag behind the registry, so confirm every candidate
        picks: list[Recommendation] = []
        for agent_id, hit in best.items():
            record = registry.get(agent_id)
            if record is None or not record.published or (not include_mock and not record.callable):
                continue
            if self._min_score is not None and hit.score < self._min_score:
                continue
            picks.append(
                Recommendation(
                    agent_id, record.display_name, record.provider_id, record.callable, hit.score, hit.kind, hit.text
                )
            )
        picks.sort(key=lambda pick: pick.score, reverse=True)
        return picks[: self._top_k]
```

**Step 4:** `uv run --no-sync pytest tests/test_cic_recommender.py -q` → `5 passed`；`uv run --no-sync pytest -q` → `131 passed`

**Step 5: Commit**

```powershell
git add src/cic_mcp_lab/cic/recommend/recommender.py tests/test_cic_recommender.py
git commit -m "feat: recommender with per-agent aggregation and registry re-check" -m "Co-authored-by: Copilot <223556219+Copilot@users.noreply.github.com>"
```

---

### Task 7: 入驻 `cic/onboard.py` + `cic-onboard`

**Files:**
- Create: `src/cic_mcp_lab/cic/onboard.py`
- Modify: `pyproject.toml`（`[project.scripts]` 增加 `cic-onboard = "cic_mcp_lab.cic.onboard:main"`）
- Test: `tests/test_cic_onboard.py`

**要点（设计文档 §2.1、§4.5、§13.10）：**
- **已下架**：只把该 Agent 所有点的 `status` 改为 delisted。不测试连接、不做 Embedding，所以 Provider 宕机时也能下架。
- **可调用 Agent**：
  - 先测试连接（§4.2），不通过就阻断，**已在索引中的旧版本保持不变**（与生产中「新版本审核通过前旧版本继续服务」一致）；
  - 通过后，`supports_image` 以入口 Tool 的 schema 为准。
- **模拟 Agent**：跳过测试连接，`supports_image` 取 yaml 中的值。
- 画像文本 = 名称 + 分类 + 标签 + 描述，共 1 段；每条示例问题单独成为一个点；写入前先删除该 Agent 的旧点。
- `cic-onboard <agent_id>... | --all`：`--all` 处理注册表中的全部 Agent，并删除已不在注册表中的 Agent 的向量。只要有一个 Agent 被阻断或出错，退出码就是 1。

**Step 1: 写失败的测试** — `tests/test_cic_onboard.py`：

```python
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
```

**Step 2:** `uv run --no-sync pytest tests/test_cic_onboard.py -q` → FAIL（`ModuleNotFoundError`）

**Step 3: 实现** — `src/cic_mcp_lab/cic/onboard.py`：

```python
"""Onboarding (design §2.1, §4.5): test connection → profile → embedding → index; delisting syncs the status."""
from __future__ import annotations

import argparse
import asyncio
import os
import sys
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Literal

from ..config import Settings, load_settings
from .probe import ProbeReport, probe
from .recommend.embedding import Embedder, make_embedder
from .recommend.vector_store import AgentPoint, Kind, QdrantVectorStore
from .registry import AgentRecord, RegistryError, api_key_for, load_registry

Prober = Callable[[AgentRecord, str], Awaitable[ProbeReport]]
Action = Literal["indexed", "status_synced", "blocked"]
_SYMBOLS = {"indexed": "✓", "status_synced": "↻", "blocked": "✗"}


@dataclass(frozen=True)
class OnboardResult:
    agent_id: str
    action: Action
    points: int
    detail: str


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


async def onboard(
    record: AgentRecord,
    *,
    embedder: Embedder,
    store: QdrantVectorStore,
    prober: Prober = probe,
    env: Mapping[str, str] = os.environ,
) -> OnboardResult:
    if not record.published:
        # Delisting needs neither the provider nor the embedding API.
        count = store.set_status(record.agent_id, record.status)
        return OnboardResult(record.agent_id, "status_synced", count, f"状态已同步为 {record.status}（{count} 个点）")

    supports_image = record.supports_image
    if record.callable:
        try:
            report = await prober(record, api_key_for(record, env))
        except RegistryError as exc:
            return OnboardResult(record.agent_id, "blocked", 0, f"测试连接未执行：{exc}")
        if not report.ok or report.entry_tool is None:
            failed = next((step for step in report.steps if not step.ok), None)
            reason = f"{failed.name}: {failed.detail}" if failed else "测试连接未完成"
            # The approved version already in the index keeps serving.
            return OnboardResult(record.agent_id, "blocked", 0, f"测试连接未通过，阻断提交：{reason}")
        supports_image = report.entry_tool.supports_image  # the entry tool's schema decides (design §4.5)

    texts = [profile_text(record), *record.examples]
    kinds: list[Kind] = ["profile", *(["example"] * len(record.examples))]
    vectors = await embedder.embed(texts)
    points = [
        AgentPoint(
            record.agent_id, record.provider_id, kind, text, record.status, supports_image, record.callable, vector
        )
        for kind, text, vector in zip(kinds, texts, vectors, strict=True)
    ]
    store.replace_agent(record.agent_id, points)
    source = "测试连接通过" if record.callable else "模拟 Agent，跳过测试连接"
    detail = (
        f"已写入 {len(points)} 个点（画像 1 + 示例问题 {len(record.examples)}）· {source}"
        f" · 图片输入：{'支持' if supports_image else '不支持'}"
    )
    return OnboardResult(record.agent_id, "indexed", len(points), detail)


async def _onboard_many(
    settings: Settings, registry: Mapping[str, AgentRecord], agent_ids: list[str], *, prune: bool
) -> bool:
    store = QdrantVectorStore.open(
        settings.qdrant_path, embedding_model=settings.embedding_model, dim=settings.embedding_dim
    )
    embedder = make_embedder(settings)
    all_ok = True
    try:
        for agent_id in agent_ids:
            try:
                result = await onboard(registry[agent_id], embedder=embedder, store=store)
            except Exception as exc:  # e.g. the embedding API failed; carry on with the other agents
                print(f"[{agent_id}] ✗ {type(exc).__name__}: {exc}")
                all_ok = False
                continue
            print(f"[{agent_id}] {_SYMBOLS[result.action]} {result.detail}")
            all_ok = all_ok and result.action != "blocked"
        if prune:
            for stale in sorted(store.agent_ids() - set(registry)):
                store.delete_agent(stale)
                print(f"[{stale}] 已不在注册表中，删除其向量")
    finally:
        store.close()
    return all_ok


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(
        description="Agent 入驻：测试连接 → 画像 → Embedding → 写入向量库；已下架的 Agent 只同步状态"
    )
    parser.add_argument("agent_ids", nargs="*", help="data/agents.yaml 中的 agent_id，可多个")
    parser.add_argument("--all", action="store_true", help="处理全部 Agent，并删除已不在注册表中的 Agent 的向量")
    args = parser.parse_args()
    if bool(args.agent_ids) == args.all:
        parser.error("请指定 agent_id，或使用 --all")
    settings = load_settings()  # also loads .env, where the provider API keys live
    registry = load_registry()
    agent_ids = sorted(registry) if args.all else args.agent_ids
    unknown = [agent_id for agent_id in agent_ids if agent_id not in registry]
    if unknown:
        raise SystemExit(f"Unknown agent: {', '.join(unknown)}")
    sys.exit(0 if asyncio.run(_onboard_many(settings, registry, agent_ids, prune=args.all)) else 1)
```

**Step 4:** `uv run --no-sync pytest tests/test_cic_onboard.py -q` → `6 passed`；`uv run --no-sync pytest -q` → `137 passed`。另外执行 `uv run --no-sync python -c "import sys; sys.argv=['cic-onboard','--help']; from cic_mcp_lab.cic.onboard import main; main()"` 确认能打印帮助（不要真正执行入驻，那需要 `.env` 和运行中的 Provider）。

**Step 5: Commit**

```powershell
git add src/cic_mcp_lab/cic/onboard.py tests/test_cic_onboard.py pyproject.toml
git commit -m "feat: cic-onboard indexes agent profiles and examples, syncs delisting" -m "Co-authored-by: Copilot <223556219+Copilot@users.noreply.github.com>"
```

---

### Task 8: `cic-recommend` + 文档

**Files:**
- Create: `src/cic_mcp_lab/cic/recommend/cli.py`
- Modify: `pyproject.toml`（增加 `cic-recommend = "cic_mcp_lab.cic.recommend.cli:main"`）、`README.md`

**要点：** 这是手工检查点使用的命令。输出格式参照设计文档 §6.4：每个候选显示名称、Provider、匹配度和匹配依据（画像 / 某条示例问题）；模拟 Agent 带「（模拟）」标记；没有候选时提示「暂无合适的 Agent」；同时打印耗时。逻辑已由前面的测试覆盖，这里是纯 CLI 胶水代码，不写单测。

**Step 1: 实现** — `src/cic_mcp_lab/cic/recommend/cli.py`：

```python
"""cic-recommend: recommend onboarded agents for a user question (design §6.4; step 4 checkpoint)."""
from __future__ import annotations

import argparse
import asyncio
import sys
import time

from ...config import load_settings
from ..registry import load_registry
from .embedding import make_embedder
from .recommender import Recommender
from .vector_store import QdrantVectorStore


async def _recommend(args: argparse.Namespace) -> None:
    settings = load_settings()
    store = QdrantVectorStore.open(
        settings.qdrant_path, embedding_model=settings.embedding_model, dim=settings.embedding_dim
    )
    try:
        recommender = Recommender(
            make_embedder(settings),
            store,
            load_registry,
            recall_top_n=settings.recall_top_n,
            top_k=args.top_k or settings.recommend_top_k,
            min_score=settings.recommend_min_score_dense,
        )
        started = time.monotonic()
        picks = await recommender.recommend(args.query, with_image=args.image, include_mock=args.include_mock)
        elapsed = time.monotonic() - started
    finally:
        store.close()

    if not picks:
        print(f"暂无合适的 Agent（{elapsed:.2f}s）")
        return
    print(f"为你找到以下 Agent（按匹配度排序，{elapsed:.2f}s）：")
    for index, pick in enumerate(picks, 1):
        name = pick.display_name if pick.callable else f"{pick.display_name}（模拟）"
        basis = "画像" if pick.matched_kind == "profile" else f'示例问题 "{pick.matched_text}"'
        print(f"  [{index}] {name} · {pick.provider_id}   匹配度 {pick.score:.2f}   依据：{basis}")


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description="按用户问题推荐已入驻的 Agent（向量召回，设计文档 §6.4）")
    parser.add_argument("query", help="用户的问题")
    parser.add_argument("--image", action="store_true", help="模拟用户附带了图片：只推荐支持图片输入的 Agent")
    parser.add_argument("--include-mock", action="store_true", help="同时推荐只有画像、不接 MCP 的模拟 Agent")
    parser.add_argument("--top-k", type=int, help="覆盖 RECOMMEND_TOP_K")
    asyncio.run(_recommend(parser.parse_args()))
```

**Step 2: `pyproject.toml`**：`[project.scripts]` 末尾追加 `cic-recommend = "cic_mcp_lab.cic.recommend.cli:main"`。

**Step 3: `README.md`**：在 `cic-call` 一行之后、`pytest` 一行之前插入：

```markdown
| `uv run cic-onboard <agent_id>... \| --all` | 入驻：测试连接（可调用 Agent）→ 画像 + 示例问题 Embedding → 写入本地 Qdrant；`status: delisted` 的 Agent 只同步状态。与其他使用 Qdrant 的命令不能同时运行 |
| `uv run cic-recommend "<问题>" [--image] [--include-mock] [--top-k N]` | 按问题推荐 Agent：向量召回 → 按 Agent 聚合 → 回查注册表（下架立即生效）→ top_k，显示匹配度与依据 |
```

并在命令表之后追加一节：

```markdown
## Agent 上下架（实验）

`data/agents.yaml` 中每个 Agent 的 `status` 为 `published` 或 `delisted`，修改它即模拟运营后台的上下架操作：

- 推荐展示前、提交任务时都会实时读取注册表，所以下架立即生效；已提交的任务照常结算。
- 运行 `uv run cic-onboard <agent_id>` 同步向量索引：下架只改状态；重新上架会重新测试连接并写入向量。
```

**Step 4: 验证**
- `uv run --no-sync pytest -q` → `137 passed`
- `uv run --no-sync python -c "import sys; sys.argv=['cic-recommend','--help']; from cic_mcp_lab.cic.recommend.cli import main; main()"` → 打印帮助
- `uv lock --check` 通过（锁文件不应变化；如有变化，一并提交 `uv.lock`）

**Step 5: Commit**

```powershell
git add src/cic_mcp_lab/cic/recommend/cli.py pyproject.toml README.md
git commit -m "feat: cic-recommend CLI; document onboarding and listing commands" -m "Co-authored-by: Copilot <223556219+Copilot@users.noreply.github.com>"
```

---

### Task 9: 手工检查点（由用户执行）

**准备：** 新增了命令，先停掉所有 provider-server，执行 `uv sync`。然后启动终端 1 `uv run provider-server --provider A --port 8001` 和终端 2 `uv run provider-server --provider B --port 8002 --no-push`。如果终端里还留着 Step 3 设置的 `PROVIDER_TASK_TTL_MS`，先执行 `Remove-Item Env:PROVIDER_TASK_TTL_MS`。

| # | 场景 | 命令 | 预期 |
| --- | --- | --- | --- |
| 1 | 全量入驻 | `uv run cic-onboard --all` | 3 个可调用 Agent 显示「测试连接通过」，其中 product_copy 的图片输入为「支持」；12 个模拟 Agent 显示「跳过测试连接」；退出码 0 |
| 2 | 基本推荐 | `uv run cic-recommend "下周想去北海道玩几天，帮我安排一下"` | [1] 旅行规划，依据是一条示例问题或画像；显示耗时 |
| 3 | 易混淆（旅行） | `uv run cic-recommend "下周想去北海道玩几天，帮我安排一下" --include-mock --top-k 5` | 旅行规划排第一；酒店 / 机票 / 签证等模拟 Agent 带「（模拟）」标记，分数相近 |
| 4 | 易混淆（办公） | `uv run cic-recommend "帮我把今天开会讨论的内容整理一下" --include-mock --top-k 3` | 记录会议纪要与周报撰写的排序和分数（供 Step 5 参考） |
| 5 | 图片过滤 | `uv run cic-recommend "帮这双鞋做个宣传" --image --include-mock --top-k 5` | 只出现支持图片输入的 Agent：商品文案、海报设计（模拟）、商品抠图（模拟） |
| 6 | 无关问题 | `uv run cic-recommend "今天天气怎么样" --include-mock` | 仍返回 2 个，但分数明显偏低（尚未设置阈值）。请记录分数，供 Step 5 定阈值 |
| 7 | 下架立即生效 | 把 `data/agents.yaml` 中 weekly_report 的 `status` 改为 `delisted`（**不要**运行 onboard），然后执行 `uv run cic-recommend "帮我写本周周报"` 和 `uv run cic-call --agent weekly_report --message "帮我写周报"` | 推荐结果中没有周报撰写（注册表回查生效）；call 输出 `[submit] 提交失败（不计费）：weekly_report has been delisted` |
| 8 | 同步下架状态 | 停掉终端 2（B），执行 `uv run cic-onboard weekly_report` | `↻ 状态已同步为 delisted（5 个点）`。不需要 Provider 在线 |
| 9 | 重新上架失败被阻断 | B 保持停止，把 `status` 改回 `published`，执行 `uv run cic-onboard weekly_report` | `✗ 测试连接未通过，阻断提交：discover: ...`，退出码 1。索引里仍是 delisted 状态，推荐结果中仍然没有它 |
| 10 | 重新上架 | 启动 B，再执行 `uv run cic-onboard weekly_report` 和 `uv run cic-recommend "帮我写本周周报"` | `✓ 已写入 5 个点 ...`；推荐结果中重新出现周报撰写 |

请把输出整理到 `step4_test_result.md`。

**以下内容不需要手工验证（已由单测覆盖）：**
- 下架后在途任务照常结算；
- 模拟 Agent 不可调用；
- 按 Agent 聚合取最大分；
- 阈值与 top_k；
- 维度不一致时报错；
- 重新入驻时替换全部点。

