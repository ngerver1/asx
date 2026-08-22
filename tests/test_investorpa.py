"""investorpa.com as a detection source.

The tests that matter here are the boundary ones. This source is the first
that can answer "which announcements exist?" across the whole exchange, and
that is exactly the capability the access decision spends its length
constraining. So most of what follows asserts what the platform must NOT do
with it.
"""

from __future__ import annotations

import ast
import json
import pathlib
import re
from datetime import datetime, timezone

import pytest

from asx.ingest.classifier import classify
from asx.ingest.investorpa import (
    COVERAGE_STARTS,
    InvestorPACredentials,
    InvestorPAClient,
    InvestorPAProtocolError,
    detections_from_text,
    ingest,
)
from asx.parse.registry import parseable_doc_classes

SRC = pathlib.Path(__file__).parent.parent / "src" / "asx"

# Captured verbatim from the live API on 20 Aug 2026. Kept real rather than
# invented: the whole point of a format fixture is that it is the provider's
# output and not our idea of it.
REAL_RESPONSE = """Found 4 announcements for: 'Director's Interest Notice' | dates: 2026-08-19 to 2026-08-20

• 2026-08-20T19:28:29+10:00 | CPO - Change of Director's Interest Notice x 3 | [PDF](https://investorpa.com/announcement-pdf/20260820/330559.pdf) | [View Details](https://investorpa.com/announcement/330559/)
• 2026-08-20T17:17:24+10:00 | LOT - Change of Director's Interest Notice - G Bittar | [PDF](https://investorpa.com/announcement-pdf/20260820/330505.pdf) | [View Details](https://investorpa.com/announcement/330505/)
• 2026-08-20T14:22:13+10:00 | IVX - Initial Director's Interest Notice - Paul FIeld | [PDF](https://investorpa.com/announcement-pdf/20260820/330383.pdf) | [View Details](https://investorpa.com/announcement/330383/)
• 2026-08-20T14:21:48+10:00 | IVX - Final Director's Interest Notice - Alistair Bennallack | [PDF](https://investorpa.com/announcement-pdf/20260820/330381.pdf) | [View Details](https://investorpa.com/announcement/330381/)

Tip: Use get_announcement_detail tool with any announcement_id from above results to get full transcribed text content."""


def _jsonrpc(text: str) -> bytes:
    return json.dumps({
        "jsonrpc": "2.0", "id": 1,
        "result": {"content": [{"type": "text", "text": text}]},
    }).encode()


class _Response:
    def __init__(self, body: bytes):
        self._body = body
        self.headers = {"Content-Type": "application/json"}

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class _Opener:
    """Stands in for urlopen so the guard is exercised without the network."""

    def __init__(self, body: bytes):
        self.body = body
        self.requests = []

    def __call__(self, request, timeout=None):
        self.requests.append(request)
        return _Response(self.body)


# --- the boundaries -------------------------------------------------------

def _string_literals(tree) -> list[str]:
    """Every string CONSTANT in a module — excluding docstrings, which are the
    prose explaining why we do not do these things. A test that reads its own
    documentation as a violation is a test that punishes writing it down."""
    docstrings = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef,
                             ast.AsyncFunctionDef)):
            doc = ast.get_docstring(node, clean=False)
            if doc is not None:
                docstrings.add(doc)
    return [n.value for n in ast.walk(tree)
            if isinstance(n, ast.Constant) and isinstance(n.value, str)
            and n.value not in docstrings]


def _built_strings(tree) -> list[str]:
    """Source of every expression that ASSEMBLES a string: f-strings, `+`
    concatenation of a literal, %-formatting and .format(). Building a URL is
    the act under test; describing one in a comment is not."""
    built = []
    for node in ast.walk(tree):
        if isinstance(node, ast.JoinedStr):
            built.append(ast.unparse(node))
        elif isinstance(node, ast.BinOp) and isinstance(node.op, (ast.Add, ast.Mod)):
            built.append(ast.unparse(node))
        elif (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
              and node.func.attr in ("format", "join")):
            built.append(ast.unparse(node))
    return built


def test_no_source_file_constructs_an_investorpa_document_url():
    """Their identifiers are sequential at ~400/day, so a URL can always be
    built — and building one would collect the whole exchange. That is the
    bulk crawl docs/SOURCE_INVESTORPA.md says "must never be built". A URL is
    used only when a search result stated it.

    Comments and docstrings are excluded deliberately: this module documents
    the pattern at length precisely so nobody reinvents it, and the prose
    saying "never build this" must not read as building it.
    """
    offenders = []
    for path in sorted(SRC.rglob("*.py")):
        tree = ast.parse(path.read_text())
        for expression in _built_strings(tree):
            if "announcement-pdf" in expression:
                offenders.append(f"{path.relative_to(SRC)}: {expression}")
    assert not offenders, f"investorpa URL construction found: {offenders}"


