"""Verification of the entity master against Phase 0 acceptance criteria
(SPEC §5.5, as amended by the Tier 0 access decision).

These are measurements, not tests: they run against live data and produce the
written evidence the acceptance ledger requires.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import psycopg


@dataclass
class AcnCoverage:
    """Criterion 0.2: >=99% of tracked entities have a resolved ACN or an
    explicit foreign flag."""
    total: int
    with_acn: int
    flagged_foreign: int
    unresolved: int

    @property
    def covered(self) -> int:
        return self.with_acn + self.flagged_foreign

    @property
    def rate(self) -> float:
        return self.covered / self.total if self.total else 0.0

    @property
    def meets_criterion(self) -> bool:
        return self.total > 0 and self.rate >= 0.99


def acn_coverage(conn: psycopg.Connection) -> AcnCoverage:
    with conn.cursor() as cur:
        cur.execute(
            """SELECT count(*) AS total,
                      count(*) FILTER (WHERE acn IS NOT NULL) AS with_acn,
                      count(*) FILTER (WHERE acn IS NULL AND entity_kind = 'foreign')
                        AS foreign_flagged,
                      count(*) FILTER (WHERE acn IS NULL AND entity_kind <> 'foreign')
                        AS unresolved
               FROM entities
               WHERE EXISTS (SELECT 1 FROM listings l WHERE l.entity_id = entities.entity_id)"""
        )
        row = cur.fetchone()
    return AcnCoverage(row["total"], row["with_acn"], row["foreign_flagged"],
                       row["unresolved"])


@dataclass
class TickerIntegrity:
    """Criterion 0.3: every symbol maps to exactly one entity per date, with
    zero unexamined many-to-one collisions."""
    open_listings: int
    collisions: list[dict] = field(default_factory=list)

    @property
    def meets_criterion(self) -> bool:
        return not self.collisions


def ticker_integrity(conn: psycopg.Connection) -> TickerIntegrity:
    """Find tickers whose validity periods overlap across different entities.

    A recycled code is legitimate *sequentially* — that is precisely why
    Invariant 1 exists — but two entities holding the same code over
    overlapping dates means a listing was never closed, and every join through
    that period is suspect.
    """
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) AS n FROM listings WHERE valid_to IS NULL")
        open_count = cur.fetchone()["n"]
        cur.execute(
            """SELECT a.exchange, a.ticker, a.entity_id AS entity_a,
                      b.entity_id AS entity_b, a.valid_from AS a_from,
                      a.valid_to AS a_to, b.valid_from AS b_from, b.valid_to AS b_to
               FROM listings a
               JOIN listings b
                 ON a.exchange = b.exchange AND a.ticker = b.ticker
                AND a.security_class = b.security_class
                AND a.entity_id < b.entity_id
                AND a.valid_from <= coalesce(b.valid_to, DATE '9999-12-31')
                AND b.valid_from <= coalesce(a.valid_to, DATE '9999-12-31')
               ORDER BY a.ticker"""
        )
        collisions = cur.fetchall()
    return TickerIntegrity(open_count, collisions)


# Triage hints for the unresolved queue. These are *suggestions for a human*,
# never an automatic classification: entity_kind='foreign' is a claim about a
# company's place of incorporation, and Invariant 8 forbids inferring it from
# a suffix. A company called "... Group PLC" is very likely foreign; the
# platform still requires someone to say so.
_TRIAGE_HINTS: list[tuple[str, str]] = [
    ("likely foreign-incorporated (suffix suggests a non-Australian body)",
     r"\m(PLC|INC|CORP|CORPORATION|NV|SA|AG|GMBH|LLC|SE|SPA|BHD|PTE)\M"),
    ("likely a trust / scheme (registered with an ARSN, not an ACN)",
     r"\m(TRUST|FUND|REIT|ETF|PROPERTY GROUP|INCOME|YIELD)\M"),
]


def unresolved_breakdown(conn: psycopg.Connection) -> list[dict]:
    """Group entities that still lack an ACN by a triage hint, so the review
    queue can be worked in batches rather than one row at a time."""
    with conn.cursor() as cur:
        cur.execute(
            """WITH unresolved AS (
                 SELECT e.entity_id,
                        (SELECT l.ticker FROM listings l
                          WHERE l.entity_id = e.entity_id AND l.valid_to IS NULL
                          ORDER BY l.ticker LIMIT 1) AS ticker,
                        (SELECT n.name FROM entity_names n
                          WHERE n.entity_id = e.entity_id AND n.valid_to IS NULL
                          ORDER BY n.valid_from DESC LIMIT 1) AS name
                 FROM entities e
                 WHERE e.acn IS NULL AND e.entity_kind <> 'foreign'
                   AND EXISTS (SELECT 1 FROM listings l
                                WHERE l.entity_id = e.entity_id AND l.valid_to IS NULL)
               )
               SELECT CASE
                        WHEN upper(name) ~ %s THEN %s
                        WHEN upper(name) ~ %s THEN %s
                        ELSE 'name does not match any ASIC registration exactly'
                      END AS hint,
                      count(*) AS n,
                      (array_agg(ticker ORDER BY ticker))[1:5] AS examples
               FROM unresolved GROUP BY 1 ORDER BY 2 DESC""",
            (_TRIAGE_HINTS[0][1], _TRIAGE_HINTS[0][0],
             _TRIAGE_HINTS[1][1], _TRIAGE_HINTS[1][0]),
        )
        return cur.fetchall()


def coverage_report(conn: psycopg.Connection) -> str:
    acn = acn_coverage(conn)
    tickers = ticker_integrity(conn)
    lines = [
        "Entity master coverage (Phase 0 acceptance evidence)",
        "",
        f"0.2  ACN coverage: {acn.covered}/{acn.total} ({acn.rate:.1%}) "
        f"{'PASS' if acn.meets_criterion else 'FAIL'} (target >=99%)",
        f"       with ACN:        {acn.with_acn}",
        f"       flagged foreign: {acn.flagged_foreign}",
        f"       unresolved:      {acn.unresolved}  <- each needs a review decision",
        "",
        f"0.3  Ticker integrity: {len(tickers.collisions)} overlapping-code collisions "
        f"{'PASS' if tickers.meets_criterion else 'FAIL'} (target 0)",
        f"       open listings:   {tickers.open_listings}",
    ]
    for c in tickers.collisions[:20]:
        lines.append(
            f"       {c['ticker']}: entities {c['entity_a']} ({c['a_from']}..{c['a_to']}) "
            f"and {c['entity_b']} ({c['b_from']}..{c['b_to']})"
        )
    if acn.unresolved:
        lines += ["", "Unresolved queue, grouped for triage (hints only — nobody",
                  "is marked foreign without a human saying so):"]
        for row in unresolved_breakdown(conn):
            examples = ", ".join(t for t in (row["examples"] or []) if t)
            lines.append(f"       {row['n']:>4}  {row['hint']}"
                         + (f"  e.g. {examples}" if examples else ""))
    return "\n".join(lines)
