"""Integration tests for the Tier 0 detection/possession flow."""

import json
from pathlib import Path
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


DOCS = Path(__file__).parent.parent / "fixtures" / "app3y" / "documents"


def _real_pdf():
    """A captured announcement that carries its own creation date."""
    from asx.ingest.lodgement import pdf_created_at

    for path in sorted(DOCS.glob("*.pdf")):
        content = path.read_bytes()
        if pdf_created_at(content):
            return content
    pytest.skip("no dated PDF in the corpus")


def _capture(tmp_path, content, meta=None, name="found.pdf"):
    capture = tmp_path / "capture"
    capture.mkdir(exist_ok=True)
    (capture / name).write_bytes(content)
    if meta is not None:
        (capture / f"{name}.meta.json").write_text(json.dumps(meta))
    return capture


def _doc(conn, ticker_free=True):
    with conn.cursor() as cur:
        cur.execute("""SELECT lodged_at, lodged_at_source FROM documents
                       ORDER BY doc_id DESC LIMIT 1""")
        return cur.fetchone()


def test_a_capture_with_no_sidecar_is_dated_from_the_document_itself(conn, tmp_path):
    """asx.ingest.lodgement existed, was documented and was tested — and
    nothing in the pipeline called it. Every capture without a sidecar
    timestamp was therefore ingested undated, and an undated document
    produces no canonical rows, so 52 captured Appendix 3Ys sat in the corpus
    yielding nothing. This is the test whose absence let that happen.
    """
    from asx.ingest.lodgement import pdf_created_at

    content = _real_pdf()
    file_captured_documents(conn, _capture(tmp_path, content))

    doc = _doc(conn)
    assert doc["lodged_at"] is not None, "captured document was left undated"
    assert doc["lodged_at_source"] == "pdf_creation"
    assert doc["lodged_at"] == pdf_created_at(content)


def test_a_sidecar_timestamp_is_recorded_as_a_human_statement(conn, tmp_path):
    """It is also the only way this path could insert a row at all: a
    timestamp with no stated source is refused by
    documents_lodged_at_provenance, so a sidecar capture raised a constraint
    violation rather than being filed."""
    file_captured_documents(conn, _capture(
        tmp_path, _real_pdf(), meta={"lodged_at": "2026-08-14T09:30:00"}))

    from asx.ids.market_time import SYDNEY

    doc = _doc(conn)
    assert doc["lodged_at_source"] == "manual"
    # A naive sidecar time is Sydney local, so 09:30 on the 14th is 23:30 UTC
    # on the 13th — the same instant, stored the way every other row is.
    assert doc["lodged_at"] == datetime(2026, 8, 14, 9, 30, tzinfo=SYDNEY)


def test_a_document_that_states_no_date_stays_undated(conn, tmp_path):
    """No third source (asx.ingest.lodgement). Guessing here would put an
    invented knowable_at on every trade the document produces, which is worse
    than producing none."""
    file_captured_documents(conn, _capture(tmp_path, b"%PDF- no creation date"))

    doc = _doc(conn)
    assert doc["lodged_at"] is None and doc["lodged_at_source"] is None


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
                           lodged_at=datetime(2026, 3, 5, tzinfo=UTC), lodged_at_source="manual").doc_id
    doc2 = ingest_document(conn, b"3y two", source="t", title="3Y",
                           lodged_at=datetime(2026, 3, 24, tzinfo=UTC), lodged_at_source="manual").doc_id

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
                          lodged_at=datetime(2026, 1, 1, tzinfo=UTC), lodged_at_source="manual").doc_id
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


