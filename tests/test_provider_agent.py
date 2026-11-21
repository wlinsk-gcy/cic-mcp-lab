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
