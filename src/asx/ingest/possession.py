"""Possession: attaching document bytes to a detection (Tier 0 §1).

Two sanctioned routes:

1. **IR website fetch** — where an alert links to the *company's own* site,
   fetched through the guard (robots-respecting, rate-limited, never
   asx.com.au).
2. **Human-triggered capture** — the owner opens an announcement personally
   in the capture browser profile; a watcher files what appeared on disk.

A third route exists incidentally: PDFs attached directly to IR emails.

Everything here funnels into attach_document(), which stores bytes in the
append-only raw zone and moves the document from 'detected' to 'unparsed'
(or 'not_applicable' where no parser handles the class).
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

import psycopg

from asx.config import raw_zone_root
from asx.ingest.fetch_guard import (
    ProhibitedSourceError,
    RobotsDisallowedError,
    fetch,
    is_prohibited,
)
from asx.raw.store import _store_bytes


def attach_document(
    conn: psycopg.Connection,
    doc_id: int,
    content: bytes,
    possession_source: str,
    fetched_at: datetime | None = None,
    root: Path | None = None,
) -> bool:
    """Attach bytes to a detected document. Returns False if the identical
    bytes are already held under another doc_id (duplicate capture), in which
    case the detection is closed as a duplicate rather than double-stored."""
    from asx.parse.registry import parseable_doc_classes

    root = root or raw_zone_root()
    sha, path = _store_bytes(content, root)

    with conn.cursor() as cur:
        cur.execute("SELECT doc_id, doc_class FROM documents WHERE sha256 = %s", (sha,))
        existing = cur.fetchone()
        if existing and existing["doc_id"] != doc_id:
            cur.execute(
                """UPDATE documents SET parse_status = 'not_applicable',
                       source_ref = coalesce(source_ref, '') ||
                                    ' [duplicate of doc ' || %s || ']'
                   WHERE doc_id = %s AND parse_status = 'detected'""",
                (existing["doc_id"], doc_id),
            )
            return False

        cur.execute("SELECT doc_class FROM documents WHERE doc_id = %s", (doc_id,))
        row = cur.fetchone()
        if row is None:
            raise KeyError(f"doc_id {doc_id} not found")
        status = "unparsed" if row["doc_class"] in parseable_doc_classes() else "not_applicable"
        cur.execute(
            """UPDATE documents
               SET sha256 = %s, storage_path = %s, fetched_at = %s,
                   possession_source = %s, parse_status = %s
               WHERE doc_id = %s""",
            (sha, str(path), fetched_at or datetime.now(timezone.utc),
             possession_source, status, doc_id),
        )
    return True


def fetch_ir_documents(conn: psycopg.Connection, limit: int = 25) -> dict:
    """Attempt possession of open detections whose links point at company IR
    sites. ASX links are skipped by construction — the guard raises on them,
    and that is treated as 'awaiting manual capture', not an error."""
    from asx.ingest.detection import open_detections

    stats = {"attempted": 0, "captured": 0, "skipped_asx": 0,
             "robots_blocked": 0, "failed": 0, "not_a_document": 0,
             "no_candidates": 0}
    for doc in open_detections(conn, limit=limit):
        urls = _document_urls_for(conn, doc["doc_id"])
        if not urls:
            # Distinguished from "candidates were skipped" on purpose: a route
            # that never runs and a route that runs and finds nothing look
            # identical in an all-zero stats dict, and the first is a dead
            # route reported as a quiet one.
            stats["no_candidates"] += 1
            continue
        for url in urls:
            if is_prohibited(url):
                stats["skipped_asx"] += 1
                continue
            stats["attempted"] += 1
            try:
                result = fetch(url)
            except ProhibitedSourceError:
                stats["skipped_asx"] += 1
                continue
            except RobotsDisallowedError:
                stats["robots_blocked"] += 1
                continue
            except Exception:
                stats["failed"] += 1
                continue
            # Verify it is actually a document before storing it as one. A
            # login wall, a cookie banner or an error page all return 200 with
            # HTML, and storing that as the announcement both poisons the raw
            # zone and flips the row out of 'detected' — destroying the
            # capture-gap signal that says the document is still missing.
            if not _looks_like_pdf(result):
                stats["not_a_document"] += 1
                continue
            if attach_document(conn, doc["doc_id"], result.content, "ir_website"):
                stats["captured"] += 1
                break     # only a successful attach ends this document's turn
    conn.commit()
    return stats


def _looks_like_pdf(result) -> bool:
    content_type = (getattr(result, "content_type", "") or "").lower()
    return (content_type.startswith("application/pdf")
            or result.content[:5] == b"%PDF-")


def _document_urls_for(conn: psycopg.Connection, doc_id: int) -> list[str]:
    """Allowlisted company-IR links recorded on the detection.

    Reads the dedicated column rather than regexing source_ref. source_ref
    holds an email Message-ID for every mailbox detection — never a URL — so
    the old regex returned [] for every real alert and this whole route was a
    silent no-op. attach_document also appends " [duplicate of doc N]" to
    source_ref, so it was never a clean URL carrier in the first place.
    """
    with conn.cursor() as cur:
        cur.execute("SELECT fetch_candidate_urls FROM documents WHERE doc_id = %s",
                    (doc_id,))
        row = cur.fetchone()
    return list(row["fetch_candidate_urls"] or []) if row else []


# --- human-triggered capture -------------------------------------------

_TICKER_IN_NAME = re.compile(r"\b([A-Z0-9]{3,6})\b")


def file_captured_documents(
    conn: psycopg.Connection,
    capture_dir: Path,
    archive_dir: Path | None = None,
) -> dict:
    """File documents the owner captured by opening them personally.

    Each file may carry a sidecar `<name>.meta.json`. If it names `doc_id` or
    `detection_key`, the bytes attach to that detection; otherwise we match on
    ticker and lodgement date, and failing that create a standalone document
    (possession without prior detection is legitimate — e.g. a document found
    while browsing).

    The capture watcher is the ONLY route by which asx.com.au content enters
    the platform, and it moves bytes that a human already opened. No automated
    request is made here.
    """
    stats = {"filed": 0, "attached": 0, "standalone": 0, "duplicate": 0}
    capture_dir = Path(capture_dir)
    for path in sorted(capture_dir.glob("**/*")):
        if not path.is_file() or path.name.endswith(".meta.json"):
            continue
        meta_path = path.with_name(path.name + ".meta.json")
        meta = json.loads(meta_path.read_text()) if meta_path.exists() else {}
        content = path.read_bytes()
        doc_id = _match_detection(conn, meta, path.name)

        if doc_id is not None:
            attached = attach_document(conn, doc_id, content, "manual_capture")
            stats["attached" if attached else "duplicate"] += 1
        else:
            _create_standalone(conn, content, meta, path.name)
            stats["standalone"] += 1
        stats["filed"] += 1

        if archive_dir:
            archive = Path(archive_dir)
            archive.mkdir(parents=True, exist_ok=True)
            path.rename(archive / path.name)
            if meta_path.exists():
                meta_path.rename(archive / meta_path.name)
    conn.commit()
    return stats


def _match_detection(conn: psycopg.Connection, meta: dict, filename: str) -> int | None:
    with conn.cursor() as cur:
        if meta.get("doc_id"):
            cur.execute(
                "SELECT doc_id FROM documents WHERE doc_id = %s AND parse_status = 'detected'",
                (meta["doc_id"],),
            )
            row = cur.fetchone()
            if row:
                return row["doc_id"]
        if meta.get("detection_key"):
            cur.execute(
                "SELECT doc_id FROM documents WHERE detection_key = %s AND parse_status = 'detected'",
                (meta["detection_key"],),
            )
            row = cur.fetchone()
            if row:
                return row["doc_id"]

        ticker = meta.get("ticker")
        if not ticker:
            m = _TICKER_IN_NAME.search(filename.upper())
            ticker = m.group(1) if m else None
        lodged = meta.get("lodged_at")
        if ticker and lodged:
            stamp = datetime.fromisoformat(lodged)
            if stamp.tzinfo is None:
                from asx.ids.market_time import SYDNEY

                stamp = stamp.replace(tzinfo=SYDNEY)
            # Same ticker, same market day: the detection this capture answers.
            cur.execute(
                """SELECT doc_id FROM documents
                   WHERE parse_status = 'detected' AND upper(ticker_as_lodged) = %s
                     AND lodged_at::date = %s::date
                   ORDER BY abs(extract(epoch FROM (lodged_at - %s))) LIMIT 1""",
                (ticker.upper(), stamp, stamp),
            )
            row = cur.fetchone()
            if row:
                return row["doc_id"]
    return None


def _create_standalone(conn: psycopg.Connection, content: bytes, meta: dict,
                       filename: str) -> int:
    from asx.raw.store import ingest_document

    lodged_at = None
    if meta.get("lodged_at"):
        lodged_at = datetime.fromisoformat(meta["lodged_at"])
        if lodged_at.tzinfo is None:
            from asx.ids.market_time import SYDNEY

            lodged_at = lodged_at.replace(tzinfo=SYDNEY)
    stored = ingest_document(
        conn, content,
        source=meta.get("source", "manual_capture"),
        source_ref=meta.get("source_ref", filename),
        ticker_as_lodged=meta.get("ticker"),
        title=meta.get("title", Path(filename).stem),
        lodged_at=lodged_at,
        possession_source="manual_capture",
    )
    from asx.ingest.classifier import classify
    from asx.parse.registry import parseable_doc_classes

    doc_class, _ = classify(meta.get("title", Path(filename).stem))
    with conn.cursor() as cur:
        cur.execute(
            """UPDATE documents SET doc_class = %s,
                   parse_status = CASE WHEN %s THEN 'unparsed' ELSE 'not_applicable' END
               WHERE doc_id = %s AND parse_status <> 'validated'""",
            (doc_class, doc_class in parseable_doc_classes(), stored.doc_id),
        )
    return stored.doc_id
