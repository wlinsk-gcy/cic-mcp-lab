import pytest

from cic_mcp_lab.config import PROJECT_ROOT, ConfigError, load_settings

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


def test_load_settings_strips_scheme_from_oss_endpoint():
    settings = load_settings({**BASE_ENV, "OSS_ENDPOINT": "https://oss-cn-guangzhou.aliyuncs.com"})
    assert settings.oss_endpoint == "oss-cn-guangzhou.aliyuncs.com"


def test_load_settings_reports_all_missing_keys():
    env = {k: v for k, v in BASE_ENV.items() if k not in {"DASHSCOPE_API_KEY", "OSS_BUCKET"}}
    with pytest.raises(ConfigError, match="DASHSCOPE_API_KEY, OSS_BUCKET"):
        load_settings(env)


def test_load_settings_rejects_non_integer_dim():
    with pytest.raises(ConfigError, match="EMBEDDING_DIM"):
        load_settings({**BASE_ENV, "EMBEDDING_DIM": "abc"})


def test_settings_repr_hides_secrets():
    text = repr(load_settings(BASE_ENV))
    assert "sk-test" not in text
    assert "'ak'" not in text
    assert "'sk'" not in text


def test_recommendation_settings_have_defaults():
    settings = load_settings(BASE_ENV)

    assert settings.qdrant_path == PROJECT_ROOT / "data" / "qdrant"
    assert (settings.recall_top_n, settings.recommend_top_k, settings.recommend_min_score_dense) == (20, 2, None)


def test_recommendation_settings_are_parsed():
    settings = load_settings(
        {
            **BASE_ENV,
            "QDRANT_PATH": "./tmp/vectors",
            "RECALL_TOP_N": "30",
            "RECOMMEND_TOP_K": "5",
            "RECOMMEND_MIN_SCORE_DENSE": "0.45",
        }
    )

    assert settings.qdrant_path == PROJECT_ROOT / "tmp" / "vectors"
    assert (settings.recall_top_n, settings.recommend_top_k, settings.recommend_min_score_dense) == (30, 5, 0.45)


@pytest.mark.parametrize(("name", "value"), [("RECALL_TOP_N", "many"), ("RECOMMEND_MIN_SCORE_DENSE", "high")])
def test_malformed_recommendation_settings_are_reported(name, value):
    with pytest.raises(ConfigError, match=name):
        load_settings({**BASE_ENV, name: value})


def test_rerank_settings_have_defaults():
    settings = load_settings(BASE_ENV)

    assert (settings.rerank_model, settings.rerank_enabled, settings.recommend_min_score_rerank) == (
        "qwen3-rerank",
        False,
        None,
    )


def test_rerank_settings_are_parsed():
    settings = load_settings(
        {**BASE_ENV, "RERANK_MODEL": "qwen3.7-text-rerank", "RERANK_ENABLED": "True", "RECOMMEND_MIN_SCORE_RERANK": "0.4"}
    )

    assert (settings.rerank_model, settings.rerank_enabled, settings.recommend_min_score_rerank) == (
        "qwen3.7-text-rerank",
        True,
        0.4,
    )


def test_malformed_rerank_switch_is_reported():
    with pytest.raises(ConfigError, match="RERANK_ENABLED"):
        load_settings({**BASE_ENV, "RERANK_ENABLED": "maybe"})
