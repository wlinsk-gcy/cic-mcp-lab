# Step 6：意图识别 + Router + cic-chat 实施计划

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** 实现由代码编排的 Router（设计文档 §6），并提供交互式命令行 `cic-chat`。

**每轮流程：**
1. 意图识别（LLM 结构化输出），共 5 种意图：supplement / select_candidate / router_answer / recommend / clarify；
2. 按意图分支：
   - 推荐：Rerank 开，阈值 0.48；
   - 选择候选；
   - 补充同一会话：沿用 conversation_id；
   - Router 自己回答；
   - 澄清；
3. 任务通过 TaskWatcher 异步提交，结果由后台协程接收，然后更新会话、打印回复、结算。
4. 调用失败时给出「重试 / 重新推荐」两个选项。

**附带两项评测：**
- `cic-intent-eval`：意图识别准确率；
- `cic-eval --variants V4`：查询改写后的推荐效果，补跑 V4。

**Architecture:**
- `cic/session.py`：会话状态，包括对话记录、第三方会话 T1/T2…、待选项（推荐候选，或失败后的「重试 / 重新推荐」）。
- `cic/intent.py`：
  - 由会话状态生成意图识别 prompt 的纯函数；
  - `IntentDecision` 结构；
  - `LlmIntentRecognizer`：用 OpenAI SDK 的 `chat.completions.parse(response_format=IntentDecision)` 调用百炼兼容接口，`enable_thinking=False`。Router 侧不使用 agentscope。
- `cic/responder.py`：`LlmResponder`，把会话记录转成 chat messages 后调用 `chat.completions.create`，用于 Router 直接回答和澄清。
- `cic/router.py`：编排逻辑。`handle(text, images)` 处理一轮输入；`on_outcome(outcome)` 处理异步到达的任务结果。通过 `emit(RouterEvent)` 输出，与 CLI 解耦，便于测试。
- `cic/cli.py`：`cic-chat`。用 prompt_toolkit 的 `prompt_async` + `patch_stdout`，任务结果在用户输入时也能打印且不打乱输入行。
- `cic/intent_eval.py`：`cic-intent-eval`，基于 `data/intent_cases.yaml`。
- `cic/eval.py`：新增 V4 = 意图识别改写（`search_query`）+ V3。

**Tech Stack:** Python 3.12、uv、openai SDK（`AsyncOpenAI.chat.completions.parse` / `create`）、prompt_toolkit（新增依赖）、pydantic、PyYAML、pytest + pytest-asyncio。

**已验证的前提（2026-11-22 实测）：**
- `qwen4-max` 在百炼 OpenAI 兼容接口上支持 `response_format={"type":"json_schema","json_schema":{...,"strict":true}}`。`AsyncOpenAI.chat.completions.parse(model, messages, response_format=PydanticModel, extra_body={"enable_thinking": False})` 能直接返回 `choices[0].message.parsed`，约 2.1～2.4 秒。`json_object` 模式不按 schema 输出，不采用。
- Pydantic 模型的字段不设默认值（可空字段用 `X | None` 且必填），以满足 strict schema。
- prompt_toolkit 尚未安装，需要 `uv add prompt-toolkit`。
- 其余组件（TaskWatcher、Recommender + Rerank、Ledger、OssUploader）在 Step 3～5 已验证。

**约定：**
- 所有命令都在 `D:\develop\accentrixProject\cic-mcp-lab` 下执行。
- 每次提交附带 `Co-authored-by: Copilot <223556219+Copilot@users.noreply.github.com>`。
- `docs/` 不提交；`img.png`、`step*_test_result.md` 属于用户，不要动；不读取、不打印 `.env`；单测中不调用真实模型。
- 执行本计划期间用户会停掉 provider-server，所以 `uv add` / `uv run` 可以正常同步；如果遇到 exe 被占用，退回 `uv run --no-sync`，并报告。

---

### Task 1: 会话状态 + 意图识别 prompt

**Files:**
- Create: `src/cic_mcp_lab/cic/session.py`、`src/cic_mcp_lab/cic/intent.py`（本 Task 只写 `IntentDecision`、`SYSTEM_PROMPT`、`build_intent_prompt`）
- Test: `tests/test_cic_intent_prompt.py`

**要点（设计文档 §6.1、§6.3）：**
- LLM 只看到会话别名 T1/T2，接触不到真实的 conversation_id。
- prompt 由 4 部分组成：最近对话（最多 12 条，每条截断到 300 字）、第三方会话（编号、Agent、状态、Agent 最后一次回复，截断到 200 字）、待选项、本次输入（是否附带图片）。
- 待选项有两种：
  - 推荐候选：列出编号和 Agent 名称；
  - 失败后的选项：[1] 重试、[2] 重新推荐其他 Agent。
- `IntentDecision` 的字段都没有默认值，以满足 strict json_schema；`candidate_index` 从 1 开始。

**Step 1: 写失败的测试** — `tests/test_cic_intent_prompt.py`：

```python
from cic_mcp_lab.cic.intent import IntentDecision, build_intent_prompt
from cic_mcp_lab.cic.session import Candidate, PendingCandidates, PendingRetry, Session, Turn


def test_threads_are_numbered_and_found_by_submission():
    session = Session()
    first = session.open_thread("trip_planner", "旅行规划", "provider-a")
    second = session.open_thread("weekly_report", "周报撰写", "provider-b")
    second.submission_id = "sub_2"

    assert (first.ref, second.ref, first.status) == ("T1", "T2", "running")
    assert session.thread_for_submission("sub_2") is second
    assert session.thread_for_submission("sub_x") is None


def test_prompt_shows_history_threads_candidates_and_the_new_input():
    session = Session()
    session.history += [Turn("user", "帮我规划去东京的旅行"), Turn("agent", "请问出发日期和预算？" + "很长" * 300, "T1", "旅行规划 · T1")]
    thread = session.open_thread("trip_planner", "旅行规划", "provider-a")
    thread.status, thread.last_reply = "input_required", "请问出发日期和预算？"
    session.pending = PendingCandidates(
        "帮我写周报", (), (Candidate("weekly_report", "周报撰写", "provider-b", 0.8, "画像"),)
    )

    prompt = build_intent_prompt(session, "用第一个", image_count=1)

    assert "用户：帮我规划去东京的旅行" in prompt
    assert "[旅行规划 · T1]：请问出发日期和预算？" in prompt
    assert "很长" * 200 not in prompt  # long turns are truncated
    assert "T1 旅行规划：等待补充" in prompt
    assert "[1] 周报撰写" in prompt
    assert "用第一个（附带 1 张图片）" in prompt


def test_prompt_marks_empty_sections_and_retry_options():
    session = Session()
    thread = session.open_thread("weekly_report", "周报撰写", "provider-b")
    thread.status = "failed"
    session.pending = PendingRetry("T1", "帮我写周报", ())

    prompt = build_intent_prompt(Session(), "你好")
    retry = build_intent_prompt(session, "重试")

    assert prompt.count("（无）") == 3
    assert "[1] 重试" in retry and "[2] 重新推荐其他 Agent" in retry and "周报撰写（T1）调用失败" in retry


def test_intent_decision_fields_are_all_required_for_strict_schema():
    schema = IntentDecision.model_json_schema()

    assert set(schema["required"]) == {"intent", "thread_ref", "candidate_index", "search_query", "reason"}
```

**Step 2:** `uv run pytest tests/test_cic_intent_prompt.py -q` → FAIL（`ModuleNotFoundError`）

**Step 3: 实现**

`src/cic_mcp_lab/cic/session.py`：

```python
"""Router session state (design §6.1): conversation history, third-party threads and the pending choice."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

ThreadStatus = Literal["running", "input_required", "completed", "failed"]
STATUS_LABELS = {"running": "处理中", "input_required": "等待补充", "completed": "已完成", "failed": "失败"}
RETRY_OPTIONS = ("重试", "重新推荐其他 Agent")


@dataclass
class Thread:
    """One conversation with a third-party agent; the LLM only ever sees its alias (T1, T2 …)."""

    ref: str
    agent_id: str
    display_name: str
    provider_id: str
    conversation_id: str | None = None  # issued by the provider with its first reply
    status: ThreadStatus = "running"
    last_reply: str = ""
    last_message: str = ""  # the last message sent to the agent; resent when the user retries
    last_images: tuple[str, ...] = ()
    submission_id: str | None = None
    task_id: str | None = None


@dataclass(frozen=True)
class Candidate:
    agent_id: str
    display_name: str
    provider_id: str
    score: float
    basis: str  # why it matched: the profile or one example question


@dataclass(frozen=True)
class PendingCandidates:
    """Recommended agents awaiting a choice; `message` goes verbatim to the chosen agent (design §6.2)."""

    message: str
    image_urls: tuple[str, ...]
    candidates: tuple[Candidate, ...]


@dataclass(frozen=True)
class PendingRetry:
    """After a failed turn: option 1 resends it to the same conversation, option 2 recommends another agent."""

    thread_ref: str
    message: str
    image_urls: tuple[str, ...]


Pending = PendingCandidates | PendingRetry


@dataclass(frozen=True)
class Turn:
    role: Literal["user", "router", "agent"]
    text: str
    thread_ref: str | None = None  # agent turns: the thread that replied
    label: str | None = None  # agent turns: e.g. "旅行规划 · T1"


@dataclass
class Session:
    history: list[Turn] = field(default_factory=list)
    threads: dict[str, Thread] = field(default_factory=dict)
    pending: Pending | None = None

    def open_thread(self, agent_id: str, display_name: str, provider_id: str) -> Thread:
        thread = Thread(f"T{len(self.threads) + 1}", agent_id, display_name, provider_id)
        self.threads[thread.ref] = thread
        return thread

    def thread_for_submission(self, submission_id: str) -> Thread | None:
        return next((t for t in self.threads.values() if t.submission_id == submission_id), None)
```

`src/cic_mcp_lab/cic/intent.py`（本 Task 部分）：

