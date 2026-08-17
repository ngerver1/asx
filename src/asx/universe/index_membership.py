"""S&P/ASX 300 membership proxy (ACCESS_DECISION §3, amendment 4).

With no price vendor subscribed, screens cannot apply a market-cap ceiling.
The substitute is index membership: "not a member of the S&P/ASX 300",
derived from an ETF issuer's published daily holdings file for a fund that
tracks that index (Vanguard's VAS is the usual choice), refreshed weekly.

**This is a proxy and must be labelled as one in any screen output.** It
differs from a true market-cap ceiling in ways worth stating plainly:
- Index membership is reviewed quarterly, so a company that has grown or
  shrunk sits in the wrong bucket until the next rebalance.
- Index inclusion also depends on liquidity and free float, not size alone.
- A recent listing can exceed the intended cap while awaiting inclusion.

Provenance (Invariant 12): every membership row records the source URL, the
issuer's publication date as knowable_at, and the as-at date of the file.

Invariant 1: holdings files list TICKERS. Each is resolved to entity_id
through the effective-dated listings table at load time; unresolvable tickers
are stored with a null entity_id and reported, never silently joined on code.
"""

from __future__ import annotations

import csv
import io
import re
from dataclasses import dataclass
from datetime import date, datetime

import psycopg

from asx.ingest.detection import entity_for_ticker

# Default proxy source. The issuer publishes this for investors; downloading
# it is within the site's terms, and we display/derive only — no
# redistribution (access decision §6).
DEFAULT_INDEX_CODE = "ASX300_PROXY_VAS"

_TICKER_COLUMN_HINTS = ("ticker", "code", "asx code", "security code", "symbol")


@dataclass
class MembershipLoad:
    index_code: str
    as_of: date
    total: int
    resolved: int
    unresolved: list[str]


def parse_holdings_csv(content: bytes) -> list[str]:
    """Extract ASX tickers from an issuer holdings CSV.

    Issuer files carry a variable preamble before the header row, so we scan
    for the first row containing a recognisable ticker column.
    """
    text = content.decode("utf-8-sig", errors="replace")
    rows = list(csv.reader(io.StringIO(text)))
    header_idx = None
    ticker_col = None
    for i, row in enumerate(rows):
        lowered = [c.strip().lower() for c in row]
        for hint in _TICKER_COLUMN_HINTS:
            if hint in lowered:
                header_idx, ticker_col = i, lowered.index(hint)
                break
        if header_idx is not None:
            break
    if header_idx is None:
        raise ValueError("no ticker column found in holdings file")

    tickers = []
    for row in rows[header_idx + 1:]:
        if len(row) <= ticker_col:
            continue
        raw = row[ticker_col].strip().upper()
        # Issuer files sometimes suffix the exchange, e.g. "BHP AU".
        m = re.match(r"^([A-Z0-9]{2,6})\b", raw)
        if m:
            tickers.append(m.group(1))
    return tickers


def load_membership(
    conn: psycopg.Connection,
    content: bytes,
    *,
    source_url: str,
    as_of: date,
    knowable_at: datetime,
    index_code: str = DEFAULT_INDEX_CODE,
    source_note: str | None = None,
) -> MembershipLoad:
    """Load one dated holdings snapshot. Idempotent per (index, ticker, date)."""
    tickers = parse_holdings_csv(content)
    resolved = 0
    unresolved: list[str] = []
    with conn.cursor() as cur:
        for ticker in tickers:
            entity_id = entity_for_ticker(conn, ticker, as_of)
            if entity_id is None:
                unresolved.append(ticker)
            else:
                resolved += 1
            cur.execute(
                """INSERT INTO index_membership
                     (index_code, entity_id, ticker_as_published, as_of,
                      knowable_at, source_url, source_note)
                   VALUES (%s, %s, %s, %s, %s, %s, %s)
                   ON CONFLICT (index_code, ticker_as_published, as_of)
                   DO UPDATE SET entity_id = EXCLUDED.entity_id""",
                (index_code, entity_id, ticker, as_of, knowable_at,
                 source_url, source_note),
            )
    conn.commit()
    return MembershipLoad(index_code, as_of, len(tickers), resolved, unresolved)


def is_index_member(
    conn: psycopg.Connection,
    entity_id: int,
    as_of: date,
    index_code: str = DEFAULT_INDEX_CODE,
    max_staleness_days: int = 14,
) -> bool | None:
    """Was this entity in the index as at a date? Returns None when no
    sufficiently fresh snapshot exists — unknown, never assumed 'no'.

    Bitemporal: only snapshots the issuer had published by `as_of` count, so
    backtests and historical screens cannot see a future rebalance.
    """
    with conn.cursor() as cur:
        cur.execute(
            """SELECT max(as_of) AS latest FROM index_membership
               WHERE index_code = %s AND as_of <= %s AND knowable_at <= %s""",
            (index_code, as_of, as_of),
        )
        latest = cur.fetchone()["latest"]
        if latest is None or (as_of - latest).days > max_staleness_days:
            return None
        cur.execute(
            """SELECT count(*) AS n FROM index_membership
               WHERE index_code = %s AND as_of = %s AND entity_id = %s""",
            (index_code, latest, entity_id),
        )
        return cur.fetchone()["n"] > 0
