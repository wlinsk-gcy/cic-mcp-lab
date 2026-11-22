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
