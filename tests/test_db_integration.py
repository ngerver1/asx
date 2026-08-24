"""Integration tests against live PostgreSQL. The `conn` fixture provides a
migrated, empty asx_test database and a tmp raw zone."""

import csv
import io
from datetime import date, datetime, timezone
from decimal import Decimal

import pytest

from asx.canonical.director_trades import TradeRow, apply_trades
from asx.canonical.shares import (
    ShareEvent,
    reconcile_entity,
    record_anchor,
    record_event,
    replay,
    shares_outstanding_sql,
)
from asx.ids.resolver import resolve_name
from asx.monitor.checks import run_monitor
from asx.monitor.ops_report import ops_report
from asx.parse.app3y import App3YParser
from asx.parse.framework import resolve_review_item, run_parser_on_doc
from asx.parse.llm import ExtractionPass
from asx.parse.reprocess import reprocess
from asx.raw.store import ingest_document, read_document
from asx.signals.director_signals import (
    ACTIONABLE_EARLY_FLAG,
    build_cluster_buys,
    build_conviction_buys,
    cluster_buys_csv,
    conviction_buys_csv,
    counter_evidence,
)

D = Decimal
UTC = timezone.utc


def _mk_entity(conn, name: str, acn: str | None = None, kind: str = "company") -> int:
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO entities (acn, entity_kind) VALUES (%s, %s) RETURNING entity_id",
            (acn, kind),
        )
        entity_id = cur.fetchone()["entity_id"]
        from asx.ids.normalize import name_norm
        cur.execute(
            """INSERT INTO entity_names (entity_id, name, name_norm, name_kind, valid_from)
               VALUES (%s, %s, %s, 'legal', '2020-01-01')""",
            (entity_id, name, name_norm(name)),
        )
    return entity_id


def _mk_doc(conn, content: bytes, *, title="doc", lodged=None, doc_class=None,
            entity_id=None, ticker=None) -> int:
    stored = ingest_document(
        conn, content, source="test", title=title,
        lodged_at=lodged or datetime(2026, 3, 10, 10, 0, tzinfo=UTC),
        # A timestamp must always say where it came from — the schema now
        # refuses one that does not (Invariant 2). Test fixtures included:
        # if they could skip it, so could a real writer.
        lodged_at_source="manual",
        ticker_as_lodged=ticker,
    )
    with conn.cursor() as cur:
        cur.execute(
            """UPDATE documents SET doc_class = coalesce(%s, doc_class),
                   entity_id = coalesce(%s, entity_id)
               WHERE doc_id = %s""",
            (doc_class, entity_id, stored.doc_id),
        )
    return stored.doc_id


class FakeExtractor:
    """Stands in for StructuredExtractor: canned payloads, no API access."""

    def __init__(self, text_payload: dict, vision_payload: dict | None = None):
        self.text_payload = text_payload
        self.vision_payload = vision_payload if vision_payload is not None else text_payload

    def extract_text_pass(self, content, schema, prompt):
        return ExtractionPass(self.text_payload, "fake-model", "text")

    def extract_vision_pass(self, content, schema, prompt):
        return ExtractionPass(self.vision_payload, "fake-model", "vision")


def _notice_3y(**kw):
    n = {
        "director_name": "Jane Citizen",
        "date_of_change": "2026-03-06",
        "interest_nature": "direct",
        "indirect_detail": None,
        "securities": [{
            "security_class": "Ordinary shares",
            "qty_acquired": 100000,
            "qty_disposed": None,
            "consideration_text": "On-market purchase $25,000",
            "consideration_aud": 25000,
            "held_before": 400000,
            "held_after": 500000,
        }],
    }
    n.update(kw)
    return n


def _payload_3y(**kw):
    """A lodgement holds NOTICES, plural — a tenth of real ones carry more
    than one director (parser v2). Tests reach in via _securities()."""
    p = {
        "company_name": "Xyz Mining Limited",
        "ticker": "XYZ",
        "is_amendment": False,
        "notices": [_notice_3y()],
        "extraction_notes": None,
    }
    p.update(kw)
    return p


def _securities(payload):
    return payload["notices"][0]["securities"]


# --- raw zone -----------------------------------------------------------

def test_ingest_idempotent_on_content_hash(conn):
    a = ingest_document(conn, b"same bytes", source="test")
    b = ingest_document(conn, b"same bytes", source="test")
    assert a.doc_id == b.doc_id
    assert not a.already_existed and b.already_existed
    assert read_document(conn, a.doc_id) == b"same bytes"


# --- resolver -----------------------------------------------------------

