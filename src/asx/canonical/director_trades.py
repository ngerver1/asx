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
# "off-market" must never fall through to the on-market patterns. Every
# keyword is word-anchored: unanchored substrings coerce "investment",
# "divestment", "replacement" etc. into substantive categories (Invariant 8).
_RULES: list[tuple[str, re.Pattern]] = [
    ("offmkt_transfer", re.compile(r"off[\s-]?market", re.I)),
    ("margin_or_forced", re.compile(r"margin\s+(call|loan)|forced\s+(sale|sell)|lender\s+disposal", re.I)),
    ("buyback_into", re.compile(r"buy[\s-]?back", re.I)),
    ("exercise", re.compile(r"exercise\s+of\s+(unlisted\s+)?(options?|rights?)|options?\s+exercise[d]?|conversion\s+of\s+(options?|notes?|convertible)", re.I)),
    ("drp", re.compile(r"dividend\s+reinvestment|reinvestment\s+of\s+(dividends?|distributions?)|\bdrp\b", re.I)),
    ("spp_participation", re.compile(r"share\s+purchase\s+plan|\bspp\b", re.I)),
    ("placement_participation", re.compile(r"\bplacement\b", re.I)),
    ("rights_participation", re.compile(r"rights\s+issue|entitlement\s+offer|(non[\s-]?)?renounceable", re.I)),
    ("vesting_incentive", re.compile(
        r"\bvest(ing|ed)?\b|performance\s+(rights?|shares?)|incentive\s+plan|employee\s+share|"
        r"\besop\b|\bltip?\b|\bstip?\b|remuneration|director\s+fee", re.I)),
    ("onmarket", re.compile(r"\bon[\s-]?market\b", re.I)),
]

# "On market" wording that describes a PRICE REFERENCE or a transfer, not an
# on-market execution — common on off-market related-party transfers.
_ONMARKET_GUARD = re.compile(
    r"based\s+on\s+market|market\s+(value|price)|on[\s-]?market\s+terms|\btransfer\b", re.I
)
_NIL = re.compile(r"\bnil\b|no\s+consideration", re.I)

# A field that ENUMERATES several transactions cannot be described by one
# classification. Catalyst Metals (CYL, 6A1339259) printed:
#
#     1. Conversion of vested Performance Rights
#     2. On market trades
#
# against 534,188 acquired and 1,106,838 disposed. The rules match the first
# mechanism and return 'vesting_incentive' — true of one event, and it
# silently buries the other: a 1,000,000-share on-market sale for $6,410,050.
# Labelling the line at all asserts something the form does not say, so an
# enumerated field is 'unknown' and goes to review to be split into one row
# per transaction (Invariant 8).
#
# Anchored to line starts and list markers so a share count ("1,000,000") or
# a price ("$1.215") is never mistaken for an enumeration.
_ENUMERATED = re.compile(
    r"(?:(?<=\n)|^)\s*(?:\d{1,2}[.)]|[ivx]{1,4}[.)]|[a-h][.)])\s+", re.I | re.M)
# Cash evidence in the text itself: money symbols/words, not bare digits (a
# date or a share count is not a price).
_CASH_HINT = re.compile(r"\$|\baud\b|\bcash\b|per\s+(share|security|unit)", re.I)


def classify_trade(
    consideration_text: str | None,
    qty_acquired: Decimal | None,
    qty_disposed: Decimal | None,
    consideration_aud: Decimal | None = None,
) -> str:
    """Classify one 3Y securities line. Returns a taxonomy value from the
    director_trades.classification enum; ambiguity yields 'unknown'.

    consideration_aud is the form's separate value-of-consideration box: the
    nature box frequently reads just "On market purchase" with the dollar
    figure printed elsewhere, so a positive AUD figure counts as cash
    evidence alongside the text.
    """
    text = (consideration_text or "").strip()
    acquired = qty_acquired is not None and qty_acquired > 0
    disposed = qty_disposed is not None and qty_disposed > 0

    # Several transactions described in one field: no single label is honest.
    if len(_ENUMERATED.findall(text)) >= 2:
        return "unknown"

    for label, pattern in _RULES:
        if not pattern.search(text):
            continue
        if label != "onmarket":
            return label
        # On-market execution: price-reference/transfer wording and nil
        # consideration disqualify — a nil-consideration shuffle between the
        # director's own vehicles is not a trade at all (SPEC §7). Direction
        # comes from the quantities; both-sides or neither-side is ambiguous.
        if _ONMARKET_GUARD.search(text) or _NIL.search(text):
            return "unknown"
        if acquired and not disposed:
            has_cash = bool(_CASH_HINT.search(text)) or (
                consideration_aud is not None and consideration_aud > 0
            )
            return "onmkt_buy_cash" if has_cash else "unknown"
        if disposed and not acquired:
            return "onmkt_sell"
        return "unknown"

    # "Nil consideration" without any matched mechanism could be a vesting, a
    # transfer between the director's own vehicles, or a gift — unknown.
    return "unknown"


