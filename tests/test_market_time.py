from datetime import date, datetime, timezone

import pytest

from asx.ids.market_time import market_date


def test_pre_open_sydney_lodgement_is_previous_utc_day():
    # 08:30 AEST on 15 Aug 2026 is 22:30 UTC on 14 Aug: the market date is
    # the Sydney date (SPEC §3 two-clocks convention).
    lodged = datetime(2026, 8, 14, 22, 30, tzinfo=timezone.utc)
    assert market_date(lodged) == date(2026, 8, 15)


def test_afternoon_lodgement_same_day():
    lodged = datetime(2026, 8, 15, 4, 0, tzinfo=timezone.utc)  # 14:00 AEST
    assert market_date(lodged) == date(2026, 8, 15)


def test_naive_datetime_rejected():
    with pytest.raises(ValueError):
        market_date(datetime(2026, 8, 15, 4, 0))
