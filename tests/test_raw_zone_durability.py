"""The raw zone has to survive the container.

Documents were stored on a local filesystem that a cloud container wipes
between sessions, so every read_document call would have failed after a
restart and the only durable copies of anything were the fixtures that
happened to be committed to git. These tests pin the fix: the extracted text
lives in the database, the file is a cache, and losing the cache costs
nothing but fidelity.
"""

from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from pathlib import Path

import pytest

from asx.raw.store import backfill, ingest_document, read_document

DOCS = Path(__file__).parent.parent / "fixtures" / "app3y" / "documents"
UTC = timezone.utc


def _pdf(name: str = "6A1339259.pdf") -> bytes:
    path = DOCS / name
    if not path.exists():
        pytest.skip(f"{name} not present")
    return path.read_bytes()


def _ingest(conn, content, tmp_path, **kw):
    return ingest_document(
        conn, content, source="test", root=tmp_path,
        lodged_at=datetime(2026, 8, 19, 1, 0, tzinfo=UTC),
        lodged_at_source="manual", **kw)


def test_the_text_layer_is_stored_with_the_document(conn, tmp_path):
    stored = _ingest(conn, _pdf(), tmp_path)
    with conn.cursor() as cur:
        cur.execute("""SELECT document_text, text_sha256, text_extractor
                       FROM documents WHERE doc_id = %s""", (stored.doc_id,))
        row = cur.fetchone()
    assert "Appendix 3Y" in row["document_text"]
    assert row["text_extractor"].startswith("pypdf-")
    assert hashlib.sha256(row["document_text"].encode()).hexdigest() == row["text_sha256"]


def test_a_document_survives_losing_the_disk(conn, tmp_path):
    """The container is reclaimed and the raw zone with it. This is the whole
    point of the change: before it, every document became unreadable."""
    content = _pdf()
    stored = _ingest(conn, content, tmp_path)
    assert read_document(conn, stored.doc_id) == content   # file still there

    Path(stored.storage_path).unlink()                     # container reclaimed

    recovered = read_document(conn, stored.doc_id)
    assert b"Appendix 3Y" in recovered
    from asx.parse.app3y_rules import extract_all
    from asx.parse.text import document_text
    forms = extract_all(document_text(recovered))
    assert forms and forms[0].get("director_name") == "James Champion de Crespigny"


def test_the_identity_of_a_document_is_the_original_not_its_text(conn, tmp_path):
    """sha256 must stay the hash of the source artifact. Hashing the text
    instead would collide two different lodgements that extract to the same
    characters — amended notices routinely do — and would change every doc_id
    whenever the extraction library was upgraded."""
    content = _pdf()
    stored = _ingest(conn, content, tmp_path)
    assert stored.sha256 == hashlib.sha256(content).hexdigest()

    again = _ingest(conn, content, tmp_path)
    assert again.doc_id == stored.doc_id and again.already_existed


def test_corrupted_stored_text_is_refused_not_returned(conn, tmp_path):
    stored = _ingest(conn, _pdf(), tmp_path)
    Path(stored.storage_path).unlink()
    with conn.cursor() as cur:
        cur.execute("UPDATE documents SET document_text = %s WHERE doc_id = %s",
                    ("tampered", stored.doc_id))
    with pytest.raises(RuntimeError, match="text fails hash check"):
        read_document(conn, stored.doc_id)


def test_a_document_with_neither_copy_says_so_plainly(conn, tmp_path):
    """A document ingested before the text layer existed. It must fail loudly
    and name the remedy, not return empty bytes that read as a blank form."""
    stored = _ingest(conn, _pdf(), tmp_path)
    Path(stored.storage_path).unlink()
    with conn.cursor() as cur:
        cur.execute("""UPDATE documents SET document_text = NULL,
                       text_sha256 = NULL WHERE doc_id = %s""", (stored.doc_id,))
    with pytest.raises(FileNotFoundError, match="re-ingested"):
        read_document(conn, stored.doc_id)


def test_a_document_with_no_text_layer_still_registers(conn, tmp_path):
    """A scanned page yields no text. That is a fact about the document for
    the review queue, not an ingestion failure."""
    stored = _ingest(conn, b"%PDF-1.7 no text layer here", tmp_path)
    with conn.cursor() as cur:
        cur.execute("SELECT document_text FROM documents WHERE doc_id = %s",
                    (stored.doc_id,))
        assert cur.fetchone()["document_text"] is None
    assert read_document(conn, stored.doc_id) == b"%PDF-1.7 no text layer here"


def test_the_text_layer_is_a_fraction_of_the_original():
    """The size argument, measured rather than asserted: this is what makes
    the durable copy fit in Postgres at all."""
    import gzip
    from asx.parse.text import document_text

    documents = sorted(DOCS.glob("*.pdf"))[:40]
    if len(documents) < 20:
        pytest.skip("corpus not present")
    raw = sum(len(d.read_bytes()) for d in documents)
    packed = sum(len(gzip.compress(document_text(d.read_bytes()).encode(), 9))
                 for d in documents)
    assert raw / packed > 50, f"only {raw / packed:.0f}x"


