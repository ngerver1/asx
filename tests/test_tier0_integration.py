"""Integration tests for the Tier 0 detection/possession flow."""

import json
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import pytest

from asx.backtest.harness import BacktestUnavailableError, run_event_study
from asx.canonical.shares import (
    reconcile_against_manual,
    record_anchor,
    record_manual_share_count,
)
from asx.ingest.detection import Detection, open_detections, record_detection
from asx.ingest.possession import attach_document, file_captured_documents
from asx.monitor.checks import check_capture_gap, run_monitor
from asx.universe.index_membership import (
    is_index_member,
    load_membership,
    parse_holdings_csv,
)

D = Decimal
UTC = timezone.utc


def _entity(conn, name, ticker=None, listed_from=date(2020, 1, 1)):
    from asx.ids.normalize import name_norm

    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO entities (entity_kind) VALUES ('company') RETURNING entity_id"
        )
        eid = cur.fetchone()["entity_id"]
        cur.execute(
            """INSERT INTO entity_names (entity_id, name, name_norm, name_kind, valid_from)
               VALUES (%s, %s, %s, 'legal', %s)""",
            (eid, name, name_norm(name), listed_from),
        )
        if ticker:
            cur.execute(
                """INSERT INTO listings (entity_id, ticker, valid_from)
                   VALUES (%s, %s, %s)""",
                (eid, ticker, listed_from),
            )
    return eid


def _detection(**kw):
    base = dict(
        detection_source="market_index_alert",
        source_ref="<msg-1@example.com>",
        ticker="XYZ",
        title="Appendix 3Y - Change of Director's Interest Notice",
        lodged_at=datetime(2026, 8, 13, 23, 30, tzinfo=UTC),  # 09:30 Sydney 14 Aug
        detected_at=datetime(2026, 8, 13, 23, 35, tzinfo=UTC),
    )
    base.update(kw)
    return Detection(**base)


# --- detection ----------------------------------------------------------

def test_detection_records_without_bytes(conn):
    eid = _entity(conn, "Xyz Mining Limited", "XYZ")
    doc_id, is_new = record_detection(conn, _detection())
    conn.commit()
    assert is_new
    with conn.cursor() as cur:
        cur.execute("SELECT * FROM documents WHERE doc_id = %s", (doc_id,))
        doc = cur.fetchone()
    assert doc["parse_status"] == "detected"
    assert doc["sha256"] is None and doc["storage_path"] is None
    assert doc["doc_class"] == "app_3y"
    assert doc["entity_id"] == eid          # resolved via listings, not by code
    assert doc["detection_source"] == "market_index_alert"


def test_detection_is_idempotent(conn):
    _entity(conn, "Xyz Mining Limited", "XYZ")
    first, new1 = record_detection(conn, _detection())
    second, new2 = record_detection(conn, _detection())
    conn.commit()
    assert first == second
    assert new1 and not new2


def test_document_cannot_leave_detected_without_bytes(conn):
    _entity(conn, "Xyz Mining Limited", "XYZ")
    doc_id, _ = record_detection(conn, _detection())
    conn.commit()
    with pytest.raises(Exception):  # CHECK constraint
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE documents SET parse_status = 'unparsed' WHERE doc_id = %s",
                (doc_id,),
            )
        conn.commit()
    conn.rollback()


# --- possession ---------------------------------------------------------

def test_capture_attaches_bytes_to_the_detection(conn, tmp_path):
    _entity(conn, "Xyz Mining Limited", "XYZ")
    doc_id, _ = record_detection(conn, _detection())
    conn.commit()

    capture = tmp_path / "capture"
    capture.mkdir()
    (capture / "xyz_3y.pdf").write_bytes(b"%PDF- captured 3y")
    (capture / "xyz_3y.pdf.meta.json").write_text(json.dumps({
        "ticker": "XYZ", "lodged_at": "2026-08-14T09:30:00",
    }))

    stats = file_captured_documents(conn, capture)
    assert stats["attached"] == 1 and stats["standalone"] == 0

    with conn.cursor() as cur:
        cur.execute("SELECT * FROM documents WHERE doc_id = %s", (doc_id,))
        doc = cur.fetchone()
    assert doc["parse_status"] == "unparsed"
    assert doc["possession_source"] == "manual_capture"
    assert doc["sha256"] is not None


