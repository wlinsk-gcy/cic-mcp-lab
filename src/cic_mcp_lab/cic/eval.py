"""cic-eval: compare recommendation variants on data/eval_queries.yaml and write a Markdown report (design §7)."""
from __future__ import annotations

import argparse
import asyncio
import sys
import time
from collections.abc import Callable, Mapping, Sequence
from datetime import datetime
from pathlib import Path

from ..config import PROJECT_ROOT, load_settings
from .eval_metrics import (
    CATEGORY_LABELS,
    EvalQuery,
    Scored,
    accuracy,
    accuracy_by_category,
    best_threshold,
    latency,
    load_eval_queries,
    separation,
    threshold_stats,
)
from .recommend.embedding import Embedder, make_embedder
from .recommend.recommender import Recommender
from .recommend.rerank import INSTRUCT, Reranker, make_reranker
from .recommend.vector_store import QdrantVectorStore
from .registry import AgentRecord, load_registry

VARIANTS = ("V1", "V2", "V3")
DESCRIPTIONS = {"V1": "只用画像（等同于删除全部示例问题）", "V2": "画像 + 示例问题", "V3": "画像 + 示例问题 + Rerank"}
SWEEP = tuple(round(0.20 + 0.05 * step, 2) for step in range(13))  # 0.20 … 0.80
EVAL_RECALL = 50  # recall every onboarded agent: the evaluation ranks the whole field


def build_recommenders(
    embedder: Embedder,
    store: QdrantVectorStore,
    registry: Callable[[], Mapping[str, AgentRecord]],
    reranker: Reranker | None,
    variants: Sequence[str],
) -> dict[str, Recommender]:
    options: dict[str, dict] = {"V1": {"kinds": ("profile",)}, "V2": {}, "V3": {"reranker": reranker}}
    if "V3" in variants and reranker is None:
        raise ValueError("V3 needs a reranker")
    return {
        name: Recommender(embedder, store, registry, recall_top_n=EVAL_RECALL, **options[name]) for name in variants
    }


async def run_eval(
    items: Sequence[EvalQuery], recommenders: Mapping[str, Recommender], embedder: Embedder
) -> dict[str, list[Scored]]:
    """Rank every query with every variant; each query is embedded once and shared by all variants."""
    results: dict[str, list[Scored]] = {name: [] for name in recommenders}
    for item in items:
        started = time.perf_counter()
        [vector] = await embedder.embed([item.query])
        embed_ms = (time.perf_counter() - started) * 1000
        for name, recommender in recommenders.items():
            started = time.perf_counter()
            ranking = await recommender.rank(item.query, vector=vector, with_image=item.image, include_mock=True)
            elapsed_ms = embed_ms + (time.perf_counter() - started) * 1000
            results[name].append(Scored(item, tuple((pick.agent_id, pick.score) for pick in ranking), elapsed_ms))
    return results


def _pct(value: float) -> str:
    return f"{value * 100:.1f}%"


def _num(value: float | None) -> str:
    return "—" if value is None else f"{value:.2f}"


