"""One test that runs the real pipeline and checks data comes out the far end.

Two finished modules sat in this codebase uncalled: asx.ingest.lodgement,
which dates a document, and RulesExtractor, which reads one without an API
key. Both were documented, both were covered — by tests that invoked them
directly. A test that calls resolve() and gets a timestamp proves the function
works and says nothing about whether any pipeline calls it, so the suite
stayed green while a capture was filed undated and `asx parse` demanded a key
it did not need. Undated documents produce no canonical rows, and
director_trades sat empty.

This is the guard that was missing. It captures a real announcement the way
`asx capture` does, reads it the way `asx parse` does — through the same
extractor selection the CLI uses, not a hand-picked one — and asserts a
canonical trade comes out. It fails if any link in that chain is disconnected
again.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from asx.cli import _extractor_for, _get_parser
from asx.ids.normalize import name_norm
from asx.ingest.possession import file_captured_documents
from asx.parse.framework import run_parser_on_doc

# An Appendix 3Y whose own arithmetic reconciles, so the rules reader can
# corroborate it and the row reaches canonical rather than review.
DOCUMENT = Path(__file__).parent.parent / "fixtures/app3y/documents/2A1690463.pdf"
ISSUER = "Adrad Holdings Limited"


def _entity(conn, name):
    with conn.cursor() as cur:
        cur.execute("INSERT INTO entities (entity_kind) VALUES ('company') "
                    "RETURNING entity_id")
        eid = cur.fetchone()["entity_id"]
        cur.execute(
            """INSERT INTO entity_names
                 (entity_id, name, name_norm, name_kind, valid_from)
               VALUES (%s, %s, %s, 'legal', %s)""",
            (eid, name, name_norm(name), date(2020, 1, 1)))
    return eid


def test_a_captured_announcement_becomes_a_director_trade(conn, tmp_path):
    if not DOCUMENT.exists():
        pytest.skip("corpus not present")
    entity_id = _entity(conn, ISSUER)

    # 1. Possession. No sidecar metadata, exactly as a document found by hand
    #    arrives — so the only timestamp available is the document's own.
    capture = tmp_path / "capture"
    capture.mkdir()
    (capture / DOCUMENT.name).write_bytes(DOCUMENT.read_bytes())
    assert file_captured_documents(conn, capture)["standalone"] == 1

    with conn.cursor() as cur:
        cur.execute("""SELECT doc_id, lodged_at, lodged_at_source, entity_id
                       FROM documents ORDER BY doc_id DESC LIMIT 1""")
        doc = cur.fetchone()

    # 2. Dating. Without this the document produces nothing at all, which is
    #    how 52 captured 3Ys came to yield no rows.
    assert doc["lodged_at"] is not None, "captured document was filed undated"
    assert doc["lodged_at_source"] == "pdf_creation"
    assert doc["entity_id"] == entity_id, "document was orphaned from its issuer"

    # 3. Extraction, through the selection `asx parse` itself makes. Naming
    #    RulesExtractor here instead would re-create the original blind spot.
    parser = _get_parser("app3y")
    outcome = run_parser_on_doc(conn, parser, doc["doc_id"],
                                _extractor_for(parser))
    assert outcome.status == "validated", (
        f"status {outcome.status} at confidence {outcome.confidence}")

    # 4. The canonical row.
    with conn.cursor() as cur:
        cur.execute("""SELECT * FROM director_trades WHERE doc_id = %s""",
                    (doc["doc_id"],))
        trades = cur.fetchall()
    assert trades, "a validated Appendix 3Y produced no director_trade"

    for t in trades:
        assert t["entity_id"] == entity_id
        assert t["person_name_raw"]
        # Invariant 2: nothing is knowable before it happened.
        assert t["knowable_at"].date() >= t["event_date"]
        if t["held_before"] is not None and t["held_after"] is not None:
            moved = (t["qty_acquired"] or 0) - (t["qty_disposed"] or 0)
            assert t["held_after"] == t["held_before"] + moved, (
                "the form's own arithmetic does not reconcile, which is the "
                "only corroboration the rules path has")


def test_parse_does_not_need_an_api_key(monkeypatch):
    """The rules path exists precisely so the pipeline runs without one. It
    was written, tested, and then never selected — `asx parse` built the LLM
    extractor unconditionally and died on a missing key."""
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    extractor = _extractor_for(_get_parser("app3y"))
    assert extractor.single_pass
    assert extractor.model.startswith("rules/app3y@")