def test_nothing_resolves_an_entity_through_their_stock_endpoint():
    """Their stock master is current-state, not effective-dated: today `ALU`
    is Alurion Resources, though every ALU announcement before Aug 2024 is
    Altium's. Resolving through it would attach a delisted company's filings
    to whoever inherited the code — the Invariant 1 failure the listings table
    exists to prevent. Tickers from this source are inputs to
    entity_for_ticker and nothing else.

    Asserted on string literals rather than raw text, for the same reason as
    above: the tool name appears in prose explaining why it is not called.
    """
    offenders = []
    for path in sorted(SRC.rglob("*.py")):
        tree = ast.parse(path.read_text())
        if any(literal == "search_stocks" for literal in _string_literals(tree)):
            offenders.append(str(path.relative_to(SRC)))
    assert not offenders, (
        f"search_stocks is named as a callable tool in {offenders}. It is a "
        f"current-ticker lookup and must never resolve an entity."
    )


def test_a_backfill_cannot_silently_cross_the_coverage_floor():
    """Asking for 2023 returns a short answer that looks complete. A hole in
    the source reported as a hole in the market is the failure Invariant 7
    exists to prevent."""
    client = InvestorPAClient(InvestorPACredentials("cid", "rt"))
    with pytest.raises(ValueError, match="coverage begins"):
        client.director_interest_notices(date_from="2023-01-01",
                                         date_to="2023-02-01")


def test_the_ir_route_does_not_claim_investorpa_documents():
    """fetch_ir_documents stores possession_source='ir_website'. An investorpa
    URL fetched there would be recorded as having come from a company's own
    site, which is false."""
    from asx.ingest.possession import _is_investorpa

    assert _is_investorpa("https://investorpa.com/announcement-pdf/20260820/1.pdf")
    assert not _is_investorpa("https://example.com/investorpa.com/x.pdf")
    assert not _is_investorpa("https://www.marketindex.com.au/a.pdf")


# --- reading their output -------------------------------------------------

def test_the_real_response_format_parses():
    detections = detections_from_text(REAL_RESPONSE).detections
    assert len(detections) == 4
    first = detections[0]
    assert first.ticker == "CPO"
    # Split on the FIRST " - " only: titles routinely contain more.
    assert first.title == "Change of Director's Interest Notice x 3"
    assert first.lodged_at == datetime.fromisoformat("2026-08-20T19:28:29+10:00")
    assert first.document_urls == [
        "https://investorpa.com/announcement-pdf/20260820/330559.pdf"]
    assert all(d.format_recognised for d in detections)


def test_a_title_containing_a_dash_keeps_its_whole_title():
    detections = detections_from_text(REAL_RESPONSE).detections
    lot = next(d for d in detections if d.ticker == "LOT")
    assert lot.title == "Change of Director's Interest Notice - G Bittar"


def test_the_timestamp_is_attributed_to_investorpa_not_market_index():
    """lodged_at_source was hardcoded to 'market_index_alert' for every sender
    until migration 0026. knowable_at is the column every analytic joins
    through, and a provenance that names the wrong observer is worse than no
    provenance at all."""
    for d in detections_from_text(REAL_RESPONSE).detections:
        assert d.lodged_at_source == "investorpa"


def test_a_line_shaped_like_a_result_but_unreadable_is_loud():
    """Silence is the failure mode. A provider who restyles their output would
    otherwise yield half-read detections that look deliberate."""
    broken = "• 2026-08-20T99:99:99+10:00 | XXX - not a parseable line"
    detections = detections_from_text(broken).detections
    assert len(detections) == 1
    assert detections[0].format_recognised is False


def test_prose_around_the_results_is_not_mistaken_for_data():
    page = detections_from_text(
        "Found 0 announcements for: 'x'\n\nTip: Use get_announcement_detail")
    assert page.detections == [] and page.stated == 0 and page.complete


