# Step 0–1：工程骨架、连通性冒烟与第三方 Agent 实施计划

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** 搭好 `cic-mcp-lab` 工程骨架，做百炼与 OSS 的连通性冒烟测试，并实现 3 个可反问、可识图、按 token 计费的第三方 Agent，用本地 CLI 直接调用（暂不经过 MCP）。

**Architecture:** `config.py` 统一读取 `.env`；`llm.py` 提供百炼模型工厂；`cic/oss.py` 负责图片上传（公共读 URL）。第三方侧的 `provider/agent.py` 用 AgentScope 2.0 的 `Agent` 加结构化输出 `{status, reply}` 表达反问或交付，每个 `conversation_id` 对应一个 Agent 实例以保留记忆，并按 `1 token = 1 Credit` 计算用量。对应设计文档 §3.1、§11 的第 0、1 步。

**Tech Stack:** Python 3.12、uv、agentscope 2.0.12（`Agent`、`DashScopeChatModel`、`UserMsg`/`DataBlock`/`URLSource`）、openai 3.57（embedding）、alibabacloud-oss-v2 1.4.1、pytest + pytest-asyncio。

**已验证的前提（2026-11-20 spike）：**
- `DashScopeChatModel` 配合自定义 `base_url` 可调用 `qwen4-max`；`agent.reply(..., structured_schema=Model)` 会在 `msg.structured_output` 返回 dict，`msg.usage` 是本次 reply 内各次模型调用累加的 token 用量；
- 同一个 `Agent` 实例的多次 `reply` 共享上下文；
- `DataBlock(source=URLSource(url, media_type))` 传入 OSS 公网图片 URL 后，模型能正确识别；
- OSS V2 SDK 要求 `region` 为 `cn-guangzhou` 形式，而 `.env` 中是 `oss-cn-guangzhou`，需要去掉 `oss-` 前缀；
- `text-embedding-v4` 可在 `.env` 配置的 endpoint 上调用，返回 1024 维向量。

**约定：**
- 所有命令都在 `D:\develop\accentrixProject\cic-mcp-lab` 下执行；
- 需要把输出通过管道交给其他命令时，先执行 `$env:PYTHONUTF8='1'`，避免中文乱码；
- 每次提交都附带 `Co-authored-by: Copilot <223556219+Copilot@users.noreply.github.com>`；
- `docs/` 已被 gitignore，本计划和设计文档都不提交。

---

### Task 1: 工程骨架

**Files:**
- Modify: `pyproject.toml`
- Modify: `src/cic_mcp_lab/__init__.py`
- Create: `src/cic_mcp_lab/cic/__init__.py`
- Create: `src/cic_mcp_lab/provider/__init__.py`
- Create: `tests/__init__.py`
- Create: `.env.example`

**Step 1: 修改 `pyproject.toml`**

把 `[project.scripts]` 整段替换为下面的内容，并在文件末尾追加 pytest 配置：

```toml
[project.scripts]
cic-smoke = "cic_mcp_lab.smoke:main"
provider-agent = "cic_mcp_lab.provider.cli:main"
```

```toml
[tool.pytest.ini_options]
asyncio_mode = "auto"
testpaths = ["tests"]
```

同时把 `description` 改为 `"CIC × MCP third-party agent integration lab"`。

**Step 2: 包文件**

`src/cic_mcp_lab/__init__.py`（替换 uv 生成的 `main`）：

```python
"""CIC × MCP third-party agent integration lab."""
```

`src/cic_mcp_lab/cic/__init__.py`：

```python
"""CIC platform side: OSS, MCP client, recommendation and router."""
```

`src/cic_mcp_lab/provider/__init__.py`：

```python
"""Mock third-party provider side: agents and MCP servers."""
```

`tests/__init__.py`：空文件。

**Step 3: `.env.example`**

```dotenv
DASHSCOPE_BASE_URL=
DASHSCOPE_API_KEY=
ROUTER_MODEL=qwen4-max
PROVIDER_MODEL=qwen4-max
EMBEDDING_MODEL=text-embedding-v4
EMBEDDING_DIM=1024
RERANK_MODEL=qwen3-rerank
RERANK_ENABLED=false
RECALL_TOP_N=20
RECOMMEND_TOP_K=2
# RECOMMEND_MIN_SCORE_DENSE=
# RECOMMEND_MIN_SCORE_RERANK=
QDRANT_PATH=./data/qdrant
OSS_REGION=
OSS_ENDPOINT=
OSS_BUCKET=
OSS_ACCESS_KEY_ID=
OSS_ACCESS_KEY_SECRET=
PROVIDER_A_API_KEY=
PROVIDER_B_API_KEY=
```

**Step 4: 验证 pytest 可运行**