def test_resolver_exact_then_fuzzy_then_alias(conn):
    target = _mk_entity(conn, "Pilbara Minerals Limited")
    _mk_entity(conn, "Acme Gold Limited")

    exact = resolve_name(conn, "PILBARA MINERALS LTD")
    assert (exact.entity_id, exact.method) == (target, "exact")

    fuzzy = resolve_name(conn, "Pilbara Minerals Ltd ACN 112 416 962")
    assert (fuzzy.entity_id, fuzzy.method) == (target, "fuzzy")

    # The fuzzy result is memoised: same string now resolves via the alias
    # table without re-scoring.
    again = resolve_name(conn, "Pilbara Minerals Ltd ACN 112 416 962")
    assert (again.entity_id, again.method) == (target, "alias")


def test_resolver_considers_former_names(conn):
    # Invariant 4: a renamed (or delisted) entity's former names stay
    # candidates — registers and old documents refer to entities by the name
    # they had then.
    entity = _mk_entity(conn, "New Name Resources Limited")
    with conn.cursor() as cur:
        cur.execute(
            """INSERT INTO entity_names (entity_id, name, name_norm, name_kind, valid_from, valid_to)
               VALUES (%s, 'Old Gold Corporation Limited', 'OLD GOLD CORPORATION', 'former',
                       '2015-01-01', '2021-06-30')""",
            (entity,),
        )
    result = resolve_name(conn, "Old Gold Corporation Ltd")
    assert (result.entity_id, result.method) == (entity, "exact")


def test_resolver_ambiguity_routes_to_review_not_guess(conn):
    _mk_entity(conn, "Acme Limited")
    _mk_entity(conn, "Acme Pty Ltd")  # same normalised name, different entity
    result = resolve_name(conn, "Acme")
    assert result.entity_id is None
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) AS n FROM review_items WHERE kind = 'resolution' AND resolved_at IS NULL")
        assert cur.fetchone()["n"] == 1


# --- share replay -------------------------------------------------------

def test_sql_replay_matches_python_and_is_bitemporal(conn):
    entity = _mk_entity(conn, "Replay Co Limited")
    doc = _mk_doc(conn, b"capital docs", doc_class="capital_reorg", entity_id=entity)

    record_anchor(conn, entity, "ORD", date(2025, 1, 1), D(100_000_000),
                  datetime(2025, 1, 1, tzinfo=UTC), doc)
    record_event(conn, entity, "ORD", "quotation", date(2025, 2, 1),
                 datetime(2025, 2, 3, tzinfo=UTC), doc, qty_delta=D(10_000_000))
    record_event(conn, entity, "ORD", "consolidation", date(2025, 6, 1),
                 datetime(2025, 6, 1, tzinfo=UTC), doc,
                 ratio_num=D(1), ratio_den=D(10))
    record_event(conn, entity, "ORD", "issue_proposed", date(2025, 7, 1),
                 datetime(2025, 7, 1, tzinfo=UTC), doc, qty_delta=D(999_999))
    conn.commit()

    events = [
        ShareEvent("quotation", date(2025, 2, 1), qty_delta=D(10_000_000),
                   knowable_at=datetime(2025, 2, 3, tzinfo=UTC)),
        ShareEvent("consolidation", date(2025, 6, 1), ratio_num=D(1), ratio_den=D(10),
                   knowable_at=datetime(2025, 6, 1, tzinfo=UTC)),
        ShareEvent("issue_proposed", date(2025, 7, 1), qty_delta=D(999_999),
                   knowable_at=datetime(2025, 7, 1, tzinfo=UTC)),
    ]

    for as_of, as_known in [
        (date(2025, 12, 31), None),
        (date(2025, 3, 1), None),
        # Bitemporal: quotation lodged 3 Feb was not knowable on 2 Feb.
        (date(2025, 3, 1), datetime(2025, 2, 2, tzinfo=UTC)),
    ]:
        sql_qty = shares_outstanding_sql(conn, entity, "ORD", as_of, as_known)
        py_qty = replay(D(100_000_000), date(2025, 1, 1), events, as_of, as_known)
        assert sql_qty == py_qty, (as_of, as_known)

    assert shares_outstanding_sql(conn, entity, "ORD", date(2025, 12, 31)) == D(11_000_000)
    # No anchor -> replay undefined, never silently zero.
    assert shares_outstanding_sql(conn, entity, "OPT", date(2025, 12, 31)) is None


