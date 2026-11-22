from collections import Counter

import pytest

from cic_mcp_lab.cic.eval_metrics import load_eval_queries
from cic_mcp_lab.cic.registry import load_registry

IMAGE_CAPABLE = {"product_copy", "poster_design", "product_cutout"}


def test_query_set_has_the_planned_mix():
    items = load_eval_queries()

    assert Counter(item.category for item in items) == {"regular": 20, "confusable": 10, "context": 5, "none": 5}
    assert all(item.history for item in items if item.category == "context")
    assert sum(item.image for item in items) == 3


def test_every_agent_is_expected_somewhere_and_image_queries_fit_image_agents():
    registry = load_registry()
    items = load_eval_queries()

    assert {item.expect for item in items if item.expect} == set(registry)
    assert all(item.expect in IMAGE_CAPABLE for item in items if item.image)


def test_no_query_repeats_a_registered_example_question():
    examples = {example for record in load_registry().values() for example in record.examples}

    assert [item.query for item in load_eval_queries() if item.query in examples] == []


def test_expect_must_be_null_exactly_for_no_fit_queries(tmp_path):
    path = tmp_path / "queries.yaml"
    path.write_text("queries:\n  - {query: 讲个笑话, expect: trip_planner, category: none}\n", encoding="utf-8")

    with pytest.raises(ValueError, match="expect"):
        load_eval_queries(path)
