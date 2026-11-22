from cic_mcp_lab.cic.intent import IntentDecision, build_intent_prompt
from cic_mcp_lab.cic.session import Candidate, PendingCandidates, PendingRetry, Session, Turn


def test_threads_are_numbered_and_found_by_submission():
    session = Session()
    first = session.open_thread("trip_planner", "旅行规划", "provider-a")
    second = session.open_thread("weekly_report", "周报撰写", "provider-b")
    second.submission_id = "sub_2"

    assert (first.ref, second.ref, first.status) == ("T1", "T2", "running")
    assert session.thread_for_submission("sub_2") is second
    assert session.thread_for_submission("sub_x") is None


def test_prompt_shows_history_threads_candidates_and_the_new_input():
    session = Session()
    session.history += [Turn("user", "帮我规划去东京的旅行"), Turn("agent", "请问出发日期和预算？" + "很长" * 300, "T1", "旅行规划 · T1")]
    thread = session.open_thread("trip_planner", "旅行规划", "provider-a")
    thread.status, thread.last_reply = "input_required", "请问出发日期和预算？"
    session.pending = PendingCandidates(
        "帮我写周报", (), (Candidate("weekly_report", "周报撰写", "provider-b", 0.8, "画像"),)
    )

    prompt = build_intent_prompt(session, "用第一个", image_count=1)

    assert "用户：帮我规划去东京的旅行" in prompt
    assert "[旅行规划 · T1]：请问出发日期和预算？" in prompt
    assert "很长" * 200 not in prompt  # long turns are truncated
    assert "T1 旅行规划：等待补充" in prompt
    assert "[1] 周报撰写" in prompt
    assert "用第一个（附带 1 张图片）" in prompt


def test_prompt_marks_empty_sections_and_retry_options():
    session = Session()
    thread = session.open_thread("weekly_report", "周报撰写", "provider-b")
    thread.status = "failed"
    session.pending = PendingRetry("T1", "帮我写周报", ())

    prompt = build_intent_prompt(Session(), "你好")
    retry = build_intent_prompt(session, "重试")

    assert prompt.count("（无）") == 3
    assert "[1] 重试" in retry and "[2] 重新推荐其他 Agent" in retry and "周报撰写（T1）调用失败" in retry


def test_intent_decision_fields_are_all_required_for_strict_schema():
    schema = IntentDecision.model_json_schema()

    assert set(schema["required"]) == {"intent", "thread_ref", "candidate_index", "search_query", "reason"}