def test_a_pdf_with_a_useless_filename_is_matched_by_reading_it(conn, tmp_path):
    """A browser names downloads whatever it likes — "documentdownload (3).pdf"
    carries no ticker and no announcement number. Matching on the filename
    then fails and the document lands standalone: possession with no link to
    the detection that predicted it, which leaves the capture gap open forever
    while the bytes sit in the raw zone.

    The document is not ambiguous about who it belongs to. Every lodged form
    prints the entity's ABN.
    """
    import shutil

    from asx.ingest.possession import file_captured_documents

    from pathlib import Path as _Path

    gold = (_Path(__file__).parent.parent / "fixtures" / "app3y" / "documents"
            / "6A1339259.pdf")
    if not gold.exists():
        import pytest
        pytest.skip("gold document not present")

    with conn.cursor() as cur:
        cur.execute("INSERT INTO entities (abn, entity_kind) VALUES "
                    "('54118912495', 'company') RETURNING entity_id")
        entity_id = cur.fetchone()["entity_id"]
        cur.execute(
            """INSERT INTO documents (source, entity_id, ticker_as_lodged,
                   title, doc_class, detection_source, detected_at,
                   detection_key, parse_status)
               VALUES ('market_index_alert', %s, 'CYL',
                   'Change of Director''s Interest Notice', 'app_3y',
                   'market_index_alert', now(), 'k-content-match', 'detected')
               RETURNING doc_id""", (entity_id,))
        doc_id = cur.fetchone()["doc_id"]
    conn.commit()

    drop = tmp_path / "cap"
    drop.mkdir()
    shutil.copy(gold, drop / "documentdownload (3).pdf")

    stats = file_captured_documents(conn, drop)
    assert stats["attached"] == 1 and stats["standalone"] == 0

    with conn.cursor() as cur:
        cur.execute("SELECT parse_status, sha256 FROM documents WHERE doc_id = %s",
                    (doc_id,))
        row = cur.fetchone()
    assert row["parse_status"] == "unparsed" and row["sha256"]


def test_content_matching_refuses_when_two_detections_could_fit(conn, tmp_path):
    """A company lodging two 3Ys on one day is ordinary. Attaching the PDF to
    the likelier-looking one would be a guess presented as provenance."""
    import shutil

    from asx.ingest.possession import file_captured_documents

    from pathlib import Path as _Path

    gold = (_Path(__file__).parent.parent / "fixtures" / "app3y" / "documents"
            / "6A1339259.pdf")
    if not gold.exists():
        import pytest
        pytest.skip("gold document not present")

    with conn.cursor() as cur:
        cur.execute("INSERT INTO entities (abn, entity_kind) VALUES "
                    "('54118912495', 'company') RETURNING entity_id")
        entity_id = cur.fetchone()["entity_id"]
        for key in ("k-dup-1", "k-dup-2"):
            cur.execute(
                """INSERT INTO documents (source, entity_id, ticker_as_lodged,
                       title, doc_class, detection_source, detected_at,
                       detection_key, parse_status)
                   VALUES ('market_index_alert', %s, 'CYL', 'Appendix 3Y',
                       'app_3y', 'market_index_alert', now(), %s, 'detected')""",
                (entity_id, key))
    conn.commit()

    drop = tmp_path / "cap"
    drop.mkdir()
    shutil.copy(gold, drop / "documentdownload.pdf")
    stats = file_captured_documents(conn, drop)
    assert stats["attached"] == 0 and stats["standalone"] == 1


def test_an_acn_mislabelled_as_an_abn_is_read_by_its_shape(conn):
    """Augustus Minerals prints "ABN 651 349 638" on its Appendix 3Y — nine
    digits, so it is an ACN wearing an ABN's label. An ABN has eleven digits
    and an ACN nine, so the number identifies itself; trusting the issuer's
    label instead leaves the document unidentifiable."""
    from asx.ingest.possession import _ABN_LABELLED_RE, _MISLABELLED_ACN_RE

    text = "Name of entity AUGUSTUS MINERALS LIMITED ABN 651 349 638 We"
    assert not _ABN_LABELLED_RE.findall(text)
    assert _MISLABELLED_ACN_RE.findall(text) == ["651 349 638"]
    # A real 11-digit ABN must NOT be mistaken for an ACN.
    assert not _MISLABELLED_ACN_RE.findall("ABN 51 121 033 396")


