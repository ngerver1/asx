"""Announcement ingestion pipeline: source -> raw zone -> classify -> resolve
entity (SPEC §5.3). Idempotent — reruns are always safe."""

from __future__ import annotations

import psycopg

from asx.ids.market_time import market_date
from asx.ingest.classifier import classify
from asx.ingest.sources import AnnouncementSource
from asx.raw.store import ingest_document


def _entity_for_ticker(conn: psycopg.Connection, ticker: str, on_date) -> int | None:
    """Resolve a ticker to an entity via the effective-dated listings table.

    The ticker is a lookup input here, never a join key (Invariant 1): the
    result is pinned to entity_id and the verbatim ticker is kept only for
    audit.
    """
    with conn.cursor() as cur:
        cur.execute(
            """SELECT DISTINCT entity_id FROM listings
               WHERE exchange = 'ASX' AND ticker = %s
                 AND valid_from <= %s
                 AND (valid_to IS NULL OR valid_to >= %s)""",
            (ticker, on_date, on_date),
        )
        rows = cur.fetchall()
    if len(rows) == 1:
        return rows[0]["entity_id"]
    return None  # zero or ambiguous: leave unresolved, surfaced by monitoring


def run_ingest(
    conn: psycopg.Connection,
    source: AnnouncementSource,
    llm_classifier=None,
) -> dict:
    """Pull new announcements, store them, classify, and resolve the issuer.

    Returns counts for the ops report. Every document reaches a terminal
    parse_status eventually; this stage leaves classes with an implemented
    parser 'unparsed' and marks everything else 'not_applicable'. When a
    later phase ships a parser for a class, reactivate its backlog with:
        UPDATE documents SET parse_status = 'unparsed'
        WHERE doc_class = '<class>' AND parse_status = 'not_applicable';

    llm_classifier is the rules-miss fallback (SPEC §5.3): without it, an
    unusually-titled standard form lands in 'other' and exits the pipeline
    unseen. Pass make_llm_classifier() in production ingestion.
    """
    from asx.parse.registry import parseable_doc_classes

    stats = {"fetched": 0, "new": 0, "resolved": 0, "unresolved": 0}
    parseable = parseable_doc_classes()

    for ann in source.fetch_new():
        stats["fetched"] += 1
        stored = ingest_document(
            conn,
            ann.content,
            source=ann.source,
            source_ref=ann.source_ref,
            ticker_as_lodged=ann.ticker_as_lodged,
            title=ann.title,
            asx_doc_types=ann.asx_doc_types or None,
            price_sensitive=ann.price_sensitive,
            lodged_at=ann.lodged_at,
        )
        if stored.already_existed:
            continue
        stats["new"] += 1

        doc_class, _method = classify(ann.title or "", ann.asx_doc_types,
                                      llm=llm_classifier)
        entity_id = None
        if ann.ticker_as_lodged and ann.lodged_at:
            # Sydney calendar date, not UTC: a 09:30 AEST lodgement is the
            # previous UTC day (SPEC §3 two-clocks convention).
            entity_id = _entity_for_ticker(
                conn, ann.ticker_as_lodged, market_date(ann.lodged_at)
            )

        with conn.cursor() as cur:
            cur.execute(
                """UPDATE documents
                   SET doc_class = %s,
                       entity_id = COALESCE(%s, entity_id),
                       parse_status = CASE WHEN %s THEN 'unparsed' ELSE 'not_applicable' END
                   WHERE doc_id = %s""",
                (doc_class, entity_id, doc_class in parseable, stored.doc_id),
            )
        stats["resolved" if entity_id else "unresolved"] += 1
    conn.commit()
    return stats