def test_sql_replay_same_date_tiebreak_matches_python_sequence(conn):
    entity = _mk_entity(conn, "Samedate Co Limited")
    doc = _mk_doc(conn, b"recap docs", doc_class="capital_reorg", entity_id=entity)
    record_anchor(conn, entity, "ORD", date(2025, 1, 1), D(90_000_000),
                  datetime(2025, 1, 1, tzinfo=UTC), doc)
    # Same effective date; insertion order assigns event_ids, the tie-break.
    seq1 = record_event(conn, entity, "ORD", "quotation", date(2025, 6, 1),
                        datetime(2025, 6, 1, tzinfo=UTC), doc, qty_delta=D(10_000_000))
    seq2 = record_event(conn, entity, "ORD", "consolidation", date(2025, 6, 1),
                        datetime(2025, 6, 1, tzinfo=UTC), doc,
                        ratio_num=D(1), ratio_den=D(10))
    conn.commit()
    assert seq1 < seq2

    events = [
        ShareEvent("consolidation", date(2025, 6, 1), datetime(2025, 6, 1, tzinfo=UTC),
                   ratio_num=D(1), ratio_den=D(10), sequence=seq2),
        ShareEvent("quotation", date(2025, 6, 1), datetime(2025, 6, 1, tzinfo=UTC),
                   qty_delta=D(10_000_000), sequence=seq1),
    ]
    sql_qty = shares_outstanding_sql(conn, entity, "ORD", date(2025, 12, 31))
    py_qty = replay(D(90_000_000), date(2025, 1, 1), events, date(2025, 12, 31))
    assert sql_qty == py_qty == D(10_000_000)


def test_reconciliation_flags_vendor_zero_as_maximal_discrepancy(conn):
    entity = _mk_entity(conn, "Zero Vendor Co Limited")
    doc = _mk_doc(conn, b"anchor doc", doc_class="capital_reorg", entity_id=entity)
    record_anchor(conn, entity, "ORD", date(2025, 1, 1), D(50_000_000),
                  datetime(2025, 1, 1, tzinfo=UTC), doc)
    conn.commit()

    within = reconcile_entity(conn, entity, "ORD", date(2025, 6, 1), vendor_qty=D(0))
    conn.commit()
    assert within is False
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) AS n FROM review_items WHERE kind = 'reconciliation'")
        assert cur.fetchone()["n"] == 1
    # No vendor data is unknown, not a pass and not a failure.
    assert reconcile_entity(conn, entity, "ORD", date(2025, 6, 1), vendor_qty=None) is None


# --- parse framework end-to-end -----------------------------------------

def _setup_3y_doc(conn, content=b"synthetic 3y text", **doc_kw):
    entity = _mk_entity(conn, "Xyz Mining Limited")
    doc_id = _mk_doc(conn, content, title="Appendix 3Y", doc_class="app_3y",
                     entity_id=entity, **doc_kw)
    return entity, doc_id


def test_framework_auto_accepts_clean_extraction(conn):
    entity, doc_id = _setup_3y_doc(conn)
    outcome = run_parser_on_doc(conn, App3YParser(), doc_id, FakeExtractor(_payload_3y()))
    assert outcome.status == "validated"

    with conn.cursor() as cur:
        cur.execute("SELECT * FROM director_trades WHERE doc_id = %s", (doc_id,))
        trades = cur.fetchall()
        assert len(trades) == 1
        assert trades[0]["classification"] == "onmkt_buy_cash"
        assert trades[0]["entity_id"] == entity
        assert trades[0]["price_per_unit"] == D("0.25")
        cur.execute("SELECT parse_status FROM documents WHERE doc_id = %s", (doc_id,))
        assert cur.fetchone()["parse_status"] == "validated"
        cur.execute("SELECT * FROM parsed_records WHERE doc_id = %s", (doc_id,))
        rec = cur.fetchone()
        assert rec["parser_name"] == "app3y" and rec["passes_agree"]


def test_framework_routes_pass_disagreement_to_review(conn):
    _entity, doc_id = _setup_3y_doc(conn)
    disagreeing = _payload_3y()
    disagreeing["notices"][0]["securities"] = [dict(_securities(disagreeing)[0], held_after=999999)]
    outcome = run_parser_on_doc(
        conn, App3YParser(), doc_id, FakeExtractor(_payload_3y(), disagreeing)
    )
    assert outcome.status == "review"
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) AS n FROM director_trades WHERE doc_id = %s", (doc_id,))
        assert cur.fetchone()["n"] == 0  # nothing failing the gate reaches canonical
        cur.execute("SELECT * FROM review_items WHERE doc_id = %s AND resolved_at IS NULL", (doc_id,))
        item = cur.fetchone()
        assert item is not None and "disagree" in item["reason"]