```python
"""Intent recognition (design §6.3): one structured LLM decision per user input."""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel

from .session import RETRY_OPTIONS, STATUS_LABELS, PendingCandidates, PendingRetry, Session, Turn

Intent = Literal["supplement", "select_candidate", "router_answer", "recommend", "clarify"]
MAX_HISTORY_TURNS = 12
MAX_TURN_CHARS = 300
MAX_REPLY_CHARS = 200

SYSTEM_PROMPT = """你是 CIC 平台 Router 的意图识别器。CIC 会把用户的请求交给第三方 Agent 处理，或由 Router 自己回答。
请根据「当前状态」和「本次输入」判断本次输入的意图，按 JSON 输出。

意图（intent）取值：
- supplement：用户在回答某个「等待补充」会话的提问；或要求对某个「已完成」会话的结果做需要该 Agent 专业能力的追问或修改（例如"第二天的行程换成购物"）。thread_ref 填该会话编号（如 T1）。会话处于「处理中」时也可以是补充对象。
- select_candidate：存在「待选项」时，用户从中做出了选择（编号、序数、Agent 名称，或"重试""换一个"这类说法）。candidate_index 填从 1 开始的编号。用户点名了不在待选项里的 Agent 时不要用 select_candidate，改用 recommend。
- router_answer：问候、闲聊、简单常识、关于平台本身的问题；以及 LLM 自己就能完成的纯文本任务，例如对已有对话内容（包括第三方 Agent 的回复）做翻译、总结、润色、改格式。
- recommend：需要专业能力或需要产出成果的新任务。即使与已有会话的 Agent 类型相同，只要是新任务（例如"再帮我规划一次大阪旅行"），也用 recommend。
- clarify：有多个会话都可能是补充对象而无法确定（例如两个会话都在等待补充，用户只说"好的，就这样"）；或需求本身不清晰。

字段要求：
- search_query：仅 recommend 时填写，把本次需求改写成一句独立、完整的任务描述（补全上下文中的指代、去掉寒暄），只用于检索 Agent。
- 不适用的字段填 null；reason 用一句中文说明判断依据。"""


class IntentDecision(BaseModel):
    # No defaults: strict json_schema needs every field listed as required (nullable where unused).
    intent: Intent
    thread_ref: str | None
    candidate_index: int | None
    search_query: str | None
    reason: str


def _clip(text: str, limit: int) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[:limit] + "…"


def _speaker(turn: Turn) -> str:
    if turn.role == "user":
        return "用户"
    if turn.role == "router":
        return "Router"
    return f"[{turn.label or turn.thread_ref}]"


def build_intent_prompt(session: Session, text: str, image_count: int = 0) -> str:
    history = [f"{_speaker(t)}：{_clip(t.text, MAX_TURN_CHARS)}" for t in session.history[-MAX_HISTORY_TURNS:]]
    threads = [
        f"- {t.ref} {t.display_name}：{STATUS_LABELS[t.status]}"
        + (f"；Agent 最后的回复：{_clip(t.last_reply, MAX_REPLY_CHARS)}" if t.last_reply else "")
        for t in session.threads.values()
    ]
    pending = session.pending
    if isinstance(pending, PendingCandidates):
        options = [f"[{i}] {c.display_name}" for i, c in enumerate(pending.candidates, 1)]
        choice = [f"推荐候选（选择后会把「{_clip(pending.message, 100)}」转发给所选 Agent）：" + "  ".join(options)]
    elif isinstance(pending, PendingRetry):
        failed = session.threads.get(pending.thread_ref)
        name = failed.display_name if failed else pending.thread_ref
        options = [f"[{i}] {option}" for i, option in enumerate(RETRY_OPTIONS, 1)]
        choice = [f"{name}（{pending.thread_ref}）调用失败，可选：" + "  ".join(options)]
    else:
        choice = []
    attachment = f"（附带 {image_count} 张图片）" if image_count else ""
    sections = [
        ("最近对话", history),
        ("第三方会话", threads),
        ("待选项", choice),
    ]
    parts = [f"## {title}\n" + ("\n".join(lines) if lines else "（无）") for title, lines in sections]
    parts.append(f"## 本次输入\n{text}{attachment}")
    return "\n\n".join(parts)
```

**Step 4:** `uv run pytest tests/test_cic_intent_prompt.py -q` → `4 passed`；`uv run pytest -q` → `163 passed`

**Step 5: Commit**

```powershell
git add src/cic_mcp_lab/cic/session.py src/cic_mcp_lab/cic/intent.py tests/test_cic_intent_prompt.py
git commit -m "feat: router session state and intent recognition prompt" -m "Co-authored-by: Copilot <223556219+Copilot@users.noreply.github.com>"
```

---

### Task 2: LLM 意图识别器 + Router 回复器

**Files:**
- Modify: `src/cic_mcp_lab/cic/intent.py`（追加 `IntentError`、`LlmIntentRecognizer`）
- Create: `src/cic_mcp_lab/cic/responder.py`
- Test: `tests/test_cic_llm_router_parts.py`

**要点：**
- 意图识别：`client.chat.completions.parse(model, messages=[system, user], response_format=IntentDecision, temperature=0, extra_body={"enable_thinking": False})`；`parsed` 为 None 时抛 `IntentError`。
- Router 回复：把会话记录转成 chat messages。用户发言 → user；Router 发言 → assistant；Agent 回复 → assistant，并加前缀 `[旅行规划 · T1 的回复]`。最多取最近 20 条。有本轮要求（澄清、无候选等）时追加到 system prompt。调用 `chat.completions.create(..., extra_body={"enable_thinking": False})`。
- 两者都通过构造参数注入 client，测试中用假 client。

**Step 1: 写失败的测试** — `tests/test_cic_llm_router_parts.py`：

```python
from types import SimpleNamespace

import pytest

from cic_mcp_lab.cic.intent import SYSTEM_PROMPT, IntentDecision, IntentError, LlmIntentRecognizer
from cic_mcp_lab.cic.responder import ROUTER_SYSTEM_PROMPT, LlmResponder, transcript
from cic_mcp_lab.cic.session import Session, Turn

DECISION = IntentDecision(intent="router_answer", thread_ref=None, candidate_index=None, search_query=None, reason="问候")


class FakeCompletions:
    def __init__(self, parsed=DECISION, content="你好！"):
        self.parsed, self.content, self.calls = parsed, content, []

    async def parse(self, **kwargs):
        self.calls.append(kwargs)
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(parsed=self.parsed, refusal="no"))])

    async def create(self, **kwargs):
        self.calls.append(kwargs)
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=self.content))])


def client(completions):
    return SimpleNamespace(chat=SimpleNamespace(completions=completions))


async def test_intent_is_parsed_with_a_strict_schema_and_thinking_off():
    completions = FakeCompletions()
    decision = await LlmIntentRecognizer(client(completions), "qwen4-max").recognize(Session(), "你好")
    call = completions.calls[0]

    assert decision == DECISION
    assert (call["model"], call["response_format"], call["extra_body"]) == (
        "qwen4-max",
        IntentDecision,
        {"enable_thinking": False},
    )
    assert call["messages"][0] == {"role": "system", "content": SYSTEM_PROMPT}
    assert call["messages"][1]["content"].endswith("## 本次输入\n你好")


async def test_missing_structured_output_is_an_error():
    with pytest.raises(IntentError, match="no"):
        await LlmIntentRecognizer(client(FakeCompletions(parsed=None)), "m").recognize(Session(), "你好")


def test_transcript_keeps_roles_and_labels_agent_replies():
    session = Session(
        history=[
            Turn("user", "帮我规划东京旅行"),
            Turn("router", "为你找到以下 Agent…"),
            Turn("agent", "Day 1 …", "T1", "旅行规划 · T1"),
        ]
    )

    assert transcript(session) == [
        {"role": "user", "content": "帮我规划东京旅行"},
        {"role": "assistant", "content": "为你找到以下 Agent…"},
        {"role": "assistant", "content": "[旅行规划 · T1 的回复]\nDay 1 …"},
    ]


async def test_router_answer_uses_the_conversation_and_this_turns_instruction():
    completions = FakeCompletions()
    session = Session(history=[Turn("user", "好的，就这样")])
    reply = await LlmResponder(client(completions), "qwen4-max").answer(session, "请澄清用户在补充哪个会话")
    call = completions.calls[0]

    assert reply == "你好！"
    assert call["messages"][0]["content"].startswith(ROUTER_SYSTEM_PROMPT)
    assert "请澄清用户在补充哪个会话" in call["messages"][0]["content"]
    assert call["messages"][1:] == [{"role": "user", "content": "好的，就这样"}]
    assert call["extra_body"] == {"enable_thinking": False}
```

**Step 2:** `uv run pytest tests/test_cic_llm_router_parts.py -q` → FAIL（`ImportError`）

**Step 3: 实现**

`intent.py` 末尾追加（import 区补充 `from typing import Any`）：

```python
class IntentError(RuntimeError):
    """The model returned no structured decision."""


class LlmIntentRecognizer:
    def __init__(self, client: Any, model: str) -> None:
        self._client = client  # openai.AsyncOpenAI pointed at Bailian's compatible-mode endpoint
        self._model = model

    async def recognize(self, session: Session, text: str, image_count: int = 0) -> IntentDecision:
        completion = await self._client.chat.completions.parse(
            model=self._model,
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": build_intent_prompt(session, text, image_count)},
            ],
            response_format=IntentDecision,
            temperature=0,
            extra_body={"enable_thinking": False},
        )
        message = completion.choices[0].message
        if message.parsed is None:
            raise IntentError(f"Intent recognition returned no decision: {message.refusal or 'empty output'}")
        return message.parsed
```

`src/cic_mcp_lab/cic/responder.py`：

```python
"""Router's own replies (design §6.2): greetings, chit-chat, text tasks on the conversation, clarifications."""
from __future__ import annotations

from typing import Any

from .session import Session

MAX_TRANSCRIPT_TURNS = 20

ROUTER_SYSTEM_PROMPT = """你是 CIC 平台的 Router 助手。CIC 把专业任务交给入驻的第三方 Agent，其余问题由你回答。
- 对话中「[xxx 的回复]」开头的内容是第三方 Agent 的回复，已原样展示给用户；需要时你可以基于对话中的任何内容（包括 Agent 的回复）完成翻译、总结、润色、改格式。
- 不要假装调用了第三方 Agent，也不要编造它们的结果或能力。
- 默认用中文简洁回答，用户要求其他语言时照做。"""


def transcript(session: Session, max_turns: int = MAX_TRANSCRIPT_TURNS) -> list[dict[str, str]]:
    messages: list[dict[str, str]] = []
    for turn in session.history[-max_turns:]:
        if turn.role == "user":
            messages.append({"role": "user", "content": turn.text})
        elif turn.role == "router":
            messages.append({"role": "assistant", "content": turn.text})
        else:
            messages.append({"role": "assistant", "content": f"[{turn.label or turn.thread_ref} 的回复]\n{turn.text}"})
    return messages


class LlmResponder:
    def __init__(self, client: Any, model: str) -> None:
        self._client = client
        self._model = model

    async def answer(self, session: Session, instruction: str | None = None) -> str:
        system = ROUTER_SYSTEM_PROMPT + (f"\n\n本轮要求：{instruction}" if instruction else "")
        completion = await self._client.chat.completions.create(
            model=self._model,
            messages=[{"role": "system", "content": system}, *transcript(session)],
            extra_body={"enable_thinking": False},
        )
        return (completion.choices[0].message.content or "").strip()
```

**Step 4:** `uv run pytest tests/test_cic_llm_router_parts.py -q` → `4 passed`；`uv run pytest -q` → `167 passed`

**Step 5: Commit**

```powershell
git add src/cic_mcp_lab/cic/intent.py src/cic_mcp_lab/cic/responder.py tests/test_cic_llm_router_parts.py
git commit -m "feat: LLM intent recognizer (strict json schema) and router responder via the OpenAI SDK" -m "Co-authored-by: Copilot <223556219+Copilot@users.noreply.github.com>"
```

---

### Task 3: Recommender 支持排除指定 Agent

**Files:**
- Modify: `src/cic_mcp_lab/cic/recommend/recommender.py`
- Test: `tests/test_cic_recommender.py`（追加）

**要点：** 失败后选「重新推荐其他 Agent」时，要排除刚失败的 Agent。排除发生在阈值和 top_k **之前**，以免推荐候选的名额被它占掉。

**Step 1: 追加失败的测试**：

```python
async def test_excluded_agents_are_left_out_before_top_k(store):
    recs = await recommender(store, [1, 0, 0], top_k=2).recommend("q", exclude=("trip",))

    assert ids(recs) == ["copy", "report"]
```

**Step 2:** `uv run pytest tests/test_cic_recommender.py -q` → 新测试失败（`TypeError: unexpected keyword argument 'exclude'`）

**Step 3: 实现** — `Recommender.recommend` 增加关键字参数 `exclude: Collection[str] = ()`（import 补 `Collection`），在 `rank()` 之后、阈值之前过滤：

```python
    async def recommend(
        self,
        query: str,
        *,
        with_image: bool = False,
        include_mock: bool = False,
        exclude: Collection[str] = (),
    ) -> list[Recommendation]:
        ranked = await self.rank(query, with_image=with_image, include_mock=include_mock)
        picks = [pick for pick in ranked if pick.agent_id not in exclude]  # e.g. the agent that just failed
        if self._min_score is not None:
            picks = [pick for pick in picks if pick.score >= self._min_score]
        return picks[: self._top_k]
```

**Step 4:** `uv run pytest tests/test_cic_recommender.py -q` → 全部通过；`uv run pytest -q` → `168 passed`

**Step 5: Commit**

```powershell
git add src/cic_mcp_lab/cic/recommend/recommender.py tests/test_cic_recommender.py
git commit -m "feat: recommender can exclude agents before threshold and top-k" -m "Co-authored-by: Copilot <223556219+Copilot@users.noreply.github.com>"
```

