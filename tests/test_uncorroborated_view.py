"""The derived view over readings that failed corroboration.

Invariant 6 keeps unvalidated extractions out of canonical, and these tests
hold that line. What they also hold is the other half: 82 of the 109 documents
in the review queue carried both a date and a quantity — more usable readings
than reached canonical — and discarding them because they could not be
self-checked is its own error. The derived zone is where an unverified fact
belongs (SPEC §3), labelled, never in director_trades.
"""

from __future__ import annotations

from asx.parse.app3y import App3YParser
from asx.parse.llm import ExtractionPass
from asx.parse.framework import run_parser_on_doc
from tests.test_db_integration import (  # noqa: F401
    _payload_3y,
    _securities,
    _setup_3y_doc,
)


class FakeRulesExtractor:
    """A single-pass extractor, like the one `asx parse` actually uses.

    It matters which is used here. A dual-pass extractor corroborates a reading
    by agreeing with itself across two passes, so it validates a notice that
    prints no holdings. The rules reader has no second pass, so the form's own
    arithmetic is its only witness — and where the form prints nothing to check,
    the reading lands in review. That is the case this view exists for.
    """

    single_pass = True
    model = "rules/app3y@2"

    def __init__(self, payload):
        self.payload = payload

    def extract_text_pass(self, content, schema, prompt):
        return ExtractionPass(self.payload, self.model, "rules")

    def extract_vision_pass(self, content, schema, prompt):
        raise NotImplementedError("reads once")


def _rows(conn, doc_id):
    with conn.cursor() as cur:
        cur.execute("""SELECT * FROM uncorroborated_director_trades
                       WHERE doc_id = %s ORDER BY security_class""", (doc_id,))
        return cur.fetchall()


def _run(conn, payload, doc_id):
    return run_parser_on_doc(conn, App3YParser(), doc_id, FakeRulesExtractor(payload))


def test_a_reading_with_nothing_to_check_against_is_surfaced(conn):
    """The dominant case: the notice prints no before/after holdings, so the
    form's arithmetic cannot corroborate the reading. That makes it unverified,
    not wrong — a director acquiring shares is worth seeing with the warning
    attached rather than not at all."""
    _entity, doc_id = _setup_3y_doc(conn)
    payload = _payload_3y()
    _securities(payload)[0].update(held_before=None, held_after=None)

    assert _run(conn, payload, doc_id).status == "review"

    rows = _rows(conn, doc_id)
    assert rows, "a usable reading was dropped instead of surfaced"
    assert rows[0]["corroboration"] == "unverified"
    assert rows[0]["qty_acquired"] is not None
    assert rows[0]["event_date"] is not None
    assert rows[0]["knowable_at"] is not None, "Invariant 2: no knowable_at, no fact"
    assert rows[0]["entity_id"] is not None, "Invariant 1: joins are on entity_id"
    assert any("arithmetic unverifiable" in f
               for f in rows[0]["uncorroborated_because"])


def test_a_contradicted_reading_is_labelled_not_mixed_in(conn):
    """A reading whose own sum does not hold was checked and failed. Sharing a
    bucket with readings nobody could check would make the view unusable as
    signal — 'we could not verify this' and 'this is wrong' are different
    facts."""
    _entity, doc_id = _setup_3y_doc(conn)
    payload = _payload_3y()
    _securities(payload)[0]["held_after"] = 1        # against a six-figure holding

    assert _run(conn, payload, doc_id).status == "review"
    rows = _rows(conn, doc_id)
    assert rows and rows[0]["corroboration"] == "contradicted"


def test_a_validated_reading_stays_out_of_the_view(conn):
    """It is in canonical. A row in both would be double-counted by anything
    that reads the two together."""
    _entity, doc_id = _setup_3y_doc(conn)
    assert _run(conn, _payload_3y(), doc_id).status == "validated"

    assert _rows(conn, doc_id) == []
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) AS n FROM director_trades WHERE doc_id = %s",
                    (doc_id,))
        assert cur.fetchone()["n"] == 1


def test_nothing_in_canonical_is_also_in_the_view(conn):
    """The two must not overlap, whatever mix of documents has been parsed."""
    _e1, validated = _setup_3y_doc(conn)
    _run(conn, _payload_3y(), validated)

    _e2, unverified = _setup_3y_doc(conn, content=b"another synthetic 3y")
    payload = _payload_3y()
    _securities(payload)[0].update(held_before=None, held_after=None)
    _run(conn, payload, unverified)

    with conn.cursor() as cur:
        cur.execute("""SELECT count(*) AS n FROM uncorroborated_director_trades
                       WHERE doc_id IN (SELECT doc_id FROM director_trades)""")
        assert cur.fetchone()["n"] == 0


def test_every_row_says_why_it_is_unverified(conn):
    """A derived row that cannot state its own coverage is prohibited output —
    the rule float_series already follows."""
    _entity, doc_id = _setup_3y_doc(conn)
    payload = _payload_3y()
    _securities(payload)[0].update(held_before=None, held_after=None)
    _run(conn, payload, doc_id)

    with conn.cursor() as cur:
        cur.execute("""SELECT count(*) AS n FROM uncorroborated_director_trades
                       WHERE cardinality(uncorroborated_because) = 0""")
        assert cur.fetchone()["n"] == 0