def test_review_resolution_goes_through_validation_gate(conn):
    _entity, doc_id = _setup_3y_doc(conn)
    bad = _payload_3y()
    bad["notices"][0]["securities"] = [dict(_securities(bad)[0], held_after=1)]  # arithmetic breaks
    outcome = run_parser_on_doc(conn, App3YParser(), doc_id, FakeExtractor(bad))
    assert outcome.status == "review"

    with conn.cursor() as cur:
        cur.execute("SELECT item_id FROM review_items WHERE doc_id = %s", (doc_id,))
        item_id = cur.fetchone()["item_id"]

    # A correction that still fails validation is refused.
    still_bad = _payload_3y()
    still_bad["notices"][0]["securities"] = [dict(_securities(still_bad)[0], held_after=2)]
    refused = resolve_review_item(conn, App3YParser(), item_id, "corrected",
                                  corrected_payload=still_bad)
    assert refused is not None and not refused.ok

    fixed = _payload_3y()
    accepted = resolve_review_item(conn, App3YParser(), item_id, "corrected",
                                   corrected_payload=fixed, note="fixed held_after")
    assert accepted is not None and accepted.ok
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) AS n FROM director_trades WHERE doc_id = %s", (doc_id,))
        assert cur.fetchone()["n"] == 1
        cur.execute("SELECT parse_status FROM documents WHERE doc_id = %s", (doc_id,))
        assert cur.fetchone()["parse_status"] == "validated"


# --- supersession -------------------------------------------------------

def test_amended_notice_supersedes_earlier(conn):
    entity = _mk_entity(conn, "Amend Co Limited")
    doc1 = _mk_doc(conn, b"original 3y", doc_class="app_3y", entity_id=entity,
                   lodged=datetime(2026, 3, 10, 10, 0, tzinfo=UTC))
    doc2 = _mk_doc(conn, b"amended 3y", doc_class="app_3y", entity_id=entity,
                   lodged=datetime(2026, 3, 12, 10, 0, tzinfo=UTC))

    def row(doc_id, lodged, qty):
        return TradeRow(
            entity_id=entity, person_name_raw="Jane Citizen", doc_id=doc_id,
            event_date=date(2026, 3, 6), knowable_at=lodged,
            security_class="ORD", qty_acquired=qty,
            consideration_text="On-market purchase $10,000",
            consideration_aud=D(10000), held_before=D(0), held_after=qty,
        )

    apply_trades(conn, doc1, [row(doc1, datetime(2026, 3, 10, 10, 0, tzinfo=UTC), D(40000))])
    apply_trades(conn, doc2, [row(doc2, datetime(2026, 3, 12, 10, 0, tzinfo=UTC), D(44000))])
    conn.commit()

    def state():
        with conn.cursor() as cur:
            cur.execute(
                "SELECT doc_id, superseded, supersedes_doc FROM director_trades ORDER BY doc_id"
            )
            return {r["doc_id"]: r for r in cur.fetchall()}

    by_doc = state()
    assert by_doc[doc1]["superseded"] is True
    assert by_doc[doc2]["superseded"] is False
    assert by_doc[doc2]["supersedes_doc"] == doc1

    # Order independence: re-applying the ORIGINAL after the amendment (a
    # reprocess, or a review item resolved late) must not resurrect it.
    apply_trades(conn, doc1, [row(doc1, datetime(2026, 3, 10, 10, 0, tzinfo=UTC), D(40000))])
    conn.commit()
    by_doc = state()
    assert by_doc[doc1]["superseded"] is True
    assert by_doc[doc2]["superseded"] is False
    assert by_doc[doc2]["supersedes_doc"] == doc1


# --- signals ------------------------------------------------------------

def test_cluster_buy_signal_requires_two_directors_and_windows_knowable_at(conn):
    entity = _mk_entity(conn, "Cluster Co Limited")
    doc1 = _mk_doc(conn, b"3y one", doc_class="app_3y", entity_id=entity)
    doc2 = _mk_doc(conn, b"3y two", doc_class="app_3y", entity_id=entity)

    def row(doc_id, name, event_day, lodged_day):
        return TradeRow(
            entity_id=entity, person_name_raw=name, doc_id=doc_id,
            event_date=date(2026, 3, event_day),
            knowable_at=datetime(2026, 3, lodged_day, 10, 0, tzinfo=UTC),
            security_class="ORD", qty_acquired=D(10000),
            consideration_text="On-market purchase $5,000",
            consideration_aud=D(5000), held_before=D(0), held_after=D(10000),
        )

    apply_trades(conn, doc1, [row(doc1, "Jane Citizen", 2, 5)])
    apply_trades(conn, doc2, [row(doc2, "John Smith", 20, 24)])
    conn.commit()

    assert build_cluster_buys(conn) == 1
    with conn.cursor() as cur:
        cur.execute("SELECT * FROM signal_cluster_buys")
        sig = cur.fetchone()
    assert sig["n_directors"] == 2
    # Invariant 2: the cluster is knowable only once the LAST notice lodged.
    assert sig["knowable_at"] == datetime(2026, 3, 24, 10, 0, tzinfo=UTC)
    assert len(sig["trade_ids"]) == 2


