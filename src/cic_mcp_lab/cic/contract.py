"""CIC entry-tool contract (design §4.3, §5): checks for tool schemas and agent results."""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Literal

CREDIT_USAGE_META_KEY = "com.cic/credit_usage"
MIN_TASK_TTL_MS = 3_600_000
_STATUSES = ("input_required", "completed")


class ContractViolation(ValueError):
    """The provider's tool or result does not follow the CIC entry-tool contract."""


class AgentCallFailed(RuntimeError):
    """The provider reported a tool error (`isError: true`); the turn is not billed."""


@dataclass(frozen=True)
class EntryTool:
    name: str
    description: str
    supports_image: bool


@dataclass(frozen=True)
class AgentReply:
    conversation_id: str
    status: Literal["input_required", "completed"]
    reply: str
    credit_usage: float


def _variants(schema: Any) -> list[dict[str, Any]]:
    """Non-null alternatives of a property schema; pydantic renders `T | None` as `anyOf [T, null]`."""
    if not isinstance(schema, dict):
        return []
    options = schema.get("anyOf", [schema])
    return [option for option in options if isinstance(option, dict) and option.get("type") != "null"]


def _is_string(schema: Any) -> bool:
    variants = _variants(schema)
    return len(variants) == 1 and variants[0].get("type") == "string"


def _is_string_array(schema: Any) -> bool:
    variants = _variants(schema)
    return len(variants) == 1 and variants[0].get("type") == "array" and _is_string(variants[0].get("items"))


def validate_entry_tool(tool: dict[str, Any]) -> EntryTool:
    name = str(tool.get("name", ""))
    schema = tool.get("inputSchema")
    if not isinstance(schema, dict) or schema.get("type") != "object":
        raise ContractViolation(f"{name}: inputSchema must be an object schema")
    properties = schema.get("properties") or {}
    required = set(schema.get("required") or [])
    if "message" not in required or not _is_string(properties.get("message")):
        raise ContractViolation(f"{name}: 'message' must be a required string parameter")
    if "conversation_id" not in properties or not _is_string(properties["conversation_id"]):
        raise ContractViolation(f"{name}: 'conversation_id' must be an optional string parameter")
    extra_required = sorted(required - {"message"})
    if extra_required:
        raise ContractViolation(f"{name}: only 'message' may be required, got {', '.join(extra_required)}")
    supports_image = "image_urls" in properties
    if supports_image and not _is_string_array(properties["image_urls"]):
        raise ContractViolation(f"{name}: 'image_urls' must be an optional array of strings")
    return EntryTool(name, str(tool.get("description") or ""), supports_image)


def parse_agent_reply(result: dict[str, Any]) -> AgentReply:
    """Validate a CallToolResult (wire JSON) from an entry tool and extract the billable reply."""
    texts = [
        item.get("text", "")
        for item in result.get("content") or []
        if isinstance(item, dict) and item.get("type") == "text"
    ]
    if result.get("isError"):
        raise AgentCallFailed("; ".join(text for text in texts if text) or "The agent reported an error")
    if not texts:
        raise ContractViolation("content must contain a text block with the reply")
    structured = result.get("structuredContent")
    if not isinstance(structured, dict):
        raise ContractViolation("structuredContent is missing")
    conversation_id = structured.get("conversation_id")
    status = structured.get("status")
    reply = structured.get("reply")
    if not isinstance(conversation_id, str) or not conversation_id:
        raise ContractViolation("structuredContent.conversation_id must be a non-empty string")
    if status not in _STATUSES:
        raise ContractViolation(f"structuredContent.status must be one of {_STATUSES}, got {status!r}")
    if not isinstance(reply, str):
        raise ContractViolation("structuredContent.reply must be a string")
    credit = (result.get("_meta") or {}).get(CREDIT_USAGE_META_KEY)
    if isinstance(credit, bool) or not isinstance(credit, int | float) or not math.isfinite(credit) or credit < 0:
        raise ContractViolation(f"{CREDIT_USAGE_META_KEY} must be a number >= 0, got {credit!r}")
    return AgentReply(conversation_id, status, reply, credit)


def check_task_ttl(ttl_ms: int | None) -> str | None:
    """Warning text when a task's ttlMs is below the contract minimum (recorded, not enforced)."""
    if ttl_ms is not None and ttl_ms < MIN_TASK_TTL_MS:
        return f"ttlMs={ttl_ms} is below the contract minimum of {MIN_TASK_TTL_MS} (1 hour)"
    return None