def test_an_initial_directors_interest_notice_is_not_read_as_a_trade():
    """An Appendix 3X states a holding at APPOINTMENT. docs/HANDOVER.md:
    "forcing it into director_trades would fabricate a purchase and corrupt
    the cluster signal". The 3Y title pattern matches the bare phrase
    "Director's Interest Notice", so a 3X fell into it until 0026 — which
    mattered little at 17 captured documents and would matter a great deal
    against a feed that returns every one on the exchange.
    """
    detections = detections_from_text(REAL_RESPONSE).detections
    initial = next(d for d in detections if "Initial" in d.title)
    doc_class, _ = classify(initial.title, None)
    assert doc_class == "app_3x"
    assert doc_class not in parseable_doc_classes()

    # The 3Z beside it must still be read: this is a narrowing, not a blanket.
    final = next(d for d in detections if "Final" in d.title)
    assert classify(final.title, None)[0] == "app_3z"


# --- transport ------------------------------------------------------------

def test_the_tool_call_goes_through_the_guard_as_a_post():
    opener = _Opener(_jsonrpc(REAL_RESPONSE))
    client = InvestorPAClient(InvestorPACredentials("cid", "rt"), opener=opener)
    client._token = "at"                      # skip the token round-trip
    page = client.director_interest_notices(date_from="2026-08-19",
                                            date_to="2026-08-20")
    assert len(page.detections) == 4
    request = opener.requests[0]
    assert request.get_method() == "POST"
    assert request.full_url == "https://investorpa.com/mcp/"
    assert request.get_header("Authorization") == "Bearer at"
    # Honest identification, never a browser string (Invariant 11).
    assert "asx-structural-alpha" in request.get_header("User-agent")
    body = json.loads(request.data)
    assert body["params"]["name"] == "search_announcements"
    # Only the forms the platform parses, not the whole feed.
    assert "Director's Interest Notice" in body["params"]["arguments"]["keywords"]


def test_an_sse_framed_answer_is_read_too():
    """Streamable-HTTP MCP servers may answer either way."""
    body = ("event: message\n"
            "data: " + _jsonrpc(REAL_RESPONSE).decode() + "\n\n").encode()
    opener = _Opener(body)
    client = InvestorPAClient(InvestorPACredentials("cid", "rt"), opener=opener)
    client._token = "at"
    assert len(client.director_interest_notices(
        date_from="2026-08-19", date_to="2026-08-20").detections) == 4


def test_an_error_answer_raises_rather_than_returning_nothing():
    """Zero detections and a failed call must not look the same: zero
    lodgements is a pipeline alarm, and so is a broken one."""
    opener = _Opener(json.dumps(
        {"jsonrpc": "2.0", "id": 1,
         "error": {"code": -32000, "message": "nope"}}).encode())
    client = InvestorPAClient(InvestorPACredentials("cid", "rt"), opener=opener)
    client._token = "at"
    with pytest.raises(InvestorPAProtocolError):
        client.director_interest_notices(date_from="2026-08-19",
                                         date_to="2026-08-20")


def test_credentials_say_what_to_do_when_absent(monkeypatch):
    monkeypatch.delenv("ASX_INVESTORPA_CLIENT_ID", raising=False)
    monkeypatch.delenv("ASX_INVESTORPA_REFRESH_TOKEN", raising=False)
    with pytest.raises(Exception, match="investorpa_consent"):
        InvestorPACredentials.from_env()


# --- end to end, against a database --------------------------------------

class _FakeClient:
    def __init__(self, detections, *, stated=None, truncated=False):
        from asx.ingest.investorpa import SearchPage

        self._page = SearchPage(
            detections=detections,
            stated=len(detections) if stated is None else stated,
            recognised=len(detections),
            truncated=truncated,
        )

    def director_interest_notices(self, **_kwargs):
        return self._page


def test_detections_reach_the_database_with_honest_provenance(conn):
    detections = detections_from_text(REAL_RESPONSE).detections
    stats = ingest(conn, client=_FakeClient(detections),
                   today=datetime(2026, 8, 20, tzinfo=timezone.utc))
    assert stats["found"] == 4
    assert stats["new"] == 4

    with conn.cursor() as cur:
        cur.execute("""SELECT detection_source, lodged_at_source, doc_class,
                              parse_status, ticker_as_lodged, entity_id,
                              fetch_candidate_urls, asx_announcement_id
                       FROM documents ORDER BY lodged_at DESC""")
        rows = cur.fetchall()

    assert [r["detection_source"] for r in rows] == ["investorpa"] * 4
    assert [r["lodged_at_source"] for r in rows] == ["investorpa"] * 4
    # The document URL arrives with the detection — the point of the source.
    assert all(r["fetch_candidate_urls"] for r in rows)
    # Their id is a publication counter, not the exchange's announcement
    # number. Writing it to this column would make two feeds' rows collide or
    # diverge at random.
    assert all(r["asx_announcement_id"] is None for r in rows)
    # The ticker is recorded verbatim for audit; entity_id stays NULL because
    # no listings row exists in this fixture. Unresolved, never guessed.
    assert {r["ticker_as_lodged"] for r in rows} == {"CPO", "LOT", "IVX"}
    assert all(r["entity_id"] is None for r in rows)

    classes = {r["doc_class"] for r in rows}
    assert classes == {"app_3y", "app_3x", "app_3z"}
    by_class = {r["doc_class"]: r["parse_status"] for r in rows}
    assert by_class["app_3y"] == "detected"
    assert by_class["app_3z"] == "detected"
    assert by_class["app_3x"] == "not_applicable"   # recorded, never parsed