Run: `uv run pytest -q`
Expected: `no tests ran`（exit code 5 属正常）

**Step 5: Commit**

```powershell
git add .gitignore .python-version pyproject.toml uv.lock README.md .env.example src tests
git commit -m "chore: scaffold cic-mcp-lab project" -m "Co-authored-by: Copilot <223556219+Copilot@users.noreply.github.com>"
```

---

### Task 2: 配置加载 `config.py`

**Files:**
- Create: `src/cic_mcp_lab/config.py`
- Test: `tests/test_config.py`

**Step 1: 写失败的测试**

```python
import pytest

from cic_mcp_lab.config import ConfigError, load_settings

BASE_ENV = {
    "DASHSCOPE_BASE_URL": "https://example.com/compatible-mode/v1",
    "DASHSCOPE_API_KEY": "sk-test",
    "ROUTER_MODEL": "qwen4-max",
    "PROVIDER_MODEL": "qwen4-max",
    "EMBEDDING_MODEL": "text-embedding-v4",
    "EMBEDDING_DIM": "1024",
    "OSS_REGION": "oss-cn-guangzhou",
    "OSS_ENDPOINT": "oss-cn-guangzhou.aliyuncs.com",
    "OSS_BUCKET": "bucket",
    "OSS_ACCESS_KEY_ID": "ak",
    "OSS_ACCESS_KEY_SECRET": "sk",
}


def test_load_settings_normalizes_oss_region_and_parses_dim():
    settings = load_settings(BASE_ENV)
    assert settings.oss_region == "cn-guangzhou"
    assert settings.embedding_dim == 1024


def test_load_settings_reports_all_missing_keys():
    env = {k: v for k, v in BASE_ENV.items() if k not in {"DASHSCOPE_API_KEY", "OSS_BUCKET"}}
    with pytest.raises(ConfigError, match="DASHSCOPE_API_KEY, OSS_BUCKET"):
        load_settings(env)


def test_load_settings_rejects_non_integer_dim():
    with pytest.raises(ConfigError, match="EMBEDDING_DIM"):
        load_settings({**BASE_ENV, "EMBEDDING_DIM": "abc"})
```

**Step 2: 运行，确认失败**

Run: `uv run pytest tests/test_config.py -q`
Expected: FAIL，`ModuleNotFoundError: No module named 'cic_mcp_lab.config'`

**Step 3: 最小实现**

```python
"""Settings loaded from the project .env file."""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parents[2]


class ConfigError(RuntimeError):
    """Raised when required settings are missing or invalid."""


@dataclass(frozen=True)
class Settings:
    dashscope_base_url: str
    dashscope_api_key: str
    router_model: str
    provider_model: str
    embedding_model: str
    embedding_dim: int
    oss_region: str
    oss_endpoint: str
    oss_bucket: str
    oss_access_key_id: str
    oss_access_key_secret: str


_REQUIRED = (
    "DASHSCOPE_BASE_URL",
    "DASHSCOPE_API_KEY",
    "ROUTER_MODEL",
    "PROVIDER_MODEL",
    "EMBEDDING_MODEL",
    "EMBEDDING_DIM",
    "OSS_REGION",
    "OSS_ENDPOINT",
    "OSS_BUCKET",
    "OSS_ACCESS_KEY_ID",
    "OSS_ACCESS_KEY_SECRET",
)


def load_settings(env: Mapping[str, str] | None = None) -> Settings:
    """Load settings from ``env``, or from ``.env`` + process env when omitted."""
    if env is None:
        load_dotenv(PROJECT_ROOT / ".env")
        env = os.environ
    missing = [name for name in _REQUIRED if not env.get(name, "").strip()]
    if missing:
        raise ConfigError(f"Missing settings in .env: {', '.join(missing)}")
    try:
        embedding_dim = int(env["EMBEDDING_DIM"])
    except ValueError as exc:
        raise ConfigError("EMBEDDING_DIM must be an integer") from exc

    def get(name: str) -> str:
        return env[name].strip()

    return Settings(
        dashscope_base_url=get("DASHSCOPE_BASE_URL"),
        dashscope_api_key=get("DASHSCOPE_API_KEY"),
        router_model=get("ROUTER_MODEL"),
        provider_model=get("PROVIDER_MODEL"),
        embedding_model=get("EMBEDDING_MODEL"),
        embedding_dim=embedding_dim,
        # OSS SDK V2 expects "cn-guangzhou", the console shows "oss-cn-guangzhou".
        oss_region=get("OSS_REGION").removeprefix("oss-"),
        oss_endpoint=get("OSS_ENDPOINT"),
        oss_bucket=get("OSS_BUCKET"),
        oss_access_key_id=get("OSS_ACCESS_KEY_ID"),
        oss_access_key_secret=get("OSS_ACCESS_KEY_SECRET"),
    )
```

