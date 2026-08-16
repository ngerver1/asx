"""Share-count history: event recording and bitemporal replay (SPEC §5.4).

Invariant 5: counts are replayed from share_events off an anchored opening
balance — never stored and overwritten. The Python replay mirrors the SQL
function shares_outstanding() exactly; tests hold the two implementations
together.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal

import psycopg

RATIO_KINDS = {"consolidation", "split"}
# Appendix 2A is the authoritative "these shares now trade" event; 3B records
# proposals used for anticipation, not counts (SPEC §5.4).
NON_COUNTING_KINDS = {"issue_proposed"}


@dataclass
class ShareEvent:
    event_kind: str
    event_date: date
    knowable_at: datetime
    qty_delta: Decimal | None = None
    ratio_num: Decimal | None = None
    ratio_den: Decimal | None = None
    # Mirrors share_events.event_id: the same-date tie-break. Ordering within
    # a date is arithmetically load-bearing when a delta and a ratio event
    # share a date (issue-then-consolidate != consolidate-then-issue).
    sequence: int = 0


def replay(
    anchor_qty: Decimal,
    anchor_date: date,
    events: list[ShareEvent],
    as_of: date,
    as_known_at: datetime | None = None,
) -> Decimal:
    """Pure replay used by tests and reconciliation; mirrors the SQL function,
    including its (event_date, event_id) ordering.

    Anchor semantics (both implementations): the anchor quantity is the count
    as at the END of anchor_date — events dated exactly on the anchor date are
    already inside the anchored figure and are not re-applied.
    """
    qty = anchor_qty
    applicable = [
        e for e in events
        if anchor_date < e.event_date <= as_of
        and e.event_kind not in NON_COUNTING_KINDS
        and (as_known_at is None or e.knowable_at <= as_known_at)
    ]
    for e in sorted(applicable, key=lambda e: (e.event_date, e.sequence)):
        if e.event_kind in RATIO_KINDS:
            qty = qty * e.ratio_num / e.ratio_den
        elif e.qty_delta is not None:
            qty = qty + e.qty_delta
    return qty


def record_anchor(
    conn: psycopg.Connection,
    entity_id: int,
    class_code: str,
    anchor_date: date,
    qty: Decimal,
    knowable_at: datetime,
    source_doc_id: int | None,
    source: str = "document",
    note: str | None = None,
) -> None:
    """Record an anchored opening balance (count as at END of anchor_date).

    Idempotent for identical reruns; a rerun carrying a DIFFERENT qty for the
    same (entity, class, date) raises instead of being silently ignored — a
    corrected opening balance must land, not vanish (Invariant 3 requires the
    fix to actually take when a parse error is reprocessed).
    Invariant 12: non-document anchors must say where they came from
    (source='vendor'|'manual' plus a note).
    """
    with conn.cursor() as cur:
        cur.execute(
            """INSERT INTO share_anchors
                 (entity_id, class_code, anchor_date, qty, knowable_at,
                  source_doc_id, source, note)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
               ON CONFLICT (entity_id, class_code, anchor_date) DO NOTHING
               RETURNING anchor_id""",
            (entity_id, class_code, anchor_date, qty, knowable_at,
             source_doc_id, source, note),
        )
        if cur.fetchone() is not None:
            return
        cur.execute(
            """SELECT qty FROM share_anchors
               WHERE entity_id = %s AND class_code = %s AND anchor_date = %s""",
            (entity_id, class_code, anchor_date),
        )
        existing = cur.fetchone()["qty"]
        if existing != qty:
            raise ValueError(
                f"anchor conflict for entity {entity_id} {class_code} "
                f"{anchor_date}: stored {existing}, incoming {qty} — correct "
                f"the stored anchor explicitly rather than re-inserting"
            )


def record_event(
    conn: psycopg.Connection,
    entity_id: int,
    class_code: str,
    event_kind: str,
    event_date: date,
    knowable_at: datetime,
    source_doc_id: int,
    qty_delta: Decimal | None = None,
    ratio_num: Decimal | None = None,
    ratio_den: Decimal | None = None,
) -> int:
    with conn.cursor() as cur:
        cur.execute(
            """INSERT INTO share_events
                 (entity_id, class_code, event_kind, event_date, knowable_at,
                  qty_delta, ratio_num, ratio_den, source_doc_id)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
               RETURNING event_id""",
            (entity_id, class_code, event_kind, event_date, knowable_at,
             qty_delta, ratio_num, ratio_den, source_doc_id),
        )
        return cur.fetchone()["event_id"]


def shares_outstanding_sql(
    conn: psycopg.Connection,
    entity_id: int,
    class_code: str,
    as_of: date,
    as_known_at: datetime | None = None,
) -> Decimal | None:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT shares_outstanding(%s, %s, %s, %s) AS qty",
            (entity_id, class_code, as_of, as_known_at),
        )
        return cur.fetchone()["qty"]


def reconcile_entity(
    conn: psycopg.Connection,
    entity_id: int,
    class_code: str,
    as_of: date,
    vendor_qty: Decimal | None,
    report_qty: Decimal | None = None,
    tolerance: Decimal = Decimal("0.005"),
) -> bool | None:
    """Weekly reconciliation (SPEC §5.4): replayed count vs vendor figure.
    Returns True/False for a completed comparison, None when vendor data was
    unavailable (unknown, not passing).

    Every downstream percentage divides by this number, so misses open review
    items rather than being logged and forgotten.
    """
    replayed = shares_outstanding_sql(conn, entity_id, class_code, as_of)
    rel_diff = None
    within = None
    if replayed is not None and vendor_qty is not None:
        # Explicit None check, not truthiness: vendor 0 vs replayed >0 is a
        # maximal discrepancy, not missing data.
        if vendor_qty == 0:
            within = replayed == 0
            rel_diff = None if within else Decimal(1)
        else:
            rel_diff = abs(replayed - vendor_qty) / vendor_qty
            within = rel_diff <= tolerance
    with conn.cursor() as cur:
        cur.execute(
            """INSERT INTO share_reconciliations
                 (entity_id, class_code, as_of, replayed_qty, vendor_qty,
                  report_qty, rel_diff, within_tolerance)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s)""",
            (entity_id, class_code, as_of, replayed, vendor_qty, report_qty,
             rel_diff, within),
        )
        if within is False or replayed is None:
            import json
            cur.execute(
                """INSERT INTO review_items (kind, payload, reason)
                   VALUES ('reconciliation', %s, %s)""",
                (json.dumps({
                    "entity_id": entity_id, "class_code": class_code,
                    "as_of": as_of.isoformat(),
                    "replayed": str(replayed) if replayed is not None else None,
                    "vendor": str(vendor_qty) if vendor_qty is not None else None,
                }),
                 "share-count replay differs from vendor beyond tolerance"
                 if within is False else "share-count replay undefined (no anchor)"),
            )
    return within
