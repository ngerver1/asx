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
import json
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
    # Allowlisted company-IR links that automated capture may fetch.
    document_urls: list[str] = field(default_factory=list)
    # Links only the owner may open. Recorded, never fetched.
    manual_open_urls: list[str] = field(default_factory=list)
    asx_doc_types: list[str] = field(default_factory=list)
    # False when the sender's expected format did not match, i.e. the fields
    # above are a best guess at an email shape nobody has calibrated against.
    format_recognised: bool = True
    # Hash of the raw message, so an email without a Message-ID still has a
    # stable identity.
    raw_sha256: str | None = None

    def key(self) -> str:
        """Stable identity for idempotent mailbox re-reads.

        Keyed on the message's own identity — never on parser output. The
        first version hashed ticker and title, which meant that calibrating
        the per-sender rules against real emails (which CLAUDE.md requires,
        and which has not happened yet) would change the key of every alert
        already ingested and re-insert the lot as new detections. The email
        is the same email whatever the parser later makes of it.
        """
        basis = f"{self.detection_source}|{self.source_ref or self.raw_sha256 or ''}"
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

    # Entity resolution uses the lodgement date where we have it. Where we do
    # not, the alert's arrival date is used *as a lookup input only* — it is
    # never written to lodged_at, because it is a fact about a different
    # event (Invariant 2). Tickers move between entities rarely enough that a
    # same-or-next-day lookup is safe, and a wrong resolution is caught by the
    # unresolved-ticker review below rather than assumed away.
    entity_id = None
    if detection.ticker:
        entity_id = entity_for_ticker(
            conn, detection.ticker,
            market_date(detection.lodged_at or detected_at),
        )

    with conn.cursor() as cur:
        cur.execute(
            """INSERT INTO documents
                 (source, source_ref, entity_id, ticker_as_lodged, title,
                  asx_doc_types, price_sensitive, lodged_at, doc_class,
                  detection_source, detected_at, detection_key, parse_status,
                  manual_open_urls, fetch_candidate_urls)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
               ON CONFLICT (detection_key) WHERE detection_key IS NOT NULL
                 DO NOTHING
               RETURNING doc_id""",
            (detection.detection_source, detection.source_ref, entity_id,
             detection.ticker, detection.title,
             detection.asx_doc_types or None, detection.price_sensitive,
             detection.lodged_at, doc_class, detection.detection_source,
             detected_at, detection.key(), status,
             detection.manual_open_urls or None,
             detection.document_urls or None),
        )
        row = cur.fetchone()
        if row is None:
            cur.execute(
                """SELECT doc_id, ticker_as_lodged, title FROM documents
                   WHERE detection_key = %s""",
                (detection.key(),),
            )
            existing = cur.fetchone()
            _flag_key_collision(conn, detection, existing)
            return existing["doc_id"], False
        doc_id = row["doc_id"]

    _queue_detection_reviews(conn, detection, doc_id, entity_id)
    return doc_id, True


def _flag_key_collision(conn: psycopg.Connection, detection: Detection,
                        existing: dict) -> None:
    """Two different announcements arriving under one detection key.

    The key is the message's identity (its Message-ID), which is what makes
    re-reads idempotent across transports — the same alert read over IMAP and
    from a saved .eml must not become two detections. The cost is that a
    sender which reuses a Message-ID would silently swallow the second
    announcement, so a key hit whose ticker or title disagrees with the stored
    row is reported rather than quietly dropped.
    """
    if existing is None:
        return
    same_ticker = (existing["ticker_as_lodged"] or None) == (detection.ticker or None)
    same_title = (existing["title"] or None) == (detection.title or None)
    if same_ticker and same_title:
        return
    with conn.cursor() as cur:
        cur.execute(
            """INSERT INTO review_items (kind, doc_id, payload, reason)
               SELECT 'detection', %s, %s, %s
               WHERE NOT EXISTS (
                 SELECT 1 FROM review_items
                 WHERE doc_id = %s AND resolved_at IS NULL
                   AND reason LIKE 'two different announcements%%')""",
            (existing["doc_id"],
             json.dumps({"stored": {"ticker": existing["ticker_as_lodged"],
                                    "title": existing["title"]},
                         "incoming": {"ticker": detection.ticker,
                                      "title": detection.title},
                         "source_ref": detection.source_ref}),
             "two different announcements arrived under one message identity "
             f"({detection.source_ref!r}). Only the first was recorded. Open "
             f"the mailbox and capture the second by hand.",
             existing["doc_id"]),
        )


def _queue_detection_reviews(conn: psycopg.Connection, detection: Detection,
                             doc_id: int, entity_id: int | None) -> None:
    """Raise a review item for anything about this alert we could not read.

    Detection is the mechanism that makes gaps visible (Invariant 7). An alert
    the ingester could not understand must therefore be *loud*: silently
    recording a half-read detection produces a row that looks deliberate and
    is never revisited.
    """
    problems = []
    if not detection.format_recognised:
        problems.append(
            "the sender's expected subject format did not match, so the "
            "ticker and title are guesses and the email may describe SEVERAL "
            "announcements of which only one was recorded"
        )
    if detection.ticker and entity_id is None:
        problems.append(
            f"ticker {detection.ticker!r} does not resolve to exactly one "
            f"listed entity on that date"
        )
    if not detection.ticker:
        problems.append("no ticker could be read from the alert")
    if not problems:
        return
    with conn.cursor() as cur:
        cur.execute(
            """INSERT INTO review_items (kind, doc_id, payload, reason)
               VALUES ('detection', %s, %s, %s)""",
            (doc_id,
             json.dumps({"detection_source": detection.detection_source,
                         "source_ref": detection.source_ref,
                         "ticker": detection.ticker,
                         "title": detection.title,
                         "manual_open_urls": detection.manual_open_urls}),
             "alert email only partly understood: " + "; ".join(problems)
             + ". Open it and, if the parse is wrong, fix SENDER_RULES and "
               "re-run — the detection key is the message identity, so "
               "re-reading will not duplicate."),
        )


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