**Step 4: 运行，确认通过**

Run: `uv run pytest tests/test_config.py -q`
Expected: `3 passed`

**Step 5: Commit**

```powershell
git add src/cic_mcp_lab/config.py tests/test_config.py
git commit -m "feat: load lab settings from .env" -m "Co-authored-by: Copilot <223556219+Copilot@users.noreply.github.com>"
```

---

### Task 3: 百炼模型工厂 `llm.py`

**Files:**
- Create: `src/cic_mcp_lab/llm.py`

这是对 SDK 构造函数的薄封装，没有自身逻辑，不写单元测试，由 Task 5 的冒烟测试做真实调用验证。

**Step 1: 实现**

```python
"""Factories for Bailian (DashScope) model clients."""
from agentscope.credential import DashScopeCredential
from agentscope.model import DashScopeChatModel
from openai import AsyncOpenAI

from .config import Settings


def make_chat_model(settings: Settings, model_name: str) -> DashScopeChatModel:
    """Non-streaming qwen chat model with thinking disabled (cheaper, faster)."""
    return DashScopeChatModel(
        credential=DashScopeCredential(
            api_key=settings.dashscope_api_key,
            base_url=settings.dashscope_base_url,
        ),
        model=model_name,
        parameters=DashScopeChatModel.Parameters(thinking_enable=False),
        stream=False,
    )


def make_openai_client(settings: Settings) -> AsyncOpenAI:
    """OpenAI-compatible client for Bailian embeddings (and rerank later)."""
    return AsyncOpenAI(
        api_key=settings.dashscope_api_key,
        base_url=settings.dashscope_base_url,
    )
```

**Step 2: 导入检查**

Run: `uv run python -c "import cic_mcp_lab.llm"`
Expected: 无输出、无报错

**Step 3: Commit**

```powershell
git add src/cic_mcp_lab/llm.py
git commit -m "feat: add Bailian model client factories" -m "Co-authored-by: Copilot <223556219+Copilot@users.noreply.github.com>"
```

---

### Task 4: OSS 上传 `cic/oss.py`

**Files:**
- Create: `src/cic_mcp_lab/cic/oss.py`
- Test: `tests/test_oss.py`

**Step 1: 写失败的测试**

```python
from datetime import date

import pytest

from cic_mcp_lab.cic.oss import build_object_key, guess_image_type, public_url


def test_build_object_key_uses_date_folder_and_lowercase_suffix():
    key = build_object_key("Photo.PNG", date(2026, 11, 20), "abc123")
    assert key == "cic-mcp-lab/20261120/abc123.png"


def test_public_url_points_to_bucket_endpoint():
    url = public_url("my-bucket", "oss-cn-guangzhou.aliyuncs.com", "cic-mcp-lab/20261120/a b.png")
    assert url == "https://my-bucket.oss-cn-guangzhou.aliyuncs.com/cic-mcp-lab/20261120/a%20b.png"


def test_guess_image_type_accepts_images_and_rejects_other_files():
    assert guess_image_type("a.png") == "image/png"
    with pytest.raises(ValueError, match="Not an image"):
        guess_image_type("report.pdf")
```

**Step 2: 运行，确认失败**

Run: `uv run pytest tests/test_oss.py -q`
Expected: FAIL，`ModuleNotFoundError: No module named 'cic_mcp_lab.cic.oss'`

**Step 3: 最小实现**

```python
"""Upload images to a public-read Aliyun OSS bucket and return public URLs."""
from __future__ import annotations

import mimetypes
import uuid
from datetime import date
from pathlib import Path
from urllib.parse import quote

import alibabacloud_oss_v2 as oss

from ..config import Settings

KEY_PREFIX = "cic-mcp-lab"


def guess_image_type(filename: str) -> str:
    media_type, _ = mimetypes.guess_type(filename)
    if not media_type or not media_type.startswith("image/"):
        raise ValueError(f"Not an image file: {filename}")
    return media_type


def build_object_key(filename: str, today: date, unique: str) -> str:
    suffix = Path(filename).suffix.lower()
    return f"{KEY_PREFIX}/{today:%Y%m%d}/{unique}{suffix}"


def public_url(bucket: str, endpoint: str, key: str) -> str:
    return f"https://{bucket}.{endpoint}/{quote(key)}"


class OssUploader:
    def __init__(self, settings: Settings) -> None:
        cfg = oss.config.load_default()
        cfg.region = settings.oss_region
        cfg.endpoint = settings.oss_endpoint
        cfg.credentials_provider = oss.credentials.StaticCredentialsProvider(
            settings.oss_access_key_id,
            settings.oss_access_key_secret,
        )
        self._client = oss.Client(cfg)
        self._bucket = settings.oss_bucket
        self._endpoint = settings.oss_endpoint

    def upload_bytes(self, data: bytes, filename: str) -> str:
        content_type = guess_image_type(filename)
        key = build_object_key(filename, date.today(), uuid.uuid4().hex)
        self._client.put_object(
            oss.PutObjectRequest(
                bucket=self._bucket,
                key=key,
                body=data,
                content_type=content_type,
            )
        )
        return public_url(self._bucket, self._endpoint, key)

    def upload_file(self, path: Path) -> str:
        return self.upload_bytes(path.read_bytes(), path.name)
```