def test_a_current_name_outranks_another_companys_former_name_when_filing(conn):
    """The Kingston/Nexus trap, reached from the capture side: a name that is
    one company's current legal name and another's former name must not make
    an unambiguous document look ambiguous."""
    from asx.ingest.possession import DocumentFacts, _entity_for_document

    with conn.cursor() as cur:
        cur.execute("INSERT INTO entities (entity_kind) VALUES ('company') "
                    "RETURNING entity_id")
        current = cur.fetchone()["entity_id"]
        cur.execute("INSERT INTO entities (entity_kind) VALUES ('company') "
                    "RETURNING entity_id")
        other = cur.fetchone()["entity_id"]
        cur.execute(
            """INSERT INTO entity_names (entity_id, name, name_norm, name_kind,
                                         valid_from, valid_to)
               VALUES (%s, 'Augustus Minerals Limited', 'AUGUSTUS MINERALS',
                       'legal', '2020-01-01', NULL),
                      (%s, 'Augustus Minerals Limited', 'AUGUSTUS MINERALS',
                       'former', '2005-01-01', '2019-12-31')""",
            (current, other))
    facts = DocumentFacts([], [], "app_3y", "Augustus Minerals Limited", "")
    assert _entity_for_document(conn, facts) == current


def test_a_file_named_by_ticker_alone_finds_its_detection(conn):
    """The owner works in tickers, so "SGQ.pdf" must be enough when only one
    announcement for that code is outstanding."""
    from pathlib import Path as _P

    from asx.ingest.possession import file_captured_documents

    gold = (_P(__file__).parent.parent / "fixtures" / "app3y" / "documents"
            / "6A1339259.pdf")
    if not gold.exists():
        import pytest
        pytest.skip("gold document not present")

    with conn.cursor() as cur:
        cur.execute("INSERT INTO entities (entity_kind) VALUES ('company') "
                    "RETURNING entity_id")
        eid = cur.fetchone()["entity_id"]
        cur.execute(
            """INSERT INTO documents (source, entity_id, ticker_as_lodged, title,
                   doc_class, detection_source, detected_at, detection_key,
                   parse_status)
               VALUES ('market_index_alert', %s, 'SGQ', 'Appendix 3Y', 'app_3y',
                   'market_index_alert', now(), 'k-ticker-only', 'detected')
               RETURNING doc_id""", (eid,))
        doc_id = cur.fetchone()["doc_id"]
    conn.commit()

    import shutil
    drop = _P(str(conn.info.host)) if False else None
    import tempfile
    tmp = _P(tempfile.mkdtemp())
    shutil.copy(gold, tmp / "SGQ.pdf")
    stats = file_captured_documents(conn, tmp)
    assert stats["attached"] == 1
    with conn.cursor() as cur:
        cur.execute("SELECT parse_status FROM documents WHERE doc_id = %s", (doc_id,))
        assert cur.fetchone()["parse_status"] == "unparsed"


def test_ticker_alone_refuses_when_the_company_lodged_several(conn):
    """Black Canyon lodged four identically-titled notices in a day. "BCA.pdf"
    cannot say which, and guessing would be provenance by coin-toss."""
    from pathlib import Path as _P

    from asx.ingest.possession import file_captured_documents

    gold = (_P(__file__).parent.parent / "fixtures" / "app3y" / "documents"
            / "6A1339259.pdf")
    if not gold.exists():
        import pytest
        pytest.skip("gold document not present")

    with conn.cursor() as cur:
        cur.execute("INSERT INTO entities (entity_kind) VALUES ('company') "
                    "RETURNING entity_id")
        eid = cur.fetchone()["entity_id"]
        for k in ("k-bca-1", "k-bca-2"):
            cur.execute(
                """INSERT INTO documents (source, entity_id, ticker_as_lodged,
                       title, doc_class, detection_source, detected_at,
                       detection_key, parse_status)
                   VALUES ('market_index_alert', %s, 'BCA', 'Appendix 3Y',
                       'app_3y', 'market_index_alert', now(), %s, 'detected')""",
                (eid, k))
    conn.commit()

    import shutil
    import tempfile
    tmp = _P(tempfile.mkdtemp())
    shutil.copy(gold, tmp / "BCA.pdf")
    stats = file_captured_documents(conn, tmp)
    assert stats["attached"] == 0


