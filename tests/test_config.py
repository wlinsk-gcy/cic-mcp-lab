import pytest

from cic_mcp_lab.config import ConfigError, load_settings

BASE_ENV = {
    "DASHSCOPE_BASE_URL": "https://example.com/compatible-mode/v1",
    "DASHSCOPE_API_KEY": "sk-test",
    "ROUTER_MODEL": "qwen4-max",
    "PROVIDER_MODEL": "qwen4-max",
    "EMBEDDING_MODEL": "text-embedding-v4",
    "EMBEDDING_DIM": "1024",
    "OSS_REGION": "oss-cn-guangzhou",
    "OSS_ENDPOINT": "oss-cn-guangzhou.aliyuncs.com",
    "OSS_BUCKET": "bucket",
    "OSS_ACCESS_KEY_ID": "ak",
    "OSS_ACCESS_KEY_SECRET": "sk",
}


def test_load_settings_normalizes_oss_region_and_parses_dim():
    settings = load_settings(BASE_ENV)
    assert settings.oss_region == "cn-guangzhou"
    assert settings.embedding_dim == 1024


def test_load_settings_reports_all_missing_keys():
    env = {k: v for k, v in BASE_ENV.items() if k not in {"DASHSCOPE_API_KEY", "OSS_BUCKET"}}
    with pytest.raises(ConfigError, match="DASHSCOPE_API_KEY, OSS_BUCKET"):
        load_settings(env)


def test_load_settings_rejects_non_integer_dim():
    with pytest.raises(ConfigError, match="EMBEDDING_DIM"):
        load_settings({**BASE_ENV, "EMBEDDING_DIM": "abc"})