---

### Task 4: Router 编排 `cic/router.py`

**Files:**
- Create: `src/cic_mcp_lab/cic/router.py`
- Test: `tests/test_cic_router.py`

**要点（设计文档 §6.2、§6.5、§6.8）：**
- **每轮流程：** 意图识别 → 输出 `[intent]` 事件 → 把用户发言写入对话记录 → 取出并清空待选项（不是选择时，待选项就此作废）→ 按意图分支。
  - `select_candidate`：校验编号是否在待选项范围内；无效时保留待选项，请 Router 提示用户重新选择。
    - 推荐候选：新建会话，把**原话 + 图片**转发给所选 Agent（不带上下文）；
    - 失败选项 [1]：用同一 conversation_id 重发失败的那条消息；
    - 失败选项 [2]：用失败的那条消息重新推荐，排除失败的 Agent。
  - `supplement`：会话不存在时澄清；会话处于 running 时不提交，提示「××正在处理上一条消息，请稍候」；否则带 conversation_id 提交。
  - `recommend`：用 `search_query` 检索（没有时用原话）；附带图片时只推荐支持图片的 Agent；有候选就展示并写入待选项，没有就提示「暂无合适的 Agent」，再由 Router 尽力回答。
  - `clarify`：把 reason 作为本轮要求交给 responder。
  - `router_answer`：由 responder 回答。
- **提交：** 会话置为 running，记录 last_message / last_images（重试时用）。`SubmitFailed` 走失败流程；`ConversationBusy` 提示稍候。
- **失败流程：** 会话置为 failed，待选项设为 `PendingRetry`，输出「暂时不可用（本次不计费）」及 [1] 重试、[2] 重新推荐其他 Agent。最新的失败会覆盖之前的待选项。
- **`on_outcome`：** 按 submission_id 找到会话。
  - 成功：更新 conversation_id、状态和最后回复；**原样输出** Agent 回复，标注来源与状态，并输出 credit 行（来自账本）；写入对话记录。
  - 失败：走失败流程。
  - 找不到会话（用 `/new` 重置后才结束的任务）：只输出提示。账本已由 TaskWatcher 结算，或判定为失败不计费。
- 所有输出都通过 `emit(RouterEvent(kind, text))`；Router 自己说的话同时写入对话记录（role=router）。

**Step 1: 写失败的测试** — `tests/test_cic_router.py`：

```python
from cic_mcp_lab.cic.contract import AgentReply
from cic_mcp_lab.cic.intent import IntentDecision
from cic_mcp_lab.cic.ledger import Ledger, LedgerEntry
from cic_mcp_lab.cic.recommend.recommender import Recommendation
from cic_mcp_lab.cic.router import Router
from cic_mcp_lab.cic.session import PendingCandidates, PendingRetry
from cic_mcp_lab.cic.task_watcher import Submission, SubmitFailed, TaskOutcome


def decide(intent, *, thread_ref=None, candidate_index=None, search_query=None, reason="r"):
    return IntentDecision(
        intent=intent, thread_ref=thread_ref, candidate_index=candidate_index, search_query=search_query, reason=reason
    )


class ScriptedRecognizer:
    def __init__(self):
        self.next = []

    async def recognize(self, session, text, image_count=0):
        return self.next.pop(0)


class EchoResponder:
    def __init__(self):
        self.instructions = []

    async def answer(self, session, instruction=None):
        self.instructions.append(instruction)
        return "答：" + next(turn.text for turn in reversed(session.history) if turn.role == "user")


class FakeRecommender:
    def __init__(self, *picks):
        self.picks, self.calls = list(picks), []

    async def recommend(self, query, *, with_image=False, include_mock=False, exclude=()):
        self.calls.append((query, with_image, tuple(exclude)))
        return [pick for pick in self.picks if pick.agent_id not in exclude]


class FakeSubmitter:
    def __init__(self):
        self.calls, self.error = [], None

    async def submit(self, agent_id, message, image_urls=(), conversation_id=None):
        if self.error:
            raise self.error
        self.calls.append((agent_id, message, tuple(image_urls), conversation_id))
        return Submission(f"sub_{len(self.calls)}", agent_id, f"task_{len(self.calls)}")


def pick(agent_id, name):
    return Recommendation(agent_id, name, "provider-a", True, 0.8, "example", f"{name}的示例问题")


TRIP, COPY = pick("trip_planner", "旅行规划"), pick("product_copy", "商品文案")


class Harness:
    def __init__(self, *picks):
        self.recognizer, self.responder = ScriptedRecognizer(), EchoResponder()
        self.recommender, self.submitter = FakeRecommender(*picks), FakeSubmitter()
        self.ledger, self.events = Ledger(), []
        self.router = Router(
            recognizer=self.recognizer,
            responder=self.responder,
            recommender=self.recommender,
            submitter=self.submitter,
            ledger=self.ledger,
            emit=self.events.append,
        )

    async def say(self, text, decision, images=()):
        self.recognizer.next.append(decision)
        await self.router.handle(text, images)

    def texts(self, kind):
        return [event.text for event in self.events if event.kind == kind]

    def finish(self, submission_id, agent_id, conversation_id, reply="请问出发日期和预算？", credit=42):
        # TaskWatcher settles before it hands the outcome over.
        self.ledger.settle(LedgerEntry(f"t_{submission_id}", "provider-a", agent_id, conversation_id, credit, 0.0001))
        answer = AgentReply(conversation_id, "input_required", reply, credit)
        self.router.on_outcome(TaskOutcome(submission_id, agent_id, "t", "push", answer, None, "", (), 1.5))

    def fail(self, submission_id, agent_id):
        outcome = TaskOutcome(submission_id, agent_id, "t", None, None, "task_failed", "MCP error -32603: boom", (), 1.0)
        self.router.on_outcome(outcome)


async def test_greetings_are_answered_by_the_router_alone():
    h = Harness(TRIP)
    await h.say("你好", decide("router_answer"))

    assert h.texts("router") == ["答：你好"]
    assert (h.recommender.calls, h.submitter.calls) == ([], [])
    assert [turn.role for turn in h.router.session.history] == ["user", "router"]


async def test_chosen_agent_gets_the_original_message_and_images_in_a_new_thread():
    h = Harness(COPY, TRIP)
    await h.say("帮这个商品写文案", decide("recommend", search_query="为商品写营销文案"), images=["https://img/1.png"])
    await h.say("用第一个", decide("select_candidate", candidate_index=1))

    assert h.recommender.calls == [("为商品写营销文案", True, ())]
    assert "[1] 商品文案" in h.texts("candidates")[0]
    assert h.submitter.calls == [("product_copy", "帮这个商品写文案", ("https://img/1.png",), None)]
    thread = h.router.session.threads["T1"]
    assert (thread.agent_id, thread.status, thread.submission_id) == ("product_copy", "running", "sub_1")
    assert h.router.session.pending is None


async def test_agent_reply_is_shown_as_is_and_the_next_supplement_reuses_its_conversation():
    h = Harness(TRIP)
    await h.say("帮我规划东京旅行", decide("recommend"))
    await h.say("1", decide("select_candidate", candidate_index=1))
    h.finish("sub_1", "trip_planner", "conv_1")
    await h.say("12月5号出发，预算1万", decide("supplement", thread_ref="T1"))

    agent_text = h.texts("agent")[0]
    assert "[旅行规划 · provider-a] T1 · 等待补充" in agent_text
    assert "请问出发日期和预算？" in agent_text and "credit_usage=42 ≈ HKD 0.0042" in agent_text
    assert ("agent", "请问出发日期和预算？", "T1") in [(t.role, t.text, t.thread_ref) for t in h.router.session.history]
    assert h.submitter.calls[-1] == ("trip_planner", "12月5号出发，预算1万", (), "conv_1")
    assert h.router.session.threads["T1"].status == "running"


async def test_supplementing_a_running_thread_waits_instead_of_submitting():
    h = Harness(TRIP)
    await h.say("帮我规划东京旅行", decide("recommend"))
    await h.say("1", decide("select_candidate", candidate_index=1))
    await h.say("预算改成2万", decide("supplement", thread_ref="T1"))

    assert len(h.submitter.calls) == 1
    assert "旅行规划（T1）正在处理上一条消息" in h.texts("router")[-1]


async def test_other_input_drops_the_pending_choice_and_invalid_choices_keep_it():
    h = Harness(TRIP, COPY)
    await h.say("帮我规划东京旅行", decide("recommend"))
    await h.say("第五个", decide("select_candidate", candidate_index=5))

    assert isinstance(h.router.session.pending, PendingCandidates)
    assert h.responder.instructions[-1] is not None
    await h.say("算了，讲个笑话", decide("router_answer"))
    assert h.router.session.pending is None
    assert h.submitter.calls == []


async def test_no_candidate_above_the_threshold_falls_back_to_a_router_answer():
    h = Harness()
    await h.say("帮我订明天去上海的机票", decide("recommend"))

    assert h.texts("router") == ["暂无合适的 Agent，我先尽力回答。", "答：帮我订明天去上海的机票"]
    assert h.router.session.pending is None


async def test_failed_turn_offers_a_retry_on_the_same_conversation():
    h = Harness(TRIP)
    await h.say("帮我规划东京旅行", decide("recommend"))
    await h.say("1", decide("select_candidate", candidate_index=1))
    h.finish("sub_1", "trip_planner", "conv_1")
    await h.say("预算1万", decide("supplement", thread_ref="T1"))
    h.fail("sub_2", "trip_planner")

    failure = h.texts("failure")[0]
    assert h.router.session.threads["T1"].status == "failed"
    assert h.router.session.pending == PendingRetry("T1", "预算1万", ())
    assert "不计费" in failure and "[1] 重试" in failure and "[2] 重新推荐其他 Agent" in failure
    await h.say("重试", decide("select_candidate", candidate_index=1))
    assert h.submitter.calls[-1] == ("trip_planner", "预算1万", (), "conv_1")


async def test_after_a_submit_failure_another_agent_is_recommended_without_the_failed_one():
    h = Harness(TRIP, COPY)
    await h.say("帮我规划东京旅行", decide("recommend"))
    h.submitter.error = SubmitFailed("trip_planner: provider down")
    await h.say("1", decide("select_candidate", candidate_index=1))
    h.submitter.error = None
    await h.say("换一个", decide("select_candidate", candidate_index=2))

    assert "provider down" in h.texts("failure")[0]
    assert h.recommender.calls[-1] == ("帮我规划东京旅行", False, ("trip_planner",))
    pending = h.router.session.pending
    assert isinstance(pending, PendingCandidates)
    assert (pending.message, [c.agent_id for c in pending.candidates]) == ("帮我规划东京旅行", ["product_copy"])


async def test_clarify_and_unknown_threads_ask_the_user():
    h = Harness()
    await h.say("好的，就这样", decide("clarify", reason="T1 和 T2 都在等待补充"))
    await h.say("补充一下", decide("supplement", thread_ref="T9"))

    assert "T1 和 T2 都在等待补充" in h.responder.instructions[0]
    assert "T9" in h.responder.instructions[1]
    assert h.submitter.calls == []


def test_outcomes_after_a_reset_are_reported_but_not_attached():
    h = Harness()
    h.router.reset()
    h.fail("sub_9", "trip_planner")

    assert "不属于当前会话" in h.texts("notice")[0]
    assert h.router.session.pending is None
```

**Step 2:** `uv run pytest tests/test_cic_router.py -q` → FAIL（`ModuleNotFoundError`）

**Step 3: 实现** — `src/cic_mcp_lab/cic/router.py`：

