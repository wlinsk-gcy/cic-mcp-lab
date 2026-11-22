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
