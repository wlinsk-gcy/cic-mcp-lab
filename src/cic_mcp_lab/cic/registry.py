"""Agents CIC has onboarded (data/agents.yaml): profile, MCP endpoint and listing status.

The lab reads a YAML file; production keeps the registry in a database (design §13.10).
"""
from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

import yaml

from ..config import PROJECT_ROOT

DEFAULT_REGISTRY_PATH = PROJECT_ROOT / "data" / "agents.yaml"
STATUSES = ("published", "delisted")
MIN_EXAMPLES, MAX_EXAMPLES = 3, 10
_REQUIRED = ("agent_id", "provider_id", "display_name", "description", "category", "tags", "examples", "price_per_credit")
_ENDPOINT = ("mcp_url", "api_key_env", "entry_tool")

Status = Literal["published", "delisted"]


class RegistryError(ValueError):
    pass


@dataclass(frozen=True)
class AgentRecord:
    agent_id: str
    provider_id: str
    display_name: str
    description: str
    mcp_url: str | None  # None for mock agents, which exist only for recommendation tests
    api_key_env: str | None  # env var holding the provider API key; keys never live in the registry file
    entry_tool: str | None
    price_per_credit: float  # HKD per Credit
    category: tuple[str, ...] = ()
    tags: tuple[str, ...] = ()
    examples: tuple[str, ...] = ()  # how users would ask; each one is embedded for recall (design §4.5)
    status: Status = "published"
    supports_image: bool = False  # read for mock agents only; a callable agent's entry tool schema decides

    @property
    def callable(self) -> bool:
        return self.mcp_url is not None

    @property
    def published(self) -> bool:
        return self.status == "published"


def _strings(value: Any, name: str, where: str) -> tuple[str, ...]:
    if not isinstance(value, list) or not all(isinstance(item, str) and item.strip() for item in value):
        raise RegistryError(f"{where}: {name} must be a list of non-empty strings")
    return tuple(item.strip() for item in value)


def _record(raw: dict[str, Any], where: str) -> AgentRecord:
    missing = [name for name in _REQUIRED if name not in raw]
    if missing:
        raise RegistryError(f"{where} is missing: {', '.join(missing)}")
    given = [name for name in _ENDPOINT if raw.get(name)]
    if given and len(given) != len(_ENDPOINT):
        raise RegistryError(f"{where}: {', '.join(_ENDPOINT)} must be given together")
    examples = _strings(raw["examples"], "examples", where)
    if not MIN_EXAMPLES <= len(examples) <= MAX_EXAMPLES:
        raise RegistryError(f"{where}: examples must have {MIN_EXAMPLES}-{MAX_EXAMPLES} items, got {len(examples)}")
    status = raw.get("status", "published")
    if status not in STATUSES:
        raise RegistryError(f"{where}: status must be one of {STATUSES}, got {status!r}")
    return AgentRecord(
        agent_id=str(raw["agent_id"]),
        provider_id=str(raw["provider_id"]),
        display_name=str(raw["display_name"]),
        description=str(raw["description"]),
        mcp_url=raw.get("mcp_url") or None,
        api_key_env=raw.get("api_key_env") or None,
        entry_tool=raw.get("entry_tool") or None,
        price_per_credit=float(raw["price_per_credit"]),
        category=_strings(raw["category"], "category", where),
        tags=_strings(raw["tags"], "tags", where),
        examples=examples,
        status=status,
        supports_image=bool(raw.get("supports_image", False)),
    )


def load_registry(path: Path = DEFAULT_REGISTRY_PATH) -> dict[str, AgentRecord]:
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    records: dict[str, AgentRecord] = {}
    endpoints: set[tuple[str, str]] = set()
    for index, raw in enumerate(data.get("agents") or []):
        record = _record(raw, f"agents[{index}]")
        if record.agent_id in records:
            raise RegistryError(f"Duplicate agent_id {record.agent_id!r}")
        if record.callable:
            endpoint = (record.mcp_url, record.entry_tool)
            if endpoint in endpoints:
                raise RegistryError(
                    f"{record.agent_id}: tool {record.entry_tool!r} at {record.mcp_url} is already registered"
                )
            endpoints.add(endpoint)
        records[record.agent_id] = record
    return records


def api_key_for(record: AgentRecord, env: Mapping[str, str] = os.environ) -> str:
    if record.api_key_env is None:
        raise RegistryError(f"{record.agent_id} is not callable: it has no MCP endpoint")
    key = env.get(record.api_key_env, "").strip()
    if not key:
        raise RegistryError(f"{record.agent_id}: environment variable {record.api_key_env} is not set")
    return key
