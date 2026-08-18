"""Reference-load bookkeeping (SPEC §4).

Every reference file enters the append-only raw zone first, then gets a
reference_loads row carrying the publisher's extract date. That date is the
reference-data analogue of knowable_at: nothing a file contains may be
treated as known before it (Invariant 2's spirit applied to reference data),
and every entity name, listing, and universe row built from it carries the
load id (Invariant 12).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timezone
from pathlib import Path

import psycopg

from asx.raw.store import ingest_file

DOC_CLASSES = {
    "asic_companies": "reference_asic_companies",
    "abn_bulk_extract": "reference_abn_extract",
    "asx_listed_companies": "reference_asx_listed",
}


@dataclass
class ReferenceLoad:
    load_id: int
    doc_id: int
    source: str
    as_at: date
    already_loaded: bool
    applied: bool


def register_load(
    conn: psycopg.Connection,
    path: Path,
    *,
    source: str,
    as_at: date,
    source_ref: str | None = None,
    notes: str | None = None,
) -> ReferenceLoad:
    """Store a reference file in the raw zone and open a load record.

    Idempotent on content: re-registering the identical publisher file returns
    the existing load with already_loaded=True, so a scheduled refresh that
    finds an unchanged file does no work.
    """
    if source not in DOC_CLASSES:
        raise ValueError(f"unknown reference source {source!r}")
    stored = ingest_file(
        conn, path,
        source=f"reference:{source}",
        doc_class=DOC_CLASSES[source],
        source_ref=source_ref,
        lodged_at=datetime.combine(as_at, time.min, tzinfo=timezone.utc),
    )
    with conn.cursor() as cur:
        cur.execute(
            """INSERT INTO reference_loads (source, doc_id, as_at, notes)
               VALUES (%s, %s, %s, %s)
               ON CONFLICT (source, doc_id) DO NOTHING
               RETURNING load_id""",
            (source, stored.doc_id, as_at, notes),
        )
        row = cur.fetchone()
        if row is not None:
            return ReferenceLoad(row["load_id"], stored.doc_id, source, as_at,
                                 already_loaded=False, applied=False)
        cur.execute(
            "SELECT load_id, as_at, applied FROM reference_loads WHERE source = %s AND doc_id = %s",
            (source, stored.doc_id),
        )
        row = cur.fetchone()
        return ReferenceLoad(row["load_id"], stored.doc_id, source, row["as_at"],
                             already_loaded=True, applied=row["applied"])


def mark_applied(conn: psycopg.Connection, load_id: int, row_count: int,
                 notes: str | None = None) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """UPDATE reference_loads
               SET applied = true, row_count = %s,
                   notes = coalesce(%s, notes)
               WHERE load_id = %s""",
            (row_count, notes, load_id),
        )


def latest_load(conn: psycopg.Connection, source: str) -> dict | None:
    with conn.cursor() as cur:
        cur.execute(
            """SELECT * FROM reference_loads
               WHERE source = %s AND applied
               ORDER BY as_at DESC, load_id DESC LIMIT 1""",
            (source,),
        )
        return cur.fetchone()
