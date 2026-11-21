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
                "如果用户提供了商品图片，追问时先用一句话说明你从图片中识别出的商品，再提出问题。\n"
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
                "开始撰写前必须了解本周完成的主要工作，缺少时先追问；问题与风险、下周计划为可选，用户未提供时在对应部分写「无」，不要为此追问。\n"
                "信息齐全后，按「本周工作 / 问题与风险 / 下周计划」三部分输出周报。"
                + _REPLY_RULES
            ),
            supports_image=False,
        ),
    )
}