**Step 4: 运行，确认通过**

Run: `uv run pytest tests/test_oss.py -q`
Expected: `3 passed`

**Step 5: Commit**

```powershell
git add src/cic_mcp_lab/cic/oss.py tests/test_oss.py
git commit -m "feat: upload images to public-read OSS" -m "Co-authored-by: Copilot <223556219+Copilot@users.noreply.github.com>"
```

---

### Task 5: 连通性冒烟 `cic-smoke`

**Files:**
- Create: `src/cic_mcp_lab/smoke.py`

这是需要真实网络的手工验证命令，不写单元测试。

**Step 1: 实现**

```python
"""Connectivity smoke test: chat model, embedding, OSS upload + public read."""
from __future__ import annotations

import asyncio
import struct
import sys
import urllib.request
import zlib

from agentscope.agent import Agent
from agentscope.message import UserMsg

from .cic.oss import OssUploader
from .config import Settings, load_settings
from .llm import make_chat_model, make_openai_client


def _solid_png(size: int = 64) -> bytes:
    """A tiny red PNG built without image libraries."""
    raw = (b"\x00" + b"\xff\x00\x00" * size) * size

    def chunk(tag: bytes, data: bytes) -> bytes:
        crc = zlib.crc32(tag + data) & 0xFFFFFFFF
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", crc)

    header = struct.pack(">IIBBBBB", size, size, 8, 2, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", header)
        + chunk(b"IDAT", zlib.compress(raw))
        + chunk(b"IEND", b"")
    )


async def _check_chat(settings: Settings) -> None:
    for model_name in sorted({settings.router_model, settings.provider_model}):
        agent = Agent(
            name="smoke",
            system_prompt="你是连通性测试助手，回答尽量简短。",
            model=make_chat_model(settings, model_name),
        )
        msg = await agent.reply(UserMsg(name="user", content="用一句话介绍你自己。"))
        print(f"[chat] {model_name}: {msg.get_text_content()} (usage: {msg.usage})")


async def _check_embedding(settings: Settings) -> None:
    client = make_openai_client(settings)
    resp = await client.embeddings.create(
        model=settings.embedding_model,
        input=["帮我规划东京旅行"],
        dimensions=settings.embedding_dim,
    )
    dim = len(resp.data[0].embedding)
    if dim != settings.embedding_dim:
        raise RuntimeError(f"expected dim {settings.embedding_dim}, got {dim}")
    print(f"[embedding] {settings.embedding_model}: dim={dim}")


async def _check_oss(settings: Settings) -> None:
    url = await asyncio.to_thread(OssUploader(settings).upload_bytes, _solid_png(), "smoke.png")
    with urllib.request.urlopen(url, timeout=10) as resp:
        print(f"[oss] public GET {resp.status} {resp.headers.get('Content-Type')} {url}")


async def _run() -> int:
    settings = load_settings()
    failures = 0
    for name, check in (("chat", _check_chat), ("embedding", _check_embedding), ("oss", _check_oss)):
        try:
            await check(settings)
        except Exception as exc:  # report every failing dependency, not just the first
            failures += 1
            print(f"[{name}] FAILED: {type(exc).__name__}: {exc}")
    return failures


def main() -> None:
    sys.exit(1 if asyncio.run(_run()) else 0)
```

**Step 2: 运行冒烟测试**

Run: `uv run cic-smoke`
Expected：三行都成功，退出码为 0：

```
[chat] qwen4-max: ……(usage: input_tokens=… output_tokens=…)
[embedding] text-embedding-v4: dim=1024
[oss] public GET 200 image/png https://<bucket>.oss-cn-guangzhou.aliyuncs.com/cic-mcp-lab/2026xxxx/xxxx.png
```

**Step 3: Commit**

```powershell
git add src/cic_mcp_lab/smoke.py pyproject.toml
git commit -m "feat: add cic-smoke connectivity check" -m "Co-authored-by: Copilot <223556219+Copilot@users.noreply.github.com>"
```

> ✅ **第 0 步检查点**：向用户展示 `cic-smoke` 的输出，确认后再进入第 1 步。

---