# --- conviction sizing (SPEC §7, second named signal) --------------------

def _conviction_trade(conn, eid, name, *, held_before, qty, spend, day=10, lodged=12):
    from asx.canonical.director_trades import TradeRow, apply_trades
    from asx.raw.store import ingest_document

    doc = ingest_document(conn, f"3y {name} {qty}".encode(), source="t", title="3Y",
                          lodged_at=datetime(2026, 3, lodged, tzinfo=UTC),
                          lodged_at_source="manual").doc_id
    apply_trades(conn, doc, [TradeRow(
        entity_id=eid, person_name_raw=name, doc_id=doc,
        event_date=date(2026, 3, day),
        knowable_at=datetime(2026, 3, lodged, 10, 0, tzinfo=UTC),
        security_class="ORD", qty_acquired=D(qty),
        consideration_text="On-market purchase",
        consideration_aud=D(spend), held_before=D(held_before),
        held_after=D(held_before + qty))])
    conn.commit()
    return doc


def test_a_director_sharply_raising_their_own_stake_is_a_signal(conn):
    """The case cluster buying cannot see: one director, buying heavily.
    On the real corpus this is a $896,000 purchase that doubled the buyer's
    holding and scored nothing, while a $2,402 two-director cluster topped the
    screen."""
    from asx.signals.director_signals import build_conviction_buys

    eid = _entity(conn, "Xyz Mining Limited", "XYZ")
    _conviction_trade(conn, eid, "Jane Citizen",
                      held_before=100000, qty=145000, spend=896000)

    assert build_conviction_buys(conn) == 1
    with conn.cursor() as cur:
        cur.execute("SELECT * FROM signal_conviction_buys")
        row = cur.fetchone()
    assert row["stake_increase"] == D("1.45")
    assert row["person_name_raw"] == "Jane Citizen"
    # Actionable from this notice's own lodgement — unlike a cluster, there is
    # no later member to wait for.
    assert row["knowable_at"] == datetime(2026, 3, 12, 10, 0, tzinfo=UTC)


def test_a_big_cheque_that_barely_moves_the_stake_is_not_a_signal(conn):
    """$500,000 against an 83-million-share holding changes that director's
    exposure by 0.1%. Ranking on dollars would put it near the top; ranking on
    conviction correctly leaves it out."""
    from asx.signals.director_signals import build_conviction_buys

    eid = _entity(conn, "Xyz Mining Limited", "XYZ")
    _conviction_trade(conn, eid, "Big Holder",
                      held_before=83000000, qty=70000, spend=500000)

    assert build_conviction_buys(conn) == 0


def test_a_large_percentage_on_a_tiny_spend_is_flagged_not_dropped(conn):
    """A 27% increase costing $2,460 says the holding was tiny, not that
    anyone changed their mind. The reader is told, and decides."""
    from asx.signals.director_signals import build_conviction_buys

    eid = _entity(conn, "Xyz Mining Limited", "XYZ")
    _conviction_trade(conn, eid, "Small Fry",
                      held_before=10000, qty=4000, spend=2460)

    assert build_conviction_buys(conn) == 1
    with conn.cursor() as cur:
        cur.execute("SELECT coverage_flags FROM signal_conviction_buys")
        assert "small_absolute_spend" in cur.fetchone()["coverage_flags"]


def test_conviction_buys_respect_the_same_size_ceiling(conn):
    """A signal that ignored the size cut would put large caps back on a
    smallcap screen through the side door."""
    from asx.signals.director_signals import build_conviction_buys

    eid = _entity(conn, "Xyz Mining Limited", "XYZ")
    _conviction_trade(conn, eid, "Jane Citizen",
                      held_before=100000, qty=145000, spend=896000)
    assert build_conviction_buys(conn) == 1

    load_membership(conn, HOLDINGS_CSV,
                    source_url="https://issuer.example/holdings.csv",
                    as_of=date(2026, 3, 11),
                    knowable_at=datetime(2026, 3, 11, tzinfo=UTC))
    assert build_conviction_buys(conn) == 0