# --- monitoring ---------------------------------------------------------

def test_monitor_treats_silence_as_alarm(conn):
    alarms = run_monitor(conn)
    assert any("ZERO documents" in a.detail for a in alarms)
    with conn.cursor() as cur:
        cur.execute("SELECT ok FROM monitor_runs ORDER BY run_id DESC LIMIT 1")
        assert cur.fetchone()["ok"] is False


def test_ops_report_renders_without_manual_work(conn):
    report = ops_report(conn)
    assert "ALARMS" in report and "REVIEW QUEUE" in report


# --- reprocess ----------------------------------------------------------

def test_reprocess_dry_run_is_side_effect_free(conn):
    _entity, doc_id = _setup_3y_doc(conn)
    run_parser_on_doc(conn, App3YParser(), doc_id, FakeExtractor(_payload_3y()))

    report = reprocess(conn, App3YParser(), FakeExtractor(_payload_3y()), apply=False)
    assert [d["doc_id"] for d in report.docs] == [doc_id]
    assert report.docs[0]["status"] == "dry_run"
    assert "DRY RUN" in report.summary()
    with conn.cursor() as cur:
        # A dry run flips no statuses and files no review items.
        cur.execute("SELECT parse_status FROM documents WHERE doc_id = %s", (doc_id,))
        assert cur.fetchone()["parse_status"] == "validated"
        cur.execute("SELECT count(*) AS n FROM review_items WHERE resolved_at IS NULL")
        assert cur.fetchone()["n"] == 0


def test_reprocess_dry_run_of_unparsed_doc_leaves_it_unparsed(conn):
    _entity, doc_id = _setup_3y_doc(conn)
    report = reprocess(conn, App3YParser(), FakeExtractor(_payload_3y()), apply=False)
    assert report.docs[0]["status"] == "dry_run"
    with conn.cursor() as cur:
        cur.execute("SELECT parse_status FROM documents WHERE doc_id = %s", (doc_id,))
        # Still unparsed: the live pipeline, not a dry run, moves documents
        # to terminal states.
        assert cur.fetchone()["parse_status"] == "unparsed"
        cur.execute("SELECT count(*) AS n FROM parsed_records WHERE doc_id = %s", (doc_id,))
        assert cur.fetchone()["n"] == 1  # parsed-zone append is the only write


def test_reprocess_apply_skips_human_resolved_docs(conn):
    _entity, doc_id = _setup_3y_doc(conn)
    bad = _payload_3y()
    bad["notices"][0]["securities"] = [dict(_securities(bad)[0], held_after=1)]
    run_parser_on_doc(conn, App3YParser(), doc_id, FakeExtractor(bad))
    with conn.cursor() as cur:
        cur.execute("SELECT item_id FROM review_items WHERE doc_id = %s", (doc_id,))
        item_id = cur.fetchone()["item_id"]
    resolve_review_item(conn, App3YParser(), item_id, "corrected",
                        corrected_payload=_payload_3y(), note="human fix")

    report = reprocess(conn, App3YParser(), FakeExtractor(bad), apply=True)
    assert report.docs[0]["status"] == "skipped_human_resolved"
    with conn.cursor() as cur:
        # The human-corrected canonical rows survive the reprocess untouched.
        cur.execute(
            "SELECT review_status, held_after FROM director_trades WHERE doc_id = %s",
            (doc_id,),
        )
        row = cur.fetchone()
    assert row["review_status"] == "human_corrected"
    assert row["held_after"] == D(500000)


def test_reprocess_excludes_rejected_docs(conn):
    _entity, doc_id = _setup_3y_doc(conn)
    bad = _payload_3y()
    bad["notices"][0]["securities"] = [dict(_securities(bad)[0], held_after=1)]
    run_parser_on_doc(conn, App3YParser(), doc_id, FakeExtractor(bad))
    with conn.cursor() as cur:
        cur.execute("SELECT item_id FROM review_items WHERE doc_id = %s", (doc_id,))
        item_id = cur.fetchone()["item_id"]
    resolve_review_item(conn, App3YParser(), item_id, "rejected", note="duplicate lodgement")

    report = reprocess(conn, App3YParser(), FakeExtractor(_payload_3y()), apply=True)
    assert report.docs == []  # human-rejected documents are not re-parsed