### Task 6: Credit 换算 `provider/credit.py`

**Files:**
- Create: `src/cic_mcp_lab/provider/credit.py`
- Test: `tests/test_credit.py`

**Step 1: 写失败的测试**

```python
import pytest
from agentscope.message import Usage

from cic_mcp_lab.provider.credit import MissingUsageError, credit_from_usage


def test_credit_is_total_tokens():
    assert credit_from_usage(Usage(input_tokens=463, output_tokens=244)) == 707


def test_missing_usage_cannot_be_billed():
    with pytest.raises(MissingUsageError):
        credit_from_usage(None)
```

**Step 2: 运行，确认失败**

Run: `uv run pytest tests/test_credit.py -q`
Expected: FAIL，`ModuleNotFoundError`

**Step 3: 最小实现**

```python
"""Mock credit rule: 1 token = 1 Credit."""
from agentscope.message import Usage


class MissingUsageError(RuntimeError):
    """The model reply carried no token usage, so it cannot be billed."""


def credit_from_usage(usage: Usage | None) -> int:
    if usage is None:
        raise MissingUsageError("Model reply has no token usage")
    return usage.input_tokens + usage.output_tokens
```

**Step 4: 运行，确认通过**

Run: `uv run pytest tests/test_credit.py -q`
Expected: `2 passed`

**Step 5: Commit**

```powershell
git add src/cic_mcp_lab/provider/credit.py tests/test_credit.py
git commit -m "feat: compute mock credit usage from tokens" -m "Co-authored-by: Copilot <223556219+Copilot@users.noreply.github.com>"
```

---

### Task 7: Agent 目录 `provider/catalog.py`

**Files:**
- Create: `src/cic_mcp_lab/provider/catalog.py`

这里只是静态数据，不写测试。`supports_image` 只对 `product_copy` 开启，用于以后验证推荐阶段的图片过滤（设计文档 §6.7 场景 11）。

**Step 1: 实现**

```python
"""Third-party agents hosted by the mock providers (design doc §2.3)."""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class AgentSpec:
    agent_id: str
    provider_id: str
    display_name: str
    description: str
    system_prompt: str
    supports_image: bool


_REPLY_RULES = """

每次回复都必须给出 status 和 reply：
- 信息不足、需要用户补充时：status=input_required，reply 只写需要用户回答的简短问题；
- 已交付结果时：status=completed，reply 写完整结果。
reply 会直接展示给用户，使用中文，不要提及 status 字段本身。"""

CATALOG: dict[str, AgentSpec] = {
    spec.agent_id: spec
    for spec in (
        AgentSpec(
            agent_id="trip_planner",
            provider_id="provider-a",
            display_name="旅行规划",
            description="根据目的地、出行日期和预算，为用户规划逐日旅行行程。",
            system_prompt=(
                "你是「旅行规划」Agent，为用户规划逐日旅行行程。\n"
                "开始规划前必须确认三项信息：目的地、出行日期或天数、总预算。缺少任一项时先追问。\n"
                "信息齐全后，输出逐日行程（每天分上午、下午、晚上）和预算分配。"
                "用户要求修改已交付的行程时，直接输出修改后的完整行程。"
                + _REPLY_RULES
            ),
            supports_image=False,
        ),
        AgentSpec(
            agent_id="product_copy",
            provider_id="provider-a",
            display_name="商品文案",
            description="根据商品图片和描述，为电商平台撰写营销文案。",
            system_prompt=(
                "你是「商品文案」Agent，根据商品图片和用户描述撰写电商营销文案。\n"
                "开始撰写前必须确认：投放平台（如小红书、淘宝、抖音）和文案风格。"
                "如果既没有商品图片，描述也不足以判断商品，就请用户提供图片或描述。缺少信息时先追问。\n"
                "信息齐全后，输出：标题、正文（150～300 字）、3～5 个话题标签。"
                + _REPLY_RULES
            ),
            supports_image=True,
        ),
        AgentSpec(
            agent_id="weekly_report",
            provider_id="provider-b",
            display_name="周报撰写",
            description="根据本周工作内容，生成结构化的工作周报。",
            system_prompt=(
                "你是「周报撰写」Agent，根据用户本周的工作内容生成结构化周报。\n"
                "开始撰写前必须了解：本周完成的主要工作、遇到的问题、下周计划。缺少信息时先追问。\n"
                "信息齐全后，按「本周工作 / 问题与风险 / 下周计划」三部分输出周报。"
                + _REPLY_RULES
            ),
            supports_image=False,
        ),
    )
}
```

**Step 2: 导入检查**

Run: `uv run python -c "from cic_mcp_lab.provider.catalog import CATALOG; print(sorted(CATALOG))"`
Expected: `['product_copy', 'trip_planner', 'weekly_report']`

