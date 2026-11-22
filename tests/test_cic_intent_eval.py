from collections import Counter

from cic_mcp_lab.cic.intent import IntentDecision
from cic_mcp_lab.cic.intent_eval import load_intent_cases, render_intent_report, run_intent_eval
from cic_mcp_lab.cic.session import PendingCandidates

INTENTS = {"supplement", "select_candidate", "router_answer", "recommend", "clarify"}


def test_cases_cover_every_intent_and_reference_real_state():
    cases = load_intent_cases()
    counts = Counter(case.expect.intent for case in cases)

    assert len(cases) >= 30 and set(counts) == INTENTS and min(counts.values()) >= 3
    for case in cases:
        if case.expect.thread_ref:
            assert case.expect.thread_ref in case.session.threads, case.name
        if case.expect.intent == "select_candidate":
            assert case.session.pending is not None, case.name


def write_cases(tmp_path):
    path = tmp_path / "cases.yaml"
    path.write_text(
        """cases:
  - name: 问候
    input: 你好
    expect: {intent: router_answer}
  - name: 补充
    threads: [{ref: T1, agent: trip_planner, status: input_required, last_reply: 请问预算？}]
    history: [{role: agent, thread: T1, text: 请问预算？}]
    input: 预算一万
    expect: {intent: supplement, thread_ref: T1}
  - name: 选择
    pending: {candidates: [trip_planner, hotel_booking], message: 去东京玩}
    input: 第二个
    expect: {intent: select_candidate, candidate_index: 2}
""",
        encoding="utf-8",
    )
    return path


class KeyedRecognizer:
    async def recognize(self, session, text, image_count=0):
        if text == "第二个":
            raise RuntimeError("timeout")
        ref = "T2" if text == "预算一万" else None  # wrong thread on purpose
        intent = "router_answer" if text == "你好" else "supplement"
        return IntentDecision(intent=intent, thread_ref=ref, candidate_index=None, search_query=None, reason="r")


async def test_results_are_scored_on_intent_and_target(tmp_path):
    cases = load_intent_cases(write_cases(tmp_path))
    results = await run_intent_eval(cases, KeyedRecognizer())
    report = render_intent_report(results, header_lines=["测试"])

    assert isinstance(cases[2].session.pending, PendingCandidates)
    assert [result.correct for result in results] == [True, False, False]
    assert results[2].error == "RuntimeError: timeout"
    assert "33.3%" in report and "## 错例" in report and "预算一万" in report
