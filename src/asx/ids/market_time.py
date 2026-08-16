"""Market-time convention (SPEC §3, two clocks): timestamps are stored in
UTC; ASX lodgements are interpreted in Australia/Sydney. Any code that needs
the *calendar date* of a lodgement must go through market_date() — taking
.date() of a UTC timestamp shifts pre-open lodgements to the previous day.
"""

from __future__ import annotations

from datetime import date, datetime
from zoneinfo import ZoneInfo

SYDNEY = ZoneInfo("Australia/Sydney")


def market_date(ts: datetime) -> date:
    """The Australia/Sydney calendar date of an aware timestamp."""
    if ts.tzinfo is None:
        raise ValueError("market_date requires an aware datetime")
    return ts.astimezone(SYDNEY).date()