def test_capture_without_a_prior_detection_creates_a_standalone_document(conn, tmp_path):
    capture = tmp_path / "capture"
    capture.mkdir()
    (capture / "found_while_browsing.pdf").write_bytes(b"%PDF- ad hoc")
    stats = file_captured_documents(conn, capture)
    assert stats["standalone"] == 1


def test_duplicate_capture_is_not_double_stored(conn, tmp_path):
    _entity(conn, "Xyz Mining Limited", "XYZ")
    doc_a, _ = record_detection(conn, _detection())
    doc_b, _ = record_detection(conn, _detection(source_ref="<msg-2@example.com>"))
    conn.commit()

    assert attach_document(conn, doc_a, b"%PDF- same bytes", "manual_capture")
    # The same announcement captured twice under two alerts.
    assert not attach_document(conn, doc_b, b"%PDF- same bytes", "manual_capture")
    conn.commit()
    with conn.cursor() as cur:
        cur.execute("SELECT parse_status FROM documents WHERE doc_id = %s", (doc_b,))
        assert cur.fetchone()["parse_status"] == "not_applicable"


# --- capture-gap monitoring --------------------------------------------

def test_uncaptured_detection_raises_the_capture_gap_alarm(conn):
    _entity(conn, "Xyz Mining Limited", "XYZ")
    old = datetime.now(UTC) - timedelta(days=6)
    record_detection(conn, _detection(detected_at=old, lodged_at=old))
    conn.commit()

    alarms = check_capture_gap(conn, datetime.now(UTC))
    assert any(a.check == "capture_gap" for a in alarms)
    assert "hole in the dataset" in " ".join(a.detail for a in alarms)


def test_captured_detection_clears_the_gap(conn):
    _entity(conn, "Xyz Mining Limited", "XYZ")
    old = datetime.now(UTC) - timedelta(days=6)
    doc_id, _ = record_detection(conn, _detection(detected_at=old, lodged_at=old))
    attach_document(conn, doc_id, b"%PDF- captured", "manual_capture")
    conn.commit()
    assert not [a for a in check_capture_gap(conn, datetime.now(UTC))
                if a.check == "capture_gap"]


def test_zero_detections_alarms(conn):
    alarms = run_monitor(conn)
    assert any("detections_all" in a.detail and "ZERO" in a.detail for a in alarms)


# --- index membership proxy --------------------------------------------

HOLDINGS_CSV = b"""Vanguard Australian Shares Index ETF
Holdings as at 31/07/2026

Ticker,Name,Weight
BHP,BHP Group Ltd,10.2
XYZ,Xyz Mining Limited,0.01
CBA AU,Commonwealth Bank,8.4
"""


def test_parse_holdings_skips_preamble_and_normalises_tickers():
    assert parse_holdings_csv(HOLDINGS_CSV) == ["BHP", "XYZ", "CBA"]


def test_membership_resolves_tickers_to_entities_and_reports_misses(conn):
    eid = _entity(conn, "Xyz Mining Limited", "XYZ")
    result = load_membership(
        conn, HOLDINGS_CSV, source_url="https://issuer.example/holdings.csv",
        as_of=date(2026, 7, 31),
        knowable_at=datetime(2026, 7, 31, tzinfo=UTC),
    )
    assert result.resolved == 1
    assert set(result.unresolved) == {"BHP", "CBA"}   # recorded, not joined on code
    assert is_index_member(conn, eid, date(2026, 8, 5)) is True


def test_membership_unknown_when_snapshot_is_stale(conn):
    eid = _entity(conn, "Xyz Mining Limited", "XYZ")
    load_membership(conn, HOLDINGS_CSV,
                    source_url="https://issuer.example/holdings.csv",
                    as_of=date(2026, 1, 1),
                    knowable_at=datetime(2026, 1, 1, tzinfo=UTC))
    # Six months later the snapshot is far too old to assert membership.
    assert is_index_member(conn, eid, date(2026, 7, 1)) is None


