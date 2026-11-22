"""Agents CIC has onboarded (data/agents.yaml): where each agent's MCP server is and how to call it."""
from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass, fields
from pathlib import Path

import yaml

from ..config import PROJECT_ROOT

DEFAULT_REGISTRY_PATH = PROJECT_ROOT / "data" / "agents.yaml"


class RegistryError(ValueError):
    pass


@dataclass(frozen=True)
class AgentRecord:
    agent_id: str
    provider_id: str
    display_name: str
    description: str
    mcp_url: str
    api_key_env: str  # env var holding the provider API key; keys never live in the registry file
    entry_tool: str
    price_per_credit: float  # HKD per Credit


_FIELDS = tuple(field.name for field in fields(AgentRecord))


def load_registry(path: Path = DEFAULT_REGISTRY_PATH) -> dict[str, AgentRecord]:
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    records: dict[str, AgentRecord] = {}
    endpoints: set[tuple[str, str]] = set()
    for index, raw in enumerate(data.get("agents") or []):
        missing = [name for name in _FIELDS if name not in raw]
        if missing:
            raise RegistryError(f"agents[{index}] is missing: {', '.join(missing)}")
        values = {name: raw[name] for name in _FIELDS}
        values["price_per_credit"] = float(values["price_per_credit"])
        record = AgentRecord(**values)
        if record.agent_id in records:
            raise RegistryError(f"Duplicate agent_id {record.agent_id!r}")
        endpoint = (record.mcp_url, record.entry_tool)
        if endpoint in endpoints:
            raise RegistryError(f"{record.agent_id}: tool {record.entry_tool!r} at {record.mcp_url} is already registered")
        endpoints.add(endpoint)
        records[record.agent_id] = record
    return records


def api_key_for(record: AgentRecord, env: Mapping[str, str] = os.environ) -> str:
    key = env.get(record.api_key_env, "").strip()
    if not key:
        raise RegistryError(f"{record.agent_id}: environment variable {record.api_key_env} is not set")
    return key
