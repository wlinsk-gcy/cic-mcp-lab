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
