"""Settings loaded from the project .env file."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parents[2]


class ConfigError(RuntimeError):
    """Raised when required settings are missing or invalid."""


@dataclass(frozen=True)
class Settings:
    dashscope_base_url: str
    dashscope_api_key: str = field(repr=False)
    router_model: str
    provider_model: str
    embedding_model: str
    embedding_dim: int
    oss_region: str
    oss_endpoint: str
    oss_bucket: str
    oss_access_key_id: str = field(repr=False)
    oss_access_key_secret: str = field(repr=False)


_REQUIRED = (
    "DASHSCOPE_BASE_URL",
    "DASHSCOPE_API_KEY",
    "ROUTER_MODEL",
    "PROVIDER_MODEL",
    "EMBEDDING_MODEL",
    "EMBEDDING_DIM",
    "OSS_REGION",
    "OSS_ENDPOINT",
    "OSS_BUCKET",
    "OSS_ACCESS_KEY_ID",
    "OSS_ACCESS_KEY_SECRET",
)


def load_settings(env: Mapping[str, str] | None = None) -> Settings:
    """Load settings from ``env``, or from ``.env`` + process env when omitted."""
    if env is None:
        load_dotenv(PROJECT_ROOT / ".env")
        env = os.environ
    missing = [name for name in _REQUIRED if not env.get(name, "").strip()]
    if missing:
        raise ConfigError(f"Missing settings in .env: {', '.join(missing)}")
    try:
        embedding_dim = int(env["EMBEDDING_DIM"])
    except ValueError as exc:
        raise ConfigError("EMBEDDING_DIM must be an integer") from exc

    def get(name: str) -> str:
        return env[name].strip()

    return Settings(
        dashscope_base_url=get("DASHSCOPE_BASE_URL"),
        dashscope_api_key=get("DASHSCOPE_API_KEY"),
        router_model=get("ROUTER_MODEL"),
        provider_model=get("PROVIDER_MODEL"),
        embedding_model=get("EMBEDDING_MODEL"),
        embedding_dim=embedding_dim,
        # OSS SDK V2 expects "cn-guangzhou", the console shows "oss-cn-guangzhou".
        oss_region=get("OSS_REGION").removeprefix("oss-"),
        oss_endpoint=get("OSS_ENDPOINT"),
        oss_bucket=get("OSS_BUCKET"),
        oss_access_key_id=get("OSS_ACCESS_KEY_ID"),
        oss_access_key_secret=get("OSS_ACCESS_KEY_SECRET"),
    )
