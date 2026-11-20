"""Connectivity smoke test: chat model, embedding, OSS upload + public read."""
from __future__ import annotations

import asyncio
import struct
import sys
import urllib.request
import zlib

from agentscope.agent import Agent
from agentscope.message import UserMsg

from .cic.oss import OssUploader
from .config import Settings, load_settings
from .llm import make_chat_model, make_openai_client


def _solid_png(size: int = 64) -> bytes:
    """A tiny red PNG built without image libraries."""
    raw = (b"\x00" + b"\xff\x00\x00" * size) * size

    def chunk(tag: bytes, data: bytes) -> bytes:
        crc = zlib.crc32(tag + data) & 0xFFFFFFFF
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", crc)

    header = struct.pack(">IIBBBBB", size, size, 8, 2, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", header)
        + chunk(b"IDAT", zlib.compress(raw))
        + chunk(b"IEND", b"")
    )


async def _check_chat(settings: Settings) -> None:
    for model_name in sorted({settings.router_model, settings.provider_model}):
        agent = Agent(
            name="smoke",
            system_prompt="你是连通性测试助手，回答尽量简短。",
            model=make_chat_model(settings, model_name),
        )
        msg = await agent.reply(UserMsg(name="user", content="用一句话介绍你自己。"))
        print(f"[chat] {model_name}: {msg.get_text_content()} (usage: {msg.usage})")


async def _check_embedding(settings: Settings) -> None:
    client = make_openai_client(settings)
    resp = await client.embeddings.create(
        model=settings.embedding_model,
        input=["帮我规划东京旅行"],
        dimensions=settings.embedding_dim,
    )
    dim = len(resp.data[0].embedding)
    if dim != settings.embedding_dim:
        raise RuntimeError(f"expected dim {settings.embedding_dim}, got {dim}")
    print(f"[embedding] {settings.embedding_model}: dim={dim}")


async def _check_oss(settings: Settings) -> None:
    url = await asyncio.to_thread(OssUploader(settings).upload_bytes, _solid_png(), "smoke.png")
    with urllib.request.urlopen(url, timeout=10) as resp:
        print(f"[oss] public GET {resp.status} {resp.headers.get('Content-Type')} {url}")


async def _run() -> int:
    settings = load_settings()
    failures = 0
    for name, check in (("chat", _check_chat), ("embedding", _check_embedding), ("oss", _check_oss)):
        try:
            await check(settings)
        except Exception as exc:  # report every failing dependency, not just the first
            failures += 1
            print(f"[{name}] FAILED: {type(exc).__name__}: {exc}")
    return failures


def main() -> None:
    sys.exit(1 if asyncio.run(_run()) else 0)