def test_re_reading_the_same_window_records_nothing_twice(conn):
    detections = detections_from_text(REAL_RESPONSE).detections
    client = _FakeClient(detections)
    first = ingest(conn, client=client,
                   today=datetime(2026, 8, 20, tzinfo=timezone.utc))
    second = ingest(conn, client=client,
                    today=datetime(2026, 8, 20, tzinfo=timezone.utc))
    assert first["new"] == 4
    assert second["new"] == 0
    assert second["duplicate"] == 4


# --- cross-feed coverage --------------------------------------------------

def test_the_two_feeds_are_reconciled_on_the_lodgement_not_the_ticker(conn):
    """Both feeds report the same ASX release timestamp, so (entity, minute)
    identifies a lodgement across them. It cannot be asx_announcement_id:
    investorpa exposes its own publication counter, not the exchange's number.
    """
    from asx.ingest.detection import Detection, record_detection

    with conn.cursor() as cur:
        cur.execute("INSERT INTO entities (entity_kind) VALUES ('company') "
                    "RETURNING entity_id")
        entity_id = cur.fetchone()["entity_id"]
        cur.execute(
            """INSERT INTO listings (entity_id, exchange, ticker,
                                     security_class, valid_from, source)
               VALUES (%s, 'ASX', 'CPO', 'ORD', DATE '2020-01-01', 'manual')""",
            (entity_id,))
    conn.commit()

    lodged = datetime.fromisoformat("2026-08-20T19:28:29+10:00")
    # The same lodgement, seen by each feed, keyed differently by construction.
    record_detection(conn, Detection(
        detection_source="market_index_alert", source_ref="<mi@x>",
        ticker="CPO", title="Change of Director's Interest Notice",
        lodged_at=lodged, announcement_id="2A1690462"))
    record_detection(conn, Detection(
        detection_source="investorpa",
        source_ref="https://investorpa.com/announcement-pdf/20260820/330559.pdf",
        ticker="CPO", title="Change of Director's Interest Notice x 3",
        lodged_at=lodged.replace(second=41), lodged_at_source="investorpa",
        document_urls=["https://investorpa.com/announcement-pdf/20260820/330559.pdf"]))
    conn.commit()

    with conn.cursor() as cur:
        cur.execute("SELECT * FROM detection_feed_coverage")
        rows = cur.fetchall()

    assert len(rows) == 1, "one lodgement, seen twice, is one row"
    row = rows[0]
    assert row["coverage"] == "both"
    assert sorted(row["feeds"]) == ["investorpa", "market_index_alert"]
    # Named, not hidden: parsing both would double-count the purchase.
    assert row["duplicate_rows"] is True
    assert row["document_rows"] == 2


def test_a_watchlist_gap_shows_as_investorpa_only(conn):
    """The reason the second feed exists: a company nobody was watching."""
    from asx.ingest.detection import Detection, record_detection

    with conn.cursor() as cur:
        cur.execute("INSERT INTO entities (entity_kind) VALUES ('company') "
                    "RETURNING entity_id")
        entity_id = cur.fetchone()["entity_id"]
        cur.execute(
            """INSERT INTO listings (entity_id, exchange, ticker,
                                     security_class, valid_from, source)
               VALUES (%s, 'ASX', 'JDO', 'ORD', DATE '2020-01-01', 'manual')""",
            (entity_id,))
    conn.commit()

    record_detection(conn, Detection(
        detection_source="investorpa",
        source_ref="https://investorpa.com/announcement-pdf/20260820/330416.pdf",
        ticker="JDO", title="Change of Director's Interest Notice",
        lodged_at=datetime.fromisoformat("2026-08-20T15:25:18+10:00"),
        lodged_at_source="investorpa"))
    conn.commit()

    with conn.cursor() as cur:
        cur.execute("SELECT coverage, duplicate_rows FROM detection_feed_coverage")
        row = cur.fetchone()
    assert row["coverage"] == "investorpa_only"
    assert row["duplicate_rows"] is False


