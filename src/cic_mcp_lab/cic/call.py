"""cic-call: send one turn to an onboarded agent over MCP and wait for its outcome (step 3 checkpoint)."""
from __future__ import annotations

import argparse
import asyncio
import os
import sys
import time
from pathlib import Path

from ..config import Settings, load_settings
from .ledger import Ledger
from .oss import OssUploader
from .registry import load_registry
from .task_watcher import SubmitFailed, TaskWatcher

DELIVERY_LABELS = {"direct": "直接返回", "push": "推送", "recheck": "订阅后补查", "poll": "轮询"}


async def _image_urls(images: list[str], settings: Settings) -> list[str]:
    urls: list[str] = []
    uploader: OssUploader | None = None
    for image in images:
        if image.startswith(("http://", "https://")):
            urls.append(image)
            continue
        uploader = uploader or OssUploader(settings)
        url = await asyncio.to_thread(uploader.upload_file, Path(image))
        print(f"[image] 已上传 {image} -> {url}")
        urls.append(url)
    return urls


async def _call(args: argparse.Namespace) -> bool:
    settings = load_settings()
    registry = load_registry()
    if args.agent not in registry:
        print(f"[error] 未入驻的 Agent：{args.agent}（可选：{', '.join(sorted(registry))}）")
        return False
    try:
        image_urls = await _image_urls(args.image or [], settings)
    except Exception as exc:
        print(f"[error] 图片上传失败：{type(exc).__name__}: {exc}")
        return False

    ledger = Ledger()
    backstop = float(os.environ.get("TASK_BACKSTOP_POLL_SECONDS", "30"))
    started = time.monotonic()
    async with TaskWatcher(load_registry, ledger, backstop_poll_seconds=backstop) as watcher:
        try:
            submission = await watcher.submit(args.agent, args.message, image_urls, args.conversation_id)
        except SubmitFailed as exc:
            print(f"[submit] 提交失败（不计费）：{exc}")
            return False
        task = submission.task_id or "无（Provider 不走任务，等待直接返回）"
        print(f"[submit] {time.monotonic() - started:.2f}s 被接受 · taskId={task}")
        for warning in submission.warnings:
            print(f"[warn] {warning}")
        outcome = await watcher.outcomes.get()

    for note in outcome.notes:
        print(f"[note] {note}")
    via = DELIVERY_LABELS.get(outcome.delivery or "")
    print(f"[result] {f'经{via}' if via else '未拿到最终状态'} · 从提交起 {outcome.elapsed_seconds:.1f}s")
    if not outcome.ok:
        print(f"[failed] {outcome.failure}: {outcome.detail}（不计费）")
        return False
    reply = outcome.reply
    print(f"[agent] conversation_id={reply.conversation_id} · status={reply.status}")
    print(reply.reply)
    entry = ledger.entries[0]
    print(
        f"[credit] credit_usage={reply.credit_usage} × {entry.price_per_credit} HKD/Credit = {entry.hkd} HKD"
        f" · 结算键={entry.settlement_key}"
    )
    if reply.status == "input_required":
        print(f"[next] Agent 需要更多信息；继续本会话请加 --conversation-id {reply.conversation_id}")
    return True


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8")  # agent replies may contain emoji; gbk pipes would crash
    parser = argparse.ArgumentParser(description="经 MCP 向已入驻的 Agent 发送一轮消息，并等待最终结果")
    parser.add_argument("--agent", required=True, help="data/agents.yaml 中的 agent_id")
    parser.add_argument("--message", required=True, help="用户本轮输入的原话")
    parser.add_argument("--conversation-id", help="继续会话时传入上一轮返回的 conversation_id")
    parser.add_argument("--image", action="append", help="本地图片路径（上传 OSS）或 https 地址，可重复")
    args = parser.parse_args()
    try:
        ok = asyncio.run(_call(args))
    except KeyboardInterrupt:
        ok = False
    sys.exit(0 if ok else 1)