# --- review resolutions -------------------------------------------------

def test_rejected_resolution_retracts_canonical_rows(conn):
    entity, doc_id = _setup_3y_doc(conn)
    run_parser_on_doc(conn, App3YParser(), doc_id, FakeExtractor(_payload_3y()))
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) AS n FROM director_trades WHERE doc_id = %s", (doc_id,))
        assert cur.fetchone()["n"] == 1

    # A later re-review (e.g. after a v2 reprocess routed it back) rejects it.
    with conn.cursor() as cur:
        cur.execute(
            """INSERT INTO review_items (kind, doc_id, payload, reason)
               VALUES ('extraction', %s, %s, 'duplicate notice')
               RETURNING item_id""",
            (doc_id, '{"parser": "app3y", "version": 1}'),
        )
        item_id = cur.fetchone()["item_id"]
    resolve_review_item(conn, App3YParser(), item_id, "rejected", note="duplicate")

    with conn.cursor() as cur:
        cur.execute("SELECT count(*) AS n FROM director_trades WHERE doc_id = %s", (doc_id,))
        assert cur.fetchone()["n"] == 0  # rejected docs feed no signals
        cur.execute("SELECT parse_status FROM documents WHERE doc_id = %s", (doc_id,))
        assert cur.fetchone()["parse_status"] == "rejected"


def test_human_correction_is_persisted_on_the_item(conn):
    _entity, doc_id = _setup_3y_doc(conn)
    bad = _payload_3y()
    bad["notices"][0]["securities"] = [dict(_securities(bad)[0], held_after=1)]
    run_parser_on_doc(conn, App3YParser(), doc_id, FakeExtractor(bad))
    with conn.cursor() as cur:
        cur.execute("SELECT item_id FROM review_items WHERE doc_id = %s", (doc_id,))
        item_id = cur.fetchone()["item_id"]
    resolve_review_item(conn, App3YParser(), item_id, "corrected",
                        corrected_payload=_payload_3y())
    with conn.cursor() as cur:
        cur.execute("SELECT payload FROM review_items WHERE item_id = %s", (item_id,))
        payload = cur.fetchone()["payload"]
    # The applied payload is on the item, so the correction is reconstructable
    # even after later reprocessing (Invariant 3: hand-edits must not be
    # destroyable by the next pipeline run).
    assert payload["applied_payload"]["notices"][0]["securities"][0]["held_after"] == 500000


# --- reload must not manufacture agreement --------------------------------

class _SinglePassExtractor:
    """A rules reader: one reading, no second opinion."""

    single_pass = True

    def __init__(self, payload):
        self.payload = payload

    def extract_text_pass(self, content, schema, prompt):
        return ExtractionPass(self.payload, "rules/app3y@3", "rules")

    def extract_vision_pass(self, content, schema, prompt):
        raise NotImplementedError("reads once")


def test_reloading_a_rules_reading_reaches_the_same_verdict(conn):
    """reprocess evaluates fresh for its dry run and reloads from the parsed
    zone for --apply, so the two must agree about whether a reading is
    corroborated.

    They did not. A rules reading is stored once and written to both pass
    slots, so the reload compared it with itself, found no disagreement, and
    scored one unwitnessed reading as two readings agreeing — the exact false
    confidence rules_extractor.py exists to prevent. --apply therefore accepted
    into canonical what its own dry run had routed to review, and 70 notices
    with no arithmetic to check them against became director_trades.
    """
    from asx.parse.framework import evaluate_doc, load_stored_evaluation

    _entity, doc_id = _setup_3y_doc(conn)
    payload = _payload_3y()
    _securities(payload)[0].update(held_before=None, held_after=None)

    with conn.cursor() as cur:
        cur.execute("SELECT * FROM documents WHERE doc_id = %s", (doc_id,))
        doc = cur.fetchone()

    fresh = evaluate_doc(conn, App3YParser(), doc, _SinglePassExtractor(payload))
    conn.commit()
    assert fresh.disagreements, "a reading with nothing behind it looked corroborated"

    reloaded = load_stored_evaluation(conn, App3YParser(), doc)
    assert reloaded is not None
    assert reloaded.disagreements == fresh.disagreements, (
        "the reload manufactured agreement out of one reading stored twice")
    assert reloaded.confidence == fresh.confidence