```python
"""Router (design §6.2): every input goes through intent recognition, then code decides what happens.

Agent outcomes arrive asynchronously via `on_outcome`. All output goes through `emit`, so the CLI and the
tests observe the same events.
"""
from __future__ import annotations

from collections.abc import Callable, Collection, Sequence
from dataclasses import dataclass
from typing import Literal, Protocol

from .intent import IntentDecision
from .ledger import Ledger
from .recommend.recommender import Recommendation
from .session import (
    RETRY_OPTIONS,
    STATUS_LABELS,
    Candidate,
    Pending,
    PendingCandidates,
    PendingRetry,
    Session,
    Thread,
    Turn,
)
from .task_watcher import ConversationBusy, Submission, SubmitFailed, TaskOutcome

EventKind = Literal["intent", "router", "candidates", "submitted", "agent", "failure", "notice"]
DELIVERY_LABELS = {"direct": "直接返回", "push": "推送", "recheck": "订阅后补查", "poll": "轮询"}


@dataclass(frozen=True)
class RouterEvent:
    kind: EventKind
    text: str


class Recognizer(Protocol):
    async def recognize(self, session: Session, text: str, image_count: int = 0) -> IntentDecision: ...


class Responder(Protocol):
    async def answer(self, session: Session, instruction: str | None = None) -> str: ...


class AgentRecommender(Protocol):
    async def recommend(
        self, query: str, *, with_image: bool = False, include_mock: bool = False, exclude: Collection[str] = ()
    ) -> list[Recommendation]: ...


class AgentSubmitter(Protocol):
    async def submit(
        self, agent_id: str, message: str, image_urls: Sequence[str] = (), conversation_id: str | None = None
    ) -> Submission: ...


def describe(decision: IntentDecision) -> str:
    target = {
        "supplement": f" → {decision.thread_ref}",
        "select_candidate": f" → [{decision.candidate_index}]",
        "recommend": f" → 「{decision.search_query}」" if decision.search_query else "",
    }.get(decision.intent, "")
    return f"[intent] {decision.intent}{target}（{decision.reason}）"


def _option_count(pending: Pending) -> int:
    return len(pending.candidates) if isinstance(pending, PendingCandidates) else len(RETRY_OPTIONS)


class Router:
    def __init__(
        self,
        *,
        recognizer: Recognizer,
        responder: Responder,
        recommender: AgentRecommender,
        submitter: AgentSubmitter,
        ledger: Ledger,
        emit: Callable[[RouterEvent], None],
        include_mock: bool = False,
    ) -> None:
        self._recognizer = recognizer
        self._responder = responder
        self._recommender = recommender
        self._submitter = submitter
        self._ledger = ledger
        self._sink = emit
        self._include_mock = include_mock
        self.session = Session()

    def reset(self) -> None:
        """Start a new session (/new); tasks still running keep settling in the ledger."""
        self.session = Session()

    async def handle(self, text: str, image_urls: Sequence[str] = ()) -> None:
        images = tuple(image_urls)
        decision = await self._recognizer.recognize(self.session, text, len(images))
        self._emit("intent", describe(decision))
        suffix = f"（附带 {len(images)} 张图片）" if images else ""
        self.session.history.append(Turn("user", text + suffix))
        # Any input other than a choice drops the pending choice (design §6.2 rule 3).
        pending, self.session.pending = self.session.pending, None
        if decision.intent == "select_candidate":
            await self._select(pending, decision.candidate_index)
        elif decision.intent == "supplement":
            await self._supplement(decision.thread_ref, text, images)
        elif decision.intent == "recommend":
            await self._recommend(text, images, decision.search_query or text)
        elif decision.intent == "clarify":
            await self._answer(
                f"用户的意图不明确（{decision.reason}）。请用一两句话向用户澄清；涉及多个会话时列出 T1、T2… 让用户指明。"
            )
        else:
            await self._answer()

    def on_outcome(self, outcome: TaskOutcome) -> None:
        """A submitted turn has finished (design §6.8); the agent's reply is shown as-is (§6.5)."""
        thread = self.session.thread_for_submission(outcome.submission_id)
        if thread is None:  # the session was reset while the task ran; the ledger settled it regardless
            state = "已结算" if outcome.ok else "失败，不计费"
            self._emit("notice", f"[notice] {outcome.agent_id} 的一个任务已结束，但不属于当前会话（{state}）")
            return
        if not outcome.ok or outcome.reply is None:
            self._fail(thread, f"{outcome.failure}: {outcome.detail}")
            return
        reply = outcome.reply
        thread.conversation_id, thread.status, thread.last_reply = reply.conversation_id, reply.status, reply.reply
        via = DELIVERY_LABELS.get(outcome.delivery or "", "未知途径")
        header = (
            f"[{thread.display_name} · {thread.provider_id}] {thread.ref} · {STATUS_LABELS[thread.status]}"
            f"（经{via}，{outcome.elapsed_seconds:.1f}s）"
        )
        self._emit("agent", f"{header}\n{reply.reply}\n{self._credit_line(thread, reply.credit_usage)}")
        self.session.history.append(Turn("agent", reply.reply, thread.ref, f"{thread.display_name} · {thread.ref}"))

    # ---- branches -------------------------------------------------------------------------------

    async def _select(self, pending: Pending | None, index: int | None) -> None:
        if pending is None or index is None or not 1 <= index <= _option_count(pending):
            self.session.pending = pending  # keep the choice open
            await self._answer("用户像是在做选择，但当前没有待选项或编号超出范围。请提示用户按编号从当前待选项中选择，或直接说明新的需求。")
            return
        if isinstance(pending, PendingCandidates):
            chosen = pending.candidates[index - 1]
            thread = self.session.open_thread(chosen.agent_id, chosen.display_name, chosen.provider_id)
            await self._submit(thread, pending.message, pending.image_urls)
            return
        failed = self.session.threads[pending.thread_ref]
        if index == 1:
            await self._submit(failed, pending.message, pending.image_urls)
        else:
            await self._recommend(pending.message, pending.image_urls, pending.message, exclude=(failed.agent_id,))

    async def _supplement(self, ref: str | None, text: str, images: tuple[str, ...]) -> None:
        thread = self.session.threads.get(ref or "")
        if thread is None:
            await self._answer(f"用户像是在补充某个会话，但会话 {ref} 不存在。请向用户澄清是在补充哪个会话。")
            return
        if thread.status == "running":
            self._say(f"{thread.display_name}（{thread.ref}）正在处理上一条消息，请稍候。")
            return
        await self._submit(thread, text, images)

    async def _recommend(
        self, message: str, images: tuple[str, ...], query: str, exclude: Collection[str] = ()
    ) -> None:
        picks = await self._recommender.recommend(
            query, with_image=bool(images), include_mock=self._include_mock, exclude=exclude
        )
        if not picks:
            self._say("暂无合适的 Agent，我先尽力回答。")
            await self._answer()
            return
        candidates = tuple(
            Candidate(
                p.agent_id,
                p.display_name,
                p.provider_id,
                p.score,
                "画像" if p.matched_kind == "profile" else f"示例问题「{p.matched_text}」",
            )
            for p in picks
        )
        self.session.pending = PendingCandidates(message, images, candidates)
        lines = ["为你找到以下 Agent（按匹配度排序）："]
        lines += [
            f"  [{i}] {c.display_name} · {c.provider_id}   匹配度 {c.score:.2f}   依据：{c.basis}"
            for i, c in enumerate(candidates, 1)
        ]
        lines.append("回复编号或 Agent 名称进行选择，或直接输入其他内容")
        text = "\n".join(lines)
        self._emit("candidates", text)
        self.session.history.append(Turn("router", text))

    async def _answer(self, instruction: str | None = None) -> None:
        self._say(await self._responder.answer(self.session, instruction))

    # ---- submitting and failures --------------------------------------------------------------------

    async def _submit(self, thread: Thread, message: str, images: tuple[str, ...]) -> None:
        thread.status = "running"
        thread.last_message, thread.last_images = message, images
        try:
            submission = await self._submitter.submit(thread.agent_id, message, images, thread.conversation_id)
        except ConversationBusy:
            self._say(f"{thread.display_name}（{thread.ref}）正在处理上一条消息，请稍候。")
            return
        except SubmitFailed as exc:
            self._fail(thread, str(exc))
            return
        thread.submission_id, thread.task_id = submission.submission_id, submission.task_id
        task = submission.task_id or "无，Provider 直接返回"
        self._emit("submitted", f"[{thread.ref}] 已提交给 {thread.display_name}（taskId={task}），处理中…")
        for warning in submission.warnings:
            self._emit("notice", f"[warn] {warning}")

    def _fail(self, thread: Thread, detail: str) -> None:
        thread.status = "failed"
        self.session.pending = PendingRetry(thread.ref, thread.last_message, thread.last_images)
        options = "  ".join(f"[{i}] {option}" for i, option in enumerate(RETRY_OPTIONS, 1))
        text = f"{thread.display_name}（{thread.ref}）暂时不可用：{detail}（本次不计费）。\n你可以选择：{options}"
        self._emit("failure", text)
        self.session.history.append(Turn("router", text))

    def _credit_line(self, thread: Thread, credit: float) -> str:
        entries = [
            entry
            for entry in self._ledger.entries
            if entry.agent_id == thread.agent_id and entry.conversation_id == thread.conversation_id
        ]
        total = sum(entry.credit_usage for entry in entries)
        hkd = f" ≈ HKD {entries[-1].hkd}" if entries else ""
        return f"[credit] {thread.agent_id} credit_usage={credit}{hkd}（本会话累计 {total} Credit）"

    def _say(self, text: str) -> None:
        self._emit("router", text)
        self.session.history.append(Turn("router", text))

    def _emit(self, kind: EventKind, text: str) -> None:
        self._sink(RouterEvent(kind, text))
```

> 注意：
> - 测试中的 `Recommendation(...)` 按 Step 5 的字段顺序构造：`agent_id, display_name, provider_id, callable, dense_score, matched_kind, matched_text`。
> - `LedgerEntry.hkd` 为 `Decimal`，`0.0042` 的字符串形式就是 `0.0042`。

**Step 4:** `uv run pytest tests/test_cic_router.py -q` → `10 passed`；`uv run pytest -q` → `178 passed`

**Step 5: Commit**

```powershell
git add src/cic_mcp_lab/cic/router.py tests/test_cic_router.py
git commit -m "feat: code-orchestrated router (intent → recommend / select / supplement / answer / clarify, retry on failure)" -m "Co-authored-by: Copilot <223556219+Copilot@users.noreply.github.com>"
```

---

### Task 5: `cic-chat` 交互式命令行

**Files:**
- Create: `src/cic_mcp_lab/cic/cli.py`
- Modify: `pyproject.toml`（依赖 `prompt-toolkit`；script `cic-chat = "cic_mcp_lab.cic.cli:main"`）、`README.md`
- Test: `tests/test_cic_cli.py`

**要点（设计文档 §6.6）：**
- 组装：Recommender（`RERANK_ENABLED` 时用 Rerank 和 Rerank 阈值，否则用向量阈值）+ TaskWatcher + Ledger + LlmIntentRecognizer / LlmResponder（共用一个 `AsyncOpenAI`）+ OssUploader。
- 后台协程 `_consume` 不断从 `watcher.outcomes` 取结果，交给 `router.on_outcome`。
- 输入用 prompt_toolkit 的 `PromptSession.prompt_async`，并在 `patch_stdout()` 中运行，这样任务结果在用户输入时也能打印，且不会打乱输入行。
- 命令：
  - `/image <路径或URL>`：上传 OSS，作为下一条消息的附件；
  - `/threads`：会话列表，显示状态、conversation_id、处理中的 taskId；
  - `/usage`：按 Provider → Agent 汇总 Credit 与 HKD；
  - `/new`：新会话；
  - `/help`、`/exit`。
- `--include-mock`：同时推荐模拟 Agent。选中模拟 Agent 时，提交会失败，按失败流程处理。
- `handle()` 抛出的异常（例如 LLM 调用失败）打印 `[error]` 后继续；Qdrant 本地库在整个会话期间保持打开（不能同时运行 `cic-onboard` / `cic-eval`）。
- `threads_text` 和 `usage_text` 两个纯函数有单测，其余是胶水代码。

