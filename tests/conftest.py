from contextlib import ExitStack

import pytest

from cic_mcp_lab.provider.server import build_app
from tests.helpers import TEST_API_KEY, FakeRuntime, running_server


@pytest.fixture
def start_server():
    """Start a Provider MCP server with a fake agent runtime; returns (url, runtime)."""
    with ExitStack() as stack:

        def start(provider_id="provider-a", **overrides):
            runtime = FakeRuntime()
            app = build_app(provider_id=provider_id, api_key=TEST_API_KEY, runtime=runtime, **overrides)
            return stack.enter_context(running_server(app)), runtime

        yield start