# --- possession -----------------------------------------------------------
#
# fetch_ir_documents, which this route is modelled on, was a silent no-op for
# its entire life: it read URLs out of source_ref, which holds a Message-ID,
# so it found nothing and reported an all-zero stats dict that was
# indistinguishable from "nothing to do". fetch_candidate_urls is still empty
# on all 1,124 rows because of it. These tests exist so the sibling route
# cannot repeat that.

def _detected_investorpa_doc(conn, *, ticker="JDO", url=None,
                             title="Change of Director's Interest Notice"):
    """A detection in the state possession finds it: known, held by nobody."""
    from asx.ingest.detection import Detection, record_detection

    url = url or "https://investorpa.com/announcement-pdf/20260820/330416.pdf"
    doc_id, _ = record_detection(conn, Detection(
        detection_source="investorpa", source_ref=url, ticker=ticker,
        title=title,
        lodged_at=datetime.fromisoformat("2026-08-20T15:25:18+10:00"),
        lodged_at_source="investorpa", document_urls=[url]))
    conn.commit()
    return doc_id


def test_a_stated_pdf_url_is_fetched_and_attributed_to_investorpa(conn,
                                                                  monkeypatch,
                                                                  tmp_path):
    from types import SimpleNamespace

    from asx.ingest import possession

    doc_id = _detected_investorpa_doc(conn)
    monkeypatch.setattr(possession, "raw_zone_root", lambda: tmp_path)
    fetched = []

    def _fetch(url, **kwargs):
        fetched.append((url, kwargs))
        return SimpleNamespace(content=b"%PDF-1.7\nfake",
                               content_type="application/pdf", url=url)

    monkeypatch.setattr(possession, "fetch", _fetch)
    stats = possession.fetch_investorpa_documents(conn)

    assert stats == {"attempted": 1, "captured": 1, "robots_blocked": 0,
                     "failed": 0, "not_a_document": 0, "no_candidates": 0}
    # No terms_basis asserted at the call site: investorpa.com is in
    # DECLARED_SOURCES, so the basis is recorded centrally. A caller passing
    # one here would be claiming a per-site sign-off nobody gave.
    assert fetched[0][1].get("terms_basis") is None
    assert fetched[0][1].get("targeted_document") is not True

    with conn.cursor() as cur:
        cur.execute("""SELECT possession_source, parse_status, sha256,
                              storage_path, fetched_at
                       FROM documents WHERE doc_id = %s""", (doc_id,))
        row = cur.fetchone()
    # Never 'ir_website': that would say a company published it on its own site.
    assert row["possession_source"] == "investorpa"
    assert row["parse_status"] == "unparsed"       # now parseable
    assert row["sha256"] and row["storage_path"] and row["fetched_at"]


def test_a_login_wall_is_refused_and_the_capture_gap_stays_open(conn,
                                                                monkeypatch,
                                                                tmp_path):
    """200 with HTML is what a login wall returns. Storing it would poison the
    raw zone AND flip the row out of 'detected', clearing the alarm that says
    the document is still missing."""
    from types import SimpleNamespace

    from asx.ingest import possession

    doc_id = _detected_investorpa_doc(conn)
    monkeypatch.setattr(possession, "raw_zone_root", lambda: tmp_path)
    monkeypatch.setattr(possession, "fetch", lambda url, **kw: SimpleNamespace(
        content=b"<html><body>Please sign in</body></html>",
        content_type="text/html", url=url))

    stats = possession.fetch_investorpa_documents(conn)
    assert stats["not_a_document"] == 1 and stats["captured"] == 0

    with conn.cursor() as cur:
        cur.execute("SELECT parse_status, sha256, possession_source "
                    "FROM documents WHERE doc_id = %s", (doc_id,))
        row = cur.fetchone()
    assert row["parse_status"] == "detected"      # still an open capture gap
    assert row["sha256"] is None
    assert row["possession_source"] is None


