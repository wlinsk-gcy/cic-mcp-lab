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
