from decimal import Decimal

from cic_mcp_lab.cic.ledger import Ledger, LedgerEntry


def entry(key, credit=42):
    return LedgerEntry(key, "provider-a", "trip_planner", "conv_1", credit, 0.0001)


def test_each_task_is_settled_once():
    ledger = Ledger()

    assert ledger.settle(entry("task_1"))
    assert not ledger.settle(entry("task_1", credit=99))
    assert ledger.settle(entry("task_2", credit=8))
    assert ledger.settle(LedgerEntry("task_1", "provider-b", "weekly_report", "conv_2", 1, 0.0001))
    assert ledger.total_credit() == 51
    assert ledger.total_hkd() == Decimal("0.0051")


def test_hkd_is_computed_exactly():
    assert entry("t", credit=1234).hkd == Decimal("0.1234")