**Step 3: Commit**

```powershell
git add src/cic_mcp_lab/provider/catalog.py
git commit -m "feat: define mock third-party agent catalog" -m "Co-authored-by: Copilot <223556219+Copilot@users.noreply.github.com>"
```

---

### Task 8: 第三方 Agent 运行时 `provider/agent.py`

**Files:**
- Create: `src/cic_mcp_lab/provider/agent.py`
- Test: `tests/test_provider_agent.py`

**Step 1: 写失败的测试**

```python
import pytest
from agentscope.message import AssistantMsg, DataBlock, TextBlock, Usage

from cic_mcp_lab.provider.agent import (
    AgentReply,
    ImagesNotSupportedError,
    ProviderAgentRuntime,
    UnknownConversationError,
)
from cic_mcp_lab.provider.catalog import AgentSpec


def make_spec(agent_id: str = "trip_planner", supports_image: bool = False) -> AgentSpec:
    return AgentSpec(
        agent_id=agent_id,
        provider_id="provider-a",
        display_name=agent_id,
        description="d",
        system_prompt="p",
        supports_image=supports_image,
    )


class FakeAgent:
    """Replays scripted (status, reply, usage) tuples and records inputs."""

    def __init__(self, *replies):
        self.replies = list(replies)
        self.inputs = []

    async def reply(self, inputs, structured_schema=None):
        assert structured_schema is AgentReply
        self.inputs.append(inputs)
        status, text, usage = self.replies.pop(0)
        return AssistantMsg(
            name="fake",
            content="The required structured output is generated.",
            structured_output={"status": status, "reply": text},
            usage=usage,
        )


def make_runtime(agents, specs) -> ProviderAgentRuntime:
    pool = iter(agents)  # a second factory call for the same conversation would raise StopIteration
    return ProviderAgentRuntime({s.agent_id: s for s in specs}, lambda _spec: next(pool))


async def test_new_conversation_returns_handle_status_and_credit():
    fake = FakeAgent(("input_required", "请问出发日期？", Usage(input_tokens=463, output_tokens=244)))
    runtime = make_runtime([fake], [make_spec()])

    result = await runtime.ask("trip_planner", "帮我规划旅行")

    assert result.conversation_id.startswith("conv_")
    assert (result.status, result.reply, result.credit_usage) == ("input_required", "请问出发日期？", 707)


async def test_follow_up_reuses_the_same_agent_memory():
    fake = FakeAgent(
        ("input_required", "请问出发日期？", Usage(input_tokens=10, output_tokens=5)),
        ("completed", "Day1 ……", Usage(input_tokens=30, output_tokens=20)),
    )
    runtime = make_runtime([fake], [make_spec()])

    first = await runtime.ask("trip_planner", "帮我规划旅行")
    second = await runtime.ask("trip_planner", "12月5日出发，预算1万", conversation_id=first.conversation_id)

    assert second.conversation_id == first.conversation_id
    assert (second.status, second.credit_usage) == ("completed", 50)
    assert [m.get_text_content() for m in fake.inputs] == ["帮我规划旅行", "12月5日出发，预算1万"]


async def test_unknown_or_foreign_conversation_is_rejected():
    fake = FakeAgent(("completed", "ok", Usage(input_tokens=1, output_tokens=1)))
    runtime = make_runtime([fake], [make_spec("trip_planner"), make_spec("weekly_report")])
    first = await runtime.ask("trip_planner", "你好")

    with pytest.raises(UnknownConversationError):
        await runtime.ask("trip_planner", "继续", conversation_id="conv_missing")
    with pytest.raises(UnknownConversationError):
        await runtime.ask("weekly_report", "继续", conversation_id=first.conversation_id)


async def test_image_urls_become_url_data_blocks():
    fake = FakeAgent(("completed", "红色", Usage(input_tokens=1, output_tokens=1)))
    runtime = make_runtime([fake], [make_spec("product_copy", supports_image=True)])

    await runtime.ask("product_copy", "这是什么颜色？", image_urls=["https://b.oss.example.com/a/x.png"])

    text_block, image_block = fake.inputs[0].content
    assert isinstance(text_block, TextBlock)
    assert isinstance(image_block, DataBlock)
    assert str(image_block.source.url) == "https://b.oss.example.com/a/x.png"
    assert image_block.source.media_type == "image/png"


async def test_images_are_rejected_for_text_only_agents():
    runtime = make_runtime([], [make_spec(supports_image=False)])

    with pytest.raises(ImagesNotSupportedError):
        await runtime.ask("trip_planner", "看看这张图", image_urls=["https://b.oss.example.com/a/x.png"])
```

**Step 2: 运行，确认失败**

