"""cic-intent-eval: measure intent recognition on data/intent_cases.yaml (design §6.3, §6.7)."""
from __future__ import annotations

import argparse
import asyncio
import math
import sys
import time
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, get_args

import yaml

from ..config import PROJECT_ROOT, load_settings
from ..llm import make_openai_client
from .intent import Intent, IntentDecision, LlmIntentRecognizer
from .registry import AgentRecord, load_registry
from .router import Recognizer
from .session import Candidate, PendingCandidates, PendingRetry, Session, Thread, Turn

DEFAULT_CASES_PATH = PROJECT_ROOT / "data" / "intent_cases.yaml"
INTENTS = get_args(Intent)


@dataclass(frozen=True)
class Expectation:
    intent: str
    thread_ref: str | None = None
    candidate_index: int | None = None


@dataclass(frozen=True)
class IntentCase:
    name: str
    text: str
    expect: Expectation
    session: Session
    image_count: int = 0


@dataclass(frozen=True)
class IntentResult:
    case: IntentCase
    decision: IntentDecision | None
    error: str | None
    latency_ms: float

    @property
    def correct(self) -> bool:
        decision, expect = self.decision, self.case.expect
        if decision is None or decision.intent != expect.intent:
            return False
        if expect.thread_ref is not None and decision.thread_ref != expect.thread_ref:
            return False
        return expect.candidate_index is None or decision.candidate_index == expect.candidate_index


def _session(raw: Mapping[str, Any], registry: Mapping[str, AgentRecord], where: str) -> Session:
    session = Session()
    for item in raw.get("threads") or []:
        record = registry.get(item["agent"])
        if record is None:
            raise ValueError(f"{where}: unknown agent {item['agent']!r}")
        ref = item["ref"]
        session.threads[ref] = Thread(
            ref,
            record.agent_id,
            record.display_name,
            record.provider_id,
            conversation_id=f"conv_{ref.lower()}",
            status=item["status"],
            last_reply=item.get("last_reply", ""),
        )
    for turn in raw.get("history") or []:
        ref = turn.get("thread")
        label = f"{session.threads[ref].display_name} · {ref}" if turn["role"] == "agent" else None
        session.history.append(Turn(turn["role"], turn["text"], ref, label))
    pending = raw.get("pending")
    if pending and "candidates" in pending:
        session.pending = PendingCandidates(
            pending["message"],
            (),
            tuple(
                Candidate(a, registry[a].display_name, registry[a].provider_id, 0.0, "画像")
                for a in pending["candidates"]
            ),
        )
    elif pending and "retry" in pending:
        session.pending = PendingRetry(pending["retry"], pending["message"], ())
    return session


def load_intent_cases(
    path: Path = DEFAULT_CASES_PATH, registry: Mapping[str, AgentRecord] | None = None
) -> list[IntentCase]:
    registry = registry if registry is not None else load_registry()
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    cases: list[IntentCase] = []
    for index, raw in enumerate(data.get("cases") or []):
        where = f"cases[{index}]"
        expect = Expectation(**raw["expect"])
        if expect.intent not in INTENTS:
            raise ValueError(f"{where}: unknown intent {expect.intent!r}")
        cases.append(
            IntentCase(
                str(raw.get("name") or where),
                str(raw["input"]),
                expect,
                _session(raw, registry, where),
                int(raw.get("image_count", 0)),
            )
        )
    return cases


async def run_intent_eval(cases: Sequence[IntentCase], recognizer: Recognizer) -> list[IntentResult]:
    results: list[IntentResult] = []
    for case in cases:
        started = time.perf_counter()
        try:
            decision, error = await recognizer.recognize(case.session, case.text, case.image_count), None
        except Exception as exc:  # a failed call counts as a wrong answer
            decision, error = None, f"{type(exc).__name__}: {exc}"
        results.append(IntentResult(case, decision, error, (time.perf_counter() - started) * 1000))
    return results


def _pct(part: int, whole: int) -> str:
    return f"{part / whole * 100:.1f}%" if whole else "—"


