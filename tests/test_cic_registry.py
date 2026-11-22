import pytest

from cic_mcp_lab.cic.registry import RegistryError, api_key_for, load_registry

ENTRY = (
    "  - {{agent_id: {agent_id}, provider_id: p, display_name: n, description: d, "
    'mcp_url: "http://h/mcp", api_key_env: K, entry_tool: {tool}, price_per_credit: 0.0001}}\n'
)


def write_registry(tmp_path, *entries):
    path = tmp_path / "agents.yaml"
    path.write_text("agents:\n" + "".join(entries), encoding="utf-8")
    return path


def test_onboarded_agents_point_at_their_provider_servers():
    registry = load_registry()

    assert {agent_id: (r.mcp_url, r.api_key_env, r.entry_tool) for agent_id, r in registry.items()} == {
        "trip_planner": ("http://127.0.0.1:8001/mcp", "PROVIDER_A_API_KEY", "trip_planner"),
        "product_copy": ("http://127.0.0.1:8001/mcp", "PROVIDER_A_API_KEY", "product_copy"),
        "weekly_report": ("http://127.0.0.1:8002/mcp", "PROVIDER_B_API_KEY", "weekly_report"),
    }


def test_one_endpoint_cannot_be_registered_twice(tmp_path):
    path = write_registry(tmp_path, ENTRY.format(agent_id="a", tool="t"), ENTRY.format(agent_id="b", tool="t"))

    with pytest.raises(RegistryError, match="already registered"):
        load_registry(path)


def test_missing_fields_are_reported(tmp_path):
    path = write_registry(tmp_path, "  - {agent_id: a}\n")

    with pytest.raises(RegistryError, match="mcp_url"):
        load_registry(path)


def test_api_key_comes_from_the_environment():
    record = load_registry()["weekly_report"]

    assert api_key_for(record, {"PROVIDER_B_API_KEY": " sk-b "}) == "sk-b"
    with pytest.raises(RegistryError, match="PROVIDER_B_API_KEY"):
        api_key_for(record, {})