Run: `uv run pytest tests/test_provider_agent.py -q`
Expected: FAIL，`ModuleNotFoundError: No module named 'cic_mcp_lab.provider.agent'`

**Step 3: 最小实现**

```python
"""Runtime of the mock third-party agents: per-conversation memory, ask-back, credit."""
from __future__ import annotations

import mimetypes
import uuid
from dataclasses import dataclass
from typing import Callable, Literal, Protocol, Sequence
from urllib.parse import urlparse

from agentscope.agent import Agent
from agentscope.message import DataBlock, Msg, TextBlock, URLSource, UserMsg
from agentscope.model import ChatModelBase
from pydantic import BaseModel, Field

from .catalog import AgentSpec
from .credit import credit_from_usage

Status = Literal["input_required", "completed"]


class AgentReply(BaseModel):
    status: Status = Field(description="input_required：需要用户补充信息；completed：已交付结果")
    reply: str = Field(description="直接展示给用户的回复内容")


@dataclass(frozen=True)
class AgentResult:
    conversation_id: str
    status: Status
    reply: str
    credit_usage: int


class UnknownAgentError(LookupError):
    pass


class UnknownConversationError(LookupError):
    pass


class ImagesNotSupportedError(ValueError):
    pass


class ReplyingAgent(Protocol):
    async def reply(self, inputs: Msg, structured_schema: type[BaseModel] | None = None) -> Msg: ...


AgentFactory = Callable[[AgentSpec], ReplyingAgent]


def image_media_type(url: str) -> str:
    media_type, _ = mimetypes.guess_type(urlparse(url).path)
    return media_type if media_type and media_type.startswith("image/") else "image/jpeg"


def build_user_msg(message: str, image_urls: Sequence[str]) -> Msg:
    blocks: list[TextBlock | DataBlock] = [TextBlock(text=message)]
    blocks += [
        DataBlock(source=URLSource(url=url, media_type=image_media_type(url)))
        for url in image_urls
    ]
    return UserMsg(name="user", content=blocks)


@dataclass
class _Conversation:
    agent_id: str
    agent: ReplyingAgent


class ProviderAgentRuntime:
    """Each conversation_id owns one agent instance, so its memory carries across turns."""

    def __init__(self, specs: dict[str, AgentSpec], agent_factory: AgentFactory) -> None:
        self._specs = specs
        self._factory = agent_factory
        self._conversations: dict[str, _Conversation] = {}

    async def ask(
        self,
        agent_id: str,
        message: str,
        image_urls: Sequence[str] = (),
        conversation_id: str | None = None,
    ) -> AgentResult:
        spec = self._specs.get(agent_id)
        if spec is None:
            raise UnknownAgentError(f"Unknown agent: {agent_id}")
        if image_urls and not spec.supports_image:
            raise ImagesNotSupportedError(f"Agent {agent_id} does not accept images")

        if conversation_id is None:
            conversation = _Conversation(agent_id, self._factory(spec))
            conversation_id = f"conv_{uuid.uuid4().hex}"
            is_new = True
        else:
            conversation = self._conversations.get(conversation_id)
            if conversation is None or conversation.agent_id != agent_id:
                raise UnknownConversationError(f"Conversation not found or expired: {conversation_id}")
            is_new = False

        msg = await conversation.agent.reply(
            build_user_msg(message, image_urls),
            structured_schema=AgentReply,
        )
        parsed = AgentReply.model_validate(msg.structured_output)
        credit_usage = credit_from_usage(msg.usage)
        if is_new:
            # Register only after a successful first turn.
            self._conversations[conversation_id] = conversation
        return AgentResult(conversation_id, parsed.status, parsed.reply, credit_usage)


def make_agent_factory(model: ChatModelBase) -> AgentFactory:
    def factory(spec: AgentSpec) -> Agent:
        return Agent(name=spec.agent_id, system_prompt=spec.system_prompt, model=model)

    return factory
```

**Step 4: 运行，确认通过**

Run: `uv run pytest tests/test_provider_agent.py -q`
Expected: `5 passed`

**Step 5: Commit**

```powershell
git add src/cic_mcp_lab/provider/agent.py tests/test_provider_agent.py
git commit -m "feat: add third-party agent runtime with ask-back and credit" -m "Co-authored-by: Copilot <223556219+Copilot@users.noreply.github.com>"
```

---

### Task 9: 本地 CLI `provider-agent`

**Files:**
- Create: `src/cic_mcp_lab/provider/cli.py`

这是交互式手工验证入口，不写单元测试。

**Step 1: 实现**