def test_membership_does_not_see_future_rebalances(conn):
    eid = _entity(conn, "Xyz Mining Limited", "XYZ")
    load_membership(conn, HOLDINGS_CSV,
                    source_url="https://issuer.example/holdings.csv",
                    as_of=date(2026, 7, 31),
                    knowable_at=datetime(2026, 7, 31, tzinfo=UTC))
    # Asking as at a date before the file existed must not use it.
    assert is_index_member(conn, eid, date(2026, 6, 1)) is None


# --- signal ceiling -----------------------------------------------------

def test_cluster_buy_excludes_index_members_and_flags_coverage(conn):
    from asx.canonical.director_trades import TradeRow, apply_trades
    from asx.raw.store import ingest_document
    from asx.signals.director_signals import build_cluster_buys

    eid = _entity(conn, "Xyz Mining Limited", "XYZ")
    doc1 = ingest_document(conn, b"3y one", source="t", title="3Y",
                           lodged_at=datetime(2026, 3, 5, tzinfo=UTC)).doc_id
    doc2 = ingest_document(conn, b"3y two", source="t", title="3Y",
                           lodged_at=datetime(2026, 3, 24, tzinfo=UTC)).doc_id

    def row(doc_id, name, day, lodged):
        return TradeRow(
            entity_id=eid, person_name_raw=name, doc_id=doc_id,
            event_date=date(2026, 3, day),
            knowable_at=datetime(2026, 3, lodged, 10, 0, tzinfo=UTC),
            security_class="ORD", qty_acquired=D(10000),
            consideration_text="On-market purchase $5,000",
            consideration_aud=D(5000), held_before=D(0), held_after=D(10000),
        )

    apply_trades(conn, doc1, [row(doc1, "Jane Citizen", 2, 5)])
    apply_trades(conn, doc2, [row(doc2, "John Smith", 20, 24)])
    conn.commit()

    # No membership data at all: unknown, so the cluster is retained and said
    # to be unknown rather than silently dropped or assumed small.
    assert build_cluster_buys(conn) == 1
    with conn.cursor() as cur:
        cur.execute("SELECT coverage_flags FROM signal_cluster_buys")
        flags = cur.fetchone()["coverage_flags"]
    assert any("size_ceiling_proxy" in f for f in flags)
    assert "membership_unknown" in flags

    # Now mark it an index member as at the cluster's knowable date.
    load_membership(conn, HOLDINGS_CSV,
                    source_url="https://issuer.example/holdings.csv",
                    as_of=date(2026, 3, 20),
                    knowable_at=datetime(2026, 3, 20, tzinfo=UTC))
    assert build_cluster_buys(conn) == 0   # above the size ceiling


# --- price-dependent work refuses to run -------------------------------

def test_backtesting_refuses_without_a_price_source():
    with pytest.raises(BacktestUnavailableError) as excinfo:
        run_event_study()
    message = str(excinfo.value)
    assert "ACCESS_DECISION" in message
    assert "survivorship" in message


# --- manual share-count reconciliation ---------------------------------

def test_reconciliation_runs_against_manually_recorded_figures(conn):
    from asx.raw.store import ingest_document

    eid = _entity(conn, "Xyz Mining Limited", "XYZ")
    doc = ingest_document(conn, b"anchor", source="t", title="2A",
                          lodged_at=datetime(2026, 1, 1, tzinfo=UTC)).doc_id
    record_anchor(conn, eid, "ORD", date(2026, 1, 1), D(50_000_000),
                  datetime(2026, 1, 1, tzinfo=UTC), doc)
    conn.commit()

    # No manual reading yet: unknown, not a pass.
    assert reconcile_against_manual(conn, eid, "ORD", date(2026, 6, 1)) is None

    record_manual_share_count(conn, eid, "ORD", date(2026, 5, 1), D(50_000_000),
                              source_note="ASX company page, read 2026-05-01")
    conn.commit()
    assert reconcile_against_manual(conn, eid, "ORD", date(2026, 6, 1)) is True

    record_manual_share_count(conn, eid, "ORD", date(2026, 5, 2), D(75_000_000),
                              source_note="ASX company page, read 2026-05-02")
    conn.commit()
    assert reconcile_against_manual(conn, eid, "ORD", date(2026, 6, 1)) is False