def _forget_the_text(conn, doc_id, stored_path):
    """Put a document back the way migration 0020 found the corpus: bytes on a
    disk that is about to vanish, and no durable text."""
    with conn.cursor() as cur:
        cur.execute("""UPDATE documents SET document_text = NULL,
                       text_sha256 = NULL, text_extractor = NULL
                       WHERE doc_id = %s""", (doc_id,))
    Path(stored_path).unlink()


def test_backfill_gives_a_pre_text_document_its_durable_copy(conn, tmp_path):
    """0020 could not reach backwards: every document ingested before it had
    no stored text, so when the old container went away read_document could
    not return one of them. The text layer only protects a corpus it has been
    run over."""
    content = _pdf()
    stored = _ingest(conn, content, tmp_path)
    _forget_the_text(conn, stored.doc_id, stored.storage_path)

    with pytest.raises(FileNotFoundError):
        read_document(conn, stored.doc_id)

    counts = backfill(conn, [DOCS])
    assert counts["text_backfilled"] == 1 and counts["bytes_lost"] == 0
    assert b"Appendix 3Y" in read_document(conn, stored.doc_id)


def test_backfill_is_idempotent(conn, tmp_path):
    stored = _ingest(conn, _pdf(), tmp_path)
    _forget_the_text(conn, stored.doc_id, stored.storage_path)
    backfill(conn, [DOCS])
    assert backfill(conn, [DOCS])["text_backfilled"] == 0


def test_backfill_reports_a_document_whose_bytes_are_gone(conn, tmp_path):
    """Counted and left NULL, never quietly marked done — an unreadable
    document is a fact the operator has to see."""
    stored = _ingest(conn, _pdf(), tmp_path)
    _forget_the_text(conn, stored.doc_id, stored.storage_path)

    counts = backfill(conn, [tmp_path / "nothing-here"])
    assert counts["bytes_lost"] == 1 and counts["text_backfilled"] == 0
    with conn.cursor() as cur:
        cur.execute("SELECT document_text FROM documents WHERE doc_id = %s",
                    (stored.doc_id,))
        assert cur.fetchone()["document_text"] is None


def test_backfill_trusts_the_hash_not_the_filename(conn, tmp_path):
    """A file sitting where the document should be, carrying its name but not
    its content, is a different document (Invariant 3)."""
    content = _pdf()
    stored = _ingest(conn, content, tmp_path)
    _forget_the_text(conn, stored.doc_id, stored.storage_path)

    decoy = tmp_path / "decoy"
    decoy.mkdir()
    (decoy / stored.sha256).write_bytes(b"%PDF-1.4 not the same document")

    counts = backfill(conn, [decoy])
    assert counts["bytes_lost"] == 1, "a mismatched file was trusted"
    assert hashlib.sha256(content).hexdigest() == stored.sha256


def test_backfill_dates_a_document_that_was_ingested_undated(conn, tmp_path):
    """An undated document produces no canonical rows, so a corpus ingested
    before the capture path called asx.ingest.lodgement yields nothing at all.
    The date comes off the document itself and is labelled as the proxy it is.
    """
    from asx.ingest.lodgement import pdf_created_at

    content = _pdf()
    created = pdf_created_at(content)
    assert created is not None, "fixture carries no creation date"

    stored = _ingest(conn, content, tmp_path)
    with conn.cursor() as cur:
        cur.execute("""UPDATE documents SET lodged_at = NULL,
                       lodged_at_source = NULL WHERE doc_id = %s""",
                    (stored.doc_id,))

    assert backfill(conn, [DOCS])["dated"] == 1
    with conn.cursor() as cur:
        cur.execute("""SELECT lodged_at, lodged_at_source FROM documents
                       WHERE doc_id = %s""", (stored.doc_id,))
        row = cur.fetchone()
    assert row["lodged_at"] == created
    assert row["lodged_at_source"] == "pdf_creation"


def test_backfill_leaves_a_document_that_states_no_date_undated(conn, tmp_path):
    """Counted as undatable, never given a substitute. A trade carrying an
    invented knowable_at is worse than a trade that is missing."""
    stored = _ingest(conn, b"%PDF- no creation date here", tmp_path)
    with conn.cursor() as cur:
        cur.execute("""UPDATE documents SET lodged_at = NULL,
                       lodged_at_source = NULL WHERE doc_id = %s""",
                    (stored.doc_id,))

    counts = backfill(conn, [])
    assert counts["undatable"] == 1 and counts["dated"] == 0
    with conn.cursor() as cur:
        cur.execute("SELECT lodged_at FROM documents WHERE doc_id = %s",
                    (stored.doc_id,))
        assert cur.fetchone()["lodged_at"] is None
