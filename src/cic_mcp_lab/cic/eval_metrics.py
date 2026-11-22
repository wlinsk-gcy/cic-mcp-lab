"""Recommendation evaluation (design §7): the query set and pure metric functions."""
from __future__ import annotations

import math
from collections.abc import Callable, Sequence
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


@dataclass(frozen=True)
class Scored:
    """One query ranked by one variant: (agent_id, score) pairs, best first."""

    item: EvalQuery
    ranking: tuple[tuple[str, float], ...]
    latency_ms: float

    @property
    def top_score(self) -> float:
        return self.ranking[0][1] if self.ranking else 0.0

    def rank_of_expected(self) -> int | None:
        for position, (agent_id, _) in enumerate(self.ranking, 1):
            if agent_id == self.item.expect:
                return position
        return None

    def expected_score(self) -> float | None:
        """The expected agent's own score, whatever its rank; None when it was not ranked."""
        return next((score for agent_id, score in self.ranking if agent_id == self.item.expect), None)


@dataclass(frozen=True)
class Accuracy:
    n: int
    hit1: float
    hit2: float
    mrr: float


@dataclass(frozen=True)
class ThresholdStats:
    threshold: float
    intercept_rate: float  # no-fit queries whose best score falls below the threshold (correctly blocked)
    false_intercept_rate: float  # queries with an expected agent that get blocked as well


@dataclass(frozen=True)
class Separation:
    expected_min: float | None  # lowest / mean score of the expected agent on every query that has one, any rank
    expected_mean: float | None
    none_max: float | None  # highest / mean best score among no-fit queries
    none_mean: float | None


def _share(results: Sequence[Scored], predicate: Callable[[Scored], bool]) -> float:
    return sum(1 for result in results if predicate(result)) / len(results) if results else 0.0


def _positives(results: Sequence[Scored]) -> list[Scored]:
    return [result for result in results if result.item.expect is not None]


def _no_fit(results: Sequence[Scored]) -> list[Scored]:
    return [result for result in results if result.item.expect is None]


def accuracy(results: Sequence[Scored]) -> Accuracy:
    """Hit@1, Hit@2 and MRR over the queries that have an expected agent."""
    positives = _positives(results)
    if not positives:
        return Accuracy(0, 0.0, 0.0, 0.0)
    ranks = [result.rank_of_expected() for result in positives]
    n = len(positives)
    return Accuracy(
        n,
        sum(1 for rank in ranks if rank == 1) / n,
        sum(1 for rank in ranks if rank is not None and rank <= 2) / n,
        sum(1 / rank for rank in ranks if rank) / n,
    )


def accuracy_by_category(results: Sequence[Scored]) -> dict[str, Accuracy]:
    present = {result.item.category for result in _positives(results)}
    return {
        category: accuracy([result for result in results if result.item.category == category])
        for category in CATEGORIES
        if category in present
    }


def threshold_stats(results: Sequence[Scored], threshold: float) -> ThresholdStats:
    def blocked(result: Scored) -> bool:
        return result.top_score < threshold

    return ThresholdStats(threshold, _share(_no_fit(results), blocked), _share(_positives(results), blocked))


def best_threshold(results: Sequence[Scored]) -> ThresholdStats:
    """The cut that best separates no-fit queries from the rest: max(intercept − false intercept)."""
    scores = sorted({result.top_score for result in results})
    if not scores:
        return ThresholdStats(0.0, 0.0, 0.0)
    candidates = [scores[0] - 0.01, *((low + high) / 2 for low, high in zip(scores, scores[1:])), scores[-1] + 0.01]
    stats = [threshold_stats(results, candidate) for candidate in candidates]
    return max(stats, key=lambda s: (s.intercept_rate - s.false_intercept_rate, -s.false_intercept_rate))


def separation(results: Sequence[Scored]) -> Separation:
    # Every query with an expected agent counts, so all variants are compared on the same queries.
    expected = [score for result in _positives(results) if (score := result.expected_score()) is not None]
    no_fit = [result.top_score for result in _no_fit(results)]
    return Separation(
        min(expected) if expected else None,
        sum(expected) / len(expected) if expected else None,
        max(no_fit) if no_fit else None,
        sum(no_fit) / len(no_fit) if no_fit else None,
    )


def latency(results: Sequence[Scored]) -> tuple[float, float]:
    """Mean and 95th-percentile (nearest rank) latency in milliseconds."""
    values = sorted(result.latency_ms for result in results)
    if not values:
        return 0.0, 0.0
    return sum(values) / len(values), values[math.ceil(0.95 * len(values)) - 1]