**Step 1: 安装依赖**

```powershell
uv add prompt-toolkit
```

（如果因为 exe 被占用而失败，报告给用户，请用户停掉 provider-server 后重试；不要用 `--no-sync` 绕过，否则无法导入。）

**Step 2: 写失败的测试** — `tests/test_cic_cli.py`：

```python
from cic_mcp_lab.cic.cli import threads_text, usage_text
from cic_mcp_lab.cic.ledger import Ledger, LedgerEntry
from cic_mcp_lab.cic.session import Session


def test_threads_list_shows_status_ids_and_the_last_reply():
    session = Session()
    trip = session.open_thread("trip_planner", "旅行规划", "provider-a")
    trip.status, trip.conversation_id, trip.last_reply = "input_required", "conv_1", "请问出发日期和预算？"
    report = session.open_thread("weekly_report", "周报撰写", "provider-b")
    report.task_id = "task_9"

    assert threads_text(session).splitlines() == [
        "T1 旅行规划 · provider-a · 等待补充 · conversation_id=conv_1",
        "  最后回复：请问出发日期和预算？",
        "T2 周报撰写 · provider-b · 处理中 · taskId=task_9",
    ]
    assert threads_text(Session()) == "（暂无会话）"


def test_usage_is_grouped_by_provider_then_agent():
    ledger = Ledger()
    for key, provider, agent, credit in [
        ("t1", "provider-a", "trip_planner", 100),
        ("t2", "provider-a", "trip_planner", 50),
        ("t3", "provider-b", "weekly_report", 20),
    ]:
        ledger.settle(LedgerEntry(key, provider, agent, "conv", credit, 0.0001))

    assert usage_text(ledger).splitlines() == [
        "provider-a：150 Credit ≈ HKD 0.0150",
        "  trip_planner：2 次 · 150 Credit ≈ HKD 0.0150",
        "provider-b：20 Credit ≈ HKD 0.0020",
        "  weekly_report：1 次 · 20 Credit ≈ HKD 0.0020",
        "合计：170 Credit ≈ HKD 0.0170",
    ]
    assert usage_text(Ledger()) == "（暂无计费记录）"
```

**Step 3:** `uv run pytest tests/test_cic_cli.py -q` → FAIL（`ModuleNotFoundError`）

**Step 4: 实现** — `src/cic_mcp_lab/cic/cli.py`：

```python
"""cic-chat: talk to the CIC Router in the terminal (design §6.6; step 6 checkpoint)."""
from __future__ import annotations

import argparse
import asyncio
import os
import sys
from collections import defaultdict
from decimal import Decimal
from pathlib import Path

from prompt_toolkit import PromptSession
from prompt_toolkit.patch_stdout import patch_stdout

from ..config import load_settings
from ..llm import make_openai_client
from .intent import LlmIntentRecognizer
from .ledger import Ledger, LedgerEntry
from .oss import OssUploader
from .recommend.embedding import make_embedder
from .recommend.recommender import Recommender
from .recommend.rerank import make_reranker
from .recommend.vector_store import QdrantVectorStore
from .registry import load_registry
from .responder import LlmResponder
from .router import Router, RouterEvent
from .session import STATUS_LABELS, Session
from .task_watcher import TaskWatcher

HELP = "命令：/image <本地路径或URL> 附加图片；/threads 会话列表；/usage 用量汇总；/new 新会话；/help 帮助；/exit 退出"


def _clip(text: str, limit: int = 60) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[:limit] + "…"


def threads_text(session: Session) -> str:
    if not session.threads:
        return "（暂无会话）"
    lines: list[str] = []
    for thread in session.threads.values():
        line = f"{thread.ref} {thread.display_name} · {thread.provider_id} · {STATUS_LABELS[thread.status]}"
        if thread.conversation_id:
            line += f" · conversation_id={thread.conversation_id}"
        if thread.status == "running" and thread.task_id:
            line += f" · taskId={thread.task_id}"
        lines.append(line)
        if thread.last_reply:
            lines.append(f"  最后回复：{_clip(thread.last_reply)}")
    return "\n".join(lines)


def _sum(entries: list[LedgerEntry]) -> tuple[float, Decimal]:
    return sum(entry.credit_usage for entry in entries), sum((entry.hkd for entry in entries), Decimal(0))


def usage_text(ledger: Ledger) -> str:
    if not ledger.entries:
        return "（暂无计费记录）"
    grouped: dict[str, dict[str, list[LedgerEntry]]] = defaultdict(lambda: defaultdict(list))
    for entry in ledger.entries:
        grouped[entry.provider_id][entry.agent_id].append(entry)
    lines: list[str] = []
    for provider_id, agents in grouped.items():
        credit, hkd = _sum([entry for entries in agents.values() for entry in entries])
        lines.append(f"{provider_id}：{credit} Credit ≈ HKD {hkd}")
        for agent_id, entries in agents.items():
            agent_credit, agent_hkd = _sum(entries)
            lines.append(f"  {agent_id}：{len(entries)} 次 · {agent_credit} Credit ≈ HKD {agent_hkd}")
    lines.append(f"合计：{ledger.total_credit()} Credit ≈ HKD {ledger.total_hkd()}")
    return "\n".join(lines)


def _print_event(event: RouterEvent) -> None:
    if event.kind in ("agent", "failure", "candidates"):
        print()  # set multi-line blocks apart
    prefix = "[Router] " if event.kind in ("router", "failure") else ""
    print(f"{prefix}{event.text}")


async def _consume(watcher: TaskWatcher, router: Router) -> None:
    while True:
        outcome = await watcher.outcomes.get()
        try:
            router.on_outcome(outcome)
        except Exception as exc:  # keep receiving outcomes whatever happens to one of them
            print(f"[error] {type(exc).__name__}: {exc}")


async def _chat(include_mock: bool) -> None:
    settings = load_settings()
    store = QdrantVectorStore.open(
        settings.qdrant_path, embedding_model=settings.embedding_model, dim=settings.embedding_dim
    )
    try:
        use_rerank = settings.rerank_enabled
        recommender = Recommender(
            make_embedder(settings),
            store,
            load_registry,
            recall_top_n=settings.recall_top_n,
            top_k=settings.recommend_top_k,
            min_score=settings.recommend_min_score_rerank if use_rerank else settings.recommend_min_score_dense,
            reranker=make_reranker(settings) if use_rerank else None,
        )
        client = make_openai_client(settings)
        ledger = Ledger()
        uploader = OssUploader(settings)
        backstop = float(os.environ.get("TASK_BACKSTOP_POLL_SECONDS", "30"))
        async with TaskWatcher(load_registry, ledger, backstop_poll_seconds=backstop) as watcher:
            router = Router(
                recognizer=LlmIntentRecognizer(client, settings.router_model),
                responder=LlmResponder(client, settings.router_model),
                recommender=recommender,
                submitter=watcher,
                ledger=ledger,
                emit=_print_event,
                include_mock=include_mock,
            )
            consumer = asyncio.create_task(_consume(watcher, router))
            prompt: PromptSession[str] = PromptSession()
            images: list[str] = []
            mode = f"Rerank {'开' if use_rerank else '关'}" + ("，含模拟 Agent" if include_mock else "")
            print(f"CIC Router 已就绪（{mode}）。{HELP}")
            try:
                with patch_stdout():
                    while True:
                        try:
                            line = (await prompt.prompt_async("> ")).strip()
                        except (EOFError, KeyboardInterrupt):
                            break
                        if not line:
                            continue
                        if line == "/exit":
                            break
                        if line == "/threads":
                            print(threads_text(router.session))
                        elif line == "/usage":
                            print(usage_text(ledger))
                        elif line == "/new":
                            router.reset()
                            images = []
                            print("已开启新会话")
                        elif line.startswith("/image "):
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
                            print(f"已附加图片（随下一条消息发送）：{url}")
                        elif line.startswith("/"):
                            print(HELP)
                        else:
                            try:
                                await router.handle(line, images)
                            except Exception as exc:
                                print(f"[error] {type(exc).__name__}: {exc}")
                            images = []
            finally:
                consumer.cancel()
    finally:
        store.close()


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description="与 CIC Router 对话：意图识别 → 推荐 / 选择 / 补充 / 直接回答（设计文档 §6）")
    parser.add_argument("--include-mock", action="store_true", help="同时推荐只有画像、不接 MCP 的模拟 Agent")
    args = parser.parse_args()
    try:
        asyncio.run(_chat(args.include_mock))
    except KeyboardInterrupt:
        pass
```

**Step 5: 配套修改**
- `pyproject.toml`：`[project.scripts]` 末尾追加 `cic-chat = "cic_mcp_lab.cic.cli:main"`（`uv add` 已把 prompt-toolkit 加进 dependencies）。
- `README.md`：在 `cic-eval` 一行之后、`pytest` 一行之前插入：

```markdown
| `uv run cic-chat [--include-mock]` | 与 CIC Router 对话：每轮意图识别 → 推荐 / 选择候选 / 补充会话 / 直接回答 / 澄清；任务异步提交，结果到达后打印并结算。命令：`/image`、`/threads`、`/usage`、`/new`、`/exit` |
```

**Step 6: 验证**
- `uv run pytest tests/test_cic_cli.py -q` → `2 passed`；`uv run pytest -q` → `180 passed`
- `uv run cic-chat --help` → 打印帮助（不要真正进入对话）

**Step 7: Commit**

```powershell
git add src/cic_mcp_lab/cic/cli.py tests/test_cic_cli.py pyproject.toml uv.lock README.md
git commit -m "feat: cic-chat terminal client with async agent replies" -m "Co-authored-by: Copilot <223556219+Copilot@users.noreply.github.com>"
```

---

### Task 6: 意图识别评测 `cic-intent-eval`

**Files:**
- Create: `data/intent_cases.yaml`、`src/cic_mcp_lab/cic/intent_eval.py`
- Modify: `pyproject.toml`（`cic-intent-eval = "cic_mcp_lab.cic.intent_eval:main"`）、`README.md`
- Test: `tests/test_cic_intent_eval.py`

**要点：**
- **测试集：** 34 条。由 19 个验收场景改写，再加上变体（按名称 / 序数选择、失败后重试 / 换 Agent、总结 / 润色、多会话判断等），覆盖 5 种意图，每种至少 3 条。
- **每条测试：** 前置状态（第三方会话、对话记录、待选项）+ 本次输入（可附带图片）→ 期望的 intent，以及 thread_ref / candidate_index（给出时也要匹配）。
- **判定：** intent 相同，且期望中给出的 thread_ref / candidate_index 也相同，才算正确；调用异常算错误。
- **报告（`reports/intent-eval-<时间>.md`）：** 总体准确率；按期望意图的准确率；错例（期望 vs 实际，及模型给出的理由）；recommend 类的 `search_query` 改写结果；平均 / P95 延迟。终端打印总体部分。

**Step 1: 写失败的测试** — `tests/test_cic_intent_eval.py`：

