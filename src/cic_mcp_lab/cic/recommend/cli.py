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
from .rerank import make_reranker
from .vector_store import QdrantVectorStore


async def _recommend(args: argparse.Namespace) -> None:
    settings = load_settings()
    use_rerank = args.rerank or settings.rerank_enabled
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
            min_score=settings.recommend_min_score_rerank if use_rerank else settings.recommend_min_score_dense,
            reranker=make_reranker(settings) if use_rerank else None,
        )
        started = time.monotonic()
        picks = await recommender.recommend(args.query, with_image=args.image, include_mock=args.include_mock)
        elapsed = time.monotonic() - started
    finally:
        store.close()

    if not picks:
        print(f"暂无合适的 Agent（{elapsed:.2f}s）")
        return
    print(f"为你找到以下 Agent（按匹配度排序{'，Rerank' if use_rerank else ''}，{elapsed:.2f}s）：")
    for index, pick in enumerate(picks, 1):
        name = pick.display_name if pick.callable else f"{pick.display_name}（模拟）"
        basis = "画像" if pick.matched_kind == "profile" else f'示例问题 "{pick.matched_text}"'
        if pick.rerank_score is not None:
            match = f"匹配度 {pick.score:.2f}（向量 {pick.dense_score:.2f}）"
        else:
            match = f"匹配度 {pick.score:.2f}"
        print(f"  [{index}] {name} · {pick.provider_id}   {match}   依据：{basis}")


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description="按用户问题推荐已入驻的 Agent（向量召回，设计文档 §6.4）")
    parser.add_argument("query", help="用户的问题")
    parser.add_argument("--image", action="store_true", help="模拟用户附带了图片：只推荐支持图片输入的 Agent")
    parser.add_argument("--include-mock", action="store_true", help="同时推荐只有画像、不接 MCP 的模拟 Agent")
    parser.add_argument("--top-k", type=int, help="覆盖 RECOMMEND_TOP_K")
    parser.add_argument("--rerank", action="store_true", help="用 Rerank 精排（也可设置 RERANK_ENABLED=true）")
    asyncio.run(_recommend(parser.parse_args()))
