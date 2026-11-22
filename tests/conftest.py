from contextlib import ExitStack

import pytest

from cic_mcp_lab.provider.server import build_app
from tests.helpers import TEST_API_KEY, FakeRuntime, running_server


@pytest.fixture
def start_server():
    """Start provider-a's MCP server with a fake agent runtime; returns (url, runtime)."""
    with ExitStack() as stack:

        def start(**overrides):
            runtime = FakeRuntime()
            app = build_app(provider_id="provider-a", api_key=TEST_API_KEY, runtime=runtime, **overrides)
            return stack.enter_context(running_server(app)), runtime

        yield start