```python
from collections import Counter

from cic_mcp_lab.cic.intent import IntentDecision
from cic_mcp_lab.cic.intent_eval import load_intent_cases, render_intent_report, run_intent_eval
from cic_mcp_lab.cic.session import PendingCandidates

INTENTS = {"supplement", "select_candidate", "router_answer", "recommend", "clarify"}


def test_cases_cover_every_intent_and_reference_real_state():
    cases = load_intent_cases()
    counts = Counter(case.expect.intent for case in cases)

    assert len(cases) >= 30 and set(counts) == INTENTS and min(counts.values()) >= 3
    for case in cases:
        if case.expect.thread_ref:
            assert case.expect.thread_ref in case.session.threads, case.name
        if case.expect.intent == "select_candidate":
            assert case.session.pending is not None, case.name


def write_cases(tmp_path):
    path = tmp_path / "cases.yaml"
    path.write_text(
        """cases:
  - name: 问候
    input: 你好
    expect: {intent: router_answer}
  - name: 补充
    threads: [{ref: T1, agent: trip_planner, status: input_required, last_reply: 请问预算？}]
    history: [{role: agent, thread: T1, text: 请问预算？}]
    input: 预算一万
    expect: {intent: supplement, thread_ref: T1}
  - name: 选择
    pending: {candidates: [trip_planner, hotel_booking], message: 去东京玩}
    input: 第二个
    expect: {intent: select_candidate, candidate_index: 2}
""",
        encoding="utf-8",
    )
    return path


class KeyedRecognizer:
    async def recognize(self, session, text, image_count=0):
        if text == "第二个":
            raise RuntimeError("timeout")
        ref = "T2" if text == "预算一万" else None  # wrong thread on purpose
        intent = "router_answer" if text == "你好" else "supplement"
        return IntentDecision(intent=intent, thread_ref=ref, candidate_index=None, search_query=None, reason="r")


async def test_results_are_scored_on_intent_and_target(tmp_path):
    cases = load_intent_cases(write_cases(tmp_path))
    results = await run_intent_eval(cases, KeyedRecognizer())
    report = render_intent_report(results, header_lines=["测试"])

    assert isinstance(cases[2].session.pending, PendingCandidates)
    assert [result.correct for result in results] == [True, False, False]
    assert results[2].error == "RuntimeError: timeout"
    assert "33.3%" in report and "## 错例" in report and "预算一万" in report
```

**Step 2:** `uv run pytest tests/test_cic_intent_eval.py -q` → FAIL（`ModuleNotFoundError`）

**Step 3: 实现**

`data/intent_cases.yaml`：

```yaml
# Intent recognition cases (design §6.3): the 19 acceptance scenarios of §6.7 plus variants.
# Each case: state (threads / history / pending) + input → expected intent (+ thread_ref / candidate_index when given).
cases:
  - name: 1 问候
    input: 你好
    expect: {intent: router_answer}

  - name: 2 新需求
    input: 帮我规划去东京的旅行
    expect: {intent: recommend}

  - name: 3 选择第一个候选
    pending: {candidates: [trip_planner, product_copy], message: 帮我规划去东京的旅行}
    input: 用第一个
    expect: {intent: select_candidate, candidate_index: 1}

  - name: 4 回答 Agent 的反问
    threads:
      - {ref: T1, agent: trip_planner, status: input_required, last_reply: 好的！东京之行没问题。请问计划哪天出发？总预算大概多少？}
    history:
      - {role: user, text: 帮我规划去东京的旅行}
      - {role: agent, thread: T1, text: 好的！东京之行没问题。请问计划哪天出发？总预算大概多少？}
    input: 12 月 5 号出发，预算 1 万
    expect: {intent: supplement, thread_ref: T1}

  - name: 5 等待补充时闲聊
    threads:
      - {ref: T1, agent: trip_planner, status: input_required, last_reply: 好的！东京之行没问题。请问计划哪天出发？总预算大概多少？}
    history:
      - {role: user, text: 帮我规划去东京的旅行}
      - {role: agent, thread: T1, text: 好的！东京之行没问题。请问计划哪天出发？总预算大概多少？}
    input: 今天星期几？
    expect: {intent: router_answer}

  - name: 6 等待补充时开新任务
    threads:
      - {ref: T1, agent: trip_planner, status: input_required, last_reply: 好的！东京之行没问题。请问计划哪天出发？总预算大概多少？}
    history:
      - {role: user, text: 帮我规划去东京的旅行}
      - {role: agent, thread: T1, text: 好的！东京之行没问题。请问计划哪天出发？总预算大概多少？}
    input: 另外帮我写本周周报
    expect: {intent: recommend}

  - name: 7 两个会话按语义归属
    threads:
      - {ref: T1, agent: trip_planner, status: input_required, last_reply: 请问计划哪天出发？总预算大概多少？}
      - {ref: T2, agent: weekly_report, status: input_required, last_reply: 请列一下你本周完成的主要工作。}
    history:
      - {role: agent, thread: T1, text: 请问计划哪天出发？总预算大概多少？}
      - {role: agent, thread: T2, text: 请列一下你本周完成的主要工作。}
    input: 预算改成 2 万
    expect: {intent: supplement, thread_ref: T1}

  - name: 8 两个会话无法判断
    threads:
      - {ref: T1, agent: trip_planner, status: input_required, last_reply: 请问计划哪天出发？总预算大概多少？}
      - {ref: T2, agent: weekly_report, status: input_required, last_reply: 请列一下你本周完成的主要工作。}
    history:
      - {role: agent, thread: T1, text: 请问计划哪天出发？总预算大概多少？}
      - {role: agent, thread: T2, text: 请列一下你本周完成的主要工作。}
    input: 好的，就这样
    expect: {intent: clarify}

  - name: 9 修改已完成的结果
    threads:
      - {ref: T1, agent: trip_planner, status: completed, last_reply: 【东京 3 日行程】Day 1 浅草寺、晴空塔；Day 2 明治神宫、涩谷；Day 3 上野公园、秋叶原。}
    history:
      - {role: agent, thread: T1, text: 【东京 3 日行程】Day 1 浅草寺、晴空塔；Day 2 明治神宫、涩谷；Day 3 上野公园、秋叶原。}
    input: 第二天的行程换成购物
    expect: {intent: supplement, thread_ref: T1}

  - name: 10 已完成后同类新任务
    threads:
      - {ref: T1, agent: trip_planner, status: completed, last_reply: 【东京 3 日行程】Day 1 浅草寺、晴空塔；Day 2 明治神宫、涩谷；Day 3 上野公园、秋叶原。}
    history:
      - {role: agent, thread: T1, text: 【东京 3 日行程】Day 1 浅草寺、晴空塔；Day 2 明治神宫、涩谷；Day 3 上野公园、秋叶原。}
    input: 再帮我规划一次大阪旅行
    expect: {intent: recommend}

  - name: 11 带图片的新任务
    image_count: 1
    input: 帮这个商品写小红书文案
    expect: {intent: recommend}

  - name: 12 没有合适 Agent 的新任务
    input: 帮我订一张明天去上海的机票
    expect: {intent: recommend}

  - name: 13 待选时转去闲聊
    pending: {candidates: [trip_planner, product_copy], message: 帮我规划去东京的旅行}
    input: 算了，讲个笑话吧
    expect: {intent: router_answer}

  - name: 14 补充另一个 Provider 的会话
    threads:
      - {ref: T1, agent: trip_planner, status: completed, last_reply: 【东京 3 日行程】Day 1 浅草寺、晴空塔；Day 2 明治神宫、涩谷；Day 3 上野公园、秋叶原。}
      - {ref: T2, agent: weekly_report, status: input_required, last_reply: 请列一下你本周完成的主要工作。}
    history:
      - {role: agent, thread: T2, text: 请列一下你本周完成的主要工作。}
    input: 周报补充：这周还完成了接口联调
    expect: {intent: supplement, thread_ref: T2}

  - name: 15 翻译 Agent 的结果
    threads:
      - {ref: T1, agent: trip_planner, status: completed, last_reply: 【东京 3 日行程】Day 1 浅草寺、晴空塔；Day 2 明治神宫、涩谷；Day 3 上野公园、秋叶原。}
    history:
      - {role: agent, thread: T1, text: 【东京 3 日行程】Day 1 浅草寺、晴空塔；Day 2 明治神宫、涩谷；Day 3 上野公园、秋叶原。}
    input: 把上面的行程翻译成英文
    expect: {intent: router_answer}

  - name: 16 点名候选之外的 Agent
    pending: {candidates: [trip_planner, product_copy], message: 帮我规划去东京的旅行}
    input: 我要用周报撰写那个
    expect: {intent: recommend}

  - name: 17 处理中时补充
    threads:
      - {ref: T1, agent: trip_planner, status: running, last_reply: 好的！东京之行没问题。请问计划哪天出发？总预算大概多少？}
    history:
      - {role: agent, thread: T1, text: 好的！东京之行没问题。请问计划哪天出发？总预算大概多少？}
      - {role: user, text: 12 月 5 号出发，预算 1 万}
    input: 预算改成 2 万
    expect: {intent: supplement, thread_ref: T1}

  - name: 18 处理中时闲聊
    threads:
      - {ref: T1, agent: trip_planner, status: running, last_reply: 好的！东京之行没问题。请问计划哪天出发？总预算大概多少？}
    history:
      - {role: user, text: 12 月 5 号出发，预算 1 万}
    input: 今天星期几？
    expect: {intent: router_answer}

  - name: 19 选择周报候选
    pending: {candidates: [weekly_report, meeting_minutes], message: 帮我写本周周报}
    input: 第一个
    expect: {intent: select_candidate, candidate_index: 1}

  - name: 20 按名称选择
    pending: {candidates: [trip_planner, hotel_booking], message: 下周去大阪玩几天}
    input: 旅行规划那个
    expect: {intent: select_candidate, candidate_index: 1}

  - name: 21 按序数选择第二个
    pending: {candidates: [trip_planner, hotel_booking], message: 下周去大阪玩几天}
    input: 第二个吧
    expect: {intent: select_candidate, candidate_index: 2}

  - name: 22 失败后重试
    threads:
      - {ref: T1, agent: weekly_report, status: failed}
    pending: {retry: T1, message: 帮我写周报：本周完成了接口联调}
    input: 重试一下
    expect: {intent: select_candidate, candidate_index: 1}

  - name: 23 失败后换 Agent
    threads:
      - {ref: T1, agent: weekly_report, status: failed}
    pending: {retry: T1, message: 帮我写周报：本周完成了接口联调}
    input: 换一个别的 Agent 吧
    expect: {intent: select_candidate, candidate_index: 2}

  - name: 24 失败后转去别的话题
    threads:
      - {ref: T1, agent: weekly_report, status: failed}
    pending: {retry: T1, message: 帮我写周报：本周完成了接口联调}
    input: 先不用了，今天天气怎么样
    expect: {intent: router_answer}

  - name: 25 总结 Agent 的结果
    threads:
      - {ref: T1, agent: trip_planner, status: completed, last_reply: 【东京 3 日行程】Day 1 浅草寺、晴空塔；Day 2 明治神宫、涩谷；Day 3 上野公园、秋叶原。}
    history:
      - {role: agent, thread: T1, text: 【东京 3 日行程】Day 1 浅草寺、晴空塔；Day 2 明治神宫、涩谷；Day 3 上野公园、秋叶原。}
    input: 帮我总结一下这份行程的重点
    expect: {intent: router_answer}

  - name: 26 关于平台的问题
    input: CIC 平台都能做什么？
    expect: {intent: router_answer}

  - name: 27 写邮件的新任务
    input: 帮我写一封邮件给客户，说明发货要延迟三天
    expect: {intent: recommend}

  - name: 28 回答文案 Agent 的反问
    threads:
      - {ref: T1, agent: product_copy, status: input_required, last_reply: 请问文案投放在哪个平台？希望什么风格？}
    history:
      - {role: agent, thread: T1, text: 请问文案投放在哪个平台？希望什么风格？}
    input: 小红书，活泼一点
    expect: {intent: supplement, thread_ref: T1}

  - name: 29 两个会话按内容归属
    threads:
      - {ref: T1, agent: trip_planner, status: input_required, last_reply: 请问计划哪天出发？总预算大概多少？}
      - {ref: T2, agent: product_copy, status: input_required, last_reply: 请问文案投放在哪个平台？希望什么风格？}
    history:
      - {role: agent, thread: T1, text: 请问计划哪天出发？总预算大概多少？}
      - {role: agent, thread: T2, text: 请问文案投放在哪个平台？希望什么风格？}
    input: 抖音，风格要搞笑
    expect: {intent: supplement, thread_ref: T2}

  - name: 30 依赖上文的新任务
    history:
      - {role: user, text: 这周我主要做了接口联调和需求评审}
      - {role: router, text: 听起来这周挺充实的！}
    input: 帮我写成周报
    expect: {intent: recommend}

  - name: 31 致谢
    threads:
      - {ref: T1, agent: trip_planner, status: completed, last_reply: 【东京 3 日行程】Day 1 浅草寺、晴空塔；Day 2 明治神宫、涩谷；Day 3 上野公园、秋叶原。}
    history:
      - {role: agent, thread: T1, text: 【东京 3 日行程】Day 1 浅草寺、晴空塔；Day 2 明治神宫、涩谷；Day 3 上野公园、秋叶原。}
    input: 谢谢，很有帮助
    expect: {intent: router_answer}

  - name: 32 润色一段话
    input: 帮我把这句话润色一下：我们的产品非常好用，欢迎购买
    expect: {intent: router_answer}

  - name: 33 需求不清晰
    input: 帮我弄一下那个
    expect: {intent: clarify}

  - name: 34 两个已完成会话都可能被修改
    threads:
      - {ref: T1, agent: trip_planner, status: completed, last_reply: 【东京 3 日行程】Day 1 浅草寺、晴空塔；Day 2 明治神宫、涩谷；Day 3 上野公园、秋叶原。}
      - {ref: T2, agent: product_copy, status: completed, last_reply: 【小红书文案】沙漠色高帮工装靴，防滑厚底，城市户外两相宜！}
    history:
      - {role: agent, thread: T1, text: 【东京 3 日行程】Day 1 浅草寺、晴空塔；Day 2 明治神宫、涩谷；Day 3 上野公园、秋叶原。}
      - {role: agent, thread: T2, text: 【小红书文案】沙漠色高帮工装靴，防滑厚底，城市户外两相宜！}
    input: 再改得简洁一点
    expect: {intent: clarify}
```