def _expected(expect: Expectation) -> str:
    target = expect.thread_ref or (f"[{expect.candidate_index}]" if expect.candidate_index else "")
    return f"{expect.intent} {target}".strip()


def _actual(result: IntentResult) -> str:
    if result.decision is None:
        return f"（错误）{result.error}"
    decision = result.decision
    target = decision.thread_ref or (f"[{decision.candidate_index}]" if decision.candidate_index else "")
    return f"{decision.intent} {target}".strip()


def render_intent_report(results: Sequence[IntentResult], *, header_lines: Sequence[str]) -> str:
    correct = sum(result.correct for result in results)
    latencies = sorted(result.latency_ms for result in results)
    mean = sum(latencies) / len(latencies) if latencies else 0.0
    p95 = latencies[math.ceil(0.95 * len(latencies)) - 1] if latencies else 0.0
    lines = ["# 意图识别评测报告（Step 6）", "", *(f"- {line}" for line in header_lines), "", "## 总体", ""]
    lines += [
        "| 用例数 | 正确 | 准确率 | 延迟（平均 / P95） |",
        "| --- | --- | --- | --- |",
        f"| {len(results)} | {correct} | {_pct(correct, len(results))} | {mean:.0f} / {p95:.0f} ms |",
        "",
        "## 按期望意图",
        "",
        "| 期望意图 | 用例数 | 正确 | 准确率 |",
        "| --- | --- | --- | --- |",
    ]
    totals = Counter(result.case.expect.intent for result in results)
    hits = Counter(result.case.expect.intent for result in results if result.correct)
    for intent in INTENTS:
        if totals[intent]:
            lines.append(f"| {intent} | {totals[intent]} | {hits[intent]} | {_pct(hits[intent], totals[intent])} |")
    wrong = [result for result in results if not result.correct]
    lines += ["", f"## 错例（{len(wrong)} 条）", ""]
    if wrong:
        lines += ["| 用例 | 输入 | 期望 | 实际 | 模型理由 |", "| --- | --- | --- | --- | --- |"]
        for result in wrong:
            reason = result.decision.reason if result.decision else ""
            lines.append(
                f"| {result.case.name} | {result.case.text} | {_expected(result.case.expect)} | {_actual(result)} | {reason} |"
            )
    else:
        lines.append("无")
    rewrites = [r for r in results if r.decision is not None and r.decision.intent == "recommend"]
    lines += ["", "## recommend 的检索改写（search_query）", ""]
    if rewrites:
        lines += ["| 输入 | search_query |", "| --- | --- |"]
        lines += [f"| {r.case.text} | {r.decision.search_query or '（空）'} |" for r in rewrites]
    else:
        lines.append("无")
    return "\n".join(lines) + "\n"


async def _evaluate(output: Path | None) -> Path:
    settings = load_settings()
    cases = load_intent_cases()
    recognizer = LlmIntentRecognizer(make_openai_client(settings), settings.router_model)
    results = await run_intent_eval(cases, recognizer)
    header = [
        f"时间：{datetime.now():%Y-%m-%d %H:%M}",
        f"模型：{settings.router_model}（structured output，enable_thinking=false，temperature=0）",
        f"用例：{len(cases)} 条（data/intent_cases.yaml，由 §6.7 的 19 个验收场景加变体组成）",
        "判定：intent 相同，且期望中给出的 thread_ref / candidate_index 也相同",
    ]
    report = render_intent_report(results, header_lines=header)
    path = output or PROJECT_ROOT / "reports" / f"intent-eval-{datetime.now():%Y%m%d-%H%M}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(report, encoding="utf-8")
    print(report.split("## 错例")[0].rstrip())
    return path


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description="意图识别评测：基于 data/intent_cases.yaml 统计准确率")
    parser.add_argument("--output", type=Path, help="报告路径，默认 reports/intent-eval-<时间>.md")
    path = asyncio.run(_evaluate(parser.parse_args().output))
    print(f"\n完整报告：{path}")