def test_routing_to_review_removes_rows_an_earlier_version_wrote(conn):
    """Canonical holds validated readings only (Invariant 6). A document that
    validated once and later fails must not keep its old rows: they describe a
    reading the platform has just decided it cannot stand behind."""
    from asx.parse.framework import evaluate_doc, route_outcome

    _entity, doc_id = _setup_3y_doc(conn)
    with conn.cursor() as cur:
        cur.execute("SELECT * FROM documents WHERE doc_id = %s", (doc_id,))
        doc = cur.fetchone()

    good = evaluate_doc(conn, App3YParser(), doc, FakeExtractor(_payload_3y()))
    assert route_outcome(conn, App3YParser(), doc, good).status == "validated"
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) AS n FROM director_trades WHERE doc_id = %s", (doc_id,))
        assert cur.fetchone()["n"] == 1

    unverifiable = _payload_3y()
    _securities(unverifiable)[0].update(held_before=None, held_after=None)
    bad = evaluate_doc(conn, App3YParser(), doc, _SinglePassExtractor(unverifiable))
    assert route_outcome(conn, App3YParser(), doc, bad).status == "review"

    with conn.cursor() as cur:
        cur.execute("SELECT count(*) AS n FROM director_trades WHERE doc_id = %s", (doc_id,))
        assert cur.fetchone()["n"] == 0, "stale rows survived the move to review"


def test_counter_evidence_reports_prior_sells_but_never_later_ones(conn):
    """A buy screened without the sells around it reads as an endorsement of
    an omission. The column reports them — under Invariant 2, so a sell that
    lodged after the signal did cannot leak backwards into it."""
    entity = _mk_entity(conn, "Counter Co Limited")

    def doc(day):
        return _mk_doc(conn, f"3y {day}".encode(), doc_class="app_3y",
                       entity_id=entity,
                       lodged=datetime(2026, 3, day, 10, 0, tzinfo=UTC))

    def sell(doc_id, day, aud):
        apply_trades(conn, doc_id, [TradeRow(
            entity_id=entity, person_name_raw=f"Seller {day}", doc_id=doc_id,
            event_date=date(2026, 3, day),
            knowable_at=datetime(2026, 3, day, 10, 0, tzinfo=UTC),
            security_class="ORD", qty_disposed=D(1000),
            consideration_text="On-market sale", consideration_aud=D(aud),
            held_before=D(5000), held_after=D(4000))])

    stale, prior, buy_doc, later = doc(1), doc(9), doc(12), doc(20)
    # Outside the 90-day lookback, so out of scope however large.
    apply_trades(conn, stale, [TradeRow(
        entity_id=entity, person_name_raw="Ancient Seller", doc_id=stale,
        event_date=date(2025, 6, 1),
        knowable_at=datetime(2025, 6, 1, 10, 0, tzinfo=UTC),
        security_class="ORD", qty_disposed=D(1000),
        consideration_text="On-market sale", consideration_aud=D(999999),
        held_before=D(5000), held_after=D(4000))])
    sell(prior, 9, 250000)
    apply_trades(conn, buy_doc, [TradeRow(
        entity_id=entity, person_name_raw="Jane Citizen", doc_id=buy_doc,
        event_date=date(2026, 3, 12),
        knowable_at=datetime(2026, 3, 12, 10, 0, tzinfo=UTC),
        security_class="ORD", qty_acquired=D(10000),
        consideration_text="On-market purchase $5,000",
        consideration_aud=D(5000), held_before=D(1000), held_after=D(11000))])
    sell(later, 20, 400000)
    conn.commit()

    with conn.cursor() as cur:
        cur.execute("SELECT classification, count(*) FROM director_trades GROUP BY 1")
        by_class = {r["classification"]: r["count"] for r in cur.fetchall()}
    assert by_class["onmkt_sell"] == 3 and by_class["onmkt_buy_cash"] == 1

    against = counter_evidence(
        conn, entity, date(2026, 3, 12), datetime(2026, 3, 12, 10, 0, tzinfo=UTC))
    # The 9 March sell only. Not the 20 March one — it was not knowable when
    # the signal was, and including it would make this screen unreproducible.
    # Not the 2025 one — outside the lookback.
    assert against == "onmkt_sell:1:250000"

    # An entity with nothing against it says so with an empty string, which is
    # distinguishable from "not checked" only because every row is checked.
    assert counter_evidence(
        conn, _mk_entity(conn, "Clean Co Limited"), date(2026, 3, 12),
        datetime(2026, 3, 12, 10, 0, tzinfo=UTC)) == ""


