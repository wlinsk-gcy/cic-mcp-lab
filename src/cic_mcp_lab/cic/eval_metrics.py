"""Recommendation evaluation (design §7): the query set and pure metric functions."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import yaml

from ..config import PROJECT_ROOT

DEFAULT_QUERIES_PATH = PROJECT_ROOT / "data" / "eval_queries.yaml"
CATEGORIES = ("regular", "confusable", "context", "none")
CATEGORY_LABELS = {"regular": "常规", "confusable": "易混淆", "context": "依赖上下文", "none": "无合适 Agent"}
Category = Literal["regular", "confusable", "context", "none"]


@dataclass(frozen=True)
class EvalQuery:
    query: str
    expect: str | None  # the agent that should come first; None = no onboarded agent fits
    category: Category
    image: bool = False  # the user attached an image, so only image-capable agents qualify
    history: tuple[str, ...] = ()  # earlier turns; V1-V3 search with the last message only (rewriting is V4)


def load_eval_queries(path: Path = DEFAULT_QUERIES_PATH) -> list[EvalQuery]:
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    items: list[EvalQuery] = []
    for index, raw in enumerate(data.get("queries") or []):
        where = f"queries[{index}]"
        category = raw.get("category")
        if category not in CATEGORIES:
            raise ValueError(f"{where}: category must be one of {CATEGORIES}, got {category!r}")
        expect = raw.get("expect")
        if (expect is None) != (category == "none"):
            raise ValueError(f"{where}: expect must be null exactly when the category is 'none'")
        query = str(raw.get("query") or "").strip()
        if not query:
            raise ValueError(f"{where}: query is empty")
        history = tuple(str(turn) for turn in raw.get("history") or ())
        items.append(EvalQuery(query, expect, category, bool(raw.get("image", False)), history))
    return items
