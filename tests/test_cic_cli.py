from cic_mcp_lab.cic.cli import threads_text, usage_text
from cic_mcp_lab.cic.ledger import Ledger, LedgerEntry
from cic_mcp_lab.cic.session import Session


def test_threads_list_shows_status_ids_and_the_last_reply():
    session = Session()
    trip = session.open_thread("trip_planner", "旅行规划", "provider-a")
    trip.status, trip.conversation_id, trip.last_reply = "input_required", "conv_1", "请问出发日期和预算？"
    report = session.open_thread("weekly_report", "周报撰写", "provider-b")
    report.task_id = "task_9"

    assert threads_text(session).splitlines() == [
        "T1 旅行规划 · provider-a · 等待补充 · conversation_id=conv_1",
        "  最后回复：请问出发日期和预算？",
        "T2 周报撰写 · provider-b · 处理中 · taskId=task_9",
    ]
    assert threads_text(Session()) == "（暂无会话）"


def test_usage_is_grouped_by_provider_then_agent():
    ledger = Ledger()
    for key, provider, agent, credit in [
        ("t1", "provider-a", "trip_planner", 100),
        ("t2", "provider-a", "trip_planner", 50),
        ("t3", "provider-b", "weekly_report", 20),
    ]:
        ledger.settle(LedgerEntry(key, provider, agent, "conv", credit, 0.0001))

    assert usage_text(ledger).splitlines() == [
        "provider-a：150 Credit ≈ HKD 0.0150",
        "  trip_planner：2 次 · 150 Credit ≈ HKD 0.0150",
        "provider-b：20 Credit ≈ HKD 0.0020",
        "  weekly_report：1 次 · 20 Credit ≈ HKD 0.0020",
        "合计：170 Credit ≈ HKD 0.0170",
    ]
    assert usage_text(Ledger()) == "（暂无计费记录）"
