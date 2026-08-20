"""Append-only raw zone (Invariant 3).

Every fetched document is stored as original bytes keyed by SHA-256, with the
`documents` table as its index. Nothing here ever mutates or deletes raw
content; a second ingest of identical bytes is an idempotent no-op.
"""

from __future__ import annotations

import hashlib
from collections.abc import Sequence
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
    # Where lodged_at came from. Never inferred: a timestamp with no
    # stated source is refused by documents_lodged_at_provenance, because
    # knowable_at is what every analytic joins on (Invariant 2).
    lodged_at_source: str | None = None,
    fetched_at: datetime | None = None,
    possession_source: str = "filedrop",
    root: Path | None = None,
) -> StoredDocument:
    """Store a document and register it. Idempotent on content hash.

    Two copies are kept, and only one of them is durable.

    The EXTRACTED TEXT goes in the database. That is the copy the platform
    relies on: the container's filesystem is wiped between sessions, so a raw
    zone that lives only on disk does not survive, and until now every
    read_document call would have failed after a restart.

    The ORIGINAL FILE is also written to the raw zone, because while the disk
    lasts it is the better artifact — layout intact, re-extractable by a
    later library that reads a page better. Nothing depends on it being there.

    sha256 stays the hash of the ORIGINAL. It is the identity of the source
    artifact and what dedupe keys on; hashing the text would collide two
    different lodgements that extract to the same characters, which amended
    notices routinely do.
    """
    root = root or raw_zone_root()
    sha, path = _store_bytes(content, root)
    fetched_at = fetched_at or datetime.now(timezone.utc)
    text, text_sha, extractor = _extract_text(content)

    with conn.cursor() as cur:
        cur.execute(
            """INSERT INTO documents
                 (source, source_ref, entity_id, ticker_as_lodged, title,
                  asx_doc_types, price_sensitive, lodged_at, lodged_at_source, fetched_at,
                  sha256, storage_path, possession_source,
                  document_text, text_sha256, text_extractor)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
               ON CONFLICT (sha256) DO NOTHING
               RETURNING doc_id""",
            (source, source_ref, entity_id, ticker_as_lodged, title,
             asx_doc_types, price_sensitive, lodged_at, lodged_at_source, fetched_at,
             sha, str(path), possession_source, text, text_sha, extractor),
        )
        row = cur.fetchone()
        if row is not None:
            return StoredDocument(row["doc_id"], sha, str(path), already_existed=False)
        cur.execute("SELECT doc_id, storage_path FROM documents WHERE sha256 = %s", (sha,))
        row = cur.fetchone()
        return StoredDocument(row["doc_id"], sha, row["storage_path"], already_existed=True)


def read_document(conn: psycopg.Connection, doc_id: int) -> bytes:
    """The document, preferring the original file and falling back to text.

    The file is a cache on a disk that does not survive the container; the
    text in the database is what does. Callers get bytes either way and pass
    them to document_text(), which reads a PDF as a PDF and anything else as
    UTF-8 — so a restart changes nothing but the fidelity of what is read.
    """
    with conn.cursor() as cur:
        cur.execute(
            """SELECT storage_path, sha256, document_text, text_sha256
               FROM documents WHERE doc_id = %s""", (doc_id,))
        row = cur.fetchone()
    if row is None:
        raise KeyError(f"doc_id {doc_id} not found")

    path = row["storage_path"]
    if path and Path(path).exists():
        content = Path(path).read_bytes()
        if hashlib.sha256(content).hexdigest() != row["sha256"]:
            raise RuntimeError(f"raw zone corruption: doc {doc_id} fails hash check")
        return content

    if row["document_text"] is None:
        raise FileNotFoundError(
            f"doc {doc_id} has neither a readable file at {path!r} nor stored "
            f"text. The raw zone is not durable across containers; a document "
            f"ingested before the text layer existed must be re-ingested."
        )
    text = row["document_text"].encode("utf-8")
    if hashlib.sha256(text).hexdigest() != row["text_sha256"]:
        raise RuntimeError(f"raw zone corruption: doc {doc_id} text fails hash check")
    return text


