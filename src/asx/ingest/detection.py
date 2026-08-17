"""Detection: recording that an announcement exists, before (or without)
holding the document itself.

Under the Tier 0 access decision, alert emails tell us an announcement was
lodged; the bytes arrive later from a company IR site or human-triggered
capture — or never. Detection rows make that gap *visible*: an announcement
detected and never captured is an alarmable hole, not a silent absence. Under
a paid feed the two facts coincide; here they must not be conflated.

knowable_at semantics are unchanged (SPEC §3): the ASX release timestamp is
when the market could know. detected_at is when *we* found out, which is
later, and is an operational figure only — it never feeds analytics.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import datetime, timezone

import psycopg

from asx.ids.market_time import market_date
from asx.ingest.classifier import classify


@dataclass
class Detection:
    """An announcement we know exists but may not yet hold."""
    detection_source: str          # market_index_alert | ir_email | ...
    source_ref: str                # email Message-ID, alert URL, etc.
    ticker: str | None = None
    title: str | None = None
    lodged_at: datetime | None = None       # ASX release timestamp
    detected_at: datetime | None = None     # when the alert reached us
    price_sensitive: bool | None = None
    document_urls: list[str] = field(default_factory=list)
    asx_doc_types: list[str] = field(default_factory=list)

    def key(self) -> str:
        """Stable identity for idempotent mailbox re-reads."""
        basis = f"{self.detection_source}|{self.source_ref}|{self.ticker or ''}|{self.title or ''}"
        return hashlib.sha256(basis.encode()).hexdigest()


def entity_for_ticker(conn: psycopg.Connection, ticker: str, on_date) -> int | None:
    """Ticker -> entity via the effective-dated listings table. The ticker is
    a lookup input only; the result is pinned to entity_id (Invariant 1)."""
    with conn.cursor() as cur:
        cur.execute(
            """SELECT DISTINCT entity_id FROM listings
               WHERE exchange = 'ASX' AND ticker = %s
                 AND valid_from <= %s AND (valid_to IS NULL OR valid_to >= %s)""",
            (ticker.upper(), on_date, on_date),
        )
        rows = cur.fetchall()
    return rows[0]["entity_id"] if len(rows) == 1 else None


def record_detection(
    conn: psycopg.Connection,
    detection: Detection,
    llm_classifier=None,
) -> tuple[int, bool]:
    """Insert a 'detected' document row. Returns (doc_id, is_new).

    Idempotent on detection_key so re-reading the mailbox is always safe.
    """
    from asx.parse.registry import parseable_doc_classes

    detected_at = detection.detected_at or datetime.now(timezone.utc)
    doc_class, _method = classify(detection.title or "", detection.asx_doc_types,
                                  llm=llm_classifier)
    # Only announcements a parser handles are worth the owner's capture time.
    # The rest are recorded (so completeness audits can still see they existed)
    # but land terminal, keeping the capture worklist to what matters. When a
    # later phase ships a parser for a class, reactivate its detections with:
    #   UPDATE documents SET parse_status = 'detected'
    #   WHERE doc_class = '<class>' AND parse_status = 'not_applicable'
    #     AND sha256 IS NULL;
    status = "detected" if doc_class in parseable_doc_classes() else "not_applicable"
    entity_id = None
    if detection.ticker and detection.lodged_at:
        entity_id = entity_for_ticker(conn, detection.ticker,
                                      market_date(detection.lodged_at))

    with conn.cursor() as cur:
        cur.execute(
            """INSERT INTO documents
                 (source, source_ref, entity_id, ticker_as_lodged, title,
                  asx_doc_types, price_sensitive, lodged_at, doc_class,
                  detection_source, detected_at, detection_key, parse_status)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
               ON CONFLICT (detection_key) WHERE detection_key IS NOT NULL
                 DO NOTHING
               RETURNING doc_id""",
            (detection.detection_source, detection.source_ref, entity_id,
             detection.ticker, detection.title,
             detection.asx_doc_types or None, detection.price_sensitive,
             detection.lodged_at, doc_class, detection.detection_source,
             detected_at, detection.key(), status),
        )
        row = cur.fetchone()
        if row is not None:
            return row["doc_id"], True
        cur.execute("SELECT doc_id FROM documents WHERE detection_key = %s",
                    (detection.key(),))
        return cur.fetchone()["doc_id"], False


def open_detections(
    conn: psycopg.Connection,
    doc_classes: set[str] | None = None,
    limit: int = 200,
) -> list[dict]:
    """Announcements detected but not yet held — the capture worklist."""
    with conn.cursor() as cur:
        if doc_classes:
            cur.execute(
                """SELECT * FROM documents
                   WHERE parse_status = 'detected' AND doc_class = ANY(%s)
                   ORDER BY lodged_at DESC NULLS LAST LIMIT %s""",
                (list(doc_classes), limit),
            )
        else:
            cur.execute(
                """SELECT * FROM documents WHERE parse_status = 'detected'
                   ORDER BY lodged_at DESC NULLS LAST LIMIT %s""",
                (limit,),
            )
        return cur.fetchall()
