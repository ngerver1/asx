"""Point-in-time export of the tracked universe (SPEC §5.4, Invariant 4).

Answers "which codes were in scope on date D", not "which codes are in scope
now". The distinction is the whole point of effective-dating the listing and
membership tables: a universe rebuilt from today's file would quietly drop
every company that has since delisted, which is the single largest source of
overstated small-cap returns.

`valid_from`/`valid_to` and `listed_from`/`listed_to` are INCLUSIVE, so the
as-at predicate is `from <= D <= coalesce(to, infinity)`.

Two name columns, deliberately:

- `company_name` is the name in force on the as-at date. It is BLANK when the
  source cannot pin it down — ASIC dates only the transition to a company's
  current name, so a company with several former names has them all sharing
  one over-approximated range. Picking one of those would be a guess dressed
  as a fact (Invariant 8); for Ampol at 1990 the honest answer is that it was
  one of CALTEX AUSTRALIA, CALTEX SECURITIES (AUSTRALIA) or CALIFORNIA
  ASPHALT PRODUCTS, and the register does not say which.
- `current_company_name` is always populated, so every row stays identifiable
  by a human regardless of the as-at date.
"""

from __future__ import annotations

import csv
import io
from dataclasses import dataclass
from datetime import date

import psycopg

COLUMNS = [
    "ticker", "company_name", "current_company_name", "entity_id",
    "entity_kind", "acn", "arbn",
    "identity", "gics_industry_group", "listing_date", "market_cap_aud",
    "market_cap_rank", "listed_from", "listed_to", "ticker_valid_from",
    "ticker_valid_to",
]

_SQL = """
SELECT l.ticker,
       n.name                        AS company_name,
       cur.name                      AS current_company_name,
       l.entity_id,
       e.entity_kind,
       trim(e.acn)                   AS acn,
       trim(e.arbn)                  AS arbn,
       CASE WHEN e.acn IS NOT NULL OR e.arbn IS NOT NULL
            THEN 'resolved' ELSE 'in_review' END AS identity,
       s.sector                      AS gics_industry_group,
       s.listing_date,
       s.market_cap_aud,
       -- Rank among issuers with a published cap; NULL where the ASX
       -- prints '--'. FILTER is not available on window functions, and
       -- NULLS LAST keeps the unpublished ones off the front of the order.
       CASE WHEN s.market_cap_aud IS NOT NULL
            THEN rank() OVER (ORDER BY s.market_cap_aud DESC NULLS LAST)
       END                           AS market_cap_rank,
       u.listed_from,
       u.listed_to,
       l.valid_from                  AS ticker_valid_from,
       l.valid_to                    AS ticker_valid_to
FROM listings l
JOIN entities e ON e.entity_id = l.entity_id
-- The name as it stood on the as-at date. The legal name in force wins; if
-- none was, a single former name in force is unambiguous and is used; several
-- overlapping former names mean the register cannot date the name, and the
-- column is left empty rather than guessed.
LEFT JOIN LATERAL (
  SELECT CASE WHEN count(*) FILTER (WHERE name_kind = 'legal') = 1
              THEN max(name) FILTER (WHERE name_kind = 'legal')
              WHEN count(*) = 1 THEN max(name) END AS name
    FROM entity_names
   WHERE entity_id = l.entity_id
     AND valid_from <= %(as_at)s
     AND (valid_to IS NULL OR valid_to >= %(as_at)s)
) n ON true
LEFT JOIN LATERAL (
  SELECT name FROM entity_names
   WHERE entity_id = l.entity_id AND name_kind = 'legal' AND valid_to IS NULL
   ORDER BY valid_from DESC LIMIT 1
) cur ON true
-- Most recent published snapshot at or before the as-at date. Market cap is
-- point-in-time with no history, so an older snapshot is the honest answer
-- and a newer one would be lookahead.
LEFT JOIN LATERAL (
  SELECT sector, listing_date, market_cap_aud FROM listing_snapshots
   WHERE entity_id = l.entity_id AND ticker = l.ticker AND as_at <= %(as_at)s
   ORDER BY as_at DESC LIMIT 1
) s ON true
LEFT JOIN LATERAL (
  SELECT listed_from, listed_to FROM universe_membership
   WHERE entity_id = l.entity_id AND listed_from <= %(as_at)s
   ORDER BY listed_from DESC LIMIT 1
) u ON true
WHERE l.exchange = 'ASX'
  AND l.valid_from <= %(as_at)s
  AND (l.valid_to IS NULL OR l.valid_to >= %(as_at)s)
ORDER BY l.ticker
"""


@dataclass
class SizeFilter:
    """A size cut, plus what it had to leave out.

    `unknown_cap` is reported, never silently dropped: the ASX publishes '--'
    for some issuers, and an unknown market cap is not a small one. Excluding
    them from a small-cap screen is correct; doing it quietly would let a
    screen claim coverage it does not have (Invariant 8, Invariant 13).
    """
    max_market_cap: float | None = None
    exclude_top: int | None = None
    kept: int = 0
    excluded_large: int = 0
    excluded_unknown_cap: int = 0

    @property
    def active(self) -> bool:
        return self.max_market_cap is not None or self.exclude_top is not None

    def apply(self, rows: list[dict]) -> list[dict]:
        if not self.active:
            self.kept = len(rows)
            return rows
        out = []
        for r in rows:
            if r["market_cap_aud"] is None:
                self.excluded_unknown_cap += 1
                continue
            too_big = (
                (self.max_market_cap is not None
                 and float(r["market_cap_aud"]) > self.max_market_cap)
                or (self.exclude_top is not None
                    and r["market_cap_rank"] is not None
                    and r["market_cap_rank"] <= self.exclude_top)
            )
            if too_big:
                self.excluded_large += 1
            else:
                out.append(r)
        self.kept = len(out)
        return out

    def note(self) -> str:
        if not self.active:
            return f"{self.kept} listings"
        bits = [f"{self.kept} listings"]
        if self.exclude_top:
            bits.append(f"outside the top {self.exclude_top} by market cap")
        if self.max_market_cap:
            bits.append(f"at or below ${self.max_market_cap:,.0f}")
        tail = (f" ({self.excluded_large} excluded as too large; "
                f"{self.excluded_unknown_cap} excluded because the ASX "
                f"publishes no market cap for them)")
        return " ".join(bits) + tail


def universe_rows(conn: psycopg.Connection, as_at: date) -> list[dict]:
    with conn.cursor() as cur:
        cur.execute(_SQL, {"as_at": as_at})
        return cur.fetchall()


def universe_csv(conn: psycopg.Connection, as_at: date,
                 size: SizeFilter | None = None) -> str:
    rows = universe_rows(conn, as_at)
    if size is not None:
        rows = size.apply(rows)
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=COLUMNS, lineterminator="\n")
    writer.writeheader()
    for row in rows:
        writer.writerow({c: ("" if row[c] is None else row[c]) for c in COLUMNS})
    return buf.getvalue()
