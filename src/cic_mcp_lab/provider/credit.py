"""Mock credit rule: 1 token = 1 Credit."""
from agentscope.message import Usage


class MissingUsageError(RuntimeError):
    """The model reply carried no token usage, so it cannot be billed."""


def credit_from_usage(usage: Usage | None) -> int:
    if usage is None:
        raise MissingUsageError("Model reply has no token usage")
    return usage.input_tokens + usage.output_tokens
