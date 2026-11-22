from mcp.server.auth.settings import AuthSettings
from mcp.server.mcpserver import MCPServer

from cic_mcp_lab.cic.probe import STEPS, connection_fingerprint, probe
from cic_mcp_lab.cic.registry import AgentRecord
from cic_mcp_lab.provider.auth import ApiKeyVerifier
from tests.helpers import TEST_API_KEY as KEY
from tests.helpers import running_server, unused_port


def record_for(url, entry_tool="trip_planner"):
    return AgentRecord("agent", "provider-x", "n", "d", url, "K", entry_tool, 0.0001)


def inline_server(*, auth):
    """A server whose only tool ignores the CIC contract (its parameter is 'query', not 'message')."""
    options = (
        {
            "token_verifier": ApiKeyVerifier({KEY: "cic"}),
            "auth": AuthSettings(issuer_url="https://auth.invalid", resource_server_url=None),
        }
        if auth
        else {}
    )
    mcp = MCPServer("inline", **options)

    async def entry(query: str) -> str:
        return query

    mcp.add_tool(entry, name="entry")
    return running_server(mcp.streamable_http_app(host="127.0.0.1"))


def step_results(report):
    return [(step.name, step.ok) for step in report.steps]


async def test_compliant_agent_passes_every_step(start_server):
    url, _ = start_server()
    report = await probe(record_for(url), KEY)

    assert report.ok
    assert [step.name for step in report.steps] == list(STEPS)
    assert report.supports_tasks
    assert report.entry_tool.supports_image is False
    assert report.fingerprint == connection_fingerprint(url, KEY, "trip_planner")


def test_fingerprint_covers_url_key_and_entry_tool():
    base = connection_fingerprint("http://h/mcp", "sk-1", "t")

    assert base == connection_fingerprint("http://h/mcp", "sk-1", "t")
    assert len({base, connection_fingerprint("http://x/mcp", "sk-1", "t"),
                connection_fingerprint("http://h/mcp", "sk-2", "t"),
                connection_fingerprint("http://h/mcp", "sk-1", "u")}) == 4
    assert "sk-1" not in base


async def test_unreachable_server_fails_discover():
    report = await probe(record_for(f"http://127.0.0.1:{unused_port()}/mcp"), KEY)

    assert step_results(report) == [("discover", False)]


async def test_server_without_auth_is_blocked():
    with inline_server(auth=False) as url:
        report = await probe(record_for(url, entry_tool="entry"), KEY)

    assert step_results(report) == [("discover", True), ("auth", False)]
    assert "HTTP 200" in report.steps[-1].detail


async def test_non_compliant_entry_tool_is_rejected():
    with inline_server(auth=True) as url:
        report = await probe(record_for(url, entry_tool="entry"), KEY)

    assert step_results(report)[-1] == ("entry_tool", False)
    assert "message" in report.steps[-1].detail
    assert not report.ok


async def test_unlisted_entry_tool_is_rejected(start_server):
    url, _ = start_server()
    report = await probe(record_for(url, entry_tool="nope"), KEY)

    assert step_results(report)[-1] == ("entry_tool", False)
    assert "nope" in report.steps[-1].detail