# --- detection persists both halves of the URL split ---------------------

def test_detection_persists_urls_and_worklist_can_show_the_manual_one(conn):
    """The IR-fetch route read URLs out of source_ref, which for a mailbox
    detection is a Message-ID — so it fetched nothing, forever, and reported
    that as a quiet day."""
    import email as _email

    from asx.ingest.detection import open_detections, record_detection
    from asx.ingest.mailbox import detection_from_email
    from asx.ingest.possession import _document_urls_for

    raw = ("From: alerts@marketindex.com.au\n"
           "Subject: XYZ - Change in Director's Interest Notice\n"
           "Message-ID: <persist@marketindex.com.au>\n"
           "Date: Tue, 18 Aug 2026 09:35:00 +1000\n\n"
           "Lodged 18/08/2026 9:30 AM\n"
           "https://www.asx.com.au/asxpdf/20260818/pdf/xyz.pdf\n"
           "https://xyzlimited.com.au/investors/3y-aug26.pdf\n")
    doc_id, is_new = record_detection(conn, detection_from_email(
        _email.message_from_string(raw)))
    assert is_new
    conn.commit()

    assert _document_urls_for(conn, doc_id) == [
        "https://xyzlimited.com.au/investors/3y-aug26.pdf"]
    row = next(r for r in open_detections(conn) if r["doc_id"] == doc_id)
    assert row["manual_open_urls"] == [
        "https://www.asx.com.au/asxpdf/20260818/pdf/xyz.pdf"]


def test_an_unresolvable_ticker_is_queued_not_silently_binned(conn):
    import email as _email

    from asx.ingest.detection import record_detection
    from asx.ingest.mailbox import detection_from_email

    raw = ("From: alerts@marketindex.com.au\n"
           "Subject: ASX:ZZZ - Announcement: Change in Director's Interest Notice\n"
           "Message-ID: <unknown-ticker@marketindex.com.au>\n"
           "Date: Tue, 18 Aug 2026 09:35:00 +1000\n\n"
           "Lodged 18/08/2026 9:30 AM\n")
    doc_id, _ = record_detection(conn, detection_from_email(
        _email.message_from_string(raw)))
    with conn.cursor() as cur:
        cur.execute(
            """SELECT reason FROM review_items
               WHERE kind = 'detection' AND doc_id = %s""", (doc_id,))
        reasons = [r["reason"] for r in cur.fetchall()]
    assert any("does not resolve" in r for r in reasons)


def test_ir_fetch_refuses_to_store_a_non_pdf_response(conn, monkeypatch):
    """A login wall or cookie banner returns 200 with HTML. Storing it as the
    announcement poisons the raw zone AND clears the capture-gap alarm that
    says the document is still missing."""
    import email as _email
    from types import SimpleNamespace

    from asx.ingest import possession
    from asx.ingest.detection import record_detection
    from asx.ingest.mailbox import detection_from_email

    raw = ("From: alerts@marketindex.com.au\n"
           "Subject: XYZ - Change in Director's Interest Notice\n"
           "Message-ID: <nonpdf@marketindex.com.au>\n"
           "Date: Tue, 18 Aug 2026 09:35:00 +1000\n\n"
           "Lodged 18/08/2026 9:30 AM\n"
           "https://xyzlimited.com.au/investors/3y-aug26.pdf\n")
    doc_id, _ = record_detection(conn, detection_from_email(
        _email.message_from_string(raw)))
    conn.commit()

    monkeypatch.setattr(possession, "fetch", lambda url, **kw: SimpleNamespace(
        content=b"<html><body>Please sign in</body></html>",
        content_type="text/html", url=url))
    stats = possession.fetch_ir_documents(conn)
    assert stats["not_a_document"] == 1 and stats["captured"] == 0

    with conn.cursor() as cur:
        cur.execute("SELECT parse_status, sha256 FROM documents WHERE doc_id = %s",
                    (doc_id,))
        row = cur.fetchone()
    assert row["parse_status"] == "detected"   # still an open capture gap
    assert row["sha256"] is None
