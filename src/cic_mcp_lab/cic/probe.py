"""Test connection for an onboarded agent (design §4.2), the prototype of the Provider Portal check."""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import re
import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import anyio

from ..config import load_settings
from ..mcpwire import PROTOCOL_VERSION, WireClient
from .contract import ContractViolation, EntryTool, validate_entry_tool
from .mcp_client import ProviderClient, describe_error
from .registry import AgentRecord, RegistryError, api_key_for, load_registry

STEPS = ("discover", "auth", "tools", "entry_tool")
TOOL_NAME = re.compile(r"[A-Za-z0-9_.-]{1,128}")


@dataclass(frozen=True)
class ProbeStep:
    name: str
    ok: bool
    detail: str


@dataclass(frozen=True)
class ProbeReport:
    agent_id: str
    steps: tuple[ProbeStep, ...]  # stops at the first failing step
    supports_tasks: bool | None
    entry_tool: EntryTool | None
    fingerprint: str
    tested_at: str

    @property
    def ok(self) -> bool:
        return len(self.steps) == len(STEPS) and all(step.ok for step in self.steps)


def connection_fingerprint(url: str, api_key: str, entry_tool: str) -> str:
    """What a passing test vouches for; changing the URL, key or entry tool invalidates it."""
    key_digest = hashlib.sha256(api_key.encode()).hexdigest()
    return hashlib.sha256(json.dumps([url, key_digest, entry_tool]).encode()).hexdigest()


def _yes(flag: bool) -> str:
    return "yes" if flag else "no"


def _anonymous_status(url: str) -> int:
    with WireClient(url, None) as wire:
        return wire.request("tools/list").status_code


def _tool_problem(tool: dict[str, Any]) -> str | None:
    name = tool.get("name")
    if not isinstance(name, str) or not TOOL_NAME.fullmatch(name):
        return f"invalid tool name {name!r}"
    schema = tool.get("inputSchema")
    if not isinstance(schema, dict) or schema.get("type") != "object":
        return f"{name}: inputSchema must be an object schema"
    return None


async def probe(record: AgentRecord, api_key: str) -> ProbeReport:
    tested_at = datetime.now(UTC).isoformat(timespec="seconds")
    steps: list[ProbeStep] = []
    supports_tasks: bool | None = None
    entry: EntryTool | None = None

    async def run() -> None:
        nonlocal supports_tasks, entry
        async with ProviderClient(record.mcp_url, api_key) as client:
            has_tools = client.server_capabilities.tools is not None
            supports_tasks = client.supports_tasks
            detail = f"protocol {client.protocol_version}; tools: {_yes(has_tools)}; tasks extension: {_yes(supports_tasks)}"
            steps.append(ProbeStep("discover", client.protocol_version == PROTOCOL_VERSION and has_tools, detail))
            if not steps[-1].ok:
                return

            status = await anyio.to_thread.run_sync(_anonymous_status, record.mcp_url)
            detail = f"anonymous tools/list -> HTTP {status}"
            steps.append(ProbeStep("auth", status == 401, detail if status == 401 else f"{detail}; MCP Server 未启用鉴权"))
            if not steps[-1].ok:
                return

            tools = await client.list_tools()
            problems = [problem for tool in tools if (problem := _tool_problem(tool))]
            if not tools:
                problems.append("the server lists no tools")
            names = ", ".join(sorted(str(tool.get("name")) for tool in tools))
            steps.append(ProbeStep("tools", not problems, "; ".join(problems) or f"{len(tools)} tools: {names}"))
            if problems:
                return

            tool = next((tool for tool in tools if tool.get("name") == record.entry_tool), None)
            if tool is None:
                steps.append(ProbeStep("entry_tool", False, f"entry tool {record.entry_tool!r} is not listed"))
                return
            try:
                entry = validate_entry_tool(tool)
            except ContractViolation as exc:
                steps.append(ProbeStep("entry_tool", False, str(exc)))
                return
            steps.append(ProbeStep("entry_tool", True, f"{entry.name}; images: {_yes(entry.supports_image)}"))

    try:
        await run()
    except Exception as exc:
        if len(steps) < len(STEPS):  # the step that was running when it failed
            steps.append(ProbeStep(STEPS[len(steps)], False, describe_error(exc)))
    return ProbeReport(
        record.agent_id,
        tuple(steps),
        supports_tasks,
        entry,
        connection_fingerprint(record.mcp_url, api_key, record.entry_tool),
        tested_at,
    )


def _print_report(record: AgentRecord, report: ProbeReport) -> None:
    print(f"[{record.agent_id}] {record.mcp_url} · 入口 Tool {record.entry_tool}")
    for step in report.steps:
        print(f"  {'✓' if step.ok else '✗'} {step.name:<10} {step.detail}")
    verdict = "通过" if report.ok else "未通过，阻断提交"
    print(f"  => {verdict} · tested_at={report.tested_at} · fingerprint={report.fingerprint[:16]}…")


async def _probe_all(registry: dict[str, AgentRecord], agent_ids: list[str]) -> bool:
    all_ok = True
    for agent_id in agent_ids:
        record = registry[agent_id]
        try:
            api_key = api_key_for(record)
        except RegistryError as exc:
            print(f"[{agent_id}] ✗ {exc}")
            all_ok = False
            continue
        report = await probe(record, api_key)
        _print_report(record, report)
        all_ok = all_ok and report.ok
    return all_ok


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description="对已入驻的 Agent 执行测试连接（设计文档 §4.2）")
    target = parser.add_mutually_exclusive_group(required=True)
    target.add_argument("agent_id", nargs="?", help="data/agents.yaml 中的 agent_id")
    target.add_argument("--all", action="store_true", help="测试全部已入驻的 Agent")
    args = parser.parse_args()
    load_settings()  # loads .env into the process environment
    registry = load_registry()
    agent_ids = sorted(registry) if args.all else [args.agent_id]
    unknown = [agent_id for agent_id in agent_ids if agent_id not in registry]
    if unknown:
        raise SystemExit(f"Unknown agent: {', '.join(unknown)}")
    sys.exit(0 if asyncio.run(_probe_all(registry, agent_ids)) else 1)
