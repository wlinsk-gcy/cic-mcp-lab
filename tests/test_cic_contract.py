import pytest

from cic_mcp_lab.cic.contract import (
    AgentCallFailed,
    ContractViolation,
    check_task_ttl,
    parse_agent_reply,
    validate_entry_tool,
)

MESSAGE = {"type": "string"}
NULLABLE_STRING = {"anyOf": [{"type": "string"}, {"type": "null"}], "default": None}
IMAGES = {"anyOf": [{"type": "array", "items": {"type": "string"}}, {"type": "null"}], "default": None}
_OMIT = object()


def tool(properties, required=("message",)):
    return {
        "name": "entry",
        "description": "d",
        "inputSchema": {"type": "object", "properties": properties, "required": list(required)},
    }


def call_result(*, credit=42, structured=None, is_error=False, text="ok"):
    return {
        "content": [] if text is None else [{"type": "text", "text": text}],
        "structuredContent": structured or {"conversation_id": "conv_1", "status": "completed", "reply": "ok"},
        "isError": is_error,
        "_meta": {} if credit is _OMIT else {"com.cic/credit_usage": credit},
    }


def test_entry_tool_accepts_plain_and_nullable_optional_fields():
    plain = validate_entry_tool(tool({"message": MESSAGE, "conversation_id": {"type": "string"}}))
    nullable = validate_entry_tool(
        tool({"message": MESSAGE, "conversation_id": NULLABLE_STRING, "image_urls": IMAGES})
    )

    assert (plain.supports_image, nullable.supports_image) == (False, True)


@pytest.mark.parametrize(
    ("schema", "reason"),
    [
        (tool({"query": MESSAGE}, required=("query",)), "message"),
        (tool({"message": MESSAGE}), "conversation_id"),
        (
            tool({"message": MESSAGE, "conversation_id": NULLABLE_STRING, "topic": MESSAGE}, required=("message", "topic")),
            "topic",
        ),
        (tool({"message": MESSAGE, "conversation_id": NULLABLE_STRING, "image_urls": MESSAGE}), "image_urls"),
    ],
)
def test_entry_tool_contract_violations(schema, reason):
    with pytest.raises(ContractViolation, match=reason):
        validate_entry_tool(schema)


def test_agent_reply_is_parsed_and_zero_credit_is_valid():
    reply = parse_agent_reply(call_result(credit=0))

    assert (reply.conversation_id, reply.status, reply.reply, reply.credit_usage) == ("conv_1", "completed", "ok", 0)


def test_tool_errors_carry_the_provider_message():
    with pytest.raises(AgentCallFailed, match="conv_x expired"):
        parse_agent_reply(call_result(is_error=True, text="conv_x expired"))


@pytest.mark.parametrize("credit", [_OMIT, None, "", "12", -1, True, float("nan")])
def test_missing_or_invalid_credit_usage_violates_the_contract(credit):
    with pytest.raises(ContractViolation, match="credit_usage"):
        parse_agent_reply(call_result(credit=credit))


@pytest.mark.parametrize(
    "structured",
    [
        {"status": "completed", "reply": "ok"},
        {"conversation_id": "conv_1", "status": "done", "reply": "ok"},
        {"conversation_id": "conv_1", "status": "completed"},
    ],
)
def test_incomplete_structured_content_violates_the_contract(structured):
    with pytest.raises(ContractViolation, match="structuredContent"):
        parse_agent_reply(call_result(structured=structured))


def test_result_without_text_content_violates_the_contract():
    with pytest.raises(ContractViolation, match="content"):
        parse_agent_reply(call_result(text=None))


def test_short_task_ttl_is_flagged():
    assert check_task_ttl(None) is None
    assert check_task_ttl(3_600_000) is None
    assert "ttlMs" in check_task_ttl(60_000)