def test_conviction_screen_carries_counter_evidence_column(conn):
    """The column has to reach the CSV: the defect it fixes was a screen that
    computed the right rows and printed them without their context."""
    entity = _mk_entity(conn, "Screened Co Limited")
    sell_doc = _mk_doc(conn, b"3y sell", doc_class="app_3y", entity_id=entity,
                       lodged=datetime(2026, 3, 9, 10, 0, tzinfo=UTC))
    buy_doc = _mk_doc(conn, b"3y buy", doc_class="app_3y", entity_id=entity,
                      lodged=datetime(2026, 3, 12, 10, 0, tzinfo=UTC))
    apply_trades(conn, sell_doc, [TradeRow(
        entity_id=entity, person_name_raw="Bob Seller", doc_id=sell_doc,
        event_date=date(2026, 3, 9),
        knowable_at=datetime(2026, 3, 9, 10, 0, tzinfo=UTC),
        security_class="ORD", qty_disposed=D(900000),
        consideration_text="On-market sale", consideration_aud=D(1285097),
        held_before=D(1000000), held_after=D(100000))])
    apply_trades(conn, buy_doc, [TradeRow(
        entity_id=entity, person_name_raw="Jane Citizen", doc_id=buy_doc,
        event_date=date(2026, 3, 12),
        knowable_at=datetime(2026, 3, 12, 10, 0, tzinfo=UTC),
        security_class="ORD", qty_acquired=D(10000),
        consideration_text="On-market purchase $50,000",
        consideration_aud=D(50000), held_before=D(1000), held_after=D(11000))])
    conn.commit()

    assert build_conviction_buys(conn) == 1
    rows = list(csv.DictReader(io.StringIO(conviction_buys_csv(conn))))
    assert len(rows) == 1
    assert rows[0]["counter_evidence"] == "onmkt_sell:1:1285097"
    # Also in the flags, so a reader filtering on flags alone cannot miss it.
    assert "counter_evidence" in rows[0]["coverage_flags"]


def test_actionable_date_is_flagged_when_it_came_from_pdf_creation(conn):
    """actionable_from is only as good as lodged_at. Where lodged_at is the
    PDF's creation time it precedes the ASX release — proven at 11h40m on the
    SPZ notices, enough to land on the wrong calendar day — so the screen says
    the date may be early instead of asserting it."""
    entity = _mk_entity(conn, "Soft Date Limited")

    def buy(doc_id, name, lodged):
        apply_trades(conn, doc_id, [TradeRow(
            entity_id=entity, person_name_raw=name, doc_id=doc_id,
            event_date=date(2026, 3, 2), knowable_at=lodged,
            security_class="ORD", qty_acquired=D(10000),
            consideration_text="On-market purchase $5,000",
            consideration_aud=D(5000), held_before=D(1000), held_after=D(11000))])

    lodged = datetime(2026, 3, 5, 10, 0, tzinfo=UTC)
    hard = _mk_doc(conn, b"3y hard", doc_class="app_3y", entity_id=entity,
                   lodged=lodged)
    soft = _mk_doc(conn, b"3y soft", doc_class="app_3y", entity_id=entity,
                   lodged=lodged)
    with conn.cursor() as cur:
        cur.execute("UPDATE documents SET lodged_at_source='market_index_alert'"
                    " WHERE doc_id=%s", (hard,))
        cur.execute("UPDATE documents SET lodged_at_source='pdf_creation'"
                    " WHERE doc_id=%s", (soft,))
    buy(hard, "Jane Citizen", lodged)
    conn.commit()

    def conviction_flags():
        build_conviction_buys(conn)
        rows = list(csv.DictReader(io.StringIO(conviction_buys_csv(conn))))
        return {r["ticker"] or r["entity"]: r["coverage_flags"] for r in rows}

    # A hard lodgement timestamp asserts its date without qualification.
    assert ACTIONABLE_EARLY_FLAG not in conviction_flags()["Soft Date Limited"]

    # A second director, dated from PDF creation, makes the cluster's
    # actionable_from soft — the cluster is knowable only once its LAST notice
    # is public, so one soft member is enough to qualify the whole row.
    buy(soft, "John Smith", lodged)
    conn.commit()
    assert ACTIONABLE_EARLY_FLAG in conviction_flags()["Soft Date Limited"]

    assert build_cluster_buys(conn) == 1
    cluster = list(csv.DictReader(io.StringIO(cluster_buys_csv(conn))))[0]
    assert ACTIONABLE_EARLY_FLAG in cluster["coverage_flags"]
