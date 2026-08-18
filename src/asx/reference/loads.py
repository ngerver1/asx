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


def _record_parts(conn: psycopg.Connection, load_id: int,
                  paths: list[Path], doc_ids: list[int]) -> None:
    """Record the load's full part list, replacing any previous list.

    Replacing is only reachable when the publisher reissued the extract and
    part 1 happened not to change; the superseded parts keep their rows in
    `documents` and their bytes in the raw zone, so nothing is lost — only the
    statement of which files the *current* applied rows came from is updated.
    """
    with conn.cursor() as cur:
        cur.execute("DELETE FROM reference_load_parts WHERE load_id = %s", (load_id,))
        for part_no, (path, doc_id) in enumerate(zip(paths, doc_ids), start=1):
            cur.execute(
                """INSERT INTO reference_load_parts (load_id, part_no, doc_id, filename)
                   VALUES (%s, %s, %s, %s)""",
                (load_id, part_no, doc_id, Path(path).name),
            )


def register_load(
    conn: psycopg.Connection,
    path: Path,
    *,
    source: str,
    as_at: date,
    source_ref: str | None = None,
    notes: str | None = None,
    parts: list[Path] | None = None,
) -> ReferenceLoad:
    """Store a reference file (or a multi-part extract) in the raw zone and
    open a load record.

    **Every part is stored, not just the first.** The ASIC company register
    ships as 14 numbered files; keeping only part 1 would leave the canonical
    tables underivable from raw, which the prime directive forbids. The parts
    load as one logical unit because a company's name records straddle part
    boundaries, so they share one load_id and are listed in
    `reference_load_parts`.

    Idempotent on content: re-registering the identical publisher file(s)
    returns the existing load with already_loaded=True, so a scheduled refresh
    that finds an unchanged extract does no work. Idempotency is judged on the
    WHOLE part list — an unchanged part 1 is not evidence of an unchanged
    extract, so a reissue that touches only part 7 is correctly re-applied.
    """
    if source not in DOC_CLASSES:
        raise ValueError(f"unknown reference source {source!r}")
    paths = [Path(p) for p in (parts or [path])]
    if paths[0].resolve() != Path(path).resolve():
        raise ValueError(
            "parts[0] must be the same file as path: part 1 anchors the load's "
            f"identity. Got path={path} and parts[0]={paths[0]}."
        )

    stored = [
        ingest_file(
            conn, p,
            source=f"reference:{source}",
            doc_class=DOC_CLASSES[source],
            source_ref=source_ref,
            lodged_at=datetime.combine(as_at, time.min, tzinfo=timezone.utc),
        )
        for p in paths
    ]
    doc_ids = [s.doc_id for s in stored]
    if len(set(doc_ids)) != len(doc_ids):
        dupes = sorted({p.name for p, d in zip(paths, doc_ids)
                        if doc_ids.count(d) > 1})
        raise ValueError(
            f"the same file content was supplied more than once as separate "
            f"parts ({dupes}). Loading it twice would double-count the extract; "
            f"check the part list rather than the loader."
        )
    primary = doc_ids[0]

    with conn.cursor() as cur:
        cur.execute(
            """INSERT INTO reference_loads (source, doc_id, as_at, notes)
               VALUES (%s, %s, %s, %s)
               ON CONFLICT (source, doc_id) DO NOTHING
               RETURNING load_id""",
            (source, primary, as_at, notes),
        )
        row = cur.fetchone()
        if row is not None:
            _record_parts(conn, row["load_id"], paths, doc_ids)
            return ReferenceLoad(row["load_id"], primary, source, as_at,
                                 already_loaded=False, applied=False)

        cur.execute(
            "SELECT load_id, as_at, applied FROM reference_loads WHERE source = %s AND doc_id = %s",
            (source, primary),
        )
        row = cur.fetchone()
        load_id, applied = row["load_id"], row["applied"]

        cur.execute(
            "SELECT doc_id FROM reference_load_parts WHERE load_id = %s ORDER BY part_no",
            (load_id,),
        )
        recorded = [r["doc_id"] for r in cur.fetchall()]

    if recorded == doc_ids:
        return ReferenceLoad(load_id, primary, source, row["as_at"],
                             already_loaded=True, applied=applied)

    # Same part 1, different extract (or a load registered before parts were
    # tracked). It is not the applied file, so it must not report as applied.
    _record_parts(conn, load_id, paths, doc_ids)
    with conn.cursor() as cur:
        cur.execute(
            """UPDATE reference_loads
               SET applied = false,
                   notes = concat_ws('; ', notes, %s)
               WHERE load_id = %s""",
            (f"part list changed on {date.today().isoformat()}: "
             f"{len(recorded)} recorded -> {len(doc_ids)} offered; re-applying",
             load_id),
        )
    return ReferenceLoad(load_id, primary, source, row["as_at"],
                         already_loaded=True, applied=False)


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