```python
"""Local REPL that talks to one mock third-party agent directly (no MCP yet)."""
from __future__ import annotations

import argparse
import asyncio
from pathlib import Path

from ..cic.oss import OssUploader
from ..config import load_settings
from ..llm import make_chat_model
from .agent import ProviderAgentRuntime, make_agent_factory
from .catalog import CATALOG

HELP = "命令：/image <本地路径或URL> 附加图片；/new 开启新会话；/exit 退出"


async def _repl(agent_id: str) -> None:
    settings = load_settings()
    runtime = ProviderAgentRuntime(
        CATALOG,
        make_agent_factory(make_chat_model(settings, settings.provider_model)),
    )
    uploader = OssUploader(settings)
    spec = CATALOG[agent_id]
    conversation_id: str | None = None
    images: list[str] = []
    total_credit = 0
    print(f"已连接 {spec.display_name}（{agent_id}）。{HELP}")

    while True:
        line = (await asyncio.to_thread(input, "> ")).strip()
        if not line:
            continue
        if line == "/exit":
            return
        if line == "/new":
            conversation_id, images = None, []
            print("已开启新会话")
            continue
        if line.startswith("/image "):
            target = line.removeprefix("/image ").strip().strip('"')
            try:
                url = (
                    target
                    if target.startswith(("http://", "https://"))
                    else await asyncio.to_thread(uploader.upload_file, Path(target))
                )
            except Exception as exc:
                print(f"[error] 图片上传失败：{type(exc).__name__}: {exc}")
                continue
            images.append(url)
            print(f"已附加图片：{url}")
            continue

        try:
            result = await runtime.ask(agent_id, line, image_urls=images, conversation_id=conversation_id)
        except Exception as exc:
            print(f"[error] {type(exc).__name__}: {exc}")
            continue
        conversation_id, images = result.conversation_id, []
        total_credit += result.credit_usage
        print(
            f"[{spec.display_name}] {result.conversation_id} · {result.status} · "
            f"credit_usage={result.credit_usage}（本会话累计 {total_credit}）"
        )
        print(result.reply)


def main() -> None:
    parser = argparse.ArgumentParser(description="直接与模拟第三方 Agent 对话（不经过 MCP）")
    parser.add_argument("--agent", choices=sorted(CATALOG), required=True)
    args = parser.parse_args()
    try:
        asyncio.run(_repl(args.agent))
    except (KeyboardInterrupt, EOFError):
        pass
```

**Step 2: 手工验证（由用户在终端执行）**

1. `uv run provider-agent --agent trip_planner`
   - 输入 `帮我规划旅行` → `input_required`，追问目的地、日期、预算；
   - 输入 `去东京，12月5日出发玩5天，预算1万人民币` → `completed`，输出逐日行程；会话 id 不变；每轮都打印 credit_usage，且累计值递增；
   - 输入 `/new`，再输入 `你好` → 生成新的会话 id。
2. `uv run provider-agent --agent product_copy`
   - `/image <本地商品图片路径>` → 打印 OSS 公网 URL；
   - 输入 `帮我写个文案` → `input_required`，追问平台和风格，且回复内容能体现模型识别出了图片中的商品；
   - 输入 `小红书，活泼风格` → `completed`，包含标题、正文和话题标签。
3. `uv run provider-agent --agent weekly_report`
   - `/image <任意图片>`，再输入 `写周报` → `[error] ImagesNotSupportedError`；
   - `/new`，输入 `写周报` → `input_required`；补充工作内容后 → `completed`。

**Step 3: Commit**

```powershell
git add src/cic_mcp_lab/provider/cli.py
git commit -m "feat: add provider-agent local REPL" -m "Co-authored-by: Copilot <223556219+Copilot@users.noreply.github.com>"
```

---

### Task 10: README 与全量测试

**Files:**
- Modify: `README.md`

**Step 1: 用以下内容替换 README**

````markdown
# cic-mcp-lab

CIC × MCP 第三方 Agent 接入实验（CLI）。

## 准备

```powershell
uv sync
Copy-Item .env.example .env   # 然后填写百炼与 OSS 配置
```

## 命令

| 命令 | 说明 |
| --- | --- |
| `uv run cic-smoke` | 百炼 chat / embedding 与 OSS 上传的连通性检查 |
| `uv run provider-agent --agent <trip_planner\|product_copy\|weekly_report>` | 直接与模拟第三方 Agent 对话（不经过 MCP） |
| `uv run pytest -q` | 运行单元测试 |
````

**Step 2: 全量测试**

Run: `uv run pytest -q`
Expected: `13 passed`

**Step 3: Commit**

```powershell
git add README.md
git commit -m "docs: document lab commands" -m "Co-authored-by: Copilot <223556219+Copilot@users.noreply.github.com>"
```

> ✅ **第 1 步检查点**：向用户展示 3 个 Agent 的手工验证结果（反问、续接会话、识图、credit 累计），确认后再编写第 2 步（MCP Server）的计划。
