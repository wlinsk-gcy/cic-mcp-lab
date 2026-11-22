import pytest
import yaml

from cic_mcp_lab.cic.registry import RegistryError, api_key_for, load_registry

ENDPOINT = {"mcp_url": "http://h/mcp", "api_key_env": "K", "entry_tool": "t"}


def entry(agent_id="a", **fields):
    """A valid mock-agent entry; a field set to None is left out."""
    data = {
        "agent_id": agent_id,
        "provider_id": "p",
        "display_name": "n",
        "description": "d",
        "category": ["c"],
        "tags": ["t"],
        "examples": ["e1", "e2", "e3"],
        "price_per_credit": 0.0001,
        **fields,
    }
    return {key: value for key, value in data.items() if value is not None}


def write_registry(tmp_path, *entries):
    path = tmp_path / "agents.yaml"
    path.write_text(yaml.safe_dump({"agents": list(entries)}, allow_unicode=True), encoding="utf-8")
    return path


def test_onboarded_agents_point_at_their_provider_servers():
    registry = load_registry()

    assert {agent_id: (r.mcp_url, r.api_key_env, r.entry_tool) for agent_id, r in registry.items() if r.callable} == {
        "trip_planner": ("http://127.0.0.1:8001/mcp", "PROVIDER_A_API_KEY", "trip_planner"),
        "product_copy": ("http://127.0.0.1:8001/mcp", "PROVIDER_A_API_KEY", "product_copy"),
        "weekly_report": ("http://127.0.0.1:8002/mcp", "PROVIDER_B_API_KEY", "weekly_report"),
    }


def test_seed_registry_adds_mock_agents_for_recommendation():
    registry = load_registry()

    assert len(registry) == 15
    assert sum(not record.callable for record in registry.values()) == 12
    assert all(record.published for record in registry.values())
    assert {agent_id for agent_id, r in registry.items() if r.supports_image and not r.callable} == {
        "poster_design",
        "product_cutout",
    }


def test_one_endpoint_cannot_be_registered_twice(tmp_path):
    path = write_registry(tmp_path, entry("a", **ENDPOINT), entry("b", **ENDPOINT))

    with pytest.raises(RegistryError, match="already registered"):
        load_registry(path)


@pytest.mark.parametrize(
    ("fields", "reason"),
    [
        ({"examples": None}, "missing: examples"),
        ({"examples": ["e1", "e2"]}, "3-10"),
        ({"examples": [f"e{i}" for i in range(11)]}, "3-10"),
        ({"tags": "t"}, "tags"),
        ({"mcp_url": "http://h/mcp"}, "together"),
        ({"status": "draft"}, "status"),
    ],
)
def test_invalid_entries_are_rejected(tmp_path, fields, reason):
    with pytest.raises(RegistryError, match=reason):
        load_registry(write_registry(tmp_path, entry(**fields)))


def test_mock_agents_have_no_endpoint_or_key(tmp_path):
    record = load_registry(write_registry(tmp_path, entry(supports_image=True)))["a"]

    assert (record.callable, record.supports_image, record.status, record.examples) == (
        False,
        True,
        "published",
        ("e1", "e2", "e3"),
    )
    with pytest.raises(RegistryError, match="not callable"):
        api_key_for(record, {"K": "sk"})


def test_api_key_comes_from_the_environment():
    record = load_registry()["weekly_report"]

    assert api_key_for(record, {"PROVIDER_B_API_KEY": " sk-b "}) == "sk-b"
    with pytest.raises(RegistryError, match="PROVIDER_B_API_KEY"):
        api_key_for(record, {})