`src/cic_mcp_lab/cic/intent_eval.py`：

```python
"""cic-intent-eval: measure intent recognition on data/intent_cases.yaml (design §6.3, §6.7)."""
from __future__ import annotations

import argparse
import asyncio
import math
import sys
import time
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, get_args

import yaml

from ..config import PROJECT_ROOT, load_settings
from ..llm import make_openai_client
from .intent import Intent, IntentDecision, LlmIntentRecognizer
from .registry import AgentRecord, load_registry
from .router import Recognizer
from .session import Candidate, PendingCandidates, PendingRetry, Session, Thread, Turn

DEFAULT_CASES_PATH = PROJECT_ROOT / "data" / "intent_cases.yaml"
INTENTS = get_args(Intent)


@dataclass(frozen=True)
class Expectation:
    intent: str
    thread_ref: str | None = None
    candidate_index: int | None = None


@dataclass(frozen=True)
class IntentCase:
    name: str
    text: str
    expect: Expectation
    session: Session
    image_count: int = 0


@dataclass(frozen=True)
class IntentResult:
    case: IntentCase
    decision: IntentDecision | None
    error: str | None
    latency_ms: float

    @property
    def correct(self) -> bool:
        decision, expect = self.decision, self.case.expect
        if decision is None or decision.intent != expect.intent:
            return False
        if expect.thread_ref is not None and decision.thread_ref != expect.thread_ref:
            return False
        return expect.candidate_index is None or decision.candidate_index == expect.candidate_index


def _session(raw: Mapping[str, Any], registry: Mapping[str, AgentRecord], where: str) -> Session:
    session = Session()
    for item in raw.get("threads") or []:
        record = registry.get(item["agent"])
        if record is None:
            raise ValueError(f"{where}: unknown agent {item['agent']!r}")
        ref = item["ref"]
        session.threads[ref] = Thread(
            ref,
            record.agent_id,
            record.display_name,
            record.provider_id,
            conversation_id=f"conv_{ref.lower()}",
            status=item["status"],
            last_reply=item.get("last_reply", ""),
        )
    for turn in raw.get("history") or []:
        ref = turn.get("thread")
        label = f"{session.threads[ref].display_name} · {ref}" if turn["role"] == "agent" else None
        session.history.append(Turn(turn["role"], turn["text"], ref, label))
    pending = raw.get("pending")
    if pending and "candidates" in pending:
        session.pending = PendingCandidates(
            pending["message"],
            (),
            tuple(
                Candidate(a, registry[a].display_name, registry[a].provider_id, 0.0, "画像")
                for a in pending["candidates"]
            ),
        )
    elif pending and "retry" in pending:
        session.pending = PendingRetry(pending["retry"], pending["message"], ())
    return session


def load_intent_cases(
    path: Path = DEFAULT_CASES_PATH, registry: Mapping[str, AgentRecord] | None = None
) -> list[IntentCase]:
    registry = registry if registry is not None else load_registry()
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    cases: list[IntentCase] = []
    for index, raw in enumerate(data.get("cases") or []):
        where = f"cases[{index}]"
        expect = Expectation(**raw["expect"])
        if expect.intent not in INTENTS:
            raise ValueError(f"{where}: unknown intent {expect.intent!r}")
        cases.append(
            IntentCase(
                str(raw.get("name") or where),
                str(raw["input"]),
                expect,
                _session(raw, registry, where),
                int(raw.get("image_count", 0)),
            )
        )
    return cases


async def run_intent_eval(cases: Sequence[IntentCase], recognizer: Recognizer) -> list[IntentResult]:
    results: list[IntentResult] = []
    for case in cases:
        started = time.perf_counter()
        try:
            decision, error = await recognizer.recognize(case.session, case.text, case.image_count), None
        except Exception as exc:  # a failed call counts as a wrong answer
            decision, error = None, f"{type(exc).__name__}: {exc}"
        results.append(IntentResult(case, decision, error, (time.perf_counter() - started) * 1000))
    return results


def _pct(part: int, whole: int) -> str:
    return f"{part / whole * 100:.1f}%" if whole else "—"


def _expected(expect: Expectation) -> str:
    target = expect.thread_ref or (f"[{expect.candidate_index}]" if expect.candidate_index else "")
    return f"{expect.intent} {target}".strip()


def _actual(result: IntentResult) -> str:
    if result.decision is None:
        return f"（错误）{result.error}"
    decision = result.decision
    target = decision.thread_ref or (f"[{decision.candidate_index}]" if decision.candidate_index else "")
    return f"{decision.intent} {target}".strip()


def render_intent_report(results: Sequence[IntentResult], *, header_lines: Sequence[str]) -> str:
    correct = sum(result.correct for result in results)
    latencies = sorted(result.latency_ms for result in results)
    mean = sum(latencies) / len(latencies) if latencies else 0.0
    p95 = latencies[math.ceil(0.95 * len(latencies)) - 1] if latencies else 0.0
    lines = ["# 意图识别评测报告（Step 6）", "", *(f"- {line}" for line in header_lines), "", "## 总体", ""]
    lines += [
        "| 用例数 | 正确 | 准确率 | 延迟（平均 / P95） |",
        "| --- | --- | --- | --- |",
        f"| {len(results)} | {correct} | {_pct(correct, len(results))} | {mean:.0f} / {p95:.0f} ms |",
        "",
        "## 按期望意图",
        "",
        "| 期望意图 | 用例数 | 正确 | 准确率 |",
        "| --- | --- | --- | --- |",
    ]
    totals = Counter(result.case.expect.intent for result in results)
    hits = Counter(result.case.expect.intent for result in results if result.correct)
    for intent in INTENTS:
        if totals[intent]:
            lines.append(f"| {intent} | {totals[intent]} | {hits[intent]} | {_pct(hits[intent], totals[intent])} |")
    wrong = [result for result in results if not result.correct]
    lines += ["", f"## 错例（{len(wrong)} 条）", ""]
    if wrong:
        lines += ["| 用例 | 输入 | 期望 | 实际 | 模型理由 |", "| --- | --- | --- | --- | --- |"]
        for result in wrong:
            reason = result.decision.reason if result.decision else ""
            lines.append(
                f"| {result.case.name} | {result.case.text} | {_expected(result.case.expect)} | {_actual(result)} | {reason} |"
            )
    else:
        lines.append("无")
    rewrites = [r for r in results if r.decision is not None and r.decision.intent == "recommend"]
    lines += ["", "## recommend 的检索改写（search_query）", ""]
    if rewrites:
        lines += ["| 输入 | search_query |", "| --- | --- |"]
        lines += [f"| {r.case.text} | {r.decision.search_query or '（空）'} |" for r in rewrites]
    else:
        lines.append("无")
    return "\n".join(lines) + "\n"


async def _evaluate(output: Path | None) -> Path:
    settings = load_settings()
    cases = load_intent_cases()
    recognizer = LlmIntentRecognizer(make_openai_client(settings), settings.router_model)
    results = await run_intent_eval(cases, recognizer)
    header = [
        f"时间：{datetime.now():%Y-%m-%d %H:%M}",
        f"模型：{settings.router_model}（structured output，enable_thinking=false，temperature=0）",
        f"用例：{len(cases)} 条（data/intent_cases.yaml，由 §6.7 的 19 个验收场景加变体组成）",
        "判定：intent 相同，且期望中给出的 thread_ref / candidate_index 也相同",
    ]
    report = render_intent_report(results, header_lines=header)
    path = output or PROJECT_ROOT / "reports" / f"intent-eval-{datetime.now():%Y%m%d-%H%M}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(report, encoding="utf-8")
    print(report.split("## 错例")[0].rstrip())
    return path


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description="意图识别评测：基于 data/intent_cases.yaml 统计准确率")
    parser.add_argument("--output", type=Path, help="报告路径，默认 reports/intent-eval-<时间>.md")
    path = asyncio.run(_evaluate(parser.parse_args().output))
    print(f"\n完整报告：{path}")
```

> `_session` 中 Agent 回复的 label 依赖 `threads` 先于 `history` 构建。`threads` 里没有的 ref 会抛 `KeyError`，这是有意为之，用来暴露写错的测试数据。

**Step 4: 配套修改**
- `pyproject.toml`：`[project.scripts]` 末尾追加 `cic-intent-eval = "cic_mcp_lab.cic.intent_eval:main"`。
- `README.md`：在 `cic-chat` 一行之后插入：

```markdown
| `uv run cic-intent-eval` | 意图识别评测：`data/intent_cases.yaml`（34 条，覆盖 5 种意图）→ 准确率、按意图准确率、错例、检索改写，报告写入 `reports/` |
```

**Step 5: 验证**
- `uv run pytest tests/test_cic_intent_eval.py -q` → `2 passed`；`uv run pytest -q` → `182 passed`
- `uv run cic-intent-eval --help` → 打印帮助（不要真正执行）

**Step 6: Commit**

```powershell
git add data/intent_cases.yaml src/cic_mcp_lab/cic/intent_eval.py tests/test_cic_intent_eval.py pyproject.toml README.md
git commit -m "feat: cic-intent-eval measures intent recognition on 34 scenario cases" -m "Co-authored-by: Copilot <223556219+Copilot@users.noreply.github.com>"
```

---

### Task 7: 推荐评测补跑 V4（查询改写）

**Files:**
- Modify: `src/cic_mcp_lab/cic/eval_metrics.py`（`Scored` 增加 `rewritten`）、`src/cic_mcp_lab/cic/eval.py`
- Test: `tests/test_cic_eval.py`（重构出公共的 store 准备函数，并追加 1 个测试）

**要点（决策 29）：**
- V4 = 意图识别 + V3，模拟真实 Router 的处理：
  1. 用该问题的 `history`（去掉「用户：」前缀后作为用户发言）构造会话，调用意图识别；
  2. intent 不是 `recommend` 时视为 Router 不推荐：排序为空，最高分为 0，等同被拦截；
  3. 是 `recommend` 时，用 `search_query`（为空时用原句）做 Embedding，再按 V3（Rerank）排序。