def find_or_create_person(conn: psycopg.Connection, raw_name: str) -> int:
    """Soft identity: one live persons row per normalised name (enforced by a
    partial unique index); merges are manual only (names collide and 3Y forms
    carry no DOB — SPEC §7). Concurrency-safe: two overlapping parses of the
    same director race through ON CONFLICT rather than creating duplicates
    that would silently break supersession grouping."""
    norm = person_name_norm(raw_name)
    with conn.cursor() as cur:
        cur.execute(
            """INSERT INTO persons (name_norm, display_name) VALUES (%s, %s)
               ON CONFLICT (name_norm) WHERE merged_into IS NULL DO NOTHING
               RETURNING person_id""",
            (norm, raw_name.strip()),
        )
        row = cur.fetchone()
        if row:
            return row["person_id"]
        cur.execute(
            """SELECT person_id FROM persons
               WHERE name_norm = %s AND merged_into IS NULL
               ORDER BY person_id LIMIT 1""",
            (norm,),
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


def _recompute_supersession(conn: psycopg.Connection, keys: set[tuple]) -> None:
    """Normalise supersession for each (entity_id, person_id, event_date,
    security_class) group: the latest-lodged document's rows are active and
    point their supersedes_doc at the previous document; every other
    document's rows are superseded. Recomputing from scratch makes
    supersession order-independent: documents can be applied, re-applied
    (reprocess), or retracted in any order and the latest lodgement always
    wins (SPEC §7's same-director-same-date heuristic).

    security_class is part of the key so two same-day notices covering
    DIFFERENT classes (shares in one, options in another) never falsely
    supersede each other; a replacement notice repeats the classes it amends.
    """
    with conn.cursor() as cur:
        for entity_id, person_id, event_date, security_class in keys:
            cur.execute(
                """SELECT DISTINCT doc_id, knowable_at FROM director_trades
                   WHERE entity_id = %s AND person_id = %s AND event_date = %s
                     AND security_class = %s
                   ORDER BY knowable_at DESC, doc_id DESC""",
                (entity_id, person_id, event_date, security_class),
            )
            docs = [r["doc_id"] for r in cur.fetchall()]
            if not docs:
                continue
            winner, rest = docs[0], docs[1:]
            cur.execute(
                """UPDATE director_trades
                   SET superseded = (doc_id <> %s),
                       supersedes_doc = CASE WHEN doc_id = %s THEN %s::bigint ELSE NULL END
                   WHERE entity_id = %s AND person_id = %s AND event_date = %s
                     AND security_class = %s""",
                (winner, winner, rest[0] if rest else None,
                 entity_id, person_id, event_date, security_class),
            )


def apply_trades(
    conn: psycopg.Connection,
    doc_id: int,
    rows: list[TradeRow],
    review_status: str = "auto",
) -> None:
    """Idempotent canonical write for one document. Amended-notice dedupe is a
    full supersession recompute over every (entity, director, event_date)
    group the document touches — latest lodgement wins regardless of the
    order documents reach canonical (SPEC §7)."""
    keys: set[tuple] = set()
    with conn.cursor() as cur:
        cur.execute(
            """SELECT DISTINCT entity_id, person_id, event_date, security_class
               FROM director_trades WHERE doc_id = %s""",
            (doc_id,),
        )
        keys |= {(r["entity_id"], r["person_id"], r["event_date"], r["security_class"])
                 for r in cur.fetchall()}
        cur.execute("DELETE FROM director_trades WHERE doc_id = %s", (doc_id,))
        for row in rows:
            person_id = find_or_create_person(conn, row.person_name_raw)
            classification = classify_trade(
                row.consideration_text, row.qty_acquired, row.qty_disposed,
                row.consideration_aud,
            )
            cur.execute(
                """INSERT INTO director_trades
                     (entity_id, person_name_raw, person_id, doc_id,
                      event_date, knowable_at, interest_nature, indirect_detail,
                      security_class, qty_acquired, qty_disposed,
                      consideration_text, consideration_aud, price_per_unit,
                      held_before, held_after, classification, confidence,
                      review_status)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                (row.entity_id, row.person_name_raw, person_id, doc_id,
                 row.event_date, row.knowable_at, row.interest_nature,
                 row.indirect_detail, row.security_class, row.qty_acquired,
                 row.qty_disposed, row.consideration_text, row.consideration_aud,
                 derive_price_per_unit(row), row.held_before, row.held_after,
                 classification, row.confidence, review_status),
            )
            keys.add((row.entity_id, person_id, row.event_date, row.security_class))
    _recompute_supersession(conn, keys)


def retract_trades(conn: psycopg.Connection, doc_id: int) -> None:
    """Remove a document's canonical rows (human 'rejected' resolution) and
    re-normalise supersession for the groups it touched, so a notice this doc
    had superseded becomes active again."""
    keys: set[tuple] = set()
    with conn.cursor() as cur:
        cur.execute(
            """SELECT DISTINCT entity_id, person_id, event_date, security_class
               FROM director_trades WHERE doc_id = %s""",
            (doc_id,),
        )
        keys = {(r["entity_id"], r["person_id"], r["event_date"], r["security_class"])
                for r in cur.fetchall()}
        cur.execute("DELETE FROM director_trades WHERE doc_id = %s", (doc_id,))
    _recompute_supersession(conn, keys)
