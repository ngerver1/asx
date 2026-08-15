"""Director-trade classification and canonical writes (SPEC §7).

Classification is the product: the raw 3Y feed has near-zero signal value; the
value is separating on-market cash purchases from everything else. Rules-first
on the consideration and nature text; per Invariant 8 anything ambiguous is
'unknown', never defaulted.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal

import psycopg

from asx.ids.normalize import person_name_norm

# Order matters: specific patterns must fire before generic ones, and
# "off-market" must never fall through to the on-market patterns.
_RULES: list[tuple[str, re.Pattern]] = [
    ("offmkt_transfer", re.compile(r"off[\s-]?market", re.I)),
    ("margin_or_forced", re.compile(r"margin\s+(call|loan)|forced\s+(sale|sell)|lender\s+disposal", re.I)),
    ("buyback_into", re.compile(r"buy[\s-]?back", re.I)),
    ("exercise", re.compile(r"exercise\s+of\s+(unlisted\s+)?(options?|rights?)|options?\s+exercise[d]?|conversion\s+of\s+(options?|notes?|convertible)", re.I)),
    ("drp", re.compile(r"dividend\s+reinvestment|\bdrp\b", re.I)),
    ("spp_participation", re.compile(r"share\s+purchase\s+plan|\bspp\b", re.I)),
    ("placement_participation", re.compile(r"placement", re.I)),
    ("rights_participation", re.compile(r"rights\s+issue|entitlement\s+offer|(non[\s-]?)?renounceable", re.I)),
    ("vesting_incentive", re.compile(
        r"vest(ing|ed)?|performance\s+(rights?|shares?)|incentive\s+plan|employee\s+share|"
        r"\besop\b|\bltip?\b|\bstip?\b|remuneration|director\s+fee", re.I)),
    ("onmarket", re.compile(r"on[\s-]?market", re.I)),
]

_CASH_HINT = re.compile(r"\$|\baud\b|cash|per\s+(share|security|unit)|\d", re.I)


def classify_trade(
    consideration_text: str | None,
    qty_acquired: Decimal | None,
    qty_disposed: Decimal | None,
) -> str:
    """Classify one 3Y securities line. Returns a taxonomy value from the
    director_trades.classification enum; ambiguity yields 'unknown'."""
    text = (consideration_text or "").strip()
    acquired = qty_acquired is not None and qty_acquired > 0
    disposed = qty_disposed is not None and qty_disposed > 0

    for label, pattern in _RULES:
        if not pattern.search(text):
            continue
        if label != "onmarket":
            return label
        # On-market: direction comes from the quantities, and a buy only
        # counts as the cash-buy signal class when the consideration reads as
        # cash. Both-sides or neither-side is ambiguous.
        if acquired and not disposed:
            return "onmkt_buy_cash" if _CASH_HINT.search(text) else "unknown"
        if disposed and not acquired:
            return "onmkt_sell"
        return "unknown"

    # "Nil consideration" without any matched mechanism could be a vesting, a
    # transfer between the director's own vehicles, or a gift — unknown.
    return "unknown"


def find_or_create_person(conn: psycopg.Connection, raw_name: str) -> int:
    """Soft identity: one persons row per normalised name; merges are manual
    only (names collide and 3Y forms carry no DOB — SPEC §7)."""
    norm = person_name_norm(raw_name)
    with conn.cursor() as cur:
        cur.execute(
            """SELECT person_id, merged_into FROM persons
               WHERE name_norm = %s ORDER BY person_id LIMIT 1""",
            (norm,),
        )
        row = cur.fetchone()
        if row:
            return row["merged_into"] or row["person_id"]
        cur.execute(
            "INSERT INTO persons (name_norm, display_name) VALUES (%s, %s) RETURNING person_id",
            (norm, raw_name.strip()),
        )
        return cur.fetchone()["person_id"]


@dataclass
class TradeRow:
    entity_id: int
    person_name_raw: str
    doc_id: int
    event_date: object
    knowable_at: object
    security_class: str
    interest_nature: str | None = None
    indirect_detail: str | None = None
    qty_acquired: Decimal | None = None
    qty_disposed: Decimal | None = None
    consideration_text: str | None = None
    consideration_aud: Decimal | None = None
    held_before: Decimal | None = None
    held_after: Decimal | None = None
    confidence: float = 1.0
    review_status: str = "auto"


def derive_price_per_unit(row: TradeRow) -> Decimal | None:
    """Only when the form safely supports it: cash consideration plus exactly
    one non-zero quantity side. Anything else stays null (SPEC §7)."""
    if row.consideration_aud is None or row.consideration_aud <= 0:
        return None
    acquired = row.qty_acquired or Decimal(0)
    disposed = row.qty_disposed or Decimal(0)
    if (acquired > 0) == (disposed > 0):  # both or neither
        return None
    qty = acquired if acquired > 0 else disposed
    return row.consideration_aud / qty


def apply_trades(conn: psycopg.Connection, doc_id: int, rows: list[TradeRow]) -> None:
    """Idempotent canonical write for one document, with amended-notice
    supersession: earlier non-superseded trades for the same entity + director
    + event_date from a different document are marked superseded — latest
    lodgement wins (SPEC §7)."""
    with conn.cursor() as cur:
        cur.execute("DELETE FROM director_trades WHERE doc_id = %s", (doc_id,))
        for row in rows:
            person_id = find_or_create_person(conn, row.person_name_raw)
            classification = classify_trade(
                row.consideration_text, row.qty_acquired, row.qty_disposed
            )
            # Supersession heuristic: same entity, same director norm, same
            # event_date, earlier lodgement, different doc.
            cur.execute(
                """UPDATE director_trades t
                   SET superseded = true
                   WHERE t.entity_id = %s AND t.person_id = %s
                     AND t.event_date = %s AND t.doc_id <> %s
                     AND t.knowable_at <= %s AND NOT t.superseded
                   RETURNING t.doc_id""",
                (row.entity_id, person_id, row.event_date, doc_id, row.knowable_at),
            )
            superseded_docs = [r["doc_id"] for r in cur.fetchall()]
            supersedes_doc = superseded_docs[0] if superseded_docs else None

            cur.execute(
                """INSERT INTO director_trades
                     (entity_id, person_name_raw, person_id, doc_id, supersedes_doc,
                      event_date, knowable_at, interest_nature, indirect_detail,
                      security_class, qty_acquired, qty_disposed,
                      consideration_text, consideration_aud, price_per_unit,
                      held_before, held_after, classification, confidence,
                      review_status)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                (row.entity_id, row.person_name_raw, person_id, doc_id, supersedes_doc,
                 row.event_date, row.knowable_at, row.interest_nature,
                 row.indirect_detail, row.security_class, row.qty_acquired,
                 row.qty_disposed, row.consideration_text, row.consideration_aud,
                 derive_price_per_unit(row), row.held_before, row.held_after,
                 classification, row.confidence, row.review_status),
            )