def test_the_two_fetch_routes_do_not_take_each_other_s_documents(conn,
                                                                 monkeypatch,
                                                                 tmp_path):
    """Provenance is the point. An investorpa PDF fetched by the IR route
    would be recorded as having come from the company's own website."""
    from types import SimpleNamespace

    from asx.ingest import possession

    ipa = _detected_investorpa_doc(conn)
    monkeypatch.setattr(possession, "raw_zone_root", lambda: tmp_path)
    monkeypatch.setattr(possession, "fetch", lambda url, **kw: SimpleNamespace(
        content=b"%PDF-1.7\nfake", content_type="application/pdf", url=url))

    # The IR route sees it and declines to touch it.
    ir_stats = possession.fetch_ir_documents(conn)
    assert ir_stats["attempted"] == 0 and ir_stats["captured"] == 0

    with conn.cursor() as cur:
        cur.execute("SELECT parse_status FROM documents WHERE doc_id = %s", (ipa,))
        assert cur.fetchone()["parse_status"] == "detected"

    # And the investorpa route does.
    assert possession.fetch_investorpa_documents(conn)["captured"] == 1


def test_a_dead_route_cannot_report_itself_as_a_quiet_one(conn, monkeypatch):
    """no_candidates and attempted are counted separately on purpose: a route
    that never runs and a route that runs and finds nothing look identical in
    an all-zero stats dict, and the first is the bug that went unnoticed on
    the IR route for the life of the project."""
    from asx.ingest import possession
    from asx.ingest.detection import Detection, record_detection

    # Detected, but no document URL was ever recorded against it.
    record_detection(conn, Detection(
        detection_source="market_index_alert", source_ref="<mi@x>",
        ticker="JDO", title="Change of Director's Interest Notice",
        lodged_at=datetime.fromisoformat("2026-08-20T15:25:18+10:00"),
        announcement_id="2A1690999"))
    conn.commit()

    def _never_called(url, **kwargs):        # noqa: ARG001
        raise AssertionError("nothing should be fetched with no candidates")

    monkeypatch.setattr(possession, "fetch", _never_called)
    stats = possession.fetch_investorpa_documents(conn)
    assert stats["no_candidates"] == 1
    assert stats["attempted"] == 0 and stats["captured"] == 0


# ==========================================================================
# Regression tests for the 20 Aug 2026 review. Each one FAILS against the
# code as first shipped, and each pins a bug that reported success while
# doing nothing — the failure mode CLAUDE.md names most sharply and that this
# module reproduced six times in one sitting.
# ==========================================================================

# The vendor's header states how many results it RETURNED, not how many exist:
# the same 19-hour window answers "Found 20" at limit=100 and "Found 5" at
# limit=5 (checked against the live API, 21 Aug 2026). So it is worthless for
# detecting truncation, and exact for detecting a line we failed to read.

def test_a_restyled_line_is_recognised_and_flagged_not_dropped():
    """A dash bullet whose link is no longer wrapped in [PDF](...) markdown.
    The strict pattern misses it; the shape pattern must still recognise it,
    so it becomes an unreadable detection with a review item rather than
    evaporating. Both patterns are built from one _BULLET, so widening one
    cannot leave the other behind again.

    Deliberately NOT a raise. An earlier version of this test demanded one,
    which would have thrown away every readable detection on the same page —
    turning one styling change into a lost day, against the rule that a single
    bad row must not destroy a batch."""
    drifted = ("Found 2 announcements for: 'x'\n\n"
               "- 2026-08-20T17:17:24+10:00 | LOT - Change of Director's "
               "Interest Notice | PDF: https://investorpa.com/a.pdf\n"
               "- 2026-08-20T17:15:44+10:00 | LOT - Change of Director's "
               "Interest Notice | PDF: https://investorpa.com/b.pdf\n")
    page = detections_from_text(drifted)
    assert page.recognised == 2 and page.stated == 2
    assert page.missing == 0                       # nothing vanished
    assert [d.format_recognised for d in page.detections] == [False, False]


def test_a_line_that_vanishes_entirely_is_counted_against_the_stated_total():
    """The case the two patterns are blind to by construction: a line neither
    parses NOR recognises. Only the vendor's own count can see it, which is
    why the count exists — it is independent of how they style anything."""
    body = ("Found 3 announcements for: 'x'\n\n"
            "• 2026-08-20T17:17:24+10:00 | LOT - Title | [PDF](https://investorpa.com/a.pdf)\n"
            "~~~ 2026-08-20 LOT something in a shape nothing recognises ~~~\n"
            "~~~ 2026-08-20 LOT another one ~~~\n")
    page = detections_from_text(body)
    assert page.recognised == 1
    assert page.stated == 3
    assert page.missing == 2, "two lines vanished and nothing else can see it"
    assert not page.complete
    # The readable one survives: a gap is reported, not widened.
    assert len(page.detections) == 1 and page.detections[0].ticker == "LOT"


