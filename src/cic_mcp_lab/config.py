"""Settings loaded from the project .env file."""
from __future__ import annotations

import os
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping, TypeVar

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parents[2]

T = TypeVar("T")


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
    qdrant_path: Path = PROJECT_ROOT / "data" / "qdrant"
    recall_top_n: int = 20
    recommend_top_k: int = 2
    recommend_min_score_dense: float | None = None  # chosen by the step 5 evaluation; unset = no threshold


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

    def optional(name: str, parse: Callable[[str], T], default: T) -> T:
        raw = env.get(name, "").strip()
        if not raw:
            return default
        try:
            return parse(raw)
        except ValueError as exc:
            raise ConfigError(f"{name} is not a valid {parse.__name__}: {raw!r}") from exc

    qdrant_path = Path(env.get("QDRANT_PATH", "").strip() or "data/qdrant")

    return Settings(
        dashscope_base_url=get("DASHSCOPE_BASE_URL"),
        dashscope_api_key=get("DASHSCOPE_API_KEY"),
        router_model=get("ROUTER_MODEL"),
        provider_model=get("PROVIDER_MODEL"),
        embedding_model=get("EMBEDDING_MODEL"),
        embedding_dim=embedding_dim,
        # OSS SDK V2 expects "cn-guangzhou", the console shows "oss-cn-guangzhou".
        oss_region=get("OSS_REGION").removeprefix("oss-"),
        # Public URLs are built as https://{bucket}.{endpoint}, so drop any scheme.
        oss_endpoint=get("OSS_ENDPOINT").removeprefix("https://").removeprefix("http://"),
        oss_bucket=get("OSS_BUCKET"),
        oss_access_key_id=get("OSS_ACCESS_KEY_ID"),
        oss_access_key_secret=get("OSS_ACCESS_KEY_SECRET"),
        qdrant_path=qdrant_path if qdrant_path.is_absolute() else PROJECT_ROOT / qdrant_path,
        recall_top_n=optional("RECALL_TOP_N", int, 20),
        recommend_top_k=optional("RECOMMEND_TOP_K", int, 2),
        recommend_min_score_dense=optional("RECOMMEND_MIN_SCORE_DENSE", float, None),
    )
