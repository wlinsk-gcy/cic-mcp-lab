import pytest

from cic_mcp_lab.cic.eval_metrics import (
    EvalQuery,
    Scored,
    accuracy,
    accuracy_by_category,
    best_threshold,
    latency,
    separation,
    threshold_stats,
)


def scored(expect, ranking, category="regular", latency_ms=10.0):
    item = EvalQuery("q", expect, "none" if expect is None else category)
    return Scored(item, tuple(ranking), latency_ms)


RESULTS = [
    scored("a", [("a", 0.8), ("b", 0.5)]),  # hit at 1
    scored("a", [("b", 0.7), ("a", 0.6)], "confusable"),  # hit at 2
    scored("a", [("b", 0.4), ("c", 0.3)], "confusable"),  # expected agent not ranked
    scored(None, [("b", 0.45)]),
    scored(None, [("c", 0.2)], latency_ms=30.0),
]


def test_accuracy_counts_only_queries_with_an_expected_agent():
    overall = accuracy(RESULTS)
    by_category = accuracy_by_category(RESULTS)

    assert (overall.n, overall.hit1, overall.hit2) == (3, pytest.approx(1 / 3), pytest.approx(2 / 3))
    assert overall.mrr == pytest.approx((1 + 0.5) / 3)
    assert set(by_category) == {"regular", "confusable"}
    assert (by_category["confusable"].n, by_category["confusable"].hit2) == (2, 0.5)


def test_queries_below_the_threshold_are_intercepted():
    stats = threshold_stats(RESULTS, 0.5)

    assert (stats.intercept_rate, stats.false_intercept_rate) == (1.0, pytest.approx(1 / 3))


def test_best_threshold_maximises_intercepted_minus_wrongly_intercepted():
    best = best_threshold(RESULTS)

    assert best.threshold == pytest.approx(0.575)  # halfway between 0.45 (no fit) and 0.7
    assert (best.intercept_rate, best.false_intercept_rate) == (1.0, pytest.approx(1 / 3))


def test_score_separation_and_latency():
    gap = separation(RESULTS)

    # the expected agent's own score on every query that has one, whatever its rank (0.8 and 0.6; one not ranked)
    assert (gap.expected_min, gap.expected_mean, gap.none_max) == (0.6, pytest.approx(0.7), 0.45)
    assert gap.none_mean == pytest.approx(0.325)
    assert latency(RESULTS) == (pytest.approx(14.0), 30.0)