def test_an_impossible_timestamp_becomes_one_unreadable_line():
    """Shape-valid but impossible times reach datetime.fromisoformat. One bad
    line must not abandon the batch with a traceback."""
    body = ("Found 2 announcements for: 'x'\n\n"
            "• 2026-08-20T25:61:61+10:00 | LOT - Title | [PDF](https://investorpa.com/a.pdf)\n"
            "• 2026-08-20T17:15:44+10:00 | LOT - Good | [PDF](https://investorpa.com/b.pdf)\n")
    page = detections_from_text(body)
    assert len(page.detections) == 2
    assert [d.format_recognised for d in page.detections] == [False, True]
    assert page.detections[1].ticker == "LOT"       # the good line still survives


def test_a_full_page_is_reported_as_possibly_truncated():
    """'Found N' is capped by the limit, so a full page cannot be told apart
    from a page that happens to be exactly N long. Silence here would make
    dropped announcements indistinguishable from announcements never lodged."""
    from asx.ingest.investorpa import page_looks_truncated

    assert page_looks_truncated(returned=500, limit=500) is True
    assert page_looks_truncated(returned=499, limit=500) is False


def test_the_window_is_the_sydney_trading_day_not_the_utc_one(conn, monkeypatch):
    """market_time.py: 'Any code that needs the calendar date of a lodgement
    must go through market_date() — taking .date() of a UTC timestamp shifts
    pre-open lodgements to the previous day.' The 09:00 UTC cron is safe by
    luck; workflow_dispatch at 23:00 Sydney is not."""
    from asx.ingest import investorpa

    asked = {}

    class _Recorder:
        def director_interest_notices(self, *, date_from, date_to, **_kw):
            from asx.ingest.investorpa import SearchPage

            asked["from"], asked["to"] = date_from, date_to
            return SearchPage(detections=[], stated=0, recognised=0)

    # 2026-08-21T14:00Z is 2026-08-22 00:00 in Sydney — a new trading day.
    investorpa.ingest(conn, client=_Recorder(), since_days=1,
                      today=datetime(2026, 8, 21, 14, 0, tzinfo=timezone.utc))
    assert asked["to"] == "2026-08-22", (
        f"asked for a window ending {asked['to']}, missing everything lodged "
        f"on the current Sydney trading day")


def test_an_investorpa_alert_keeps_its_document_link_for_the_owner():
    """partition_urls' docstring promises manual_open links are "KEPT, not
    dropped", and the sender rule's comment says own_hosts merely "keeps the
    alert's links out of the automatic fetch set". Both are false for this
    sender: the own_hosts branch continues without appending, so the
    announcement PDF URL — the only route to the document, and the whole
    reason this sender has a rule — is discarded from both lists."""
    import email as _email

    from asx.ingest.mailbox import detection_from_email

    raw = ("From: alerts@investorpa.com\n"
           "Subject: ASX:CYL - Change of Director's Interest Notice\n"
           "Message-ID: <ipa-2@investorpa.com>\n"
           "Date: Wed, 19 Aug 2026 09:35:00 +1000\n\n"
           "https://investorpa.com/announcement-pdf/20260819/293079.pdf\n")
    d = detection_from_email(_email.message_from_string(raw))
    assert d.document_urls or d.manual_open_urls, (
        "the announcement PDF URL was dropped from BOTH lists; the detection "
        "now has no route to the document at all")


def test_skipping_a_document_on_the_ir_route_leaves_a_trace(conn, monkeypatch):
    """possession.py's own comment: "a route that never runs and a route that
    runs and finds nothing look identical in an all-zero stats dict, and the
    first is a dead route reported as a quiet one." The investorpa skip
    continues without incrementing anything, so it is invisible."""
    from asx.ingest import possession

    _detected_investorpa_doc(conn)

    def _never_called(url, **kwargs):        # noqa: ARG001
        raise AssertionError("the IR route must not fetch an investorpa URL")

    monkeypatch.setattr(possession, "fetch", _never_called)
    stats = possession.fetch_ir_documents(conn)
    assert sum(stats.values()) > 0, (
        f"the skipped document left no trace anywhere in {stats}")