def _extract_text(content: bytes) -> tuple[str | None, str | None, str | None]:
    """The document's text layer, its checksum, and what produced it.

    A text layer is a READING of the document, not the document (Invariant 6),
    so the extractor is recorded: a later library that reads a page better has
    to be able to find what the old one produced and supersede it.

    A document that yields no text still registers. It has bytes, it is held,
    and the emptiness is a fact about the document — a scanned page — that the
    review queue should see rather than an ingestion failure.
    """
    from asx.parse.text import UnreadableDocument, document_text

    try:
        text = document_text(content)
    except UnreadableDocument:
        raise           # an environment fault, never silently an empty document
    except Exception:
        return None, None, None
    if not text.strip():
        return None, None, None
    version = ""
    try:
        import pypdf
        version = f"pypdf-{pypdf.__version__}"
    except Exception:
        version = "utf8"
    return (text, hashlib.sha256(text.encode("utf-8")).hexdigest(),
            version if content[:5] == b"%PDF-" else "utf8")


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
    lodged_at_source: str | None = None,
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
                 (source, source_ref, title, doc_class, lodged_at, lodged_at_source, fetched_at,
                  sha256, storage_path, possession_source, parse_status)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'not_applicable')
               ON CONFLICT (sha256) DO NOTHING
               RETURNING doc_id""",
            (source, source_ref or str(src), src.name, doc_class, lodged_at, lodged_at_source,
             datetime.now(timezone.utc), sha, str(path), possession_source),
        )
        row = cur.fetchone()
        if row is not None:
            return StoredDocument(row["doc_id"], sha, str(path), already_existed=False)
        cur.execute("SELECT doc_id, storage_path FROM documents WHERE sha256 = %s", (sha,))
        row = cur.fetchone()
        return StoredDocument(row["doc_id"], sha, row["storage_path"], already_existed=True)


def _index_by_hash(dirs: Sequence[Path]) -> dict[str, Path]:
    """sha256 -> path for every file under `dirs`, so bytes can be found by
    identity rather than by the filename they happen to carry."""
    index: dict[str, Path] = {}
    for d in dirs:
        for p in Path(d).rglob("*"):
            if p.is_file():
                index.setdefault(hashlib.sha256(p.read_bytes()).hexdigest(), p)
    return index


def _locate(sha: str, storage_path: str | None,
            index: dict[str, Path]) -> bytes | None:
    """The document's original bytes, wherever they still are.

    Identity is the hash, never the location: a candidate whose contents do
    not hash to `sha` is a different document and is ignored rather than
    trusted for having the right path.
    """
    candidates = [Path(storage_path)] if storage_path else []
    candidates.append(raw_zone_root() / sha[:2] / sha[2:4] / sha)
    if sha in index:
        candidates.append(index[sha])
    for c in candidates:
        try:
            if c.is_file():
                content = c.read_bytes()
                if hashlib.sha256(content).hexdigest() == sha:
                    return content
        except OSError:
            continue
    return None


def backfill(conn: psycopg.Connection,
             search_dirs: Sequence[Path] = ()) -> dict[str, int]:
    """Give a document what today's pipeline would have recorded for it.

    Two things are filled, because both need the same original bytes and
    finding those bytes is the expensive part: the durable text layer, and
    the lodgement timestamp.

    Migration 0020 made the extracted text the durable artifact and the file
    on disk a cache, because the disk does not survive the container. It could
    not reach backwards: every document ingested before it has document_text
    NULL, so once the old container went away read_document could not return a
    single one of them. The text layer only protects a corpus it has been run
    over.

    So this looks for the bytes wherever they still are — the recorded
    storage_path, the raw zone, or a directory named on the command line, such
    as the fixtures that happen to be committed to git — and stores the
    reading. sha256 is untouched: it is the identity of the original artifact
    (Invariant 3), and the bytes are verified against it before anything is
    written.

    Dating matters more than it looks: an undated document produces no
    canonical rows at all, so a corpus ingested before the capture path
    called asx.ingest.lodgement sits in the database yielding nothing. The
    timestamp is read from the document itself and labelled 'pdf_creation',
    never inferred — a row that states no date stays undated and is counted
    as `undatable`.

    A document that is found but yields no text is counted as `no_text_layer`
    and left NULL rather than marked done. It is a scanned page, and pretending
    otherwise would hide it from the review queue (Invariant 8).

    Text must be backfilled before dating is attempted on the same run: the
    creation date lives in PDF metadata, which the text layer does not carry,
    so once the bytes are gone a document can never be dated again.
    """
    from asx.ingest import lodgement

    index = _index_by_hash(search_dirs) if search_dirs else {}
    counts = {"text_backfilled": 0, "dated": 0,
              "no_text_layer": 0, "undatable": 0, "bytes_lost": 0}

    with conn.cursor() as cur:
        cur.execute(
            """SELECT doc_id, sha256, storage_path,
                      document_text IS NULL AS needs_text,
                      lodged_at IS NULL AS needs_date
                 FROM documents
                WHERE sha256 IS NOT NULL
                  AND (document_text IS NULL OR lodged_at IS NULL)
                ORDER BY doc_id""")
        pending = cur.fetchall()

    for row in pending:
        content = _locate(row["sha256"], row["storage_path"], index)
        if content is None:
            counts["bytes_lost"] += 1
            continue

        if row["needs_text"]:
            text, text_sha, extractor = _extract_text(content)
            if text is None:
                counts["no_text_layer"] += 1
            else:
                with conn.cursor() as cur:
                    cur.execute(
                        """UPDATE documents
                              SET document_text = %s, text_sha256 = %s,
                                  text_extractor = %s
                            WHERE doc_id = %s AND document_text IS NULL""",
                        (text, text_sha, extractor, row["doc_id"]))
                counts["text_backfilled"] += 1

        if row["needs_date"]:
            dated = lodgement.resolve(pdf_content=content)
            if not dated.known:
                counts["undatable"] += 1
            else:
                with conn.cursor() as cur:
                    cur.execute(
                        """UPDATE documents
                              SET lodged_at = %s, lodged_at_source = %s
                            WHERE doc_id = %s AND lodged_at IS NULL""",
                        (dated.at, dated.source, row["doc_id"]))
                counts["dated"] += 1
    return counts
