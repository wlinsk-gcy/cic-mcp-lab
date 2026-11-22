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
