"""Local REPL that talks to one mock third-party agent directly (no MCP yet)."""
from __future__ import annotations

import argparse
import asyncio
from pathlib import Path

from ..cic.oss import OssUploader
from ..config import load_settings
from ..llm import make_chat_model
from .agent import ProviderAgentRuntime, make_agent_factory
from .catalog import CATALOG

HELP = "命令：/image <本地路径或URL> 附加图片；/new 开启新会话；/exit 退出"


async def _repl(agent_id: str) -> None:
    settings = load_settings()
    runtime = ProviderAgentRuntime(
        CATALOG,
        make_agent_factory(make_chat_model(settings, settings.provider_model)),
    )
    uploader = OssUploader(settings)
    spec = CATALOG[agent_id]
    conversation_id: str | None = None
    images: list[str] = []
    total_credit = 0
    print(f"已连接 {spec.display_name}（{agent_id}）。{HELP}")

    while True:
        line = (await asyncio.to_thread(input, "> ")).strip()
        if not line:
            continue
        if line == "/exit":
            return
        if line == "/new":
            conversation_id, images = None, []
            print("已开启新会话")
            continue
        if line.startswith("/image "):
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
            print(f"已附加图片：{url}")
            continue

        try:
            result = await runtime.ask(agent_id, line, image_urls=images, conversation_id=conversation_id)
        except Exception as exc:
            print(f"[error] {type(exc).__name__}: {exc}")
            continue
        conversation_id, images = result.conversation_id, []
        total_credit += result.credit_usage
        print(
            f"[{spec.display_name}] {result.conversation_id} · {result.status} · "
            f"credit_usage={result.credit_usage}（本会话累计 {total_credit}）"
        )
        print(result.reply)


def main() -> None:
    parser = argparse.ArgumentParser(description="直接与模拟第三方 Agent 对话（不经过 MCP）")
    parser.add_argument("--agent", choices=sorted(CATALOG), required=True)
    args = parser.parse_args()
    try:
        asyncio.run(_repl(args.agent))
    except (KeyboardInterrupt, EOFError):
        pass