def test_the_answer_is_picked_by_request_id_not_by_being_last():
    """A server may emit the tool result and then a log notification or a
    ping. Taking the last frame left us reading the notification, which has
    neither 'result' nor 'error' — so a perfectly good answer was reported as
    carrying no text content."""
    result_frame = json.dumps({
        "jsonrpc": "2.0", "id": 1,
        "result": {"content": [{"type": "text", "text": REAL_RESPONSE}]}})
    trailing_notification = json.dumps({
        "jsonrpc": "2.0", "method": "notifications/message",
        "params": {"level": "info", "data": "search complete"}})
    body = (f"event: message\ndata: {result_frame}\n\n"
            f"event: message\ndata: {trailing_notification}\n\n").encode()

    opener = _Opener(body)
    client = InvestorPAClient(InvestorPACredentials("cid", "rt"), opener=opener)
    client._token = "at"
    page = client.director_interest_notices(date_from="2026-08-19",
                                            date_to="2026-08-20")
    assert len(page.detections) == 4


def test_a_frame_that_answers_a_different_request_is_not_accepted():
    """Silently accepting someone else's answer is worse than failing."""
    from asx.ingest.investorpa import InvestorPAProtocolError, _tool_text

    other = json.dumps({"jsonrpc": "2.0", "id": 99,
                        "result": {"content": [{"type": "text", "text": "x"}]}})
    with pytest.raises(InvestorPAProtocolError, match="no frame answered"):
        _tool_text(f"data: {other}\n\n".encode(), 1)


def test_a_possibly_truncated_page_is_reported_in_the_run_stats(conn):
    """A page exactly as long as the limit cannot be told from a truncated one,
    so ingest reports it rather than treating it as complete. The stats dict is
    what cmd_detect prints and what the workflow reads, so the fact has to
    reach it — a truncation nobody can see is announcements gone missing while
    the run reports success."""
    detections = detections_from_text(REAL_RESPONSE).detections
    stats = ingest(conn, client=_FakeClient(detections, truncated=True),
                   today=datetime(2026, 8, 20, tzinfo=timezone.utc))
    assert stats["truncated"] is True
    assert stats["found"] == 4          # and the page's contents still landed


def test_lines_the_vendor_counted_but_we_never_saw_reach_the_run_stats(conn):
    """page.missing is the only signal that a line vanished. It must survive
    into the stats rather than being computed and dropped."""
    detections = detections_from_text(REAL_RESPONSE).detections
    stats = ingest(conn, client=_FakeClient(detections, stated=9),
                   today=datetime(2026, 8, 20, tzinfo=timezone.utc))
    assert stats["missing"] == 5
    assert stats["found"] == 4, "the readable detections still landed"


def test_no_sender_rule_can_silently_lose_a_document_link():
    """The standing accounting invariant, and the correction to the test that
    let this bug ship.

    `test_investorpa_links_are_recorded_not_auto_fetched` asserted
    `document_urls == []` — which a function that drops EVERY link satisfies
    just as well as one that correctly withholds a fetch candidate. So the
    announcement PDF URL disappeared from both lists and the suite stayed
    green.

    The property that actually matters is conservation: a link that looks like
    a document, on a host nothing is prohibited from touching, must end up
    somewhere a human or a fetcher can reach it. Asserted across every
    configured sender rather than for one of them, because the next
    uncalibrated aggregator will have the same shape and nobody will think to
    write this test again.
    """
    from asx.ingest.mailbox import SENDER_RULES, partition_urls

    for rule in SENDER_RULES:
        host = rule.own_hosts[0] if rule.own_hosts else "example-ir.com.au"
        document = f"https://{host}/announcements/3y-aug26.pdf"
        manual, candidates = partition_urls([document], rule)
        assert document in manual or document in candidates, (
            f"{rule.detection_source} drops {document} from both lists: the "
            f"detection would be recorded with no route to the document at "
            f"all, which is indistinguishable from an announcement that has "
            f"no document"
        )


def test_a_calibrated_sender_still_drops_its_tracking_furniture():
    """The counterpart. Recording own-host links for an uncalibrated sender
    must not become 'record everything': Market Index has been read against
    real emails, so its non-announcement own-host links are known to be
    tracking redirects and list management, and putting those on the owner's
    worklist would bury the documents among them."""
    from asx.ingest.mailbox import SENDER_RULES, partition_urls

    market_index = next(r for r in SENDER_RULES
                        if r.detection_source == "market_index_alert")
    noise = "https://www.marketindex.com.au/manage/unsubscribe?u=123"
    manual, candidates = partition_urls([noise], market_index)
    assert noise not in manual and noise not in candidates
