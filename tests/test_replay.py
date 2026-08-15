from datetime import date, datetime, timezone
from decimal import Decimal

from asx.canonical.shares import ShareEvent, replay

D = Decimal


def _dt(y, m, d):
    return datetime(y, m, d, tzinfo=timezone.utc)


def test_delta_events_accumulate():
    events = [
        ShareEvent("quotation", date(2025, 2, 1), qty_delta=D(1000), knowable_at=_dt(2025, 2, 1)),
        ShareEvent("buyback_cancel", date(2025, 3, 1), qty_delta=D(-200), knowable_at=_dt(2025, 3, 1)),
    ]
    assert replay(D(10000), date(2025, 1, 1), events, date(2025, 12, 31)) == D(10800)


def test_consolidation_applies_ratio():
    # 1:10 consolidation — routine in ASX small caps (Invariant 5).
    events = [
        ShareEvent("consolidation", date(2025, 6, 1), ratio_num=D(1), ratio_den=D(10),
                   knowable_at=_dt(2025, 6, 1)),
    ]
    assert replay(D(100_000_000), date(2025, 1, 1), events, date(2025, 12, 31)) == D(10_000_000)


def test_split_applies_ratio():
    events = [
        ShareEvent("split", date(2025, 6, 1), ratio_num=D(2), ratio_den=D(1),
                   knowable_at=_dt(2025, 6, 1)),
    ]
    assert replay(D(500), date(2025, 1, 1), events, date(2025, 12, 31)) == D(1000)


def test_event_order_matters():
    # Issue then consolidate != consolidate then issue.
    issue_then_consolidate = [
        ShareEvent("quotation", date(2025, 2, 1), qty_delta=D(10_000_000), knowable_at=_dt(2025, 2, 1)),
        ShareEvent("consolidation", date(2025, 6, 1), ratio_num=D(1), ratio_den=D(10),
                   knowable_at=_dt(2025, 6, 1)),
    ]
    assert replay(D(90_000_000), date(2025, 1, 1), issue_then_consolidate,
                  date(2025, 12, 31)) == D(10_000_000)


def test_as_of_cuts_future_events():
    events = [
        ShareEvent("quotation", date(2025, 2, 1), qty_delta=D(1000), knowable_at=_dt(2025, 2, 1)),
        ShareEvent("quotation", date(2025, 8, 1), qty_delta=D(1000), knowable_at=_dt(2025, 8, 1)),
    ]
    assert replay(D(0), date(2025, 1, 1), events, date(2025, 6, 30)) == D(1000)


def test_bitemporal_as_known_at_excludes_late_knowledge():
    # Invariant 2: an event that happened in February but was only lodged in
    # September must not appear in a July-dated view of the world.
    events = [
        ShareEvent("quotation", date(2025, 2, 1), qty_delta=D(1000),
                   knowable_at=_dt(2025, 9, 15)),  # late lodgement
    ]
    known_in_july = replay(D(0), date(2025, 1, 1), events, date(2025, 6, 30),
                           as_known_at=_dt(2025, 7, 1))
    assert known_in_july == D(0)
    known_today = replay(D(0), date(2025, 1, 1), events, date(2025, 6, 30))
    assert known_today == D(1000)


def test_proposed_issues_never_count():
    # Appendix 3B proposals anticipate; Appendix 2A quotation counts (SPEC §5.4).
    events = [
        ShareEvent("issue_proposed", date(2025, 2, 1), qty_delta=D(5000), knowable_at=_dt(2025, 2, 1)),
    ]
    assert replay(D(100), date(2025, 1, 1), events, date(2025, 12, 31)) == D(100)