- V4 的耗时包含意图识别。
- `Scored.rewritten` 记录改写后的检索语句，或「（intent，不推荐）」。
- 报告：V4 存在时追加一节「V4 查询改写」，列出每条问题的改写结果和期望 Agent 的排名；说明中注明 V4 的拦截包含意图层面的拦截。
- `cic-eval` 默认跑 V1～V4。V4 需要 `LlmIntentRecognizer` 和 reranker。只跑 V4 时，不做 V1～V3 共用的那次原句 Embedding。

**Step 1: 写失败的测试** — 在 `tests/test_cic_eval.py` 中：
- 把现有测试里建 store 和 registry 的部分抽成函数 `def seeded():`，返回 `(store, registry)`，现有测试改为调用它（行为不变）；
- 追加：

```python
class RewritingRecognizer:
    async def recognize(self, session, text, image_count=0):
        if text == "讲个笑话":
            return IntentDecision(intent="router_answer", thread_ref=None, candidate_index=None, search_query=None, reason="闲聊")
        assert [turn.text for turn in session.history] == ["我想出去走走"]  # history without the 用户： prefix
        return IntentDecision(intent="recommend", thread_ref=None, candidate_index=None, search_query="想去旅行", reason="新任务")


async def test_v4_searches_with_the_rewritten_query_and_skips_non_recommend_intents():
    store, registry = seeded()
    items = [EvalQuery("帮我安排一下", "trip", "context", history=("用户：我想出去走走",)), EvalQuery("讲个笑话", None, "none")]
    embedder = LookupEmbedder()

    recommenders = build_recommenders(embedder, store, lambda: registry, NameReranker(), ["V4"])
    results = await run_eval(items, recommenders, embedder, recognizer=RewritingRecognizer())
    store.close()

    first, second = results["V4"]
    assert (first.rewritten, first.rank_of_expected()) == ("想去旅行", 1)
    assert (second.ranking, second.rewritten) == ((), "（router_answer，不推荐）")
    assert embedder.calls == 1  # only the rewritten query; V1-V3's shared embedding is skipped
    assert "## V4 查询改写" in render_report(results, names={"trip": "旅行规划"}, header_lines=["测试"])
```

（文件顶部补充 `from cic_mcp_lab.cic.intent import IntentDecision`；`VECTORS` 中已有 `"想去旅行"`。）

**Step 2:** `uv run pytest tests/test_cic_eval.py -q` → 新测试失败

**Step 3: 实现**

`eval_metrics.py`：`Scored` 末尾追加字段 `rewritten: str | None = None  # V4: the search query after rewriting, or why nothing was recommended`。

`eval.py`：
- `VARIANTS = ("V1", "V2", "V3", "V4")`；`DESCRIPTIONS["V4"] = "V3 + 查询改写（意图识别给出的 search_query）"`。
- `build_recommenders`：`options["V4"] = {"reranker": reranker}`；V3 或 V4 在列表中却没有 reranker 时抛 `ValueError("V3/V4 need a reranker")`。
- `run_eval(items, recommenders, embedder, recognizer=None)`：

```python
async def run_eval(
    items: Sequence[EvalQuery],
    recommenders: Mapping[str, Recommender],
    embedder: Embedder,
    recognizer: Recognizer | None = None,
) -> dict[str, list[Scored]]:
    """Rank every query with every variant. V1-V3 share one embedding of the raw query; V4 rewrites it first."""
    if "V4" in recommenders and recognizer is None:
        raise ValueError("V4 needs an intent recognizer")
    results: dict[str, list[Scored]] = {name: [] for name in recommenders}
    shared = [name for name in recommenders if name != "V4"]
    for item in items:
        if shared:
            started = time.perf_counter()
            [vector] = await embedder.embed([item.query])
            embed_ms = (time.perf_counter() - started) * 1000
            for name in shared:
                started = time.perf_counter()
                ranking = await recommenders[name].rank(
                    item.query, vector=vector, with_image=item.image, include_mock=True
                )
                elapsed_ms = embed_ms + (time.perf_counter() - started) * 1000
                results[name].append(Scored(item, tuple((p.agent_id, p.score) for p in ranking), elapsed_ms))
        if "V4" in recommenders:
            results["V4"].append(await _rewrite_and_rank(item, recommenders["V4"], embedder, recognizer))
    return results


async def _rewrite_and_rank(
    item: EvalQuery, recommender: Recommender, embedder: Embedder, recognizer: Recognizer
) -> Scored:
    """V4: what the Router would do — recognise the intent, then search with the rewritten query."""
    started = time.perf_counter()
    session = Session(history=[Turn("user", turn.removeprefix("用户：")) for turn in item.history])
    decision = await recognizer.recognize(session, item.query, 1 if item.image else 0)
    if decision.intent != "recommend":
        return Scored(item, (), (time.perf_counter() - started) * 1000, f"（{decision.intent}，不推荐）")
    query = decision.search_query or item.query
    [vector] = await embedder.embed([query])
    ranking = await recommender.rank(query, vector=vector, with_image=item.image, include_mock=True)
    elapsed_ms = (time.perf_counter() - started) * 1000
    return Scored(item, tuple((p.agent_id, p.score) for p in ranking), elapsed_ms, query)
```

（import 补充 `from .router import Recognizer`、`from .session import Session, Turn`、`from .intent import LlmIntentRecognizer`、`from ..llm import make_openai_client`。）

- `render_report`：
  - 在「总体指标」的说明中追加一行：`"> - V4 = 意图识别 + V3：意图不是 recommend 的问题视为 Router 不推荐（最高分 0，等同被拦截）；耗时包含意图识别。"`；
  - 在「逐条结果」之前，若 `"V4" in results`，追加：

```python
    if "V4" in results:
        lines += ["## V4 查询改写", "", "| 问题 | 类别 | 改写后的检索语句 | 期望（排名） |", "| --- | --- | --- | --- |"]
        for result in results["V4"]:
            rank = result.rank_of_expected() if result.item.expect else None
            expected = "—" if result.item.expect is None else f"{name_of(result.item.expect)}（{rank or '未推荐'}）"
            lines.append(
                f"| {result.item.query} | {CATEGORY_LABELS[result.item.category]} | {result.rewritten} | {expected} |"
            )
        lines.append("")
```

- `_evaluate`：V4 在列表中时构造 `LlmIntentRecognizer(make_openai_client(settings), settings.router_model)`，传给 `run_eval`；`make_reranker` 的条件改为 `"V3" in variants or "V4" in variants`；header 中「依赖上下文类……」一句改为 `"依赖上下文类：V1～V3 只用最后一句检索；V4 由意图识别结合前几轮对话改写"`。
- `main()`：`--variants` 默认改为 `V1,V2,V3,V4`，help 同步修改。

**Step 4:** `uv run pytest tests/test_cic_eval.py -q` → `2 passed`；`uv run pytest -q` → `183 passed`

**Step 5: Commit**

```powershell
git add src/cic_mcp_lab/cic/eval_metrics.py src/cic_mcp_lab/cic/eval.py tests/test_cic_eval.py
git commit -m "feat: V4 evaluation rewrites queries through intent recognition before rerank" -m "Co-authored-by: Copilot <223556219+Copilot@users.noreply.github.com>"
```

---

### Task 8: 手工验收（由用户执行）

**准备：**
- 新增了依赖和命令：先停掉所有 provider-server，执行 `uv sync`。
- 确认 `.env` 中已设置 `RERANK_ENABLED=true`、`RECOMMEND_MIN_SCORE_RERANK=0.48`、`RECOMMEND_MIN_SCORE_DENSE=0.45`；15 个 Agent 均为 published，并已入驻。
- 终端 1：`uv run provider-server --provider A --port 8001`；终端 2：`uv run provider-server --provider B --port 8002 --no-push`。

**A. 两项评测（不需要 Provider，但要先关掉 cic-chat，因为 Qdrant 只允许一个进程打开）：**

| # | 命令 | 产出 |
| --- | --- | --- |
| A1 | `uv run cic-intent-eval` | 34 条意图用例的准确率、按意图准确率、错例；报告在 `reports/intent-eval-*.md` |
| A2 | `uv run cic-eval` | V1～V4 推荐指标对比，包括 V4 改写表；报告在 `reports/step5-eval-*.md` |

**B. `cic-chat` 验收（设计文档 §6.7 的 19 个场景）。** 终端 3 执行 `uv run cic-chat`，按顺序输入：

| # | 输入 / 操作 | 期望 |
| --- | --- | --- |
| 1 | `你好` | `[intent] router_answer`，Router 直接回复 |
| 2 | `帮我规划去东京的旅行` | `recommend` → 展示候选，旅行规划排第 1 |
| 3 | `用第一个` | `select_candidate → [1]` → 提交 trip_planner，新建 T1；随后异步打印 Agent 的反问（等待补充）和 credit |
| 4 | `12 月 5 号出发，预算 1 万` | `supplement → T1`，沿用 conversation_id；Agent 能接上上下文 |
| 5 | （T1 等待补充时）`今天星期几？` | `router_answer`；`/threads` 中 T1 状态不变 |
| 6 | `另外帮我写本周周报` → 选择周报撰写 | `recommend` → 选择后新建 T2（Provider B，经轮询拿到结果，对应场景 19） |
| 7 | （T1、T2 均等待补充）`预算改成 2 万` | `supplement → T1` |
| 8 | （T1、T2 均等待补充）`好的，就这样` | `clarify`，Router 询问是哪个会话 |
| 9 | （T1 已完成后）`第二天的行程换成购物` | `supplement → T1` |
| 10 | `再帮我规划一次大阪旅行` → 选择 | `recommend` → 新建 T3 |
| 11 | `/image <本地商品图>`，然后输入 `帮这个商品写小红书文案` → 选择 | 只推荐支持图片的 Agent；选择后带 image_urls 调用 |
| 12 | `帮我订一张明天去上海的机票` | `recommend` → 没有 Agent 超过阈值 →「暂无合适的 Agent，我先尽力回答」 |
| 13 | （有推荐候选时）`算了，讲个笑话吧` | `router_answer`，待选项被清空 |
| 14a | （T2 等待补充）停掉终端 2（B），对 T2 说 `周报补充：这周还完成了接口联调` | `supplement → T2` → 提交失败（ConnectError），不计费 → 提示 [1] 重试 / [2] 重新推荐其他 Agent；再输入 `换一个` → 重新推荐，候选中不再有周报撰写。注：模拟 Provider 只在内存中保存会话，B 重启后 T2 的 conversation_id 失效，此时对 T2 重试会得到 `agent_error: Conversation not found`。这是 Provider 侧的行为，Router 同样按失败处理、不计费 |
| 14b | B 仍停止，`/new` 后输入 `帮我写本周周报：本周完成了接口联调` → 选择周报撰写 | 提交失败 → 提示两个选项；启动 B 后输入 `重试` → 重新提交成功（这是新会话，此前没有 conversation_id），结果经轮询到达并计费 |
| 15 | （T1 已完成）`把上面的行程翻译成英文` | `router_answer`：Router 直接翻译，不调用 Agent |
| 16 | （候选为旅行规划等时）`我要用周报撰写那个` | 不直接调用，按新需求重新推荐 |
| 17 | 终端 1 改为 `--delay 90` 重启 A；`/new` 后走一遍 2→3，在 T1 处理中时输入 `预算改成 2 万` | 判定为 `supplement → T1`，但不提交，提示「旅行规划（T1）正在处理上一条消息，请稍候」 |
| 18 | （同上，T1 仍在处理中）`今天星期几？` | 立即回答；约 90 秒后 T1 的结果经推送到达并打印，`/usage` 显示只结算一次 |
| 19 | （B 以 `--no-push` 启动，场景 6 已覆盖） | 周报结果经轮询到达，并计费 |

最后执行 `/usage` 和 `/threads`，查看汇总。

请把 A1、A2 的终端输出（或报告），以及 B 的完整对话记录整理到 `step6_test_result.md`。我会逐条核对，然后把意图识别准确率、V4 结论写入设计文档 §7 和 §6。

