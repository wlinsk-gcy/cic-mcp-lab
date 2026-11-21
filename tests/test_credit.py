import pytest
from agentscope.message import Usage

from cic_mcp_lab.provider.credit import MissingUsageError, credit_from_usage


def test_credit_is_total_tokens():
    assert credit_from_usage(Usage(input_tokens=463, output_tokens=244)) == 707


def test_missing_usage_cannot_be_billed():
    with pytest.raises(MissingUsageError):
        credit_from_usage(None)
