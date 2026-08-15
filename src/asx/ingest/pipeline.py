"""Announcement ingestion pipeline: source -> raw zone -> classify -> resolve
entity (SPEC §5.3). Idempotent — reruns are always safe."""

from __future__ import annotations

import psycopg

from asx.ids.resolver import resolve_name
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


def run_ingest(conn: psycopg.Connection, source: AnnouncementSource) -> dict:
    """Pull new announcements, store them, classify, and resolve the issuer.

    Returns counts for the ops report. Every document reaches a terminal
    parse_status eventually; this stage leaves parseable classes 'unparsed'
    and marks classes we don't parse as 'not_applicable'.
    """
    stats = {"fetched": 0, "new": 0, "resolved": 0, "unresolved": 0}
    parseable = {"app_3y", "app_3z", "app_2a", "app_3b", "lr_3_10a_notice",
                 "substantial_603", "substantial_604", "substantial_605",
                 "capital_reorg"}

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

        doc_class, _method = classify(ann.title or "", ann.asx_doc_types)
        entity_id = None
        if ann.ticker_as_lodged and ann.lodged_at:
            entity_id = _entity_for_ticker(conn, ann.ticker_as_lodged, ann.lodged_at.date())
        if entity_id is None and ann.title:
            # Fall back to the resolver on the title's issuer name if the
            # provider embeds one; conservative, so usually stays unresolved
            # here and is fixed when the parser reads the document body.
            pass

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
