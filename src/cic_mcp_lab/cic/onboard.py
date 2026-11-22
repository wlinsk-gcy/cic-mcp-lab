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
from .recommend.profile import profile_text
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