def render_report(
    results: Mapping[str, Sequence[Scored]], *, names: Mapping[str, str], header_lines: Sequence[str]
) -> str:
    def name_of(agent_id: str | None) -> str:
        return "—" if agent_id is None else names.get(agent_id, agent_id)

    lines = ["# 推荐效果评测报告（Step 5）", "", *(f"- {line}" for line in header_lines), "", "## 总体指标", ""]
    lines += [
        "| 方案 | 说明 | Hit@1 | Hit@2 | MRR | 常规 Hit@1 | 易混淆 Hit@1 | 依赖上下文 Hit@1 "
        "| 期望 Agent 得分（最低 / 平均） | 无关问题最高分（最高 / 平均） | 建议阈值 | 拦截率 | 误拦截率 | 耗时（平均 / P95） |",
        "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for name, scored in results.items():
        overall, by_category = accuracy(scored), accuracy_by_category(scored)
        gap, best = separation(scored), best_threshold(scored)
        mean_ms, p95_ms = latency(scored)

        def hit1(category: str) -> str:
            return _pct(by_category[category].hit1) if category in by_category else "—"

        lines.append(
            f"| {name} | {DESCRIPTIONS[name]} | {_pct(overall.hit1)} | {_pct(overall.hit2)} | {overall.mrr:.3f} "
            f"| {hit1('regular')} | {hit1('confusable')} | {hit1('context')} "
            f"| {_num(gap.expected_min)} / {_num(gap.expected_mean)} | {_num(gap.none_max)} / {_num(gap.none_mean)} "
            f"| {best.threshold:.3f} | {_pct(best.intercept_rate)} | {_pct(best.false_intercept_rate)} "
            f"| {mean_ms:.0f} / {p95_ms:.0f} ms |"
        )
    lines += [
        "",
        "> - Hit@k / MRR 只统计有期望 Agent 的问题；拦截 = 最高分低于阈值；拦截率针对「无合适 Agent」类，误拦截率针对其余问题。",
        "> - 建议阈值取使（拦截率 − 误拦截率）最大的分数线。V3 的分数是 Rerank 分，与 V1 / V2 的向量分不可直接比较。",
        "",
        "## 阈值扫描",
        "",
    ]
    for name, scored in results.items():
        lines += [f"### {name}", "", "| 阈值 | 无合适 Agent 拦截率 | 正常问题误拦截率 |", "| --- | --- | --- |"]
        for threshold in SWEEP:
            stats = threshold_stats(scored, threshold)
            lines.append(f"| {threshold:.2f} | {_pct(stats.intercept_rate)} | {_pct(stats.false_intercept_rate)} |")
        lines.append("")

    lines += ["## 错例（期望 Agent 未排第 1）", ""]
    for name, scored in results.items():
        misses = [result for result in scored if result.item.expect is not None and result.rank_of_expected() != 1]
        lines += [f"### {name}（{len(misses)} 条）", ""]
        if not misses:
            lines += ["无", ""]
            continue
        lines += ["| 问题 | 类别 | 期望（排名） | 实际前 3 |", "| --- | --- | --- | --- |"]
        for result in misses:
            rank = result.rank_of_expected()
            top3 = "；".join(f"{name_of(agent_id)} {score:.2f}" for agent_id, score in result.ranking[:3])
            lines.append(
                f"| {result.item.query} | {CATEGORY_LABELS[result.item.category]} "
                f"| {name_of(result.item.expect)}（{rank or '未召回'}） | {top3} |"
            )
        lines.append("")

    variants = list(results)
    lines += [
        "## 逐条结果（期望 Agent 的排名 · 最高分）",
        "",
        f"| 问题 | 类别 | 期望 | {' | '.join(variants)} |",
        f"| --- | --- | --- | {' | '.join('---' for _ in variants)} |",
    ]
    for index, row in enumerate(next(iter(results.values()), [])):
        cells = []
        for name in variants:
            result = results[name][index]
            rank = result.rank_of_expected() if row.item.expect else None
            label = "—" if row.item.expect is None else (str(rank) if rank else "未召回")
            cells.append(f"{label} · {result.top_score:.2f}")
        lines.append(
            f"| {row.item.query} | {CATEGORY_LABELS[row.item.category]} | {name_of(row.item.expect)} "
            f"| {' | '.join(cells)} |"
        )
    return "\n".join(lines) + "\n"


async def _evaluate(variants: list[str], output: Path | None) -> Path:
    settings = load_settings()
    registry = load_registry()
    items = load_eval_queries()
    unavailable = sorted(
        {
            item.expect
            for item in items
            if item.expect and (item.expect not in registry or not registry[item.expect].published)
        }
    )
    store = QdrantVectorStore.open(
        settings.qdrant_path, embedding_model=settings.embedding_model, dim=settings.embedding_dim
    )
    try:
        embedder = make_embedder(settings)
        reranker = make_reranker(settings) if "V3" in variants else None
        recommenders = build_recommenders(embedder, store, load_registry, reranker, variants)
        indexed = len(store.agent_ids())
        results = await run_eval(items, recommenders, embedder)
    finally:
        store.close()

    counts = {category: sum(1 for item in items if item.category == category) for category in CATEGORY_LABELS}
    mocks = sum(not record.callable for record in registry.values())
    breakdown = " / ".join(f"{CATEGORY_LABELS[category]} {n}" for category, n in counts.items())
    header = [
        f"时间：{datetime.now():%Y-%m-%d %H:%M}",
        f"Agent：注册表 {len(registry)} 个（其中模拟 {mocks} 个），索引中 {indexed} 个；全部参与排序",
        f"测试问题：{len(items)} 条（{breakdown}），其中 {sum(item.image for item in items)} 条附带图片，"
        "只在支持图片的 Agent 中检索",
        f"Embedding：{settings.embedding_model}（{settings.embedding_dim} 维，cosine）；"
        f"Rerank：{settings.rerank_model}（instruct：{INSTRUCT}）",
        "依赖上下文类只用最后一句检索，没有做查询改写（V4 待 Step 6 补跑）",
        "每条问题只 Embedding 一次，各方案共用；耗时 = Embedding + 检索（+ Rerank）",
    ]
    if unavailable:
        header.insert(0, f"⚠ 以下期望 Agent 不在注册表或已下架，会拉低指标：{', '.join(unavailable)}")
    report = render_report(results, names={a: r.display_name for a, r in registry.items()}, header_lines=header)
    path = output or PROJECT_ROOT / "reports" / f"step5-eval-{datetime.now():%Y%m%d-%H%M}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(report, encoding="utf-8")
    print(report.split("## 阈值扫描")[0].rstrip())
    return path


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description="推荐效果评测：V1（只用画像）/ V2（画像 + 示例问题）/ V3（+ Rerank）")
    parser.add_argument("--variants", default=",".join(VARIANTS), help="逗号分隔，默认 V1,V2,V3")
    parser.add_argument("--output", type=Path, help="报告路径，默认 reports/step5-eval-<时间>.md")
    args = parser.parse_args()
    variants = [variant.strip().upper() for variant in args.variants.split(",") if variant.strip()]
    unknown = [variant for variant in variants if variant not in VARIANTS]
    if unknown or not variants:
        parser.error(f"未知方案：{', '.join(unknown) or '（空）'}；可选 {', '.join(VARIANTS)}")
    path = asyncio.run(_evaluate(variants, args.output))
    print(f"\n完整报告：{path}")
