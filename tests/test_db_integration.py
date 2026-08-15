"""Integration tests against live PostgreSQL. The `conn` fixture provides a
migrated, empty asx_test database and a tmp raw zone."""

from datetime import date, datetime, timezone
from decimal import Decimal

import pytest

from asx.canonical.director_trades import TradeRow, apply_trades
from asx.canonical.shares import (
    ShareEvent,
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
from asx.signals.director_signals import build_cluster_buys

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


def _payload_3y(**kw):
    p = {
        "company_name": "Xyz Mining Limited",
        "ticker": "XYZ",
        "director_name": "Jane Citizen",
        "date_of_change": "2026-03-06",
        "interest_nature": "direct",
        "indirect_detail": None,
        "is_amendment": False,
        "securities": [{
            "security_class": "Ordinary shares",
            "qty_acquired": 100000,
            "qty_disposed": None,
            "consideration_text": "On-market purchase $25,000",
            "consideration_aud": 25000,
            "held_before": 400000,
            "held_after": 500000,
        }],
        "extraction_notes": None,
    }
    p.update(kw)
    return p


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
    disagreeing["securities"] = [dict(disagreeing["securities"][0], held_after=999999)]
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
    bad["securities"] = [dict(bad["securities"][0], held_after=1)]  # arithmetic breaks
    outcome = run_parser_on_doc(conn, App3YParser(), doc_id, FakeExtractor(bad))
    assert outcome.status == "review"

    with conn.cursor() as cur:
        cur.execute("SELECT item_id FROM review_items WHERE doc_id = %s", (doc_id,))
        item_id = cur.fetchone()["item_id"]

    # A correction that still fails validation is refused.
    still_bad = _payload_3y()
    still_bad["securities"] = [dict(still_bad["securities"][0], held_after=2)]
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

    with conn.cursor() as cur:
        cur.execute(
            "SELECT doc_id, superseded, supersedes_doc FROM director_trades ORDER BY doc_id"
        )
        rows = cur.fetchall()
    by_doc = {r["doc_id"]: r for r in rows}
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

def test_reprocess_dry_run_reports_diff_without_applying(conn):
    _entity, doc_id = _setup_3y_doc(conn)
    run_parser_on_doc(conn, App3YParser(), doc_id, FakeExtractor(_payload_3y()))

    report = reprocess(conn, App3YParser(), FakeExtractor(_payload_3y()), apply=False)
    assert [d["doc_id"] for d in report.docs] == [doc_id]
    assert report.docs[0]["changed_fields"] == []  # same parser version, same payload
    assert "DRY RUN" in report.summary()
