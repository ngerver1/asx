"""Append-only raw zone (Invariant 3).

Every fetched document is stored as original bytes keyed by SHA-256, with the
`documents` table as its index. Nothing here ever mutates or deletes raw
content; a second ingest of identical bytes is an idempotent no-op.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import psycopg

from asx.config import raw_zone_root


@dataclass
class StoredDocument:
    doc_id: int
    sha256: str
    storage_path: str
    already_existed: bool


def _store_bytes(content: bytes, root: Path) -> tuple[str, Path]:
    sha = hashlib.sha256(content).hexdigest()
    path = root / sha[:2] / sha[2:4] / sha
    if path.exists():
        # Write-once: identical key means identical content by construction;
        # verify anyway so corruption surfaces here, not downstream.
        if path.read_bytes() != content:
            raise RuntimeError(f"raw zone corruption: {path} does not match its hash")
        return sha, path
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_bytes(content)
    tmp.rename(path)
    return sha, path


def ingest_document(
    conn: psycopg.Connection,
    content: bytes,
    *,
    source: str,
    source_ref: str | None = None,
    entity_id: int | None = None,
    ticker_as_lodged: str | None = None,
    title: str | None = None,
    asx_doc_types: list[str] | None = None,
    price_sensitive: bool | None = None,
    lodged_at: datetime | None = None,
    fetched_at: datetime | None = None,
    possession_source: str = "filedrop",
    root: Path | None = None,
) -> StoredDocument:
    """Store raw bytes and register the document. Idempotent on content hash."""
    root = root or raw_zone_root()
    sha, path = _store_bytes(content, root)
    fetched_at = fetched_at or datetime.now(timezone.utc)

    with conn.cursor() as cur:
        cur.execute(
            """INSERT INTO documents
                 (source, source_ref, entity_id, ticker_as_lodged, title,
                  asx_doc_types, price_sensitive, lodged_at, fetched_at,
                  sha256, storage_path, possession_source)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
               ON CONFLICT (sha256) DO NOTHING
               RETURNING doc_id""",
            (source, source_ref, entity_id, ticker_as_lodged, title,
             asx_doc_types, price_sensitive, lodged_at, fetched_at,
             sha, str(path), possession_source),
        )
        row = cur.fetchone()
        if row is not None:
            return StoredDocument(row["doc_id"], sha, str(path), already_existed=False)
        cur.execute("SELECT doc_id, storage_path FROM documents WHERE sha256 = %s", (sha,))
        row = cur.fetchone()
        return StoredDocument(row["doc_id"], sha, row["storage_path"], already_existed=True)


def read_document(conn: psycopg.Connection, doc_id: int) -> bytes:
    with conn.cursor() as cur:
        cur.execute("SELECT storage_path, sha256 FROM documents WHERE doc_id = %s", (doc_id,))
        row = cur.fetchone()
    if row is None:
        raise KeyError(f"doc_id {doc_id} not found")
    content = Path(row["storage_path"]).read_bytes()
    if hashlib.sha256(content).hexdigest() != row["sha256"]:
        raise RuntimeError(f"raw zone corruption: doc {doc_id} fails hash check")
    return content


def _store_file(src: Path, root: Path) -> tuple[str, Path]:
    """Stream a file into the raw zone by hash. Reference datasets are
    hundreds of megabytes, so nothing is read into memory whole."""
    digest = hashlib.sha256()
    with src.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
    sha = digest.hexdigest()
    path = root / sha[:2] / sha[2:4] / sha
    if path.exists():
        return sha, path
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    with src.open("rb") as fh, tmp.open("wb") as out:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            out.write(chunk)
    tmp.rename(path)
    return sha, path


def ingest_file(
    conn: psycopg.Connection,
    src: Path,
    *,
    source: str,
    doc_class: str,
    source_ref: str | None = None,
    lodged_at: datetime | None = None,
    possession_source: str = "reference_download",
    root: Path | None = None,
) -> StoredDocument:
    """Register a large file (a reference dataset) in the raw zone without
    reading it into memory. Idempotent on content hash: re-downloading an
    unchanged publisher file yields the same doc_id."""
    root = root or raw_zone_root()
    src = Path(src)
    sha, path = _store_file(src, root)
    with conn.cursor() as cur:
        cur.execute(
            """INSERT INTO documents
                 (source, source_ref, title, doc_class, lodged_at, fetched_at,
                  sha256, storage_path, possession_source, parse_status)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,'not_applicable')
               ON CONFLICT (sha256) DO NOTHING
               RETURNING doc_id""",
            (source, source_ref or str(src), src.name, doc_class, lodged_at,
             datetime.now(timezone.utc), sha, str(path), possession_source),
        )
        row = cur.fetchone()
        if row is not None:
            return StoredDocument(row["doc_id"], sha, str(path), already_existed=False)
        cur.execute("SELECT doc_id, storage_path FROM documents WHERE sha256 = %s", (sha,))
        row = cur.fetchone()
        return StoredDocument(row["doc_id"], sha, row["storage_path"], already_existed=True)
